"""Хранилище автопроверки правил эксперта (#190, спека #187): прогоны (rule_checks) и выборки для них.

Прогон: правило предложения на эталоне ОС (mode rule) или весь действующий набор агента против голой инструкции
(mode set, первый понедельник месяца). Статусы: queued (задача поставлена), done, postponed (потолок расходов контура:
ждёт следующего месяца), failed. Итог: строк эталона, к эталону, от эталона, без изменений, совет и подробности.

ВНИМАНИЕ про тесты: соединение: `connection.get_connection()` атрибутом модуля: изоляция тестов держится на подмене
этого атрибута.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from oiltech_digest.db import connection

QUEUED, DONE, POSTPONED, FAILED = "queued", "done", "postponed", "failed"
RULE, SET = "rule", "set"
# Строки задачи, которая упала или пропала из очереди: прогон не выполнен (как срез, feedback_store).
_FAILED_SQL = ("(c.status = 'queued' AND (j.status = 'failed' OR (c.job_id IS NULL"
               " AND c.created_at < now() - interval '10 minutes')))")


def create(*, action_id: int | None, mode: str, agent: str, status: str, note: str | None = None,
           details: dict[str, Any] | None = None) -> int:
    with connection.get_connection() as conn:
        check_id = int(conn.execute(
            "INSERT INTO rule_checks (action_id, mode, agent, status, note, details, finished_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, CASE WHEN %s = 'queued' THEN NULL ELSE now() END) RETURNING id",
            (action_id, mode, agent, status, note, Json(details or {}), status)).fetchone()[0])
        conn.commit()
    return check_id


def attach_job(check_id: int, job_id: int) -> None:
    with connection.get_connection() as conn:
        conn.execute("UPDATE rule_checks SET job_id = %s WHERE id = %s", (int(job_id), int(check_id)))
        conn.commit()


def get(check_id: int) -> dict[str, Any] | None:
    with connection.get_connection() as conn:
        return conn.cursor(row_factory=dict_row).execute("SELECT * FROM rule_checks WHERE id = %s",
                                                         (int(check_id),)).fetchone()


def finish(check_id: int, *, status: str, row_count: int = 0, toward: int = 0, away: int = 0, unchanged: int = 0,
           advice: str | None = None, note: str | None = None, details: dict[str, Any] | None = None) -> None:
    with connection.get_connection() as conn:
        conn.execute(
            "UPDATE rule_checks SET status = %s, row_count = %s, toward = %s, away = %s, unchanged = %s, advice = %s,"
            " note = %s, details = details || %s::jsonb, finished_at = now() WHERE id = %s",
            (status, int(row_count), int(toward), int(away), int(unchanged), advice, note, Json(details or {}),
             int(check_id)))
        conn.commit()


def latest(action_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """Последний прогон по предложению (и набору): у задачи, которая упала, статус «не выполнен» (только чтение)."""
    ids = sorted({int(one) for one in action_ids})
    if not ids:
        return {}
    with connection.get_connection() as conn:
        rows = conn.cursor(row_factory=dict_row).execute(
            f"""
            SELECT DISTINCT ON (c.action_id) c.*, CASE WHEN {_FAILED_SQL} THEN 'failed' ELSE c.status END AS shown,
                   CASE WHEN {_FAILED_SQL} THEN COALESCE(j.error_message, %s) ELSE c.note END AS shown_note
            FROM rule_checks c LEFT JOIN background_jobs j ON j.id = c.job_id
            WHERE c.action_id = ANY(%s)
            ORDER BY c.action_id, c.id DESC
            """, (_LOST, ids)).fetchall()
    return {int(row["action_id"]): {**row, "status": row["shown"], "note": row["shown_note"]} for row in rows}


def settle_failed() -> None:
    """Прогоны, чья задача упала или пропала: «не выполнен», иначе предложение ждало бы проверки вечно."""
    with connection.get_connection() as conn:
        # Целевая строка c и её копия x с задачей j: в условии и причине поля c (той же строки).
        conn.execute(
            f"UPDATE rule_checks c SET status = 'failed', finished_at = now(), note = COALESCE(j.error_message, %s)"
            f" FROM rule_checks x LEFT JOIN background_jobs j ON j.id = x.job_id WHERE x.id = c.id AND {_FAILED_SQL}",
            (_LOST,))
        conn.commit()


_LOST = "задача автопроверки не поставлена или пропала из очереди"


def proposals_waiting_check(*, kind: str, retry_postponed: bool) -> list[int]:
    """Предложения правил без решения, у которых проверки ещё не было (засеянное SQL предложение, процесс остановился
    до постановки); retry_postponed: и те, чья проверка ждала потолка расходов (новый месяц). Упавшая проверка сама не
    повторяется (иначе сбой оплачивался бы каждый цикл): её повторяет администратор кнопкой."""
    statuses = [POSTPONED] if retry_postponed else []
    with connection.get_connection() as conn:
        rows = conn.execute(
            """
            SELECT fa.id FROM feedback_actions fa
            LEFT JOIN LATERAL (SELECT c.status FROM rule_checks c WHERE c.action_id = fa.id ORDER BY c.id DESC LIMIT 1
                              ) last ON TRUE
            WHERE fa.kind = %s AND fa.status = 'pending' AND (last.status IS NULL OR last.status = ANY(%s))
            ORDER BY fa.id
            """, (kind, statuses)).fetchall()
    return [int(row[0]) for row in rows]


def record_usage(job_id: int | None, rows: Sequence[dict[str, Any]], *, stage: str,
                 recipe: dict[str, Any]) -> None:
    """Расход автопроверки своей стадией: строка на (задача, сигнал); у повторного поиска сигнала нет: одна строка
    на задачу. Повторное применение итога той же задачи счёт не удваивает."""
    if not rows:
        return
    with connection.get_connection() as conn:
        if job_id is not None:
            conn.execute("DELETE FROM ai_processing_runs WHERE job_id = %s AND stage = %s AND signal_id IS NULL",
                         (job_id, stage))
        for row in rows:
            conn.execute(
                "INSERT INTO ai_processing_runs (job_id, signal_id, stage, provider, model, input_tokens, output_tokens,"
                " total_tokens, cost_usd, status, recipe) VALUES (%s, %s, %s, 'openai', %s, %s, %s, %s, %s, 'ok', %s)"
                " ON CONFLICT (job_id, signal_id, stage) WHERE signal_id IS NOT NULL DO NOTHING",
                (job_id, row.get("signal_id"), stage, row.get("model"), row["input_tokens"], row["output_tokens"],
                 row["input_tokens"] + row["output_tokens"], row["cost_usd"], Json(recipe)))
        conn.commit()


def pair_nodes(signal_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """Карточки пар «Дедупа», как их видит судья (signal_dedup.pair_prompt): заголовки, тема, суть, компании-участники
    и ссылки (у сигнала ленты: его статья)."""
    ids = sorted({int(one) for one in signal_ids})
    if not ids:
        return {}
    with connection.get_connection() as conn:
        rows = conn.cursor(row_factory=dict_row).execute(
            "SELECT s.id, s.title, s.title_ru, s.theme, s.summary, s.participants_json,"
            " ARRAY(SELECT e.source_url FROM signal_evidence e WHERE e.signal_id = s.id AND e.source_url IS NOT NULL"
            "       UNION SELECT a.url FROM articles a WHERE a.id = s.origin_article_id) AS urls"
            " FROM signals s WHERE s.id = ANY(%s)", (ids,)).fetchall()
    found = {}
    for row in rows:
        companies = [str(one.get("name")) for one in row["participants_json"] or [] if isinstance(one, dict)
                     and one.get("name")]
        found[int(row["id"])] = {"signal": {"title": row["title"], "title_ru": row["title_ru"], "theme": row["theme"],
                                            "summary": row["summary"], "companies": companies},
                                 "urls": [url for url in row["urls"] or [] if url]}
    return found
