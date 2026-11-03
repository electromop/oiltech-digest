"""Правка русских заголовков принятых статей открытых месяцев ленты (решение владельца 03.11).

Новые принятые публикации правит «Суть и перевод» (pipeline.title_ru_for_article с edit_russian). Написанные до правки
заголовки сами не переписываются, как суть и перевод после #150: этот прогон — по слову владельца. Он берёт статьи
открытых месяцев ленты (feed_window), принятые или отмеченные «в дайджест», у которых русский заголовок лежит копией
заголовка издателя и не отвечает правилам, которые видно кодом (pipeline.title_follows_rules). Закрытые месяцы не
трогает: выпуск закрытого месяца только на просмотр.

Задачи — process_articles с явным списком и пометкой only=["translation"] в полосе пересчётов: правит тот же шаг
воркера, что у новых статей, ядро пишет только заголовок. Сборка NL до правки оставит заголовок копией, поэтому прогон —
после пересборки NL. Правка человека (corrected_title блока ОС) лежит поверх title_ru, прогон её не трогает.
"""

from __future__ import annotations

import json
from typing import Any

from oiltech_digest import config, feed_window, network_policy
# Модулем, а не функцией: тестовая фикстура подменяет connection.get_connection.
from oiltech_digest.db import connection, repository
from oiltech_digest.processing.pipeline import _needs_translation, title_follows_rules

# Что пишет ядро по задаче прогона (external_ai.stages_to_write).
STAGES = ["translation"]
_PENDING = ("queued", "running", "finalizing")


def selection() -> dict[str, Any]:
    """Статьи открытых месяцев, принятые или отмеченные «в дайджест» и видимые в ленте, с русским заголовком копией не
    по правилам. Статья, которая уже стоит в задаче прогона, не берётся: её правка ещё не записана."""
    window = feed_window.current()
    with connection.get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT a.id, a.title, c.title_ru, s.name, m.status IS NOT DISTINCT FROM 'digest'
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            JOIN article_cards c ON c.article_id = a.id
            LEFT JOIN article_marks m ON m.article_id = a.id
            WHERE {window.sql('a')}
              AND ({feed_window.relevance_sql('a', 'c')} IS TRUE OR m.status = 'digest')
              AND {feed_window.ungated_visible_sql('a', 's')}
              AND NOT EXISTS (
                SELECT 1 FROM background_jobs j
                WHERE j.kind = 'process_articles' AND j.status = ANY(%s) AND j.payload_json->'only' = %s::jsonb
                  AND j.payload_json->'article_ids' @> to_jsonb(a.id))
            ORDER BY a.id
            """,
            (list(_PENDING), json.dumps(STAGES)),
        ).fetchall()
    picked = [(int(article_id), in_digest) for article_id, title, title_ru, source, in_digest in rows
              if _copy_off_the_rules((title or "").strip(), title_ru, source)]
    return {"months": window.open_months, "article_ids": [one for one, _ in picked],
            "digest_ids": {one for one, in_digest in picked if in_digest}}


def _copy_off_the_rules(title: str, title_ru: str | None, source: str | None) -> bool:
    """Русский заголовок, который лежит копией (как его клал перевод до правки) и правилам кодом не отвечает."""
    return (bool(title) and not _needs_translation(title) and title_ru == title[:200]
            and not title_follows_rules(title, source))


def cost_estimate(count: int, *, days: int = 30) -> dict[str, Any]:
    """Стоимость `count` правок: N × (in·p_in + out·p_out) / 10⁶ по средним токенам стадии translation за `days` дней у
    модели, что переводила чаще всех (модель — из .env воркера NL, на ядре её иначе не узнать). Вход и ответ у правки
    той же формы, что у перевода. Сверять — со счётом провайдера."""
    with connection.get_connection() as conn:
        row = conn.execute(
            """
            SELECT model, count(*), avg(input_tokens), avg(output_tokens)
            FROM ai_processing_runs
            WHERE stage = 'translation' AND status = 'ok' AND provider = 'openai' AND model IS NOT NULL
              AND created_at > now() - make_interval(days => %s)
            GROUP BY model
            ORDER BY count(*) DESC, model
            LIMIT 1
            """,
            (days,),
        ).fetchone()
    if row is None:
        return {"count": count, "days": days, "model": None, "runs": 0, "usd_per_call": None, "usd_total": None}
    model, runs, avg_in, avg_out = row
    price_in, price_out = config.price_for_model(model)
    per_call = (float(avg_in) * price_in + float(avg_out) * price_out) / 1_000_000
    return {"count": count, "days": days, "model": model, "runs": int(runs),
            "avg_input_tokens": round(float(avg_in)), "avg_output_tokens": round(float(avg_out)),
            "price_in": price_in, "price_out": price_out, "usd_per_call": per_call, "usd_total": per_call * count}


def enqueue(article_ids: list[int], *, batch_size: int = 20) -> list[int]:
    """Задачи внешнего контура: process_articles с явным списком и пометкой only=["translation"], полоса пересчётов."""
    decision = network_policy.route_ai_bulk()
    if decision.execution_region != "external":
        # Локальный конвейер пометки only не знает, а у статьи с заголовком перевод пропускает: задача прошла бы молча.
        raise RuntimeError("правка заголовков идёт только через внешний контур ИИ")
    batch = max(1, batch_size)
    jobs = []
    for start in range(0, len(article_ids), batch):
        chunk = article_ids[start:start + batch]
        job = repository.create_background_job(
            "process_articles", {"article_ids": chunk, "limit": len(chunk), "offline": False, "only": STAGES},
            queue_name=decision.queue_name, execution_region=decision.execution_region,
            capability=decision.capability,
        )
        jobs.append(int(job["id"]))
    return jobs
