"""Счёт экзамена агента «Отбор» (#133, спека #128): решения модели против разметки эксперта.

Мерится то, что дошло бы до сигнала (otbor_answer.outcome), по каждому прогону; сбой — промах по любой метке.
Решение «брать или нет» — доля верных по каждому классу эксперта отдельно: «брать» — сколько взятых экспертом модель
взяла, «не брать» — сколько отклонённых экспертом отклонила; сбалансированная доля — среднее двух. Рядом контроль —
решение машины по группе манифеста, как в разборе разметки 23.10 (решения машины в таблице нет), — и константа.
Страницы-списки в метрику решения не входят и считаются отдельно (решение 29 журнала гриля).

Правило прохождения (решение 29): на тех же строках экзамена по каждому классу не хуже гейта на случайных статьях
ленты и не хуже нынешней цепочки на строках с решением машины, класс «брать» не хуже судьи на строках радара (класс
«не брать» у радара отдельно не сравнивается: в нём две строки); сбалансированная доля на всех строках не ниже 80%.
Остальные метки — по черновику порогов разбора разметки (раздел 3.4); пороги утверждает владелец до платного прогона.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from oiltech_digest.otbor_answer import ACCEPT, REJECT

BALANCED_MIN = 0.80
THRESHOLDS = {"industry": 0.80, "oilfield_relevance": 0.75, "reject_reason": 0.70, "category": 0.80}
# Решение машины по группе манифеста (разбор разметки 23.10, раздел 3): строки старых 50 отбирались среди принятых
# гейтом и видимых на радаре, находки со ссылками — среди неотсеянных; у материалов коллег решения машины нет.
MACHINE = {"feed_accepted": ACCEPT, "card_markup": ACCEPT, "radar_links": ACCEPT,
           "feed_rejected": REJECT, "radar_filtered": REJECT}
FEED_GROUPS = ("feed_rejected", "feed_accepted")
# Срезы решения: подпись и строки среза; контроль — у строк с решением машины.
SLICES = {
    "feed_random": ("лента, случайные статьи (контроль: гейт)", lambda item: item["group"] in FEED_GROUPS),
    "feed_old": ("лента, старые 50", lambda item: item["group"] == "card_markup" and item["origin"] == "feed"),
    "radar_filtered": ("радар, «Отсеянные» (контроль: судья и ревью)", lambda item: item["group"] == "radar_filtered"),
    "radar_visible": ("радар, неотсеянные", lambda item: item["group"] == "radar_links"
                      or (item["group"] == "card_markup" and item["origin"] == "radar")),
    "radar": ("радар целиком (контроль: судья)", lambda item: item["origin"] == "radar"),
    "chain": ("все строки с решением машины (контроль: цепочка)", lambda item: item["group"] in MACHINE),
    "all": ("все строки", lambda item: True),
}
# Правило прохождения: срез и классы, где модель не хуже контроля (решение 29).
RULES = (("feed_random", (ACCEPT, REJECT)), ("radar", (ACCEPT,)), ("chain", (ACCEPT, REJECT)))
LABELS = ("decision", "industry", "oilfield_relevance", "reject_reason", "category")
# Значение сигнала по метке разметки (otbor_answer.outcome пишет решение в otbor_decision, категорию — в
# signal_category).
_FIELDS = {"decision": "otbor_decision", "industry": "industry", "oilfield_relevance": "oilfield_relevance",
           "reject_reason": "reject_reason", "category": "signal_category"}
_MISSING = object()

# Значения сигнала по прогонам: номер строки → по значению на прогон (None — сбой: решения нет).
Finals = Mapping[str, Sequence[Mapping[str, Any] | None]]


def evaluate(items: list[dict[str, Any]], finals: Finals, control: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Метрики модели на строках части: решение по срезам и правило прохождения, остальные метки с порогами,
    страницы-списки, сбои и спорные, оценка на весь поток ленты (если с ядра выгружены размеры страт)."""
    decided = [item for item in items if not item["list_page"]]
    decision = {name: _slice(title, [item for item in decided if belongs(item)], finals)
                for name, (title, belongs) in SLICES.items()}
    rule = _rule(decision)
    labels = _labels(items, finals, control)
    failed = [check["name"] for check in rule if not check["passed"]]
    failed += [key for key, row in labels.items() if row["passed"] is False]
    return {"decision": decision, "rule": rule, "labels": labels, "list_pages": _list_pages(items, finals),
            "answers": _answers(items, finals), "stream": stream(items, finals, (control or {}).get("strata")),
            "passed": not failed, "failed": failed}


def value(final: Mapping[str, Any] | None, label: str) -> Any:
    """Значение метки в ответе модели после проверки кодом; сбой — ни с чем не совпадает."""
    return _MISSING if final is None else final.get(_FIELDS[label])


def machine_decision(item: Mapping[str, Any]) -> str | None:
    return MACHINE.get(item["group"])


