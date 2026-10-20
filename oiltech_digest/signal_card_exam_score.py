"""Счёт экзамена агента «Карточка сигнала» (#98): значения карточки против разметки эксперта.

Мерится то, что дойдёт до карточки: ответ модели после проверок кодом (signal_card.card_values).
Рядом с каждым атрибутом — результат константы: самое частое значение эксперта на тех же строках, для
даты события — дата публикации. Пороги спеки (#93, поправки 18.10): компании — точность ≥ 90% и
полнота ≥ 80% по упоминаниям; регион ≥ 85%; тип события, класс технологии, зрелость ≥ 75%; связь с
нефтесервисом — средняя доля верных по значениям ≥ 75%; дата события — ошибок не больше, чем у
константы; выдуманных компаний и дат — 0.

Два вида отчёта: строго — по меткам эксперта; с допусками — в ячейке из списка допусков засчитывается и
законный второй ответ, а компания из списка допусков необязательна: её не требуют и за неё не штрафуют.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
import re
from typing import Any

from oiltech_digest.signal_card import FROM_TEXT, RELEVANCE_CODES
from oiltech_digest.signal_card_pack import UNDEFINED
from oiltech_digest.word_forms import named_among, same_name, split_abbreviation, without_legal_form, words

THRESHOLDS = {"companies_precision": 0.90, "companies_recall": 0.80, "region": 0.85, "event_type": 0.75,
              "technology_class": 0.75, "maturity": 0.75, "oilfield_relevance": 0.75}
CHOICES = ("event_type", "region", "technology_class", "maturity")
# Атрибут разметки → колонка карточки (signal_card.CHOICE_COLUMNS: зрелость — technology_maturity).
_CARD_COLUMNS = {"event_type": "event_type", "region": "region", "technology_class": "technology_class",
                 "maturity": "technology_maturity", "event_date": "event_date",
                 "event_date_source": "event_date_source"}
_RELEVANCE_RU = {code: value for value, code in RELEVANCE_CODES.items()}
# Карточки нет (сбой модели, ответ не по схеме): ни с одной меткой не совпадает.
_MISSING = object()


# Значения карточки по прогонам: ID строки → по значению на прогон (None — карточки нет).
Finals = Mapping[str, Sequence[Mapping[str, Any] | None]]


def evaluate(items: list[dict[str, Any]], finals: Finals, names: CompanyNames, *, lenient: bool) -> dict[str, Any]:
    """Метрики в целом (с порогами) и по долям набора. finals — значения карточки по прогонам."""
    overall = _metrics(items, finals, names, lenient)
    for key, row in overall.items():
        row["passed"] = _passed(key, row)
    strata = sorted({item["stratum"] for item in items})
    by_stratum = {stratum: _metrics([item for item in items if item["stratum"] == stratum], finals, names, lenient)
                  for stratum in strata}
    failed = [key for key, row in overall.items() if row["passed"] is False]
    return {"overall": overall, "by_stratum": by_stratum, "passed": not failed, "failed": failed}


def _passed(key: str, row: dict[str, Any]) -> bool | None:
    if key in THRESHOLDS:
        return row["agent"] is not None and row["agent"] >= THRESHOLDS[key]
    if key == "event_date":
        return row["agent"] is not None and row["agent"] <= row["constant"]
    if key in ("invented_companies", "invented_dates"):
        return row["agent"] == 0
    return None


def _metrics(items, finals, names, lenient) -> dict[str, dict[str, Any]]:
    precision, recall, invented = _companies(items, finals, names, lenient)
    return {
        "companies_precision": precision, "companies_recall": recall,
        **{key: _accuracy(items, finals, key, lenient) for key in CHOICES},
        "oilfield_relevance": _per_value(items, finals, "oilfield_relevance", lenient),
        "event_date": _event_date(items, finals, lenient),
        # Источник даты — только где дата события не день публикации: иначе «из текста» и «по дате
        # публикации» дают одну и ту же дату, и расхождение ничего не значит.
        "event_date_source": _accuracy([item for item in items if item["labels"]["event_date"] != item["published"]],
                                       finals, "event_date_source", lenient),
        "invented_companies": invented,
        "invented_dates": _invented_dates(items, finals, lenient),
    }


def _card_value(final: Mapping[str, Any] | None, key: str) -> Any:
    """Значение атрибута карточки в форме разметки эксперта."""
    if final is None:
        return _MISSING
    if key == "companies":
        return [item["name"] for item in final["participants_json"]]
    if key == "oilfield_relevance":
        return _RELEVANCE_RU.get(final["oilfield_relevance"], UNDEFINED)
    return final[_CARD_COLUMNS[key]]


def _allowed(item: dict[str, Any], key: str, lenient: bool) -> list[Any]:
    return [item["labels"][key], *(item["tolerated"].get(key, []) if lenient else [])]


def _most_common(values: list[Any]) -> Any:
    return Counter(values).most_common(1)[0][0] if values else None


def _share(hits: int, total: int) -> float | None:
    return hits / total if total else None


def _stability(items, finals, key, form=lambda value: value) -> float | None:
    """Доля сигналов, у которых все прогоны дали одно и то же значение (и ни один не сорвался)."""
    stable = 0
    for item in items:
        seen = [_card_value(final, key) for final in finals[item["id"]]]
        stable += all(one is not _MISSING for one in seen) and len({form(one) for one in seen}) == 1
    return _share(stable, len(items))


def _accuracy(items, finals, key, lenient) -> dict[str, Any]:
    hits = sum(_card_value(final, key) in _allowed(item, key, lenient) for item in items for final in finals[item["id"]])
    total = sum(len(finals[item["id"]]) for item in items)
    constant = _most_common([item["labels"][key] for item in items])
    return {"agent": _share(hits, total), "n": total, "stability": _stability(items, finals, key),
            "constant": _share(sum(constant in _allowed(item, key, lenient) for item in items), len(items)),
            "constant_value": constant}


def _per_value(items, finals, key, lenient) -> dict[str, Any]:
    """Средняя по значениям эксперта доля верных: редкое значение весит столько же, сколько частое."""
    groups: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        groups[item["labels"][key]].append(item)
    constant = _most_common([item["labels"][key] for item in items])
    agent, fixed = [], []
    for group in groups.values():
        hits = sum(_card_value(final, key) in _allowed(item, key, lenient) for item in group for final in finals[item["id"]])
        agent.append(_share(hits, sum(len(finals[item["id"]]) for item in group)))
        fixed.append(_share(sum(constant in _allowed(item, key, lenient) for item in group), len(group)))
    return {"agent": _mean(agent), "n": sum(len(finals[item["id"]]) for item in items),
            "stability": _stability(items, finals, key), "constant": _mean(fixed), "constant_value": constant,
            "by_value": {str(label): share for label, share in zip(groups, agent)}}


def _mean(values: list[float | None]) -> float | None:
    known = [one for one in values if one is not None]
    return sum(known) / len(known) if known else None


def _event_date(items, finals, lenient) -> dict[str, Any]:
    """Дата события: доля ошибок агента против доли ошибок ответа «всегда дата публикации»."""
    errors = sum(_card_value(final, "event_date") not in _allowed(item, "event_date", lenient)
                 for item in items for final in finals[item["id"]])
    total = sum(len(finals[item["id"]]) for item in items)
    constant = sum(item["published"] not in _allowed(item, "event_date", lenient) for item in items)
    return {"agent": _share(errors, total), "errors": errors, "n": total,
            "stability": _stability(items, finals, "event_date"),
            "constant": _share(constant, len(items)), "constant_errors": constant, "constant_value": "дата публикации"}


def _invented_dates(items, finals, lenient) -> dict[str, Any]:
    """Выдуманная дата — «из текста», но её нет ни в разметке, ни в тексте, и это не день публикации
    (с сутками допуска, как у проверки кодом)."""
    invented = 0
    for item in items:
        allowed = _allowed(item, "event_date", lenient)
        for final in finals[item["id"]]:
            day = _card_value(final, "event_date")
            if day is _MISSING or day is None or final["event_date_source"] != FROM_TEXT or day in allowed:
                continue
            published = item["published"]
            if published and abs(day - published) <= timedelta(days=1):
                continue
            invented += not date_in_text(day, f"{item['title']}\n{item['text']}")
    return {"agent": invented, "n": sum(len(finals[item["id"]]) for item in items)}


def baseline_metrics(items: list[dict[str, Any]], finals: Finals, names: CompanyNames, *,
                     lenient: bool) -> dict[str, float | None]:
    """Базовая линия без ИИ (signal_card_exam_baseline) — те же метрики компаний и региона."""
    precision, recall, _ = _companies(items, finals, names, lenient)
    return {"companies_precision": precision["agent"], "companies_recall": recall["agent"],
            "region": _accuracy(items, finals, "region", lenient)["agent"]}


def judged(items: list[dict[str, Any]], values: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Судья радара против эксперта — на всех радарных строках набора (values — выгрузка судьи с ядра), без
    вызовов модели; выгрузки нет — None."""
    if values is None:
        return None
    radar = [item for item in items if item["origin"] == "radar" and item["id"] in values]
    finals = {item["id"]: [values[item["id"]]] for item in radar}
    return {view: judge_metrics(radar, finals, lenient=view == "lenient") for view in ("strict", "lenient")}


