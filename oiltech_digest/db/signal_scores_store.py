"""Хранилище второго вызова «Карточки» (#132, спека #128): что этапу баллов читать и как писать баллы сигнала.

Баллы — у сигнала после «Отбора», принятого им или человеком, категории «бизнес» или «технология» (у «другого» баллов
нет), когда основной вызов «Карточки» уже заполнил карточку: вход второго вызова — её дата события и стадия зрелости.
Пишутся балл, профиль, разбивка по критериям, снимок критериев (как у статей и радара, ADR 0002), ступень, рецепт с
итогами в двух вариантах подмешивания ключевых слов; «Решение платформы» пересчитывает код
(repository.platform_decision_sql). «Не тот блок» меняет категорию сразу и помечает баллы «пересчитываются».

ВНИМАНИЕ про тесты: соединение — `connection.get_connection()` атрибутом модуля (как в signal_card_store):
изоляция тестов держится на подмене этого атрибута.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from oiltech_digest.db import connection, signal_card_store
from oiltech_digest.db.repository import CARD_SET_BY_HUMAN, refresh_platform_decisions
from oiltech_digest.db.signal_card_store import PIPELINE_SINCE_SQL
from oiltech_digest.feed_window import OTBOR_ACCEPT
from oiltech_digest.scoring_profiles import BUSINESS, TECH_RADAR

# Вид задачи этапа и стадия его расхода в ai_processing_runs.
KIND = "signal_scores"
# Профиль баллов по категории сигнала: это заменяет пункт 2 ADR 0002 («статья ленты всегда оценивается business»).
PROFILES = {"business": BUSINESS, "technology": TECH_RADAR}
# Сколько задач шаг ставит одному сигналу: вторая — на случай сбоя модели; дальше — повтор командой (--ids).
DAILY_ATTEMPTS = 2
# Кого оценивает второй вызов: после «Отбора» и принятого (им или человеком), не дубль, с карточкой основного вызова и
# в витрине — бизнес или технология.
_SCORABLE_SQL = (f"(s.otbor_decision = '{OTBOR_ACCEPT}' AND s.merged_into_signal_id IS NULL"
                 f" AND s.card_recipe IS NOT NULL AND s.signal_category IN ({', '.join(map(repr, PROFILES))}))")


def score_inputs(signal_ids: Sequence[int]) -> list[dict[str, Any]]:
    """Главная публикация сигнала (как у основного вызова: signal_card_store.card_inputs) и поля по договорённости
    (спека #128, «Передача между агентами»): категория, отрасль, связь и применение, дата события и стадия зрелости из
    карточки, суть. Только те, кого второй вызов оценивает (_SCORABLE_SQL)."""
    publications = {int(row["signal_id"]): row for row in signal_card_store.card_inputs(signal_ids)}
    if not publications:
        return []
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT s.id AS signal_id, s.signal_category, s.industry, s.oilfield_relevance, s.oilfield_application,
                   s.event_date, s.event_date_source, s.event_date_precision, s.technology_maturity, s.summary
            FROM signals s
            WHERE s.id = ANY(%s) AND {_SCORABLE_SQL}
            ORDER BY s.id
            """,
            (sorted(publications),),
        )
        fields = cur.fetchall()
    return [{**publications[int(row["signal_id"])], **row} for row in fields]