# --- Решение -------------------------------------------------------------------------------------


def _slice(title: str, items: list[dict[str, Any]], finals: Finals) -> dict[str, Any]:
    """Решение на строках среза: модель (по прогонам), контроль (решение машины, по строке) и константы."""
    expert = [item["labels"]["decision"] for item in items]
    agent = _classes([(item["labels"]["decision"], value(final, "decision"))
                      for item in items for final in finals[item["id"]]])
    controlled = [item for item in items if machine_decision(item)]
    control = (_classes([(item["labels"]["decision"], machine_decision(item)) for item in controlled])
               if controlled and len(controlled) == len(items) else None)
    return {"title": title, "rows": len(items), "accept_rows": expert.count(ACCEPT),
            "reject_rows": expert.count(REJECT), "agent": agent, "control": control,
            "constant": {"always_accept": _share(expert.count(ACCEPT), len(items)),
                         "always_reject": _share(expert.count(REJECT), len(items))}}


def _classes(pairs: list[tuple[str, Any]]) -> dict[str, float | None]:
    """Доля верных по каждому классу эксперта, сбалансированная (если классов два) и точность «брать»."""
    shares = {}
    for name in (ACCEPT, REJECT):
        given = [answer for expert, answer in pairs if expert == name]
        shares[name] = _share(sum(answer == name for answer in given), len(given))
    taken = [expert for expert, answer in pairs if answer == ACCEPT]
    known = [share for share in shares.values() if share is not None]
    return {**shares, "balanced": sum(known) / 2 if len(known) == 2 else None,
            "precision_accept": _share(taken.count(ACCEPT), len(taken))}


def _rule(decision: dict[str, Any]) -> list[dict[str, Any]]:
    """Проверки правила прохождения: класс без строк в срезе не сравнивается."""
    checks = []
    for name, kinds in RULES:
        part = decision[name]
        for kind in kinds:
            if not part[f"{kind}_rows"] or part["control"] is None:
                continue
            agent, bound = part["agent"][kind], part["control"][kind]
            checks.append({"name": f"{name}:{kind}", "slice": name, "class": kind, "agent": agent, "bound": bound,
                           "passed": agent is not None and agent >= bound})
    balanced = decision["all"]["agent"]["balanced"]
    checks.append({"name": "balanced", "slice": "all", "class": None, "agent": balanced, "bound": BALANCED_MIN,
                   "passed": balanced is not None and balanced >= BALANCED_MIN})
    return checks


# --- Остальные метки -----------------------------------------------------------------------------


def _labels(items: list[dict[str, Any]], finals: Finals, control: Mapping[str, Any] | None) -> dict[str, Any]:
    """Отрасль — на всех строках; связь — средняя по значениям эксперта доля верных; причина — где «не брать» и у
    эксперта, и у модели; категория — где «брать» у обоих. Константа — самое частое значение эксперта там же."""
    rejected = [item for item in items if item["labels"]["decision"] == REJECT]
    taken = [item for item in items if item["labels"]["decision"] == ACCEPT]
    labels = {
        "industry": _accuracy(items, finals, "industry"),
        "oilfield_relevance": _per_value(items, finals, "oilfield_relevance"),
        "reject_reason": _accuracy(rejected, finals, "reject_reason", when=REJECT),
        "category": {**_accuracy(taken, finals, "category", when=ACCEPT),
                     "control": _category_control(taken, (control or {}).get("rows") or {})},
    }
    for key, row in labels.items():
        row["threshold"] = THRESHOLDS[key]
        row["passed"] = None if row["agent"] is None else row["agent"] >= THRESHOLDS[key]
    return labels


def _accuracy(items: list[dict[str, Any]], finals: Finals, label: str, *, when: str | None = None) -> dict[str, Any]:
    """Доля верных по прогонам; when — только ответы с этим решением модели (причина у отказа, категория у
    взятого)."""
    pairs = [(item["labels"][label], value(final, label)) for item in items for final in finals[item["id"]]
             if when is None or value(final, "decision") == when]
    constant = _most_common([item["labels"][label] for item in items])
    return {"agent": _share(sum(expert == answer for expert, answer in pairs), len(pairs)), "n": len(pairs),
            "constant": _share(sum(item["labels"][label] == constant for item in items), len(items)),
            "constant_value": constant}


def _per_value(items: list[dict[str, Any]], finals: Finals, label: str) -> dict[str, Any]:
    """Средняя по значениям эксперта доля верных: редкое значение весит столько же, сколько частое."""
    groups: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        groups[item["labels"][label]].append(item)
    constant = _most_common([item["labels"][label] for item in items])
    by_value = {str(code): _accuracy(group, finals, label)["agent"] for code, group in groups.items()}
    return {"agent": _mean(list(by_value.values())), "n": sum(len(finals[item["id"]]) for item in items),
            "constant": _mean([1.0 if code == constant else 0.0 for code in groups]), "constant_value": constant,
            "by_value": by_value}