def judge_metrics(items: list[dict[str, Any]], finals: Finals, *, lenient: bool) -> dict[str, Any]:
    """Судья радара против эксперта: дата события и связь с нефтесервисом, которые на проде у находок радара
    показывает судья (агент их не перезаписывает)."""
    return {"rows": len(items), "event_date": _event_date(items, finals, lenient),
            "oilfield_relevance": _per_value(items, finals, "oilfield_relevance", lenient)}


def _companies(items, finals, names, lenient):
    """Точность и полнота — по упоминаниям (с порогами) и по сигналам (среднее по прогонам сигналов); рядом —
    константа: самая частая компания эксперта у каждого сигнала."""
    agent = _company_tally(items, finals, names, lenient)
    labels = [name for item in items for name in item["labels"]["companies"]]
    top = _most_common([names.key(name) for name in labels])
    constant = next((name for name in labels if names.key(name) == top), None)
    fixed = _company_tally(items, {item["id"]: [{"participants_json": [{"name": constant}] if constant else []}]
                                   for item in items}, names, lenient)
    total = sum(len(finals[item["id"]]) for item in items)
    stability = _stability(items, finals, "companies", lambda given: frozenset(map(names.key, given)))
    precision = {"agent": _share(agent["hits"], agent["named"]), "n": total, "stability": stability,
                 "constant": _share(fixed["hits"], fixed["named"]), "constant_value": constant,
                 "by_signal": _mean(agent["by_signal_precision"]), "named": agent["named"], "matched": agent["hits"]}
    recall = {"agent": _share(agent["hits"], agent["required"]), "n": total, "stability": stability,
              "constant": _share(fixed["hits"], fixed["required"]), "constant_value": constant,
              "by_signal": _mean(agent["by_signal_recall"]), "expected": agent["required"], "matched": agent["hits"]}
    return precision, recall, {"agent": agent["invented"], "n": total}


