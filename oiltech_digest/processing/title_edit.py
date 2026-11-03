"""Правка русских заголовков принятых статей последних двух недель (решение владельца 03.11).

Новые принятые публикации правит «Суть и перевод» (pipeline.title_ru_for_article с edit_russian). Написанные до правки
заголовки сами не переписываются, как суть и перевод после #150: этот прогон — по слову владельца. Он берёт видимые в
ленте статьи, собранные за последние RECENT_DAYS дней (правило владельца 03.11: пересчёты только по сигналам последних
двух недель, старше их уже не смотрят), принятые или отмеченные «в дайджест», у которых русский заголовок лежит копией
заголовка издателя и не отвечает правилам, которые видно кодом (pipeline.title_follows_rules). Закрытые месяцы ленты не
трогает: выпуск закрытого месяца только на просмотр.

Задачи — process_articles с явным списком и пометкой only=STAGES в полосе пересчётов (rescore.enqueue_stage): правит
тот же шаг воркера, что у новых статей, ядро пишет только заголовок. Сборка NL до правки оставит заголовок копией,
поэтому прогон — после пересборки NL. Правка человека (corrected_title блока ОС) лежит поверх title_ru, прогон её не
трогает.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from oiltech_digest import feed_window
# Модулем, а не функцией: тестовая фикстура подменяет connection.get_connection.
from oiltech_digest.db import connection, marks
from oiltech_digest.processing.pipeline import _needs_translation, title_follows_rules

# Что пишет ядро по задаче прогона (external_ai.stages_to_write).
STAGES = ["translation"]
# Окно прогона по дате сбора: последние две недели (правило владельца 03.11).
RECENT_DAYS = 14
_PENDING = ("queued", "running", "finalizing")


def selection() -> dict[str, Any]:
    """Видимые в ленте статьи открытых месяцев, собранные за последние RECENT_DAYS дней, принятые или отмеченные «в
    дайджест», с русским заголовком копией не по правилам. Статья, которая уже стоит в задаче прогона, не берётся: её
    правка ещё не записана. Окно считается от сегодняшнего дня окна ленты: те же часы, что у открытых месяцев."""
    window = feed_window.current()
    since = window.today - timedelta(days=RECENT_DAYS)
    digest = marks.article_marked_sql("a.id", "digest")
    with connection.get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT a.id, a.title, c.title_ru, s.name, {digest}
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            JOIN article_cards c ON c.article_id = a.id
            WHERE {window.sql('a')} AND a.collected_at >= %s
              AND {feed_window.visible_sql('a', 'c', 's')}
              AND ({feed_window.relevance_sql('a', 'c')} IS TRUE OR {digest})
              AND NOT EXISTS (
                SELECT 1 FROM background_jobs j
                WHERE j.kind = 'process_articles' AND j.status = ANY(%s) AND j.payload_json->'only' = %s::jsonb
                  AND j.payload_json->'article_ids' @> to_jsonb(a.id))
            ORDER BY a.id
            """,
            (since, list(_PENDING), json.dumps(STAGES)),
        ).fetchall()
    picked = [(int(article_id), in_digest) for article_id, title, title_ru, source, in_digest in rows
              if _copy_off_the_rules((title or "").strip(), title_ru, source)]
    return {"months": window.open_months, "since": since, "article_ids": [one for one, _ in picked],
            "digest_ids": {one for one, in_digest in picked if in_digest}}


def _copy_off_the_rules(title: str, title_ru: str | None, source: str | None) -> bool:
    """Русский заголовок, который лежит копией (как его клал перевод до правки) и правилам кодом не отвечает."""
    return (bool(title) and not _needs_translation(title) and title_ru == title[:200]
            and not title_follows_rules(title, source))