def _category_control(taken: list[dict[str, Any]], rows: Mapping[str, Any]) -> dict[str, Any] | None:
    """Категория нынешней цепочки у строк, которые взяли и эксперт, и машина: у статьи сбора — «бизнес» (ADR 0002,
    пункт 2), у находки радара — судьи (выгрузка с ядра)."""
    pairs = []
    for item in taken:
        if machine_decision(item) != ACCEPT:
            continue
        category = "business" if item["origin"] == "feed" else (rows.get(item["id"]) or {}).get("signal_category")
        if category:
            pairs.append((item["labels"]["category"], category))
    return {"agent": _share(sum(expert == given for expert, given in pairs), len(pairs)), "n": len(pairs)} if pairs \
        else None


# --- Страницы-списки, сбои, поток ----------------------------------------------------------------


def _list_pages(items: list[dict[str, Any]], finals: Finals) -> dict[str, Any]:
    """Страницы-списки радара: вне метрики решения (решение 29), считаются отдельно."""
    pages = [item for item in items if item["list_page"]]
    answers = [value(final, "decision") for item in pages for final in finals[item["id"]]]
    return {"rows": len(pages), "expert_accept": sum(item["labels"]["decision"] == ACCEPT for item in pages),
            "agent_accept": _share(answers.count(ACCEPT), len(answers)),
            "by_row": {item["id"]: [_shown(value(final, "decision")) for final in finals[item["id"]]]
                       for item in pages}}


def _answers(items: list[dict[str, Any]], finals: Finals) -> dict[str, Any]:
    """Сбои (ответа, дошедшего бы до сигнала, нет), спорные (шаги не сошлись и после повтора) и устойчивость
    решения: доля строк, где все прогоны дали одно решение без сбоев."""
    every = [final for item in items for final in finals[item["id"]]]
    stable = sum(all(final is not None for final in finals[item["id"]])
                 and len({value(final, "decision") for final in finals[item["id"]]}) == 1 for item in items)
    return {"answers": len(every), "failures": sum(final is None for final in every),
            "disputed": sum(bool(final and final.get("disputed")) for final in every),
            "stability": _share(stable, len(items))}


def stream(items: list[dict[str, Any]], finals: Finals, strata: Mapping[str, int] | None) -> dict[str, Any] | None:
    """Оценка на весь поток ленты: строки случайных статей части взвешены размером своей страты в окне пакета
    (выборка — 50 отклонённых и 30 принятых гейтом, а в потоке их доли другие). Нет размеров страт — None."""
    feed = [item for item in items if item["group"] in FEED_GROUPS and not item["list_page"]]
    counts = Counter(item["group"] for item in feed)
    if not strata or not all(counts[group] and int(strata.get(group) or 0) > 0 for group in FEED_GROUPS):
        return None
    weight = {group: int(strata[group]) / counts[group] for group in FEED_GROUPS}
    agent_pairs = [(item["labels"]["decision"], value(final, "decision"),
                    weight[item["group"]] / len(finals[item["id"]])) for item in feed for final in finals[item["id"]]]
    gate_pairs = [(item["labels"]["decision"], machine_decision(item), weight[item["group"]]) for item in feed]
    total = sum(weight[item["group"]] for item in feed)
    return {"strata": {group: int(strata[group]) for group in FEED_GROUPS},
            "agent": _weighted(agent_pairs), "gate": _weighted(gate_pairs),
            "expert_accept": sum(weight[item["group"]] for item in feed
                                 if item["labels"]["decision"] == ACCEPT) / total,
            "agent_accept": sum(share for _, answer, share in agent_pairs if answer == ACCEPT) / total,
            "gate_accept": int(strata["feed_accepted"]) / sum(int(strata[group]) for group in FEED_GROUPS)}


def _weighted(pairs: list[tuple[str, Any, float]]) -> dict[str, float | None]:
    shares = {}
    for name in (ACCEPT, REJECT):
        total = sum(share for expert, _, share in pairs if expert == name)
        shares[name] = sum(share for expert, answer, share in pairs if expert == name and answer == name) / total \
            if total else None
    known = [share for share in shares.values() if share is not None]
    return {**shares, "balanced": sum(known) / 2 if len(known) == 2 else None}


def _shown(answer: Any) -> str:
    return "сбой" if answer is _MISSING else str(answer)


def _most_common(values: list[Any]) -> Any:
    counted = Counter(one for one in values if one is not None)
    return counted.most_common(1)[0][0] if counted else None


def _mean(values: list[float | None]) -> float | None:
    known = [one for one in values if one is not None]
    return sum(known) / len(known) if known else None


def _share(hits: int | float, total: int | float) -> float | None:
    return hits / total if total else None
