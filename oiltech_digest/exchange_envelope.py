"""Конверт пакета обмена с потребителем выпуска, формат версии 2 (#198, спека #197; описание: docs/exchange-format.md).

Пакет для потребителя подписан ключом платформы (Ed25519, RFC 8032) и зашифрован на открытый ключ потребителя (HPKE,
RFC 9180: режим base, DHKEM(X25519, HKDF-SHA256), HKDF-SHA256, ChaCha20-Poly1305). Строка формата входит в подпись и в
шифрование: подпись и шифротекст версии 2 не годятся ни для какой другой версии. Версию 1 платформа не отправляет.

Ключи в настройках ядра (.env), вне репозитория и образа: base64 от 32 байт, несколько через запятую. Первый ключ в
списке подписывает и шифрует, остальные действуют для приёма: так ключи меняются без простоя, а пакет называет свой
ключ отпечатком. Закрытые ключи платформы: подпись пакетов (EXCHANGE_SIGNING_KEYS) и расшифровка откликов
(EXCHANGE_DECRYPTION_KEYS); открытые ключи потребителя: шифрование пакетов (EXCHANGE_CONSUMER_KEYS) и проверка подписи
откликов (EXCHANGE_CONSUMER_SIGNING_KEYS). Отклики принимает следующий тикет (#200), здесь их ключи только читаются.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.hazmat.primitives import hpke
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

from oiltech_digest import config

FORMAT = "digest-exchange-package"
VERSION = 2
LABEL = f"{FORMAT}/{VERSION}".encode("ascii")
SUITE = hpke.Suite(hpke.KEM.X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.CHACHA20_POLY1305)
KEY_BYTES = 32
# Настройки ключей: имя и чей ключ (закрытый платформы или открытый потребителя).
SIGNING, DECRYPTION = "EXCHANGE_SIGNING_KEYS", "EXCHANGE_DECRYPTION_KEYS"
CONSUMER, CONSUMER_SIGNING = "EXCHANGE_CONSUMER_KEYS", "EXCHANGE_CONSUMER_SIGNING_KEYS"
NO_SIGNING_KEY = f"нет ключа подписи платформы ({SIGNING})"
NO_CONSUMER_KEY = f"нет открытого ключа потребителя ({CONSUMER})"


class NotConfigured(Exception):
    """Ключей для отправки нет или они испорчены: текст называет настройку и номер ключа, но не сам ключ."""


def key_id(public_key: bytes) -> str:
    """Отпечаток ключа: первые 8 байт SHA-256 от 32 байт открытого ключа, 16 знаков hex строчными."""
    return hashlib.sha256(public_key).hexdigest()[:16]


def _signing_public(private_key: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(private_key).public_key().public_bytes_raw()


def _encryption_public(private_key: bytes) -> bytes:
    return X25519PrivateKey.from_private_bytes(private_key).public_key().public_bytes_raw()


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def new_signing_key() -> bytes:
    """Новый закрытый ключ подписи платформы (Ed25519), 32 байта."""
    return Ed25519PrivateKey.generate().private_bytes_raw()


def new_key_pair(kind: str) -> dict[str, str]:
    """Пара ключей платформы: signing (подпись пакетов) или decryption (расшифровка откликов). Закрытая часть идёт в
    настройку ядра setting, открытая с отпечатком передаётся потребителю."""
    if kind == "signing":
        private = new_signing_key()
        public, setting = _signing_public(private), SIGNING
    elif kind == "decryption":
        private = X25519PrivateKey.generate().private_bytes_raw()
        public, setting = _encryption_public(private), DECRYPTION
    else:
        raise ValueError(f"неизвестный вид ключа: {kind}")
    return {"setting": setting, "private": _b64(private), "public": _b64(public), "fingerprint": key_id(public)}


def _keys(setting: str) -> list[bytes]:
    """Ключи настройки по порядку; не base64 или не 32 байта: NotConfigured с номером ключа."""
    keys = []
    for position, item in enumerate((part.strip() for part in str(getattr(config, setting) or "").split(",")), 1):
        if not item:
            continue
        try:
            raw = base64.b64decode(item, validate=True)
        except ValueError:
            raise NotConfigured(f"{setting}: ключ {position} не в base64") from None
        if len(raw) != KEY_BYTES:
            raise NotConfigured(f"{setting}: ключ {position} не {KEY_BYTES} байта")
        keys.append(raw)
    return keys


def sending_keys() -> tuple[bytes, bytes]:
    """Ключ подписи платформы и открытый ключ потребителя для нового пакета: первые в своих настройках."""
    signing, consumer = _keys(SIGNING), _keys(CONSUMER)
    if not signing:
        raise NotConfigured(NO_SIGNING_KEY)
    if not consumer:
        raise NotConfigured(NO_CONSUMER_KEY)
    return signing[0], consumer[0]


def describe() -> dict[str, Any]:
    """Отпечатки ключей из настроек (их сверяют с потребителем) и что мешает отправке."""
    problems: list[str] = []
    publics = {SIGNING: _signing_public, DECRYPTION: _encryption_public}
    found: dict[str, list[str]] = {}
    for name, setting in (("signing", SIGNING), ("decryption", DECRYPTION), ("consumer", CONSUMER),
                          ("consumer_signing", CONSUMER_SIGNING)):
        try:
            keys = _keys(setting)
        except NotConfigured as error:
            problems.append(str(error))
            keys = []
        found[name] = [key_id(publics[setting](key) if setting in publics else key) for key in keys]
    if not found["signing"] and not any(problem.startswith(SIGNING + ":") for problem in problems):
        problems.append(NO_SIGNING_KEY)
    if not found["consumer"] and not any(problem.startswith(CONSUMER + ":") for problem in problems):
        problems.append(NO_CONSUMER_KEY)
    return {**found, "problems": problems}


def seal(payload: bytes, signing_key: bytes, recipient: bytes) -> bytes:
    """Файл пакета: содержимое зашифровано на открытый ключ получателя recipient и подписано ключом платформы.
    В ciphertext enc (32 байта) и сразу шифротекст; подпись над строкой формата и байтами ciphertext."""
    ciphertext = SUITE.encrypt(payload, X25519PublicKey.from_public_bytes(recipient), info=LABEL)
    signature = Ed25519PrivateKey.from_private_bytes(signing_key).sign(LABEL + ciphertext)
    envelope = {
        "format": FORMAT,
        "version": VERSION,
        "signer": key_id(_signing_public(signing_key)),
        "recipient": key_id(recipient),
        "ciphertext": _b64(ciphertext),
        "signature": _b64(signature),
    }
    return json.dumps(envelope).encode("ascii")
