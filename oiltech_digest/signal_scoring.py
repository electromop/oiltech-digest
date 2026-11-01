"""Счёт второго вызова «Карточки» (#132, спека #128): то, что по линейкам считает код, а не модель.

«Свежесть» — по возрасту события из текста (event_dates), «Зрелость / доказательность» — в ступени, заданной стадией
зрелости карточки, итог по весам экрана «Скоринг» — в двух вариантах подмешивания ключевых слов (экзамен #133
выберет), ступень итога — по границам настройки SIGNAL_SCORE_BANDS. Числа ступеней «Свежести» и стадии «Зрелости»
живут в линейках (scoring_rubrics.json) рядом с их текстом: одна линейка идёт в инструкцию модели, в правила
разметки эксперта и сюда. Без базы и без модели: этим пользуются ядро (signal_scores.score_values) и экзамен.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from oiltech_digest import config, event_dates
from oiltech_digest.processing.pipeline import SCORE_AI_WEIGHT, SCORE_KEYWORD_WEIGHT

STRONG, USEFUL, WATCH, WEAK = "strong", "useful", "watch", "weak"
BAND_ORDER = (STRONG, USEFUL, WATCH, WEAK)
BAND_LABELS = {STRONG: "сильный", USEFUL: "полезный", WATCH: "наблюдать", WEAK: "слабый"}
# Критерии, которые линейки отдают коду: «Свежесть» считает код, «Зрелость» держится в ступени стадии карточки.
COMPUTED_FRESHNESS = "freshness"


def computed(rubric: dict[str, Any] | None) -> bool:
    """Балл критерия считает код, а не модель («Свежесть»)."""
    return bool(rubric) and rubric.get("computed") == COMPUTED_FRESHNESS


def bounded(rubric: dict[str, Any] | None) -> bool:
    """Балл критерия держится в ступени стадии зрелости карточки («Зрелость / доказательность»)."""
    return bool(rubric) and bool(rubric.get("bounded_by_stage"))


def _range(scale: list[dict[str, Any]], band: str) -> tuple[int, int]:
    found = next(one for one in scale if one["code"] == band)
    return int(found["min"]), int(found["max"])


def freshness(rubric: dict[str, Any], scale: list[dict[str, Any]], values: dict[str, Any], published: date | None,
              received: date | None) -> tuple[float | None, str | None]:
    """Балл «Свежести» и почему его нет: по дням от даты публикации (нет её — от даты поступления), внутри ступени
    линейно. Даты события в тексте нет или её период (месяц, год) попадает в разные ступени — балла нет, критерий в
    итог не входит; у периода в одной ступени — балл по его середине: первое число месяца не подставляется."""
    found = event_dates.ages(values, published, received)
    if found is None:
        return None, "нет даты события в тексте"
    youngest, oldest = found
    limits = rubric["age_days"]
    if _age_band(limits, youngest) != _age_band(limits, oldest):
        return None, "дата события известна до месяца или года, возраст попадает в разные ступени"
    return round(_by_age(limits, scale, (youngest + oldest) / 2), 2), None


def _age_band(limits: dict[str, int], age: float) -> str:
    return next((band for band in BAND_ORDER[:-1] if age <= limits[band]), WEAK)


def _by_age(limits: dict[str, int], scale: list[dict[str, Any]], age: float) -> float:
    """Ступень по возрасту, внутри неё линейно: первый день ступени — её верхний балл, последний — нижний. У «слабой»
    последний день — limits["weak"], дальше 0."""
    band = _age_band(limits, age)
    index = BAND_ORDER.index(band)
    first = 0 if index == 0 else limits[BAND_ORDER[index - 1]] + 1
    last = limits[band]
    low, high = _range(scale, band)
    if age >= last:
        return float(low)
    return high - (age - first) * (high - low) / (last - first)


def maturity_range(rubric: dict[str, Any], scale: list[dict[str, Any]], stage: str | None,
                   named_customer: bool | None) -> tuple[int, int]:
    """Где может стоять балл «Зрелости / доказательности»: в ступени стадии зрелости карточки. Внедрение даёт
    «сильный» только у названного заказчика, иначе это запуск продукта — rubric["without_named_customer"] (40-64).
    Стадии нет в линейке («неприменимо», «не определено») — без ограничения."""
    band = (rubric.get("stage_bands") or {}).get(stage or "")
    if band is None:
        return 0, 100
    if band == STRONG and not named_customer:
        band = rubric.get("without_named_customer") or WATCH
    return _range(scale, band)


def final_score(item: dict[str, Any], *, keywords: bool) -> float | None:
    """Балл критерия в итоге: с подмешиванием ключевых слов — как у статей ленты (pipeline.normalize_score_payload:
    ключи добавляют вес, только когда совпадений много), без — балл модели или кода как есть. bounds — ступень, из
    которой балл не выходит и с ключевыми словами («Зрелость» в ступени стадии карточки)."""
    score, keyword = item["ai_score"], item.get("keyword_score")
    if score is None or not keywords or keyword is None:
        return score
    blended = max(score, keyword * SCORE_KEYWORD_WEIGHT + score * SCORE_AI_WEIGHT)
    if item.get("bounds"):
        blended = min(max(blended, item["bounds"][0]), item["bounds"][1])
    return round(blended, 2)


def totals(items: list[dict[str, Any]]) -> dict[str, float | None]:
    """Итог по весам в двух вариантах — с подмешиванием ключевых слов (keywords) и без (plain). Критерий без балла
    («Свежесть» без даты события) в итог не входит: веса остальных пересчитываются на их сумму."""
    included = [item for item in items if item["ai_score"] is not None]
    weight = sum(float(item["weight"]) for item in included)
    if not weight:
        return {"keywords": None, "plain": None}
    return {variant: round(sum(final_score(item, keywords=variant == "keywords") * float(item["weight"])
                               for item in included) / weight, 2)
            for variant in ("keywords", "plain")}


def chosen(found: dict[str, float | None]) -> float | None:
    """Итог, который идёт в балл сигнала, — вариант настройки SIGNAL_SCORES_KEYWORDS."""
    return found["keywords" if config.SIGNAL_SCORES_KEYWORDS else "plain"]


def bands_problem(bands: tuple[int, ...]) -> str | None:
    """Чем границы ступеней итога не годятся; None, если годятся: три границы (сильный, полезный, наблюдать) от 0 до
    100 по убыванию."""
    if len(bands) != 3 or not 100 >= bands[0] > bands[1] > bands[2] >= 0:
        return (f"{','.join(map(str, bands))}: нужны три границы от 0 до 100 по убыванию (сильный, полезный, "
                "наблюдать)")
    return None


def band(score: float | None) -> str | None:
    """Ступень итога по границам настройки SIGNAL_SCORE_BANDS (сильный, полезный, наблюдать; ниже — слабый). Границы
    проверяет постановка этапа (bands_problem)."""
    if score is None:
        return None
    strong, useful, watch = config.SIGNAL_SCORE_BANDS
    return STRONG if score >= strong else USEFUL if score >= useful else WATCH if score >= watch else WEAK
