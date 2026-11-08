"""Пересчёты статей ленты через внешний контур: полоса, цена стадии, задачи с пометкой only.

До 08.11 здесь жил и пересчёт балла статей с ИИ (enqueue-rescore, сессия G, ADR 0002); балл статьи снят вместе с
тегом (тему и баллы ставит «Карточка» у сигнала, #132). Осталось общее, что берёт правка русских заголовков
(title_edit, enqueue-title-edit): цена стадии, полоса и задачи (stage_cost_estimate, rescore_lane, enqueue_stage).
Только окно ленты (feed_window: текущий месяц, до дня смены окна по МСК ещё и прошлый): архив только на просмотр.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from psycopg import errors as pg_errors

from oiltech_digest import config, lanes, network_policy
# Модулем, а не функцией: тестовая фикстура подменяет connection.get_connection.
from oiltech_digest.db import connection, repository


def rescore_lane(*, now: datetime | None = None) -> dict[str, Any]:
    """Куда встанут задачи пересчёта и есть ли у полосы живой воркер — для сухого прогона.

    Без полосы пересчётов (AI_BULK_LANE_ENABLED) задачи ушли бы в поток дня external-ai и
    стояли бы перед обработкой новых статей: 18.09 28 пачек задержали поток на 4,5 ч."""
    decision = network_policy.route_ai_bulk()
    lane: dict[str, Any] = {"queue": decision.queue_name, "region": decision.execution_region,
                            "bulk": decision.queue_name == lanes.AI_BULK, "workers": [], "warnings": []}
    if decision.execution_region != "external":
        lane["warnings"].append("внешний контур ИИ выключен — с --no-dry-run команда откажет")
        return lane
    if not lane["bulk"]:
        lane["warnings"].append(
            f"полоса пересчётов выключена (AI_BULK_LANE_ENABLED) — задачи встали бы в поток дня "
            f"{decision.queue_name} перед новыми статьями; --no-dry-run откажет без --allow-live-lane"
        )
    now = now or datetime.now(timezone.utc)
    try:
        consumers = repository.list_external_consumers()
    except pg_errors.UndefinedTable:
        consumers = []
    for consumer in consumers:
        if decision.queue_name in (consumer.get("queues") or []) and consumer.get("last_seen_at"):
            minutes = (now - consumer["last_seen_at"]).total_seconds() / 60
            lane["workers"].append((consumer["consumer"], round(minutes)))
    if not any(minutes <= lanes.CONSUMER_ACTIVE_HOURS * 60 for _, minutes in lane["workers"]):
        lane["warnings"].append(
            f"у полосы {decision.queue_name} нет живого воркера (задачу не просили {lanes.CONSUMER_ACTIVE_HOURS} ч) "
            "— задачи будут ждать"
        )
    return lane


def stage_cost_estimate(stage: str, count: int, *, days: int = 30) -> dict[str, Any]:
    """Стоимость `count` вызовов стадии (translation: перевод и правка заголовка):
    N × (in·p_in + out·p_out) / 10⁶ (ADR 0002).

    in и out — средние токены стадии за `days` дней у модели, что вызывалась на ней чаще всех:
    это модель из .env воркера NL, на ядре её иначе не узнать. Рассуждение модели входит в
    output_tokens. Ставки — config.price_for_model. Сверять — со счётом провайдера."""
    with connection.get_connection() as conn:
        row = conn.execute(
            """
            SELECT model, count(*), avg(input_tokens), avg(output_tokens)
            FROM ai_processing_runs
            WHERE stage = %s AND status = 'ok' AND provider = 'openai' AND model IS NOT NULL
              AND created_at > now() - make_interval(days => %s)
            GROUP BY model
            ORDER BY count(*) DESC, model
            LIMIT 1
            """,
            (stage, days),
        ).fetchone()
    if row is None:
        return {"count": count, "days": days, "model": None, "runs": 0, "usd_per_call": None, "usd_total": None}
    model, runs, avg_in, avg_out = row
    price_in, price_out = config.price_for_model(model)
    per_call = (float(avg_in) * price_in + float(avg_out) * price_out) / 1_000_000
    return {
        "count": count, "days": days, "model": model, "runs": int(runs),
        "avg_input_tokens": round(float(avg_in)), "avg_output_tokens": round(float(avg_out)),
        "price_in": price_in, "price_out": price_out,
        "usd_per_call": per_call, "usd_total": per_call * count,
    }


def enqueue_stage(article_ids: list[int], stages: list[str], *, what: str, batch_size: int = 20,
                  allow_live_lane: bool = False) -> list[int]:
    """Задачи внешнего контура: process_articles с явным списком и пометкой only=stages (правка заголовков:
    title_edit.STAGES); `what`: что ставится, для текста отказа.

    Только в полосу пересчётов: в потоке дня сотня пачек встала бы перед новыми статьями.
    Сознательно в поток дня — allow_live_lane."""
    decision = network_policy.route_ai_bulk()
    if decision.execution_region != "external":
        # Локальный конвейер пометки only не знает, а готовую стадию у статьи пропускает:
        # задача прошла бы молча впустую.
        raise RuntimeError(f"{what} идёт только через внешний контур ИИ")
    if decision.queue_name != lanes.AI_BULK and not allow_live_lane:
        raise RuntimeError(
            f"полоса пересчётов выключена (AI_BULK_LANE_ENABLED): задачи встали бы в поток дня "
            f"{decision.queue_name} перед новыми статьями. Включите полосу (и воркер external-worker-bulk "
            "на NL) или поставьте сознательно: --allow-live-lane"
        )
    batch = max(1, batch_size)
    jobs = []
    for start in range(0, len(article_ids), batch):
        chunk = article_ids[start : start + batch]
        job = repository.create_background_job(
            "process_articles",
            {"article_ids": chunk, "limit": len(chunk), "offline": False, "only": list(stages)},
            queue_name=decision.queue_name,
            execution_region=decision.execution_region,
            capability=decision.capability,
        )
        jobs.append(int(job["id"]))
    return jobs
