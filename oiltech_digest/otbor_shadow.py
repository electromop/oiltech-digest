"""Проверка «Отбора» на реальных данных (#134, спека #128): прежде чем витрины переключатся на «Отбор», он решает
публикации последних недель, а решения пишутся рядом (db/otbor_shadow_store.py). Владелец смотрит отчёт, затем
переключение переносит решения в витрины.

Задача та же, что у «Отбора» (вид otbor), с пометкой shadow в задаче: сборке NL не нужно ничего нового, а ядро при
записи итога кладёт решение в таблицу проверки (otbor.apply_result). Флаг «Отбора» проверке не нужен, модель —
явная (OTBOR_MODEL или названная), без отката на модель ленты. Задачи идут по полосе пересчётов, не перед потоком
дня. Сухой прогон по умолчанию: число публикаций и цена по длине запросов, ничего не пишет.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from functools import partial
from typing import Any

from oiltech_digest import config, feed_window, lanes, network_policy, otbor
from oiltech_digest.db import otbor_inputs, otbor_shadow_store, repository
from oiltech_digest.otbor_answer import ACCEPT, REJECT, code_label
from oiltech_digest.otbor_pack_format import load_dictionaries
from oiltech_digest.signal_card import estimate

# Статей на одно чтение текстов для оценки цены: тексты окна разом в память не нужны.
_CHUNK = 400


def plan(since: date, *, model: str | None = None) -> dict[str, Any]:
    """Что поставит проверка: статьи окна (otbor_shadow_store.candidate_article_ids), цена по длине запросов и
    полоса. Модель — названная или OTBOR_MODEL; пусто — отказ."""
    model = model or config.OTBOR_MODEL
    if not model:
        raise otbor.OtborSetupError("модель не задана (OTBOR_MODEL или --model): модель ленты «Отбор» не берёт")
    ids = otbor_shadow_store.candidate_article_ids(since=since, window=feed_window.current())
    dictionaries = load_dictionaries()
    instructions = len(otbor._instructions(dictionaries))
    chars = 0
    for start in range(0, len(ids), _CHUNK):
        rows = otbor_inputs.article_inputs(ids[start:start + _CHUNK])
        chars += sum(instructions + len(otbor.model_input(otbor._item(row))) for row in rows)
    decision = network_policy.route_ai_bulk()
    return {
        "since": since.isoformat(), "model": model, "prompt_version": otbor.PROMPT_VERSION,
        "dictionaries_version": dictionaries.get("version"), "article_ids": ids,
        "estimate": estimate(chars, len(ids), model, output_tokens=otbor.ESTIMATED_OUTPUT_TOKENS),
        "queue": decision.queue_name, "batch": config.OTBOR_BATCH,
        # Во внешнем контуре только полоса пересчётов: в потоке дня сотни пачек встали бы перед новыми статьями.
        "bulk_lane": decision.queue_name == lanes.AI_BULK or decision.execution_region != "external",
    }


def enqueue(*, article_ids: list[int] | tuple[int, ...] = (), signal_ids: list[int] | tuple[int, ...] = (),
            model: str, trigger: str) -> list[dict[str, Any]]:
    """Задачи проверки пачками по OTBOR_BATCH по полосе пересчётов: статьи и сигналы (находки, материалы) своими
    задачами. Флаг «Отбора» не нужен: витрины проверка не меняет. trigger — пометка запуска (у проверки #134
    TRIGGER, у пробного прогона поиска своя): она в рецепте каждой строки."""
    if not model:
        raise otbor.OtborSetupError("модель не задана: модель ленты «Отбор» не берёт")
    if not trigger:
        raise ValueError("нужна пометка запуска (trigger)")
    route = network_policy.route_ai_bulk()
    batch = config.OTBOR_BATCH
    return [
        repository.create_background_job(
            otbor.KIND,
            {key: ids[start:start + batch], "model": model, "reasoning": config.OTBOR_REASONING, "trigger": trigger,
             "shadow": True},
            queue_name=route.queue_name, execution_region=route.execution_region, capability=route.capability,
        )
        for key, ids in (("article_ids", list(article_ids)), ("signal_ids", list(signal_ids)))
        for start in range(0, len(ids), batch)
    ]


def enqueue_plan(planned: dict[str, Any]) -> list[dict[str, Any]]:
    """Задачи проверки по плану; без полосы пересчётов во внешнем контуре — отказ."""
    if not planned["bulk_lane"]:
        raise otbor.OtborSetupError(
            f"полоса пересчётов выключена (AI_BULK_LANE_ENABLED): задачи проверки встали бы в поток дня "
            f"{planned['queue']} перед новыми статьями")
    return enqueue(article_ids=planned["article_ids"], model=planned["model"], trigger=otbor_shadow_store.TRIGGER)


def report(since: date, *, trigger: str = otbor_shadow_store.TRIGGER, examples: int = 15) -> dict[str, Any]:
    """Отчёт владельцу по статьям с `since` (спека #128): размеры разделов отказа (причины справочника, отказ без
    причины), принятое по категориям, сколько технологий из сбора «Отбор» отправит на радар против сигналов радара за
    то же время, гейт против «Отбора» и примеры в клетках расхождения, повод публикации, сбои и версии инструкции."""
    label = partial(code_label, load_dictionaries())
    rows = otbor_shadow_store.article_rows(since=since, trigger=trigger)
    decided = [row for row in rows if row["decision"]]
    accepted = [row for row in decided if row["decision"] == ACCEPT]
    rejected = [row for row in decided if row["decision"] == REJECT]
    gate: dict[str, dict[str, int]] = {}
    for row in decided:
        side = gate.setdefault("accept" if row["gate_relevant"] else "reject", {"accept": 0, "reject": 0})
        side[row["decision"]] += 1
    reasons = Counter(row["values_json"].get("reject_reason") for row in rejected)
    occasions: dict[str, dict[str, Any]] = {}
    for row in decided:
        code = row["values_json"].get("occasion")
        entry = occasions.setdefault(code, {"code": code, "label": label("occasion", code), "accept": 0, "reject": 0})
        entry[row["decision"]] += 1

    def example(row: dict[str, Any]) -> dict[str, Any]:
        values = row["values_json"]
        return {"article_id": row["article_id"], "title": row["title"], "source": row["source"],
                "reason": label("reject_reason", values.get("reject_reason")) or None,
                "category": label("category", values.get("signal_category")) or None,
                "occasion": label("occasion", values.get("occasion")), "justification": values.get("verdict_reason"),
                "disputed": row["disputed"]}

    return {
        "since": since.isoformat(), "trigger": trigger, "publications": len(rows), "decided": len(decided),
        "failed": dict(Counter(row["failure"] for row in rows if not row["decision"])),
        "versions": dict(Counter(f"{row['recipe'].get('prompt_version')} / {row['recipe'].get('dictionaries_version')}"
                                 for row in decided)),
        "gate": gate,
        "categories": dict(Counter(row["values_json"].get("signal_category") for row in accepted)),
        "reasons": [{"code": code, "label": label("reject_reason", code) if code else "Без причины (спорное)",
                     "count": count} for code, count in reasons.most_common()],
        "disputed": sum(1 for row in decided if row["disputed"]),
        "occasions": sorted(occasions.values(), key=lambda entry: -(entry["accept"] + entry["reject"])),
        "technology_from_feed": sum(1 for row in accepted if row["values_json"].get("signal_category") == "technology"),
        "radar_now": otbor_shadow_store.radar_since(since),
        "examples": {
            "gate_accepted_otbor_rejected": [example(row) for row in rejected if row["gate_relevant"]][:examples],
            "gate_rejected_otbor_accepted": [example(row) for row in accepted if not row["gate_relevant"]][:examples],
        },
    }


class SwitchError(ValueError):
    """Переключение нельзя провести сейчас: флаг выключен, агент не настроен или окно проверено не целиком."""


def switch(since: date, *, triggers: tuple[str, ...] = (otbor_shadow_store.TRIGGER,),
           apply: bool = False) -> dict[str, Any]:
    """Переключение витрин (#134): решения проверки статей окна (и сигналов, если их пометку назвали) — в сигналы тем
    же путём, что пишет сам «Отбор» (otbor_store.write_decision), со шагом «перенесено из проверки» в «Ходе решения».
    По умолчанию сухой прогон: что перенесётся и куда, что пропущено, что дальше пройдёт конвейер и почём.

    Только при включённом флаге и настроенных «Отборе» и обоих вызовах «Карточки»: с флагом новое решает «Отбор», а
    без него перенос оставил бы ленту наполовину на гейте. Статья окна, решённая гейтом, без строки проверки (пришла
    после проверки) — отказ: сначала дозапуск проверки. Принятой с карточкой прогона по истории (сделана до решения:
    без темы, с категорией ленты) карточка делается заново полосой пересчётов; баллы ставит шаг дня, когда новая
    карточка готова. Принятой, которую гейт отклонял, суть и перевод даст обработка ленты, затем карточку и баллы —
    шаги дня, как новой."""
    from oiltech_digest import signal_card, signal_scores
    from oiltech_digest.db import otbor_store

    found = otbor_shadow_store.switch_rows(since=since, window=feed_window.current(), triggers=triggers)
    rows = [*found["eligible"], *found["signals"]]
    accepted = [row for row in rows if row["decision"] == ACCEPT]
    categories = Counter(row["values_json"].get("signal_category") for row in accepted)
    eligible_ids = {int(row["id"]) for row in found["eligible"]}
    window_rows = found["window"]
    accepted_articles = [int(row["article_id"]) for row in accepted if row["article_id"] is not None]
    cards = otbor_shadow_store.card_signal_ids(accepted_articles)
    scored = sum(1 for row in accepted if row["values_json"].get("signal_category") in ("business", "technology"))
    card_cost, scores_cost = (otbor_shadow_store.average_cost(stage) for stage in ("signal_card", "signal_scores"))
    planned = {
        "since": since.isoformat(), "triggers": list(triggers), "transfer": len(rows),
        "business": categories.get("business", 0), "technology": categories.get("technology", 0),
        "other": categories.get("other", 0), "rejected": len(rows) - len(accepted),
        "failed": sum(1 for row in window_rows if not row["decision"]),
        "not_eligible": sum(1 for row in window_rows if row["decision"] and int(row["id"]) not in eligible_ids),
        "missing": found["missing"], "cards": len(cards), "scores": scored,
        # На какой инструкции решения: правку инструкции после проверки (замечания эксперта) видно до переноса.
        "versions": dict(Counter(f"{row['recipe'].get('prompt_version')} / {row['recipe'].get('dictionaries_version')}"
                                 for row in rows)),
        "feed_stages": sum(1 for row in accepted if row["article_id"] is not None and not row["summarized"]),
        "card_cost_usd": None if card_cost is None else round(card_cost * len(cards), 2),
        "scores_cost_usd": None if scores_cost is None else round(scores_cost * scored, 2),
        "applied": False,
    }
    if not apply:
        return planned
    if not config.OTBOR_ENABLED:
        raise SwitchError("OTBOR_ENABLED выключен: сначала флаг (новое решает «Отбор»), потом перенос")
    if not config.SIGNAL_CARD_ENABLED:
        raise SwitchError("SIGNAL_CARD_ENABLED выключен: принятым нужна карточка")
    try:
        otbor.configured_model()
        signal_card.configured_model()
        signal_scores.configured_model()
    except (otbor.OtborSetupError, signal_card.CardSetupError, signal_scores.ScoresSetupError) as exc:
        raise SwitchError(str(exc)) from exc
    if found["missing"]:
        raise SwitchError(f"у {len(found['missing'])} статей окна, решённых гейтом, нет решения проверки: сначала "
                          f"otbor-shadow --since {since.isoformat()} --apply и дождаться задач")
    otbor_shadow_store.forget_cards(accepted_articles, log=[{
        "stage": "«Карточка»", "text": "Карточка будет сделана заново: сделана до решения «Отбора»"}])
    switched = 0
    for row in rows:
        decided = str(row["recipe"].get("decided_at") or "")[:10]
        log = [*row["decision_log"], {"stage": "«Отбор»",
                                      "text": f"Перенесено из проверки на реальных данных (решение {decided})"}]
        recipe = {**row["recipe"], "switched": True}
        write = (otbor_store.write_decision if row["article_id"] is not None else otbor_store.write_signal_decision)
        written = write(int(row["article_id"] or row["signal_id"]), row["values_json"], disputed=row["disputed"],
                        log=log, recipe=recipe)
        if written:
            otbor_shadow_store.mark_switched(int(row["id"]))
            switched += 1
    redo = otbor_shadow_store.cards_to_redo(since=since, triggers=triggers)
    jobs = signal_card.enqueue_cards(redo, trigger="switch", bulk=True) if redo else []
    return {**planned, "applied": True, "switched": switched, "card_signals": len(redo), "card_jobs": len(jobs)}
