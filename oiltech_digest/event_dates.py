"""Дата события сигнала (#96, #132, спека #128): откуда она, с какой точностью известна и сколько событию дней.

Дату ставит «Карточка» (signal_card): из текста — с той точностью, что в тексте (день, месяц или год), иначе по дате
публикации. У месяца и года в карточке первый день периода, но как день его не читает никто: возраст события —
по периоду (решение 29 журнала гриля по разметке эксперта: «первое число месяца не подставляется»). Возраст считается
от даты публикации, а если её нет, от даты поступления. Дата публикации вместо даты события не подставляется ни для
возврата в «Отбор», ни для «Свежести» (signal_scores).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import re
from typing import Any

# Источник даты события — значения справочника date_source, по ним код решает, откуда дата.
FROM_TEXT = "из текста"
BY_PUBLICATION = "по дате публикации"
# Точность даты события (#132): модель пишет её так, как она названа в тексте — ГГГГ-ММ-ДД, ГГГГ-ММ или ГГГГ.
DAY, MONTH, YEAR = "day", "month", "year"
EARLIEST_EVENT = date(2000, 1, 1)
_PRECISIONS = ((re.compile(r"(\d{4})-(\d{2})-(\d{2})"), DAY), (re.compile(r"(\d{4})-(\d{2})"), MONTH),
               (re.compile(r"(\d{4})"), YEAR))


def period(value: str) -> tuple[date, str] | None:
    """Дата модели → первый день её периода и точность: ГГГГ-ММ-ДД — день, ГГГГ-ММ — месяц, ГГГГ — год."""
    for pattern, precision in _PRECISIONS:
        if match := pattern.fullmatch(value):
            year, month, day = (int(part) for part in (*match.groups(), 1, 1)[:3])
            try:
                return date(year, month, day), precision
            except ValueError:
                return None
    return None


def from_answer(value: str, source: str, published: date | None,
                undefined: str) -> tuple[date | None, str, str | None]:
    """Дата события из ответа модели, откуда она и её точность: «по дате публикации» — дата публикации; «из текста» —
    дата модели (день, месяц или год), если её период начался не позже публикации и не раньше 2000 года, иначе
    `undefined`. Дата публикации — по UTC, поэтому сутки допуска: событие дня публикации по местному времени бывает
    на день позже её даты по UTC. Даты публикации нет (находка «режима ChatGPT») — событие не позже сегодняшнего
    дня. «Из текста» без даты — даты в тексте нет, и по инструкции это «по дате публикации»."""
    if source == FROM_TEXT and not str(value or "").strip():
        source = BY_PUBLICATION
    if source == BY_PUBLICATION:
        return (published, source, DAY) if published else (None, undefined, None)
    if source != FROM_TEXT:
        return None, undefined, None
    found = period(value.strip())
    if found is None:
        return None, undefined, None
    start, precision = found
    latest = (published or datetime.now(timezone.utc).date()) + timedelta(days=1)
    if not EARLIEST_EVENT <= start <= latest:
        return None, undefined, None
    return start, source, precision


def period_end(start: date, precision: str | None) -> date:
    """Последний день периода даты события: у дня — он сам, у месяца — последний день месяца, у года — 31 декабря."""
    if precision == YEAR:
        return date(start.year, 12, 31)
    if precision == MONTH:
        return date(start.year + start.month // 12, start.month % 12 + 1, 1) - timedelta(days=1)
    return start


def label(start: date, precision: str | None) -> str:
    """Дата события так, как она известна: ДД.ММ.ГГГГ, ММ.ГГГГ или ГГГГ — первое число не подставляется."""
    return {YEAR: f"{start:%Y}", MONTH: f"{start:%m.%Y}"}.get(precision or DAY, f"{start:%d.%m.%Y}")


def reference(published: date | None, received: date | None) -> tuple[date | None, str]:
    """От какого дня считается возраст события: дата публикации, нет её — дата поступления; и как его назвать."""
    return (published, "публикации") if published else (received, "поступления")


def ages(values: dict[str, Any], published: date | None, received: date | None) -> tuple[int, int] | None:
    """Возраст события в днях — самый малый и самый большой по периоду даты (у дня они равны); None — даты события
    в тексте нет или не от чего считать. Событие позже публикации — возраст 0."""
    if values.get("event_date_source") != FROM_TEXT or values.get("event_date") is None:
        return None
    day, _ = reference(published, received)
    if day is None:
        return None
    start, precision = values["event_date"], values.get("event_date_precision")
    return max((day - period_end(start, precision)).days, 0), max((day - start).days, 0)


def old_event_fact(values: dict[str, Any], published: date | None, received: date | None,
                   max_days: int) -> str | None:
    """Факт для «Отбора», если событие старое: дата события из текста старше `max_days` даже по последнему дню своего
    периода. Без даты в тексте факта нет."""
    found = ages(values, published, received)
    if found is None or found[0] <= max_days:
        return None
    day, name = reference(published, received)
    start, precision = values["event_date"], values.get("event_date_precision")
    least = "" if (precision or DAY) == DAY else "не меньше "
    return (f"дата события в тексте {label(start, precision)}, событию {least}{found[0]} дн. к дате {name} "
            f"{day:%d.%m.%Y}")
