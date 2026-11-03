"""Хранилище пробного прогона широкого поиска (#138, signal_search_trial): находки пробного прогона (signals.selector =
SIGNAL_SELECTOR_TRIAL) и чтения для отчёта. Решения «Отбора» по находкам — в таблице проверки #134 (otbor_shadow_store).

ВНИМАНИЕ про тесты: соединение — `connection.get_connection()` атрибутом модуля (как в signal_card_store):
изоляция тестов держится на подмене этого атрибута.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from psycopg.rows import dict_row

from oiltech_digest.db import connection
from oiltech_digest.db.repository import SIGNAL_SELECTOR_TRIAL


def evidence_owners(urls: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Каким сигналам (любым) принадлежат ссылки: {url: {id, selector}} — пробный прогон чужих ссылок не берёт."""
    urls = [url for url in urls if url]
    if not urls:
        return {}
    with connection.get_connection() as conn:
        rows = conn.execute(
            "SELECT e.source_url, s.id, s.selector FROM signal_evidence e JOIN signals s ON s.id = e.signal_id"
            " WHERE e.source_url = ANY(%s)", (urls,),
        ).fetchall()
    return {row[0]: {"id": int(row[1]), "selector": row[2]} for row in rows}


def findings(signal_ids: Sequence[int]) -> list[dict[str, Any]]:
    """Находки пробного прогона для отчёта: заголовок, тема, «Ход решения», ссылки с заголовками."""
    ids = sorted({int(value) for value in signal_ids})
    if not ids:
        return []
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT s.id, s.title, s.theme, s.decision_log,
                   ARRAY(SELECT e.source_url FROM signal_evidence e WHERE e.signal_id = s.id ORDER BY e.id) AS urls
            FROM signals s
            WHERE s.id = ANY(%s) AND s.selector = %s
            ORDER BY s.id
            """,
            (ids, SIGNAL_SELECTOR_TRIAL),
        )
        return cur.fetchall()


def platform_cards(signal_ids: Sequence[int]) -> list[dict[str, Any]]:
    """Сигналы платформы, которые пробный прогон нашёл снова, — карточками для замера полноты: заголовки, суть,
    компании и ссылки (свои и склеенных дублей)."""
    ids = sorted({int(value) for value in signal_ids if value is not None})
    if not ids:
        return []
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT s.id, s.title, s.title_ru, s.summary, s.companies_json AS companies,
                   ARRAY(SELECT e.source_url FROM signal_evidence e
                         WHERE e.signal_id = s.id
                            OR e.signal_id IN (SELECT m.id FROM signals m WHERE m.merged_into_signal_id = s.id)
                        ) AS urls
            FROM signals s WHERE s.id = ANY(%s)
            """,
            (ids,),
        )
        return cur.fetchall()


def collected_cards(*, since: date | datetime) -> list[dict[str, Any]]:
    """Статьи сбора с этой даты по сей день карточками для замера полноты (#138): что из эталона эксперта уже собрал
    парсер (материал периода он мог собрать и после его конца). Заголовок, русский заголовок и суть ленты, если есть;
    статья на удалении не в счёт."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.id, a.title, c.title_ru, c.summary, ARRAY[a.url] AS urls
            FROM articles a
            LEFT JOIN LATERAL (SELECT title_ru, summary FROM article_cards WHERE article_id = a.id
                               ORDER BY id DESC LIMIT 1) c ON TRUE
            WHERE a.collected_at >= %s AND NOT a.pending_deletion
            """,
            (since,),
        )
        return cur.fetchall()


def otbor_jobs(search_job_id: int) -> list[dict[str, Any]]:
    """Задачи «Отбора» пробного прогона: номер, состояние, сколько находок."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT id, status, jsonb_array_length(COALESCE(payload_json->'signal_ids', '[]'::jsonb)) AS findings,
                   error_message
            FROM background_jobs
            WHERE kind = 'otbor' AND payload_json->>'search_trial_job_id' = %s
            ORDER BY id
            """,
            (str(int(search_job_id)),),
        )
        return cur.fetchall()


def costs(job_ids: Sequence[int]) -> dict[str, float]:
    """Фактический расход задач пробного прогона по стадиям (ai_processing_runs), USD: поиск — с платой за вызовы
    web_search, как его пишет ядро."""
    ids = sorted({int(value) for value in job_ids})
    if not ids:
        return {}
    with connection.get_connection() as conn:
        rows = conn.execute(
            "SELECT stage, COALESCE(SUM(cost_usd), 0) FROM ai_processing_runs WHERE job_id = ANY(%s) GROUP BY stage",
            (ids,),
        ).fetchall()
    return {str(stage): round(float(cost), 4) for stage, cost in rows}


def search_call_history() -> dict[str, float] | None:
    """Средний вызов поиска GPT по прошлым прогонам радара (стадия radar_research): токены входа, выхода и вызовы
    web_search на вызов модели. Вызовов строка хранит в error_message («calls=N web_search=M»). Нет истории — None."""
    with connection.get_connection() as conn:
        row = conn.execute(
            """
            SELECT SUM(input_tokens), SUM(output_tokens),
                   SUM((substring(error_message from 'calls=(\\d+)'))::int),
                   SUM((substring(error_message from 'web_search=(\\d+)'))::int)
            FROM ai_processing_runs
            WHERE stage = 'radar_research' AND error_message LIKE 'calls=%'
            """,
        ).fetchone()
    tokens_in, tokens_out, calls, searches = row or (None, None, None, None)
    if not calls:
        return None
    return {"input_tokens": float(tokens_in or 0) / calls, "output_tokens": float(tokens_out or 0) / calls,
            "web_search_calls": float(searches or 0) / calls}
