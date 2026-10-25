"""Прогон по истории агента «Карточка сигнала» (#98, спека #93): карточки сигналам ленты с августа 2026 (по
дате сбора статьи), всем находкам радара и материалам коллег (дальше «Отбора» — только принятое им) — фоном,
по полосе пересчётов, чтобы не задерживать поток дня.

Задачи выдаются по порядку создания, поэтому первыми ставятся текущее окно ленты, радар и материалы, затем
остальное.
«Заполнено» — карточка с рецептом текущих инструкции, справочников и модели: повторный запуск за неё не платит,
а после смены справочников, инструкции или модели перезаполняет. Дубли, сигналы в идущих задачах агента и
сигналы без главной публикации (агенту нечего читать) — мимо. Сухой прогон печатает число сигналов и цену — по
длине запросов, как сухой прогон экзамена.
"""

from __future__ import annotations

import re
from typing import Any

from oiltech_digest import config, feed_window, lanes, network_policy, signal_card
from oiltech_digest.db import signal_card_store
from oiltech_digest.db.feed_signals import FEED_SIGNALS_SINCE
from oiltech_digest.signal_card_pack import load_dictionaries

# Сигналов на одно чтение текстов для оценки цены: тексты всей истории разом в память не нужны.
_CHUNK = 200


def plan() -> dict[str, Any]:
    """Что поставит прогон по истории: сигналы по порядку постановки, что пропущено и почему, цена и полоса."""
    model = signal_card.configured_model()
    version = load_dictionaries().get("version")
    window = feed_window.current()
    rows = signal_card_store.history_signals(since=FEED_SIGNALS_SINCE, window=window)
    live = [row for row in rows if not row["pending"]]
    wanted = [row for row in live if not _filled(row["card_recipe"], model, version)]
    chars = _request_chars([int(row["id"]) for row in wanted])
    queued = [row for row in wanted if int(row["id"]) in chars]
    decision = network_policy.route_ai_bulk()
    return {
        "model": model, "prompt_version": signal_card.PROMPT_VERSION, "dictionaries_version": version,
        "since": FEED_SIGNALS_SINCE.isoformat(), "months": window.open_months,
        "signals": len(rows), "pending": len(rows) - len(live), "filled": len(live) - len(wanted),
        "no_publication": len(wanted) - len(queued),
        "first": [int(row["id"]) for row in queued if row["first"]],
        "rest": [int(row["id"]) for row in queued if not row["first"]],
        "estimate": signal_card.estimate(sum(chars.values()), len(queued), model),
        "queue": decision.queue_name, "batch": config.SIGNAL_CARD_BATCH,
        # Во внешнем контуре — только полоса пересчётов: в потоке дня сотни пачек истории встали бы перед новыми
        # статьями на часы. Местный стек (без внешних воркеров) — одна очередь ai.
        "bulk_lane": decision.queue_name == lanes.AI_BULK or decision.execution_region != "external",
    }


def enqueue(planned: dict[str, Any]) -> list[dict[str, Any]]:
    """Задачи агента по плану — по полосе пересчётов; без неё — отказ."""
    if not planned["bulk_lane"]:
        raise signal_card.CardSetupError(
            f"полоса пересчётов выключена (AI_BULK_LANE_ENABLED): задачи истории встали бы в поток дня "
            f"{planned['queue']} перед новыми статьями. Включите полосу и воркер external-worker-bulk на NL")
    return signal_card.enqueue_cards(planned["first"] + planned["rest"], trigger="history", bulk=True)


def _filled(recipe: dict[str, Any] | None, model: str, version: str | None) -> bool:
    """Карточка заполнена текущим рецептом. Модель в рецепте — из ответа модели: снимок с датой
    (gpt-5.4-nano-2026-03-17) — та же заданная модель, а не другая."""
    if not recipe:
        return False
    return (recipe.get("prompt_version") == signal_card.PROMPT_VERSION
            and recipe.get("dictionaries_version") == version
            and re.fullmatch(re.escape(model) + r"(-\d{4}-\d{2}-\d{2})?", str(recipe.get("model") or "")) is not None)


def _request_chars(signal_ids: list[int]) -> dict[int, int]:
    """Длина запроса модели по сигналам — по пакету, который ядро соберёт при выдаче задачи (build_payload):
    сигнала без главной публикации в пакете нет."""
    chars: dict[int, int] = {}
    for start in range(0, len(signal_ids), _CHUNK):
        payload = signal_card.build_payload({"signal_ids": signal_ids[start:start + _CHUNK]})
        for item in payload["signals"]:
            chars[int(item["signal_id"])] = signal_card.request_chars(item, payload["dictionaries"])
    return chars
