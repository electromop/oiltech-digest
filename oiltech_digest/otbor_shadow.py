"""Проверка «Отбора» на реальных данных (#134, спека #128): прежде чем витрины переключатся на «Отбор», он решает
статьи ленты последних недель, а решения пишутся рядом (db/otbor_shadow_store.py). Владелец смотрит отчёт, затем
переключение переносит решения в витрины.

Задача та же, что у «Отбора» (вид otbor), с пометкой shadow в задаче: сборке NL не нужно ничего нового, а ядро при
записи итога кладёт решение в таблицу проверки (otbor.apply_result). Флаг «Отбора» проверке не нужен, модель —
явная (OTBOR_MODEL или названная), без отката на модель ленты. Задачи идут по полосе пересчётов, не перед потоком
дня. Сухой прогон по умолчанию: число статей и цена по длине запросов, ничего не пишет.
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

# Статей на одно чтение текстов для оценки цены: тексты окна разом в память не нужны.
_CHUNK = 400


class SwitchError(ValueError):
    """Переключение нельзя провести сейчас: флаг выключен, агенты не настроены или окно проверено не целиком."""


def plan(since: date, *, model: str | None = None) -> dict[str, Any]:
    """Что поставит проверка: статьи окна (otbor_shadow_store.candidate_article_ids), цена по длине запросов и
    полоса. Модель — названная или OTBOR_MODEL; пусто — отказ."""
    model = model or config.OTBOR_MODEL
    if not model:
        raise otbor.OtborSetupError("модель не задана (OTBOR_MODEL или --model): модель ленты «Отбор» не берёт")
    ids = otbor_shadow_store.candidate_article_ids(since=since, window=feed_window.current())
    chars = sum(otbor.request_chars(otbor_inputs.article_inputs(ids[start:start + _CHUNK]))
                for start in range(0, len(ids), _CHUNK))
    decision = network_policy.route_ai_bulk()
    return {
        "since": since.isoformat(), "model": model, "prompt_version": otbor.PROMPT_VERSION,
        "dictionaries_version": load_dictionaries().get("version"), "article_ids": ids,
        "estimate": otbor.estimate(chars, len(ids), model),
        "queue": decision.queue_name, "batch": config.OTBOR_BATCH,
        # Во внешнем контуре только полоса пересчётов: в потоке дня сотни пачек встали бы перед новыми статьями.
        "bulk_lane": decision.queue_name == lanes.AI_BULK or decision.execution_region != "external",
    }


def enqueue(planned: dict[str, Any]) -> list[dict[str, Any]]:
    """Задачи проверки по плану пачками по OTBOR_BATCH, по полосе пересчётов; без неё во внешнем контуре — отказ.
    Флаг «Отбора» не нужен: витрины проверка не меняет."""
    if not planned["bulk_lane"]:
        raise otbor.OtborSetupError(
            f"полоса пересчётов выключена (AI_BULK_LANE_ENABLED): задачи проверки встали бы в поток дня "
            f"{planned['queue']} перед новыми статьями")
    ids, batch = planned["article_ids"], planned["batch"]
    route = network_policy.route_ai_bulk()
    return [
        repository.create_background_job(
            otbor.KIND,
            {"article_ids": ids[start:start + batch], "model": planned["model"], "reasoning": config.OTBOR_REASONING,
             "trigger": otbor_shadow_store.TRIGGER, "shadow": True},
            queue_name=route.queue_name, execution_region=route.execution_region, capability=route.capability,
        )
        for start in range(0, len(ids), batch)
    ]


def report(since: date, *, examples: int = 15) -> dict[str, Any]:
    """Отчёт владельцу по статьям с `since` (спека #128): размеры разделов отказа (причины справочника, отказ без
    причины), принятое по категориям, сколько технологий из сбора «Отбор» отправит на радар против того, сколько радар
    находил за RADAR_BASELINE_WEEKS недель до окна, гейт против «Отбора» и примеры в клетках расхождения, повод
    публикации, сбои и версии инструкции."""
    label = partial(code_label, load_dictionaries())
    rows = otbor_shadow_store.article_rows(since=since)
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

    weeks = max(1.0, (date.today() - since).days / 7)
    radar = otbor_shadow_store.radar_counts(since)
    technology = sum(1 for row in accepted if row["values_json"].get("signal_category") == "technology")
    return {
        "since": since.isoformat(), "publications": len(rows), "decided": len(decided),
        "failed": dict(Counter(row["failure"] for row in rows if not row["decision"])),
        "versions": _versions(decided),
        "gate": gate,
        "categories": dict(Counter(row["values_json"].get("signal_category") for row in accepted)),
        "reasons": [{"code": code, "label": label("reject_reason", code) if code else "Без причины (спорное)",
                     "count": count} for code, count in reasons.most_common()],
        "disputed": sum(1 for row in decided if row["disputed"]),
        "occasions": sorted(occasions.values(), key=lambda entry: -(entry["accept"] + entry["reject"])),
        "technology_from_feed": technology, "technology_per_week": round(technology / weeks, 1),
        "radar_now": radar["window"],
        "radar_before_per_week": round(radar["before"] / otbor_shadow_store.RADAR_BASELINE_WEEKS, 1),
        "examples": {
            "gate_accepted_otbor_rejected": [example(row) for row in rejected if row["gate_relevant"]][:examples],
            "gate_rejected_otbor_accepted": [example(row) for row in accepted if not row["gate_relevant"]][:examples],
        },
    }


def _versions(rows: list[dict[str, Any]]) -> dict[str, int]:
    """На какой инструкции и справочниках решения: правку инструкции после проверки видно до переноса."""
    return dict(Counter(f"{row['recipe'].get('prompt_version')} / {row['recipe'].get('dictionaries_version')}"
                        for row in rows))


def _price(stage: str, model: str, calls: int) -> float | None:
    """Цена вызовов стадии по ставкам заданной модели и средним токенам вызова на этой базе; расхода нет — None."""
    tokens = otbor_shadow_store.average_tokens(stage)
    if tokens is None or not model:
        return None
    price_in, price_out = config.price_for_model(model)
    return round(calls * (tokens[0] * price_in + tokens[1] * price_out) / 1_000_000, 2)


def switch(since: date, *, apply: bool = False) -> dict[str, Any]:
    """Переключение витрин (#134): решения проверки статей окна — в сигналы тем же путём, что пишет сам «Отбор»
    (otbor_shadow_store.transfer: статья перепроверяется и пишется одной транзакцией), со шагом «перенесено из
    проверки» в «Ходе решения». По умолчанию сухой прогон: что перенесётся и куда (с номерами статей), что пропущено,
    что дальше пройдёт конвейер и почём.

    Только при включённом флаге и настроенных «Отборе» и обоих вызовах «Карточки»: с флагом новое решает «Отбор», а
    без него перенос оставил бы ленту наполовину на гейте. Отказ, пока идут задачи проверки или у статьи окна, решённой
    гейтом, нет строки проверки (пришла после неё): сначала дозапуск проверки. Принятой с карточкой прогона по
    истории (сделана до решения: без темы, с категорией ленты) карточка делается заново полосой пересчётов, пачками по
    ходу переноса; баллы ставит шаг дня, когда новая карточка готова. Принятой, которую гейт отклонял, суть и перевод
    даст обработка ленты, затем карточку и баллы — шаги дня, как новой."""
    from oiltech_digest import signal_card, signal_scores

    window = feed_window.current()
    found = otbor_shadow_store.switch_rows(since=since, window=window)
    rows = found["eligible"]
    accepted = [row for row in rows if row["decision"] == ACCEPT]
    eligible_ids = {int(row["id"]) for row in rows}
    ids: dict[str, list[int]] = {"business": [], "technology": [], "other": [], "rejected": []}
    for row in rows:
        key = row["values_json"].get("signal_category") if row["decision"] == ACCEPT else "rejected"
        ids.setdefault(key or "other", []).append(int(row["article_id"]))
    cards = otbor_shadow_store.card_signal_ids([int(row["article_id"]) for row in accepted])
    scored = len(ids["business"]) + len(ids["technology"])
    planned = {
        "since": since.isoformat(), "transfer": len(rows), "ids": ids,
        "failed": [int(row["article_id"]) for row in found["window"] if not row["decision"]],
        "not_eligible": [int(row["article_id"]) for row in found["window"]
                         if row["decision"] and int(row["id"]) not in eligible_ids],
        "missing": found["missing"], "pending": otbor_shadow_store.pending_articles(),
        "versions": _versions(rows), "cards": len(cards), "scores": scored,
        "feed_stages": sum(1 for row in accepted if not row["summarized"]),
        "card_cost_usd": _price("signal_card", config.SIGNAL_CARD_MODEL, len(cards)),
        "scores_cost_usd": _price("signal_scores", config.SIGNAL_SCORES_MODEL, scored),
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
    if planned["pending"]:
        raise SwitchError(f"идут задачи проверки ({planned['pending']} статей): дождаться их, потом перенос")
    if found["missing"]:
        raise SwitchError(f"у {len(found['missing'])} статей окна, решённых гейтом, нет решения проверки: сначала "
                          f"otbor-shadow --since {since.isoformat()} --apply и дождаться задач")
    switched: list[int] = []
    skipped: list[int] = []
    jobs: list[dict[str, Any]] = []
    for number, row in enumerate(rows, start=1):
        decided = str(row["recipe"].get("decided_at") or "")[:10]
        log = [*row["decision_log"], {"stage": "«Отбор»",
                                      "text": f"Перенесено из проверки на реальных данных (решение {decided})"}]
        written = otbor_shadow_store.transfer(row, window=window, log=log, recipe={**row["recipe"], "switched": True})
        (switched if written else skipped).append(int(row["article_id"]))
        if number % config.SIGNAL_CARD_BATCH == 0:
            # Карточку заново — по ходу переноса: шаг дня «Карточки» увидит её в задаче и в свою полосу не возьмёт.
            jobs += _cards_again(since)
    jobs += _cards_again(since)
    return {**planned, "applied": True, "switched": switched, "skipped": skipped, "card_jobs": len(jobs)}


def _cards_again(since: date) -> list[dict[str, Any]]:
    """Задачи «Карточки» по полосе пересчётов перенесённым принятым без карточки (otbor_shadow_store.cards_to_redo)."""
    from oiltech_digest import signal_card

    redo = otbor_shadow_store.cards_to_redo(since=since)
    return signal_card.enqueue_cards(redo, trigger="switch", bulk=True) if redo else []
