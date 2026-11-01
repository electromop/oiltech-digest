"""Счёт экзамена «Карточки» (#133, спека #128): тема основного вызова и баллы второго против разметки эксперта.

Мерится то, что дошло бы до сигнала: карточка после проверки кодом ядра (signal_card.card_values) и баллы после
signal_scores.score_values, итог в двух вариантах подмешивания ключевых слов (signal_scoring.totals). Сбой — промах.

- Ступень итога (границы SIGNAL_SCORE_BANDS) против общей оценки эксперта (EXPERT_BANDS): доля совпавших ответов.
- Порядок: пары строк с разной ступенью эксперта; верно, если итог выше у строки с более высокой ступенью, ничья —
  половина, сбой — промах; среднее по прогонам. Константа — 50%.
- По каждому критерию: средняя ошибка и сдвиг (модель минус эксперт) там, где число есть у обоих; у «Свежести» ещё
  совпадение «есть дата / нет даты» (её считает код по дате основного вызова).
- Тема основного вызова — доля совпавших с темой эксперта; ошибки «Рынок против темы отрасли» (#149) отдельно.
- Страна события основного вызова (#137) — против региона эксперта #98 по таблице «страна → регион», без порога.

Решение и правило прохождения — по строкам пакета: с ними сравниваются константа, эксперт сам с собой (итог по весам из
его же баллов) и контроль с ядра (балл и тег ленты, балл и тема судьи). «Сильные сверх пакета» (выбранное «в дайджест»,
эталонный ТОП) — отдельной строкой: доля ответов «сильный» и порядок вместе с ними; иначе их число решало бы, кто
прошёл (константа «всегда сильный» на смеси). Правило утвердил владелец 27.10: ступень лучше нынешних баллов ядра
(контроль) на тех же строках, порядок от 85%; порог темы в решение второго вызова не входит. Эксперт сам с собой —
для сведения: как порог он оказался потолком, а не полом. rules_text (с границами итога) — в отметке утверждения.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from oiltech_digest import config, signal_scoring
from oiltech_digest.signal_scoring import BAND_ORDER, STRONG, USEFUL, WATCH, WEAK

THRESHOLDS = {"order": 0.85, "theme": 0.65}
# Порог ступени — не число, а нынешние баллы ядра на тех же строках (control_agreement): модель их заменяет.
BAND_RULE = "control"
BAND_RULE_RU = "лучше нынешних баллов ядра на тех же строках"
VARIANTS = ("keywords", "plain")
# Общая оценка эксперта → ступень: «наблюдать» и «фоновый» у эксперта одна ступень (разбор разметки, раздел 1.5).
EXPERT_BANDS = {"strong_signal": STRONG, "approved": USEFUL, "watch_later": WATCH, "background_material": WATCH,
                "reject": WEAK, "wrong_domain": WEAK}
# Оценка «сильного сверх пакета» вместо общей оценки эксперта.
EXTRA = "strong_extra"
_RANK = {STRONG: 3, USEFUL: 2, WATCH: 1, WEAK: 0}
NO_DATE = "нет даты"

# Значения по прогонам: номер строки → по значению на прогон (None — сбой).
Finals = Mapping[str, Sequence[Mapping[str, Any] | None]]


def rules_text() -> str:
    """Пороги и границы итога словами — их называет отметка утверждения владельцем: другие — другое утверждение."""
    bands = "/".join(str(bound) for bound in config.SIGNAL_SCORE_BANDS)
    return (f"пороги: границы итога {bands}, ступень {BAND_RULE_RU}, порядок {THRESHOLDS['order']:.0%}, "
            f"тема {THRESHOLDS['theme']:.0%}")


def evaluate(rows: list[dict[str, Any]], finals: Finals, control: Mapping[str, Any]) -> dict[str, Any]:
    """Метрики модели второго вызова в обоих вариантах, правило прохождения и вариант ближе к эксперту; control —
    нынешние баллы ядра по строке: ступень модели на строках с ними должна быть выше их ступени там же."""
    core = control_agreement(rows, control)
    variants = {variant: _variant(rows, finals, variant, core) for variant in VARIANTS}
    passing = [variant for variant in VARIANTS if variants[variant]["passed"]]
    chosen = _closer(variants, passing or list(VARIANTS))
    every = [final for row in rows for final in finals[row["id"]]]
    return {**variants, "variant": chosen, "passed": bool(passing), "failed": variants[chosen]["failed"],
            "answers": {"answers": len(every), "failures": sum(final is None for final in every)},
            "totals": _totals_by_rating(rows, finals, chosen), "bands": _bands(rows, finals, chosen)}


def _closer(variants: dict[str, Any], candidates: list[str]) -> str:
    """Вариант ближе к эксперту: выше доля ступени, затем порядок; при равенстве — нынешняя настройка."""
    current = "keywords" if config.SIGNAL_SCORES_KEYWORDS else "plain"
    return max(candidates, key=lambda variant: (variants[variant]["band"]["agent"] or 0,
                                                variants[variant]["order"]["agent"] or 0, variant == current))


def _variant(rows: list[dict[str, Any]], finals: Finals, variant: str, core: Mapping[str, Any]) -> dict[str, Any]:
    totals = {row["id"]: [_total(final, variant) for final in finals[row["id"]]] for row in rows}
    pack = [row for row in rows if not row["extra"]]
    extra = [signal_scoring.band(total) for row in rows if row["extra"] for total in totals[row["id"]]]
    same = _band([row for row in pack if row["id"] in core["ids"]], totals)["agent"]
    band = {**_band(pack, totals), "same_rows": same, "floor": core["band"],
            "extra_strong": _share(extra.count(STRONG), len(extra))}
    order = {**order_share(pack, totals), "with_extra": order_share(rows, totals)["agent"]}
    # Ступень — строго лучше ядра (равная его не заменяет), порядок — от порога.
    checks = {"band": same is not None and core["band"] is not None and same > core["band"],
              "order": order["agent"] is not None and order["agent"] >= THRESHOLDS["order"]}
    failed = [name for name, passed in checks.items() if not passed]
    return {"band": band, "order": order, "criteria": _criteria(pack, finals, variant),
            "freshness": _freshness(pack, finals), "passed": not failed, "failed": failed}


def _total(final: Mapping[str, Any] | None, variant: str) -> float | None:
    return None if final is None else final["totals"][variant]


# --- Ступень и порядок ---------------------------------------------------------------------------


def _band(rows: list[dict[str, Any]], totals: Mapping[str, Sequence[float | None]]) -> dict[str, Any]:
    """Доля ответов со ступенью эксперта; устойчивость — одна ступень во всех прогонах без сбоев."""
    pairs = [(row["band"], signal_scoring.band(total)) for row in rows for total in totals[row["id"]]]
    stable = sum(all(total is not None for total in totals[row["id"]])
                 and len({signal_scoring.band(total) for total in totals[row["id"]]}) == 1 for row in rows)
    return {"agent": _share(sum(expert == given for expert, given in pairs), len(pairs)), "n": len(pairs),
            "stability": _share(stable, len(rows))}


def order_share(rows: list[dict[str, Any]], totals: Mapping[str, Sequence[float | None]]) -> dict[str, Any]:
    """Доля верно упорядоченных пар строк с разной ступенью эксперта (ничья — половина, нет итога — промах), среднее
    по прогонам; по профилю — пары внутри профиля."""
    runs = max((len(values) for values in totals.values()), default=0)
    pairs = [(high, low) for high in rows for low in rows if _RANK[high["band"]] > _RANK[low["band"]]]
    if not pairs or not runs:
        return {"agent": None, "pairs": len(pairs), "by_profile": {}}

    def share(selected: list[tuple[dict[str, Any], dict[str, Any]]]) -> float | None:
        if not selected:
            return None
        return sum(_concordant(totals[high["id"]][run], totals[low["id"]][run])
                   for run in range(runs) for high, low in selected) / (len(selected) * runs)

    profiles = sorted({row["profile"] for row in rows})
    return {"agent": share(pairs), "pairs": len(pairs),
            "by_profile": {profile: share([(high, low) for high, low in pairs
                                           if high["profile"] == low["profile"] == profile]) for profile in profiles}}


def _concordant(high: float | None, low: float | None) -> float:
    if high is None or low is None:
        return 0.0
    return 1.0 if high > low else 0.5 if high == low else 0.0


def _bands(rows: list[dict[str, Any]], finals: Finals, variant: str) -> dict[str, Any]:
    """Ступень эксперта против ступени модели (ответов, строки пакета) и точность «сильного»: «сильный» даёт
    «кандидата» «Решения платформы», так что его точность — доля кандидатов, которых эксперт счёл сильными."""
    matrix: dict[str, Counter[str]] = {band: Counter() for band in BAND_ORDER}
    for row in rows:
        if row["extra"]:
            continue
        for final in finals[row["id"]]:
            matrix[row["band"]][signal_scoring.band(_total(final, variant)) or "none"] += 1
    called_strong = sum(counts[STRONG] for counts in matrix.values())
    return {"matrix": {band: dict(counts) for band, counts in matrix.items()},
            "strong_precision": _share(matrix[STRONG][STRONG], called_strong),
            "strong_recall": _share(matrix[STRONG][STRONG], sum(matrix[STRONG].values()))}


def _totals_by_rating(rows: list[dict[str, Any]], finals: Finals, variant: str) -> dict[str, Any]:
    """Итог модели по общей оценке эксперта (среднее по прогонам на строку): цифры модели, по которым пересматривают
    границы ступеней, если совпадение низкое (решение 30 журнала гриля)."""
    by_rating: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        values = [total for final in finals[row["id"]] if (total := _total(final, variant)) is not None]
        if values:
            by_rating[row["rating"]].append(sum(values) / len(values))
    return {rating: {"rows": len(values), "min": min(values), "mean": sum(values) / len(values), "max": max(values)}
            for rating, values in by_rating.items()}


# --- Критерии ------------------------------------------------------------------------------------


def _criteria(rows: list[dict[str, Any]], finals: Finals, variant: str) -> dict[str, Any]:
    """Средняя ошибка и сдвиг балла критерия против эксперта (строки пакета) — где число есть у обоих."""
    diffs: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        for final in finals[row["id"]]:
            for item in (final or {}).get("items") or []:
                expert = row["scores"].get(item["name"])
                given = signal_scoring.final_score(item, keywords=variant == "keywords")
                if isinstance(expert, (int, float)) and given is not None:
                    diffs[item["name"]].append(given - expert)
    return {name: {"mae": sum(abs(one) for one in values) / len(values), "bias": sum(values) / len(values),
                   "n": len(values)} for name, values in diffs.items()}


def _freshness(rows: list[dict[str, Any]], finals: Finals) -> dict[str, Any]:
    """«Свежесть» (её считает код по дате основного вызова): есть ли дата — как у эксперта (строки пакета)."""
    pairs = []
    for row in rows:
        expert = next((value for name, value in row["scores"].items() if name in row["computed"]), None)
        if expert is None:
            continue
        for final in finals[row["id"]]:
            item = next((one for one in (final or {}).get("items") or [] if one["name"] in row["computed"]), None)
            pairs.append(item is not None and (item["ai_score"] is None) == (expert == NO_DATE))
    return {"agreement": _share(sum(pairs), len(pairs)), "n": len(pairs)}


# --- Тема ----------------------------------------------------------------------------------------


def theme(rows: list[dict[str, Any]], cards: Finals, market: str | None = None) -> dict[str, Any]:
    """Тема основного вызова против темы эксперта — у строк пакета; сбой — промах. market — имя темы рынка в списке
    (signal_themes.market): по нему ошибки «Рынок против темы отрасли» (#149)."""
    pack = [row for row in rows if not row["extra"]]
    pairs = [(row["id"], row["theme"], (card or {}).get("theme")) for row in pack for card in cards[row["id"]]]
    agent = _share(sum(expert == given for _, expert, given in pairs), len(pairs))
    return {"agent": agent, "n": len(pairs), "threshold": THRESHOLDS["theme"],
            "passed": agent is not None and agent >= THRESHOLDS["theme"], "market": _market_errors(pairs, market)}


def _market_errors(pairs: list[tuple[str, Any, Any]], market: str | None) -> dict[str, Any] | None:
    """Ошибки «Рынок против темы отрасли» (#149): у эксперта тема рынка, у модели другая из списка (missed), и
    наоборот (extra) — ответы и строки; «не определено» и сбой — не тема отрасли. Темы рынка в списке нет — None."""
    if market is None:
        return None

    def errors(found: list[str]) -> dict[str, Any]:
        return {"answers": len(found), "rows": sorted(set(found))}
    return {"theme": market,
            "missed": errors([number for number, expert, given in pairs
                              if expert == market and given not in (None, market)]),
            "extra": errors([number for number, expert, given in pairs
                             if given == market and expert not in (None, market)])}


# Регионы эксперта, с которыми страну не сверить: у «глобально» страны нет, «не определено» — регион не понят.
_REGIONS_WITHOUT_COUNTRY = frozenset({"глобально", "не определено"})


def country_region(items: list[dict[str, Any]], cards: Finals, countries: Any) -> dict[str, Any]:
    """Страна события основного вызова против региона эксперта #98 по таблице «страна → регион» (#137; эксперт страну
    не размечает): ответ совпал, если регион эксперта среди регионов хоть одной страны события; без стран и без
    карточки — промах. Строки с регионом «глобально» и «не определено» не сверяются. Порога нет: расхождения и
    совпавшие (страна могла быть не та, но в том же регионе) — списками для глаз. countries — справочник стран
    (signal_card_countries.Countries)."""
    compared = [item for item in items if item["labels"]["region"] not in _REGIONS_WITHOUT_COUNTRY]
    hits = empty = 0
    mismatches, matches = [], []
    for item in compared:
        region = item["labels"]["region"]
        given = [list((card or {}).get("event_countries") or []) for card in cards[item["id"]]]
        matched = [any(region in countries.regions(name) for name in names) for names in given]
        hits += sum(matched)
        empty += sum(not names for names in given)
        (matches if all(matched) else mismatches).append({"id": item["id"], "region": region, "countries": given})
    answers = sum(len(cards[item["id"]]) for item in compared)
    return {"agent": _share(hits, answers), "n": answers, "rows": len(compared), "no_country": empty,
            "skipped": len(items) - len(compared), "mismatches": mismatches, "matched": matches}


# --- Константа, эксперт сам с собой, контроль ------------------------------------------------------


def baseline(rows: list[dict[str, Any]], weights: Mapping[str, Mapping[str, float]],
             control: Mapping[str, Any]) -> dict[str, Any]:
    """То, с чем сравнивается модель, — без её ответов: константа (самая частая ступень и тема эксперта, его среднее по
    критерию), эксперт сам с собой (итог по весам из его баллов) и контроль с ядра (балл и тег ленты, балл и тема
    судьи) — на строках пакета, контроль — где он есть."""
    pack = [row for row in rows if not row["extra"]]
    common_band = _most_common([row["band"] for row in pack])
    common_theme = _most_common([row["theme"] for row in pack])
    themed = [row for row in pack if (control.get(row["id"]) or {}).get("theme")]
    means = _expert_means(pack)
    return {
        "constant": {"band": _share(sum(row["band"] == common_band for row in pack), len(pack)),
                     "band_value": common_band, "order": 0.5,
                     "theme": _share(sum(row["theme"] == common_theme for row in pack), len(pack)),
                     "theme_value": common_theme,
                     "criteria": {name: _constant_error(pack, name, mean) for name, mean in means.items()}},
        "expert": expert_agreement(rows, weights),
        "control": {**{key: value for key, value in control_agreement(rows, control).items() if key != "ids"},
                    "theme_rows": len(themed),
                    "theme": _share(sum(row["theme"] == control[row["id"]]["theme"] for row in themed), len(themed))},
    }


def control_agreement(rows: list[dict[str, Any]], control: Mapping[str, Any]) -> dict[str, Any]:
    """Нынешние баллы ядра (балл ленты и судьи) на строках пакета, где они есть: ступень и порядок против эксперта.
    Ступень модели проходит, если выше их ступени на тех же строках (правило владельца 27.10)."""
    controlled = [row for row in rows if not row["extra"] and (control.get(row["id"]) or {}).get("score") is not None]
    totals = {row["id"]: [float(control[row["id"]]["score"])] for row in controlled}
    return {"rows": len(controlled), "ids": {row["id"] for row in controlled},
            "band": _band(controlled, totals)["agent"], "order": order_share(controlled, totals)["agent"]}


def expert_agreement(rows: list[dict[str, Any]], weights: Mapping[str, Mapping[str, float]]) -> dict[str, Any]:
    """Эксперт сам с собой на строках пакета: итог по весам из его баллов против его общей оценки — ступень и порядок.
    Для сведения: как порог он оказался потолком, а не полом."""
    pack = [row for row in rows if not row["extra"]]
    totals = {row["id"]: [expert_total(row, weights[row["profile"]])] for row in pack}
    return {"band": _band(pack, totals)["agent"], "order": order_share(pack, totals)["agent"]}


def expert_total(row: Mapping[str, Any], weights: Mapping[str, float]) -> float | None:
    """Итог по весам из баллов эксперта; «нет даты» у «Свежести» — критерий в итог не входит (как у кода)."""
    items = [{"weight": weight, "ai_score": value if isinstance(value, (int, float)) else None, "keyword_score": None}
             for name, weight in weights.items() for value in [row["scores"].get(name)]]
    return signal_scoring.totals(items)["plain"]


def _expert_means(rows: list[dict[str, Any]]) -> dict[str, float]:
    values: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        for name, value in row["scores"].items():
            if isinstance(value, (int, float)):
                values[name].append(float(value))
    return {name: sum(found) / len(found) for name, found in values.items()}


def _constant_error(rows: list[dict[str, Any]], name: str, mean: float) -> float:
    found = [float(value) for row in rows if isinstance(value := row["scores"].get(name), (int, float))]
    return sum(abs(value - mean) for value in found) / len(found)


def _most_common(values: list[Any]) -> Any:
    counted = Counter(one for one in values if one is not None)
    return counted.most_common(1)[0][0] if counted else None


def _share(hits: int | float, total: int | float) -> float | None:
    return hits / total if total else None
