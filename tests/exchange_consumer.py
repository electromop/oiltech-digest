"""Двойник потребителя выпуска в тестах: забирает пакеты из точки обмена и вскрывает их по описанию формата версии 2
(docs/exchange-format.md) своими ключами, следует за платформой по пакетам обновления (State), собирает пакеты откликов
и отдаёт квитанции (#200).

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
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

FORMAT = "digest-exchange-package"
VERSION = 2
LABEL = b"digest-exchange-package/2"
SUITE = hpke.Suite(hpke.KEM.X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.CHACHA20_POLY1305)
ENVELOPE_FIELDS = {"format", "version", "signer", "recipient", "ciphertext", "signature"}
PACKAGE_FIELDS = {"number", "kind", "month", "cards"}
UPDATE_FIELDS = {"number", "kind", "cards", "merges", "removals"}
MERGE_FIELDS = {"signal_id", "merged_into"}
# Пакет откликов: виды откликов и метка источника (настоящая метка потребителя в тестах не нужна).
REACTION_KINDS = ("save", "work", "export", "useful", "not_oilfield", "not_new", "few_facts", "card_error")
SOURCE = "Тестовый потребитель"
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
    """Потребитель: свой ключ расшифровки X25519 и ключ подписи откликов Ed25519; доверяет ключу подписи платформы,
    который ему передали, и шифрует отклики на открытый ключ платформы."""

    def __init__(self, platform_signing_key: bytes, platform_encryption_key: bytes | None = None) -> None:
        self.decryption_key = X25519PrivateKey.generate()
        self.signing_key = Ed25519PrivateKey.generate()
        self.platform_signing_key = platform_signing_key
        self.platform_encryption_key = platform_encryption_key

    @property
    def signing_public(self) -> bytes:
        """Открытый ключ подписи откликов: его платформа держит в настройке и проверяет им пакеты откликов."""
        return self.signing_key.public_key().public_bytes_raw()

    def seal(self, content: Any, *, recipient: bytes | None = None, signing_key: Ed25519PrivateKey | None = None,
             label: bytes = LABEL, **envelope: Any) -> bytes:
        """Пакет откликов по описанию формата: содержимое в JSON, HPKE на ключ платформы (recipient), подпись своим
        ключом над строкой формата и шифротекстом. envelope: поля конверта поверх собранных (испорченный пакет)."""
        recipient = recipient or self.platform_encryption_key
        assert recipient is not None, "нет открытого ключа платформы"
        payload = content if isinstance(content, bytes) else json.dumps(content, ensure_ascii=False).encode("utf-8")
        ciphertext = SUITE.encrypt(payload, X25519PublicKey.from_public_bytes(recipient), info=label)
        signer = signing_key or self.signing_key
        signature = signer.sign(label + ciphertext)
        sealed = {
            "format": FORMAT, "version": VERSION,
            "signer": key_id(signer.public_key().public_bytes_raw()), "recipient": key_id(recipient),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            "signature": base64.b64encode(signature).decode("ascii"),
        }
        return json.dumps({**sealed, **envelope}).encode("ascii")

    def send_reactions(self, client: Any, headers: dict[str, str], number: int, data: bytes,
                       *, prefix: str = "/api/exchange/v1") -> Any:
        return client.put(f"{prefix}/reactions/{number}", content=data, headers={**headers,
                                                                                "Content-Type": "application/json"})

    def receipt(self, client: Any, headers: dict[str, str], number: int, outcome: str, message: str,
                *, prefix: str = "/api/exchange/v1") -> Any:
        return client.put(f"{prefix}/receipts/{number}", json={"number": number, "outcome": outcome,
                                                                "message": message}, headers=headers)

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


def reactions(number: int, items: list[tuple[int, str, bool, str]], *, source: str = SOURCE) -> dict[str, Any]:
    """Содержимое пакета откликов: (номер сигнала, вид, поставлен ли, день по Москве)."""
    return {"number": number, "kind": "reactions", "source": source,
            "reactions": [{"signal_id": signal_id, "reaction": kind, "placed": placed, "day": day}
                          for signal_id, kind, placed, day in items]}


def check_package(content: Any) -> None:
    """Содержимое выпуска, дозагрузки или обновления по формату версии 2: поля строго, значения своего вида."""
    if isinstance(content, dict) and content.get("kind") == "update":
        check_update(content)
        return
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


def check_update(content: dict[str, Any]) -> None:
    """Обновление: номер, вид, карточки, склейки, снятия; сигнал в пакете один раз (карточкой, склеенным или снятым),
    оставшийся сигнал склейки в этом же пакете не склеен и не снят."""
    assert set(content) == UPDATE_FIELDS, f"обновление: поля {sorted(content)}"
    assert type(content["number"]) is int and 0 < content["number"] < 2**63, content["number"]
    assert isinstance(content["cards"], list) and isinstance(content["merges"], list)
    assert isinstance(content["removals"], list)
    seen: set[int] = set()
    for index, card in enumerate(content["cards"]):
        check_card(card, f"cards[{index}]")
        assert card["signal_id"] not in seen, f"сигнал {card['signal_id']} в пакете дважды"
        seen.add(card["signal_id"])
    for merge in content["merges"]:
        assert isinstance(merge, dict) and set(merge) == MERGE_FIELDS, f"склейка: {merge!r}"
        assert all(type(merge[name]) is int and merge[name] > 0 for name in MERGE_FIELDS), merge
        assert merge["signal_id"] not in seen, f"сигнал {merge['signal_id']} в пакете дважды"
        seen.add(merge["signal_id"])
    for removed in content["removals"]:
        assert type(removed) is int and removed > 0, removed
        assert removed not in seen, f"сигнал {removed} в пакете дважды"
        seen.add(removed)
    gone = {merge["signal_id"] for merge in content["merges"]} | set(content["removals"])
    for merge in content["merges"]:
        assert merge["merged_into"] != merge["signal_id"], merge
        assert merge["merged_into"] not in gone, f"склейка в сигнал, который в этом же пакете склеен или снят: {merge}"


class State:
    """Что знает потребитель после принятых пакетов, по его правилам приёма обновления: карточки (копия новее по дате
    обновления заменяет прежнюю), снятые (пометка снимается копией новее момента снятия), склеенные (сигнал → с чем).
    Пакет, который потребитель отклонил бы, роняет тест."""

    def __init__(self) -> None:
        self.cards: dict[int, dict[str, Any]] = {}
        self.removed: dict[int, dt.datetime] = {}
        self.merged: dict[int, int] = {}

    def root(self, signal_id: int) -> int:
        for _ in range(20):
            if signal_id not in self.merged:
                return signal_id
            signal_id = self.merged[signal_id]
        raise AssertionError("петля склеек")

    def apply(self, content: dict[str, Any], *, now: dt.datetime | None = None) -> None:
        now = now or dt.datetime.now(dt.timezone.utc)
        if content["kind"] != "update":
            for card in content["cards"]:
                self._store(card)
            return
        rooted = {merge["merged_into"] for merge in content["merges"] if merge["signal_id"] in self.cards}
        for card in content["cards"]:
            assert card["signal_id"] in self.cards or card["signal_id"] in rooted or card["signal_id"] in self.merged, (
                f"карточки {card['signal_id']} у потребителя нет, а обновление меняет только его карточки")
            self._store(card)
        for merge in content["merges"]:
            merged, into = merge["signal_id"], self.root(merge["merged_into"])
            if merged not in self.cards or into == merged:
                continue
            assert into in self.cards, f"склейка {merged} → {into}: карточки {into} нет"
            self.merged[merged] = into
            del self.cards[merged]
            self.removed.pop(merged, None)
        for removed in content["removals"]:
            if removed in self.cards and removed not in self.removed:
                self.removed[removed] = now

    def _store(self, card: dict[str, Any]) -> None:
        if card["signal_id"] in self.merged:
            return  # карточку склеенного сигнала потребитель пропускает: расклейку формат не передаёт
        updated = dt.datetime.fromisoformat(card["updated_at"])
        known = self.cards.get(card["signal_id"])
        if known is not None and updated <= dt.datetime.fromisoformat(known["updated_at"]):
            return
        self.cards[card["signal_id"]] = card
        removed = self.removed.get(card["signal_id"])
        if removed is not None and removed < updated:
            del self.removed[card["signal_id"]]


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