def _company_tally(items, finals, names, lenient) -> dict[str, Any]:
    """Совпадения компаний по прогонам: названные, совпавшие и требуемые упоминания, выдуманные, доли по сигналам.
    Компания из допусков необязательна: её не требуют и за неё не штрафуют."""
    tally: dict[str, Any] = {"hits": 0, "named": 0, "required": 0, "invented": 0,
                             "by_signal_precision": [], "by_signal_recall": []}
    for item in items:
        optional = item["tolerated"].get("companies", []) if lenient else []
        required = [name for name in item["labels"]["companies"] if not names.among(name, optional)]
        text = text_tokens(f"{item['title']}\n{item['text']}", keep_case=True)
        for final in finals[item["id"]]:
            given = _card_value(final, "companies") if final is not None else []
            tally["invented"] += sum(not names.in_text(name, text) for name in given)
            counted = [name for name in given if not names.among(name, optional)]
            matched = names.matched(counted, required)
            tally["hits"] += matched
            tally["named"] += len(counted)
            tally["required"] += len(required)
            if counted:
                tally["by_signal_precision"].append(matched / len(counted))
            if required:
                tally["by_signal_recall"].append(matched / len(required))
    return tally


# --- Имена компаний ------------------------------------------------------------------------------

def text_tokens(text: str, *, keep_case: bool = False) -> list[str]:
    """Слова имени или текста — те же, что сверяет ядро (word_forms.words): без различий «ё», дефисов, кавычек
    («КТК‑Р» — это «КТК-Р») и, кроме keep_case, регистра."""
    return [word if keep_case else word.casefold() for word in words(text)]


def _spelled(name: str) -> tuple[str, ...]:
    return tuple(text_tokens(name, keep_case=True))