def new_signal_ids(*, days: int, limit: int) -> list[int]:
    """Сигналы для шага планировщика: оцениваемые (_SCORABLE_SQL), без баллов второго вызова и принятые за `days`
    дней, или с баллами, которые «пересчитываются» после «не тот блок», — не в идущей задаче этапа и не исчерпали
    попыток (DAILY_ATTEMPTS)."""
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
            WHERE {_SCORABLE_SQL}
              AND ((s.scores_recipe IS NULL AND {PIPELINE_SINCE_SQL} >= now() - make_interval(days => %(days)s))
                   OR s.scores_stale_since IS NOT NULL)
              AND NOT EXISTS (SELECT 1 FROM taken t WHERE t.id = s.id)
            ORDER BY s.id
            LIMIT %(limit)s
            """,
            {"days": int(days), "window": int(days) + 1, "limit": int(limit), "attempts": DAILY_ATTEMPTS},
        ).fetchall()
    return [int(row[0]) for row in rows]


def scorable_ids(signal_ids: Sequence[int]) -> list[int]:
    """Из названных сигналов — те, кого второй вызов оценивает, в порядке названия."""
    with connection.get_connection() as conn:
        found = {int(row[0]) for row in conn.execute(
            f"SELECT s.id FROM signals s WHERE s.id = ANY(%s) AND {_SCORABLE_SQL}", ([int(one) for one in signal_ids],),
        ).fetchall()}
    return [int(one) for one in signal_ids if int(one) in found]


def write_scores(signal_id: int, values: dict[str, Any], *, category: str, recipe: dict[str, Any],
                 log: list[dict[str, str]]) -> bool:
    """Записать баллы второго вызова: балл, профиль, разбивку, снимок критериев, ступень, рецепт; снять пометку
    «пересчитываются» и пересчитать «Решение платформы». Только если сигнал ещё оценивается и его категория та, по
    профилю которой считали (`category`): человек мог сменить её «не тем блоком» посреди задачи — тогда баллы
    пересчитает следующая задача. False — не записано."""
    with connection.get_connection() as conn:
        row = conn.execute(
            f"""
            UPDATE signals s
            SET score = %s, score_profile = %s, score_items_json = %s, criteria_snapshot = %s, score_band = %s,
                scores_recipe = %s, scores_stale_since = NULL,
                decision_log = COALESCE(s.decision_log, '[]'::jsonb) || %s::jsonb, updated_at = now()
            WHERE s.id = %s AND {_SCORABLE_SQL} AND s.signal_category = %s
            RETURNING s.id
            """,
            (values["score"] if values["score"] is not None else 0, values["profile"], Json(values["items"]),
             Json(values["criteria_snapshot"]), values["band"], Json(recipe), Json(log), int(signal_id), category),
        ).fetchone()
        if row is not None:
            refresh_platform_decisions([int(signal_id)], conn=conn)
        conn.commit()
    return row is not None


def wrong_block(signal_id: int, *, log: list[dict[str, str]]) -> tuple[str, bool] | None:
    """«Не тот блок» у сигнала после «Отбора» (#132): категория — другая витрина сразу (её ставит человек), и так же
    до карточки; баллы, если они уже есть, — «пересчитываются» до ответа второго вызова. (новая категория, можно ли
    сразу ставить второй вызов: карточка готова); None — сигнал не после «Отбора», не принят им или «другое»: тогда
    «не тот блок» — только блок ОС."""
    with connection.get_connection() as conn:
        row = conn.execute(
            f"""
            UPDATE signals s
            SET signal_category = CASE s.signal_category WHEN 'business' THEN 'technology' ELSE 'business' END,
                card_set_by = COALESCE(s.card_set_by, '{{}}'::jsonb) || '{{"signal_category": "{CARD_SET_BY_HUMAN}"}}',
                scores_stale_since = CASE WHEN s.scores_recipe IS NOT NULL THEN now() END,
                decision_log = COALESCE(s.decision_log, '[]'::jsonb) || %s::jsonb, updated_at = now()
            WHERE s.id = %s AND s.otbor_decision = '{OTBOR_ACCEPT}' AND s.merged_into_signal_id IS NULL
              AND s.signal_category IN ({', '.join(map(repr, PROFILES))})
            RETURNING s.signal_category, s.card_recipe IS NOT NULL
            """,
            (Json(log), int(signal_id)),
        ).fetchone()
        conn.commit()
    return (row[0], bool(row[1])) if row is not None else None


def record_usage(job_id: int | None, rows: list[dict[str, Any]]) -> None:
    """Расход этапа — в ai_processing_runs под его именем, строка на (задача, сигнал): повторное применение итога той же
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
                (job_id, row["signal_id"], KIND, row.get("model"), row["input_tokens"], row["output_tokens"],
                 row["input_tokens"] + row["output_tokens"], row["cost_usd"]),
            )
        conn.commit()
