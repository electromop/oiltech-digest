"""Хранилище «Суть и перевод» сигналов (#131): что этапу читать у находок и материалов, принятых «Отбором», и как
писать суть и русский заголовок.

Публикация — та же, что видел «Отбор» (otbor_store.signal_inputs); поля «Отбора» — категория, связь и применение
сигнала. Пишется только пустое: суть материала — описание автора, правку человека этап не трогает.

ВНИМАНИЕ про тесты: соединение — `connection.get_connection()` атрибутом модуля (как в signal_card_store):
изоляция тестов держится на подмене этого атрибута.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from oiltech_digest.db import connection, otbor_store
from oiltech_digest.db.repository import CARD_SET_BY_AGENT, CARD_SET_BY_HUMAN, SIGNAL_SELECTOR_OTBOR
from oiltech_digest.feed_window import OTBOR_ACCEPT

# Вид задачи этапа; расход — в ai_processing_runs под стадиями сути и перевода, как у статей ленты.
KIND = "signal_summary"
STAGES = ("summary", "translation")
# Сколько задач шаг ставит одному сигналу: вторая — на случай сбоя модели; дальше — повтор командой.
DAILY_ATTEMPTS = 2
# Поле этапа → колонка сигнала.
COLUMNS = {"summary": "summary", "translation": "title_ru"}
# Дальше «Отбора» идут только принятые им находки и материалы — и шагом дня, и по названным номерам, и в пакете.
_ACCEPTED_SQL = (f"s.selector = '{SIGNAL_SELECTOR_OTBOR}' AND s.merged_into_signal_id IS NULL"
                 f" AND s.otbor_decision = '{OTBOR_ACCEPT}'")


def summary_inputs(signal_ids: Sequence[int]) -> list[dict[str, Any]]:
    """Публикация сигнала (как у «Отбора») и его поля: суть, русский заголовок, кто их поставил, категория, связь и
    применение «Отбора». Только принятые им (_ACCEPTED_SQL)."""
    rows = otbor_store.signal_inputs(signal_ids)
    if not rows:
        return []
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT s.id, s.summary, s.title_ru, s.card_set_by, s.signal_category, s.oilfield_relevance,"
            f" s.oilfield_application FROM signals s WHERE s.id = ANY(%s) AND {_ACCEPTED_SQL}",
            ([int(row["signal_id"]) for row in rows],),
        )
        fields = {int(row["id"]): row for row in cur.fetchall()}
    return [{**row, **{key: value for key, value in fields[int(row["signal_id"])].items() if key != "id"}}
            for row in rows if int(row["signal_id"]) in fields]


def missing(row: dict[str, Any]) -> list[str]:
    """Стадии, которых у сигнала ещё нет: пустое поле, которое не ставил человек."""
    set_by = row.get("card_set_by") or {}
    return [stage for stage, column in COLUMNS.items()
            if not str(row.get(column) or "").strip() and set_by.get(column) != CARD_SET_BY_HUMAN]


def new_signal_ids(*, days: int, limit: int) -> list[int]:
    """Сигналы для шага планировщика: находки и материалы, принятые «Отбором», без сути или русского заголовка,
    заведены за `days` дней, не дубли, не в идущей задаче этапа и не исчерпали попыток (DAILY_ATTEMPTS)."""
    with connection.get_connection() as conn:
        rows = conn.execute(
            f"""
            WITH attempts AS (
              SELECT (jsonb_array_elements_text(j.payload_json->'signal_ids'))::bigint AS id,
                     j.status IN ('queued', 'running', 'finalizing') AS pending
              FROM background_jobs j
              WHERE j.kind = '{KIND}' AND j.created_at >= now() - make_interval(days => %(window)s)
                AND jsonb_typeof(j.payload_json->'signal_ids') = 'array'
            ),
            taken AS (
              SELECT id FROM attempts GROUP BY id HAVING bool_or(pending) OR count(*) >= %(attempts)s
            )
            SELECT s.id
            FROM signals s
            WHERE {_ACCEPTED_SQL}
              AND ((COALESCE(btrim(s.summary), '') = ''
                    AND s.card_set_by->>'summary' IS DISTINCT FROM '{CARD_SET_BY_HUMAN}')
                   OR (COALESCE(btrim(s.title_ru), '') = ''
                       AND s.card_set_by->>'title_ru' IS DISTINCT FROM '{CARD_SET_BY_HUMAN}'))
              AND s.created_at >= now() - make_interval(days => %(days)s)
              AND NOT EXISTS (SELECT 1 FROM taken t WHERE t.id = s.id)
            ORDER BY s.id
            LIMIT %(limit)s
            """,
            {"days": int(days), "window": int(days) + 1, "limit": int(limit), "attempts": DAILY_ATTEMPTS},
        ).fetchall()
    return [int(row[0]) for row in rows]


def accepted_ids(signal_ids: Sequence[int]) -> list[int]:
    """Из названных сигналов — принятые «Отбором» находки и материалы, в порядке названия."""
    with connection.get_connection() as conn:
        found = {int(row[0]) for row in conn.execute(
            f"SELECT s.id FROM signals s WHERE s.id = ANY(%s) AND {_ACCEPTED_SQL}", ([int(one) for one in signal_ids],),
        ).fetchall()}
    return [int(one) for one in signal_ids if int(one) in found]


def write_summary(signal_id: int, values: dict[str, str], *, log: list[dict[str, str]]) -> list[str]:
    """Записать суть и русский заголовок (values: колонка → текст) — только в пустое поле, которое не ставил
    человек; кто поставил — этап (card_set_by). Шаг — в «Ход решения». Записанные колонки; [] — нечего или сигнала
    нет."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT summary, title_ru, card_set_by FROM signals WHERE id = %s FOR UPDATE", (int(signal_id),))
        row = cur.fetchone()
        if row is None:
            return []
        set_by = dict(row["card_set_by"] or {})
        updates = {column: text for column, text in values.items()
                   if text and not str(row.get(column) or "").strip() and set_by.get(column) != CARD_SET_BY_HUMAN}
        if updates:
            set_by.update({column: CARD_SET_BY_AGENT for column in updates})
            assignments = ", ".join(f"{column} = %s" for column in updates)
            cur.execute(
                f"UPDATE signals SET {assignments}, card_set_by = %s,"
                " decision_log = COALESCE(decision_log, '[]'::jsonb) || %s::jsonb, updated_at = now() WHERE id = %s",
                [*updates.values(), Json(set_by), Json(log), int(signal_id)],
            )
        conn.commit()
    return list(updates)


def record_usage(job_id: int | None, rows: list[dict[str, Any]]) -> None:
    """Расход этапа — в ai_processing_runs строкой на (задача, сигнал, стадия): повторное применение итога той же
    задачи счёт не удваивает (частичный уникальный индекс по signal_id)."""
    if not rows:
        return
    with connection.get_connection() as conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO ai_processing_runs
                  (job_id, signal_id, stage, provider, model, input_tokens, output_tokens, total_tokens, cost_usd, status)
                VALUES (%s, %s, %s, 'openai', %s, %s, %s, %s, %s, 'ok')
                ON CONFLICT (job_id, signal_id, stage) WHERE signal_id IS NOT NULL DO NOTHING
                """,
                (job_id, row["signal_id"], row["stage"], row.get("model"), row["input_tokens"], row["output_tokens"],
                 row["input_tokens"] + row["output_tokens"], row["cost_usd"]),
            )
        conn.commit()
