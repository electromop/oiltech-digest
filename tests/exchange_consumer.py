"""Двойник потребителя выпуска в тестах: забирает пакеты из точки обмена и вскрывает их по описанию формата версии 2
(docs/exchange-format.md) своими ключами.

Код платформы (exchange_envelope, exchange_card) здесь не используется: тесты проверяют и платформу, и описание, по
которому пакеты читает потребитель. Строгая проверка содержимого повторяет правила потребителя: лишнее поле, поле не
того вида и пустой заголовок отклоняют пакет. Общий образец содержимого: tests/exchange_samples/release-2026-09.json,
тот же файл лежит в тестах потребителя.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hpke
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

FORMAT = "digest-exchange-package"
VERSION = 2
LABEL = b"digest-exchange-package/2"
SUITE = hpke.Suite(hpke.KEM.X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.CHACHA20_POLY1305)
ENVELOPE_FIELDS = {"format", "version", "signer", "recipient", "ciphertext", "signature"}
PACKAGE_FIELDS = {"number", "kind", "month", "cards"}
CARD_FIELDS = {
    "signal_id", "title", "summary", "why_important", "hypothesis", "theme", "event_date", "event_type", "companies",
    "region", "technology_class", "maturity", "oilfield_relevance", "oilfield_application", "updated_at", "publications",
}
TEXT_FIELDS = ("summary", "hypothesis", "theme", "event_type", "region", "technology_class", "maturity",
               "oilfield_relevance", "oilfield_application")
WHY_FIELDS = {"text", "set_by"}
WHY_AUTHORS = {"agent", "human", "radar"}
PUBLICATION_FIELDS = {"title", "source", "url", "date"}
SAMPLE = Path(__file__).parent / "exchange_samples" / "release-2026-09.json"


def sample() -> dict[str, Any]:
    """Общий образец содержимого пакета версии 2."""
    content: dict[str, Any] = json.loads(SAMPLE.read_text(encoding="utf-8"))
    return content


def key_id(public_key: bytes) -> str:
    """Отпечаток ключа: первые 8 байт SHA-256 от 32 байт открытого ключа, 16 знаков hex строчными."""
    return hashlib.sha256(public_key).hexdigest()[:16]


def _standard_base64(value: Any, name: str) -> bytes:
    assert isinstance(value, str) and value, f"{name}: нет строки base64"
    assert re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", value) and len(value) % 4 == 0, f"{name}: не стандартный base64"
    return base64.b64decode(value, validate=True)


class Consumer:
    """Потребитель: свой ключ расшифровки X25519; доверяет ключу подписи платформы, который ему передали."""

    def __init__(self, platform_signing_key: bytes) -> None:
        self.decryption_key = X25519PrivateKey.generate()
        self.platform_signing_key = platform_signing_key

    @property
    def public_key(self) -> bytes:
        """Открытый ключ потребителя: его платформа держит в настройке и шифрует на него пакеты."""
        return self.decryption_key.public_key().public_bytes_raw()

    def open(self, data: bytes) -> dict[str, Any]:
        """Файл пакета → содержимое: конверт по формату, подпись ключом платформы до расшифровки, получатель, затем
        расшифровка своим ключом и строгая проверка содержимого версии 2."""
        envelope = json.loads(data)
        assert set(envelope) == ENVELOPE_FIELDS, f"конверт: поля {sorted(envelope)}"
        assert envelope["format"] == FORMAT and envelope["version"] == VERSION, envelope
        assert type(envelope["version"]) is int
        assert envelope["signer"] == key_id(self.platform_signing_key), "подписан чужим ключом"
        assert re.fullmatch(r"[0-9a-f]{16}", envelope["signer"]) and re.fullmatch(r"[0-9a-f]{16}", envelope["recipient"])
        ciphertext = _standard_base64(envelope["ciphertext"], "ciphertext")
        signature = _standard_base64(envelope["signature"], "signature")
        Ed25519PublicKey.from_public_bytes(self.platform_signing_key).verify(signature, LABEL + ciphertext)
        assert envelope["recipient"] == key_id(self.public_key), "зашифрован для другого получателя"
        payload = SUITE.decrypt(ciphertext, self.decryption_key, info=LABEL)
        content = json.loads(payload.decode("utf-8"))
        check_package(content)
        return content

    def take(self, client: Any, headers: dict[str, str], *, prefix: str = "/api/exchange/v1") -> list[dict[str, Any]]:
        """Проход канала: номера пакетов из точки обмена, затем каждый пакет по порядку номеров, вскрытый."""
        listed = client.get(f"{prefix}/packages", headers=headers)
        assert listed.status_code == 200, listed.text
        numbers = listed.json()["packages"]
        assert isinstance(numbers, list) and all(type(number) is int and number > 0 for number in numbers), numbers
        opened = []
        for number in sorted(numbers):
            answer = client.get(f"{prefix}/packages/{number}", headers=headers)
            assert answer.status_code == 200, answer.text
            content = self.open(answer.content)
            assert content["number"] == number, (content["number"], number)
            opened.append(content)
        return opened


def check_package(content: Any) -> None:
    """Содержимое выпуска или дозагрузки по формату версии 2: поля строго, значения своего вида."""
    assert isinstance(content, dict) and set(content) == PACKAGE_FIELDS, f"пакет: поля {sorted(content)}"
    assert type(content["number"]) is int and 0 < content["number"] < 2**63, content["number"]
    assert content["kind"] in ("release", "addition"), content["kind"]
    assert isinstance(content["month"], str) and re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", content["month"])
    assert isinstance(content["cards"], list)
    seen: set[int] = set()
    for index, card in enumerate(content["cards"]):
        check_card(card, f"cards[{index}]")
        assert card["signal_id"] not in seen, f"карточка {card['signal_id']} в пакете дважды"
        seen.add(card["signal_id"])
    if content["kind"] == "addition":
        assert content["cards"], "пустая дозагрузка"


def check_card(card: Any, where: str = "card") -> None:
    assert isinstance(card, dict) and set(card) == CARD_FIELDS, f"{where}: поля {sorted(card)}"
    assert type(card["signal_id"]) is int and 0 < card["signal_id"] < 2**63, where
    assert isinstance(card["title"], str) and card["title"].strip(), f"{where}: пустой заголовок"
    for name in TEXT_FIELDS:
        assert card[name] is None or isinstance(card[name], str), f"{where}.{name}: {card[name]!r}"
    why = card["why_important"]
    if why is not None:
        assert isinstance(why, dict) and set(why) == WHY_FIELDS, f"{where}.why_important: {why!r}"
        assert why["text"] is None or isinstance(why["text"], str), f"{where}.why_important.text"
        assert why["set_by"] in WHY_AUTHORS, f"{where}.why_important.set_by: {why['set_by']!r}"
    _check_day(card["event_date"], f"{where}.event_date")
    assert isinstance(card["companies"], list) and all(isinstance(name, str) for name in card["companies"]), where
    moment = dt.datetime.fromisoformat(card["updated_at"])
    assert moment.tzinfo is not None, f"{where}.updated_at без часового пояса"
    assert isinstance(card["publications"], list), where
    for index, publication in enumerate(card["publications"]):
        place = f"{where}.publications[{index}]"
        assert isinstance(publication, dict) and set(publication) == PUBLICATION_FIELDS, f"{place}: {publication!r}"
        for name in ("title", "source"):
            assert publication[name] is None or isinstance(publication[name], str), f"{place}.{name}"
        parts = urlsplit(publication["url"])
        assert parts.scheme in ("http", "https") and parts.netloc, f"{place}.url: {publication['url']!r}"
        _check_day(publication["date"], f"{place}.date")


def _check_day(value: Any, where: str) -> None:
    if value is None:
        return
    assert isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value), f"{where}: {value!r}"
    dt.date.fromisoformat(value)


def same_fields_as_sample(content: dict[str, Any]) -> None:
    """Поля пакета совпадают с общим образцом: те же поля пакета, карточки, «почему важно» и публикации."""
    reference = sample()
    check_package(reference)
    assert set(content) == set(reference)
    card_fields = {name for card in reference["cards"] for name in card}
    why_fields = {name for card in reference["cards"] if card["why_important"] for name in card["why_important"]}
    publication_fields = {name for card in reference["cards"] for one in card["publications"] for name in one}
    for card in content["cards"]:
        assert set(card) == card_fields
        if card["why_important"] is not None:
            assert set(card["why_important"]) == why_fields
        for publication in card["publications"]:
            assert set(publication) == publication_fields