class CompanyNames:
    """Сравнение имён агента и эксперта: канон по алиасам (справочник и список экзамена), слова — по
    основам, без различий регистра и формы собственности (word_forms.same_name — форму собственности не считает и
    ядро). «Имя (АББР)» — правило экзамена, ядро его не знает: аббревиатура (word_forms.split_abbreviation) равна
    имени другой стороны. Компания — целиком: «Газпром» — не «Газпром нефть». Имена хранятся как
    написаны: регистр нужен, чтобы искать компанию в тексте так же, как ядро — в цитате."""

    def __init__(self, entries: Sequence[dict[str, Any]], aliases: Sequence[dict[str, Any]] = ()):
        """entries — справочник компаний ядра, aliases — алиасы экзамена (имена эксперта не как в тексте)."""
        self.groups: list[list[tuple[str, ...]]] = []
        for entry in (*entries, *aliases):
            spelled = (tuple(text_tokens(name, keep_case=True)) for name in (entry["name"], *entry["aliases"]))
            forms = [form for form in spelled if form]
            joined = [group for group in self.groups if any(same_name(a, b) for a in forms for b in group)]
            self.groups = [group for group in self.groups if group not in joined]
            self.groups.append(forms + [form for group in joined for form in group])
        # Имена справочника ядра: длинное закрывает короткое в тексте так же, как у ядра в цитате. Алиасы экзамена
        # не закрывают — ядро их не знает, и экзамен не был бы строже карточки.
        self.dictionary = [tuple(text_tokens(name, keep_case=True)) for entry in entries
                           for name in (entry["name"], *entry["aliases"])]

    def _group(self, own: tuple[str, ...]) -> list[tuple[str, ...]]:
        return next((group for group in self.groups if any(same_name(own, form) for form in group)), [own])

    def forms(self, name: str) -> list[tuple[str, ...]]:
        """Написания компании — её группа алиасов; у «Имя (АББР)» — группа имени без аббревиатуры."""
        return self._group(_spelled(split_abbreviation(name)[0]))

    def _abbreviation(self, name: str) -> list[tuple[str, ...]]:
        short = split_abbreviation(name)[1]
        return self._group(_spelled(short)) if short else []

    def same(self, one: str, other: str) -> bool:
        """Одна компания: совпали написания, или аббревиатура одной — написание другой. Аббревиатуры между собой
        не сравниваются: одно заглавное слово в скобках («(США)») бывает у разных компаний."""
        mine, theirs = self.forms(one), self.forms(other)
        return (any(same_name(a, b) for a in (*mine, *self._abbreviation(one)) for b in theirs)
                or any(same_name(a, b) for a in mine for b in self._abbreviation(other)))

    def among(self, name: str, others: list[str]) -> bool:
        return any(self.same(name, other) for other in others)

    def key(self, name: str) -> tuple[str, ...]:
        """Одно написание на компанию — для устойчивости между прогонами: как в сравнении, без формы собственности
        и аббревиатуры в скобках (одна аббревиатура без имени — свой ключ)."""
        return min(tuple(word.casefold() for word in without_legal_form(form)) for form in self.forms(name))

    def matched(self, given: list[str], expected: list[str]) -> int:
        left = list(expected)
        hits = 0
        for name in given:
            found = next((other for other in left if self.same(name, other)), None)
            if found is not None:
                left.remove(found)
                hits += 1
        return hits

    def in_text(self, name: str, text: list[str]) -> bool:
        """Компания есть в тексте (его слова с регистром — text_tokens(keep_case=True)) — названа под любым своим
        именем по правилу ядра (word_forms.named_among): все слова имени, кроме формы собственности, и не только
        внутри длинного имени другой компании справочника. Выдуманной — нет. Имя — как написано, со скобкой: ядро
        «Имя (АББР)» не делит."""
        forms = self._group(_spelled(name))
        return named_among(forms, text, [form for form in self.dictionary if form not in forms])


# --- Даты в тексте -------------------------------------------------------------------------------

_RU_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
              "ноября", "декабря")
_EN_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
              "november", "december")


def date_in_text(day: date, text: str) -> bool:
    """Названа ли дата в тексте: ГГГГ-ММ-ДД, ДД.ММ.ГГГГ, «24 сентября», «24 September», «Sept. 24»."""
    low = str(text or "").casefold().replace("ё", "е")
    d, m, y = day.day, day.month, day.year
    en, ru = _EN_MONTHS[m - 1], _RU_MONTHS[m - 1]
    month_en = rf"(?:{en}|{en[:3]}|{'sept' if m == 9 else en[:3]})\.?"
    year = rf"(?:{y}|{y % 100:02d})"
    patterns = (
        rf"\b{y}-{m:02d}-{d:02d}\b", rf"\b0?{d}[./]0?{m}[./]{year}\b", rf"\b0?{m}/0?{d}/{year}\b",
        rf"\b0?{d}(?:-?го)?\s+{ru}\b", rf"\b0?{d}(?:st|nd|rd|th)?\s+(?:of\s+)?{month_en}(?!\w)",
        rf"(?<!\w){month_en}\s+0?{d}(?:st|nd|rd|th)?\b",
    )
    return any(re.search(pattern, low) for pattern in patterns)
