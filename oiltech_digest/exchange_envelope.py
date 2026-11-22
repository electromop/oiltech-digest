"""Конверт пакета обмена с потребителем выпуска, формат версии 2 (#198, спека #197; описание: docs/exchange-format.md).

Пакет для потребителя подписан ключом платформы (Ed25519, RFC 8032) и зашифрован на открытый ключ потребителя (HPKE,
RFC 9180: режим base, DHKEM(X25519, HKDF-SHA256), HKDF-SHA256, ChaCha20-Poly1305). Строка формата входит в подпись и в
шифрование: подпись и шифротекст версии 2 не годятся ни для какой другой версии. Версию 1 платформа не отправляет.

Ключи в настройках ядра (.env), вне репозитория и образа: base64 от 32 байт, несколько через запятую. Первый ключ в
списке подписывает и шифрует, остальные действуют для приёма: так ключи меняются без простоя, а пакет называет свой
ключ отпечатком. Закрытые ключи платформы: подпись пакетов (EXCHANGE_SIGNING_KEYS) и расшифровка откликов
(EXCHANGE_DECRYPTION_KEYS); открытые ключи потребителя: шифрование пакетов (EXCHANGE_CONSUMER_KEYS) и проверка подписи
откликов (EXCHANGE_CONSUMER_SIGNING_KEYS). Пакет откликов потребитель подписывает своим ключом и шифрует на ключ
платформы (#200): open_package проверяет его по порядку (формат и версия, подпись до расшифровки, получатель,
расшифровка), первое несовпадение даёт Rejected с причиной.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
import hashlib
import json
import re
from typing import Any

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hpke
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
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
NO_DECRYPTION_KEY = f"нет ключа платформы для расшифровки откликов ({DECRYPTION})"
NO_CONSUMER_SIGNING_KEY = f"нет открытого ключа подписи потребителя ({CONSUMER_SIGNING})"
# Поля файла пакета, ровно такие, и отпечаток ключа в них.
ENVELOPE_FIELDS = ("format", "version", "signer", "recipient", "ciphertext", "signature")
_KEY_ID = re.compile(r"[0-9a-f]{16}")


class NotConfigured(Exception):
    """Ключей для отправки нет или они испорчены: текст называет настройку и номер ключа, но не сам ключ."""


class Rejected(Exception):
    """Пакет откликов не принят: причина словами (отпечатки ключей, но не сами ключи), её получают потребитель и журнал
    обмена."""


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


# Виды ключей в настройках ядра: имя в ответах, настройка, как из ключа получить открытую часть (у закрытых ключей
# платформы) и как выпустить новый. У открытых ключей потребителя в настройке уже открытая часть, выпускает их он.
_KINDS: dict[str, tuple[str, Callable[[bytes], bytes] | None, Callable[[], bytes] | None]] = {
    "signing": (SIGNING, _signing_public, new_signing_key),
    "decryption": (DECRYPTION, _encryption_public, lambda: X25519PrivateKey.generate().private_bytes_raw()),
    "consumer": (CONSUMER, None, None),
    "consumer_signing": (CONSUMER_SIGNING, None, None),
}


def new_key_pair(kind: str) -> dict[str, str]:
    """Пара ключей платформы: signing (подпись пакетов) или decryption (расшифровка откликов). Закрытая часть идёт в
    настройку ядра setting, открытая с отпечатком передаётся потребителю."""
    setting, public_of, generate = _KINDS.get(kind, ("", None, None))
    if public_of is None or generate is None:
        raise ValueError(f"неизвестный вид ключа платформы: {kind}")
    private = generate()
    public = public_of(private)
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
    found: dict[str, list[str]] = {}
    for name, (setting, public_of, _generate) in _KINDS.items():
        try:
            keys = _keys(setting)
        except NotConfigured as error:
            problems.append(str(error))
            keys = []
        found[name] = [key_id(public_of(key) if public_of else key) for key in keys]
    if not found["signing"] and not any(problem.startswith(SIGNING + ":") for problem in problems):
        problems.append(NO_SIGNING_KEY)
    if not found["consumer"] and not any(problem.startswith(CONSUMER + ":") for problem in problems):
        problems.append(NO_CONSUMER_KEY)
    # Что мешает принять пакет откликов (#200): своя строка во вкладке, отправке пакетов это не мешает.
    reactions = [problem for problem in problems if problem.startswith((DECRYPTION + ":", CONSUMER_SIGNING + ":"))]
    problems = [problem for problem in problems if problem not in reactions]
    if not found["decryption"] and not any(one.startswith(DECRYPTION + ":") for one in reactions):
        reactions.append(NO_DECRYPTION_KEY)
    if not found["consumer_signing"] and not any(one.startswith(CONSUMER_SIGNING + ":") for one in reactions):
        reactions.append(NO_CONSUMER_SIGNING_KEY)
    return {**found, "problems": problems, "reaction_problems": reactions}


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


def _base64(value: Any, name: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise Rejected(f"{name}: нужна строка base64")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise Rejected(f"{name}: не base64 со стандартным алфавитом") from None


def _envelope(data: bytes) -> dict[str, Any]:
    """Файл пакета: JSON-объект ровно с полями формата."""
    try:
        envelope = json.loads(data)
    except (ValueError, RecursionError):
        raise Rejected("файл пакета не читается как JSON") from None
    if not isinstance(envelope, dict):
        raise Rejected("файл пакета не JSON-объект")
    extra = sorted(str(name) for name in envelope if name not in ENVELOPE_FIELDS)
    if extra:
        raise Rejected(f"в файле пакета лишнее поле {extra[0][:40]}")
    missing = [name for name in ENVELOPE_FIELDS if name not in envelope]
    if missing:
        raise Rejected(f"в файле пакета нет поля {missing[0]}")
    return envelope


def open_package(data: bytes) -> tuple[bytes, str]:
    """Файл пакета откликов → (содержимое, отпечаток ключа подписи потребителя). Проверка по порядку, первое
    несовпадение: Rejected с причиной: формат и версия файла, подпись открытым ключом потребителя до расшифровки,
    получатель, расшифровка ключом платформы. Ключей откликов в настройках нет: NotConfigured (сбой платформы, а не
    пакета)."""
    envelope = _envelope(data)
    kind, version = envelope["format"], envelope["version"]
    if kind != FORMAT or type(version) is not int or version != VERSION:
        raise Rejected(f"формат «{str(kind)[:60]}» версии {str(version)[:10]}, точка принимает «{FORMAT}» версии "
                       f"{VERSION}")
    signer, recipient = envelope["signer"], envelope["recipient"]
    for name, value in (("signer", signer), ("recipient", recipient)):
        if not isinstance(value, str) or not _KEY_ID.fullmatch(value):
            raise Rejected(f"{name}: нужен отпечаток ключа, 16 знаков hex")
    ciphertext = _base64(envelope["ciphertext"], "ciphertext")
    signature = _base64(envelope["signature"], "signature")
    trusted, own = _keys(CONSUMER_SIGNING), _keys(DECRYPTION)
    if not trusted:
        raise NotConfigured(NO_CONSUMER_SIGNING_KEY)
    if not own:
        raise NotConfigured(NO_DECRYPTION_KEY)
    public = next((key for key in trusted if key_id(key) == signer), None)
    if public is None:
        raise Rejected(f"пакет подписан ключом {signer}, которого платформа не знает")
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, LABEL + ciphertext)
    except InvalidSignature:
        raise Rejected("подпись не сходится") from None
    private = next((key for key in own if key_id(_encryption_public(key)) == recipient), None)
    if private is None:
        raise Rejected(f"пакет зашифрован на ключ {recipient}, а не на ключ платформы")
    try:
        payload = SUITE.decrypt(ciphertext, X25519PrivateKey.from_private_bytes(private), info=LABEL)
    except (InvalidTag, ValueError):
        raise Rejected("пакет не расшифровывается ключом платформы") from None
    return payload, signer
