"""Карточка пакета для потребителя выпуска (#198, спека #197; формат версии 2: docs/exchange-format.md).

Та же карточка, что на экране витрины: её читают showcase_cards.read (поля сигнала с применёнными правками заголовка и
сути, в том числе поданными после закрытия месяца) и showcase_cards.publications (свои публикации и склеенных дублей в
порядке экрана), а здесь она только раскладывается по полям формата. Баллов, «Решения платформы» и причины отказа
«Отбора» в пакете нет. Пустое и «не заполнялось»: null; «не определено» идёт строкой справочника, как на экране.

Отпечаток карточки (журнал отправленного) считается без даты обновления: дата обновления это момент, когда платформа
увидела этот отпечаток, а не часть карточки.
"""

from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache
import hashlib
import json
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from oiltech_digest.db.showcase_cards import shown_title

_MSK = ZoneInfo("Europe/Moscow")
# Кто написал «почему важно» (signals.card_set_by.why_interesting): агент «Карточка» или человек; остальное (судья и
# ревью пачки радара, карточки до #96 без отметки) потребитель получает пометкой radar и такой текст не хранит.
_WHY_AUTHORS = {"agent": "agent", "human": "human"}
# Гипотеза в пакете, только если её поставил человек или агент (#162, #163). Выключенный раздел гипотезы (решение
# владельца 22.11) прячет её раньше, в showcase_cards.read: в пакет уходит null.
_HYPOTHESIS_AUTHORS = ("agent", "human")


@lru_cache(maxsize=1)
def _relevance_labels() -> dict[str, str]:
    """Связь с нефтесервисом словами справочника «Отбора», как на экране: прямая, переносимая, нет связи."""
    from oiltech_digest.otbor_pack_format import load_dictionaries

    return {item["code"]: item["label"] for item in load_dictionaries()["oilfield_relevance"]["values"]}


def _text(value: Any) -> str | None:
    """Строка как есть; пустая и пробелы: null (не заполнялось)."""
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def _day(value: Any) -> str | None:
    """День по Москве «ГГГГ-ММ-ДД»: у даты как есть, у момента (и его записи ISO 8601) его день по Москве."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value) if len(value) > 10 else date.fromisoformat(value)
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=_MSK)
        return moment.astimezone(_MSK).date().isoformat()
    return value.isoformat()


def _web_link(url: Any) -> bool:
    """Ссылка откроется у читателя в браузере: только http и https с адресом сайта (иначе потребитель отклонит
    весь пакет)."""
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _set_by(row: dict, field: str) -> str | None:
    marks = row.get("card_set_by")
    return marks.get(field) if isinstance(marks, dict) else None


def _why(row: dict) -> dict[str, Any] | None:
    text = _text(row.get("why_interesting"))
    if text is None:
        return None
    return {"text": text, "set_by": _WHY_AUTHORS.get(_set_by(row, "why_interesting") or "", "radar")}


def _event_date(row: dict) -> str | None:
    """Дата события (решение владельца 19.11): пусто только у даты, которую понять нельзя совсем (ни день, ни месяц, ни
    год), потребитель пишет «дата не определена». Известны месяц или год: уходит первый день периода, как он хранится
    (event_date_precision в формате не передаётся)."""
    return _day(row.get("event_date"))


def _companies(row: dict) -> list[str]:
    participants = row.get("participants_json")
    if not isinstance(participants, list):
        return []
    return [str(item["name"]) for item in participants if isinstance(item, dict) and _text(item.get("name"))]


def _relevance(value: Any) -> str | None:
    text = _text(value)
    return None if text is None else _relevance_labels().get(text, text)


def _publication(evidence: dict) -> dict[str, Any]:
    return {
        "title": _text(evidence.get("title_ru")) or _text(evidence.get("title")),
        "source": _text(evidence.get("publisher")),
        "url": str(evidence["source_url"]),
        "date": _day(evidence.get("published_at")),
    }


class CardError(ValueError):
    """Карточку нельзя отдать потребителю: формат требует то, чего у сигнала нет."""


def card(row: dict) -> dict[str, Any]:
    """Карточка пакета без даты обновления из карточки экрана витрины (showcase_cards.read с публикациями в "evidence")."""
    title = shown_title(row)
    if title is None:
        raise CardError(f"у сигнала №{row.get('id')} нет заголовка")
    hypothesis = _text(row.get("hypothesis")) if _set_by(row, "hypothesis") in _HYPOTHESIS_AUTHORS else None
    return {
        "signal_id": int(row["id"]),
        "title": title,
        "summary": _text(row.get("summary")) or _text(row.get("thesis")),
        "why_important": _why(row),
        "hypothesis": hypothesis,
        "theme": _text(row.get("theme")),
        "event_date": _event_date(row),
        "event_type": _text(row.get("event_type")),
        "companies": _companies(row),
        "region": _text(row.get("region")),
        "technology_class": _text(row.get("technology_class")),
        "maturity": _text(row.get("technology_maturity")),
        "oilfield_relevance": _relevance(row.get("oilfield_relevance")),
        "oilfield_application": _text(row.get("oilfield_application")),
        "publications": [_publication(one) for one in row.get("evidence") or () if _web_link(one.get("source_url"))],
    }


def stamped(body: dict[str, Any], updated_at: datetime) -> dict[str, Any]:
    """Карточка с датой обновления: когда платформа увидела её нынешний отпечаток (ISO 8601 с часовым поясом)."""
    if updated_at.tzinfo is None:
        raise ValueError("дата обновления карточки без часового пояса")
    return {**body, "updated_at": updated_at.isoformat()}


def canonical(value: Any) -> bytes:
    """Одна запись значения: ключи по алфавиту, без пробелов, UTF-8. По ней считаются отпечатки."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()
