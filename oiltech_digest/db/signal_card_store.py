"""Хранилище карточек сигналов (#96, ADR 0003): что агенту «Карточка сигнала» читать и как писать.

Карточка живёт в самом сигнале: атрибуты — колонки signals, у каждого значения — кто его поставил
(card_set_by), у карточки — рецепт (card_recipe)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from oiltech_digest.db import connection
from oiltech_digest.db.repository import (_FEED_ARTICLE_SHOWN_SQL, CARD_SET_BY_AGENT, CARD_SET_BY_HUMAN,
                                          SIGNAL_ORIGIN_FEED, radar_origin_sql)
from oiltech_digest.feed_window import OTBOR_REJECT, FeedWindow

# Вид задачи агента и стадия его расхода в ai_processing_runs.
KIND = "signal_card"
# Поле карточки → колонки signals; первая колонка решает, пусто ли поле.
CARD_COLUMNS: dict[str, tuple[str, ...]] = {
    "event_date": ("event_date", "event_date_source"),
    "participants": ("participants_json",),
    "event_type": ("event_type",),
    "region": ("region",),
    "technology_class": ("technology_class",),
    "technology_maturity": ("technology_maturity",),
    "oilfield_relevance": ("oilfield_relevance",),
    "oilfield_application": ("oilfield_application",),
    "why_interesting": ("why_interesting",),
}
_JSON_COLUMNS = frozenset({"participants_json"})


def card_inputs(signal_ids: Sequence[int]) -> list[dict[str, Any]]:
    """Главная публикация сигналов: у сигнала ленты — его статья, у находки радара — ссылка с текстом
    издателя (статья корпуса или докачанная страница) раньше сводки модели, затем по силе. Среди
    публикаций с текстом издателя выбор тот же, что у пакета эксперту (signal_card_pack._radar_candidates):
    экзамен (#98) сверяет агента с разметкой того же текста. Дата публикации — как в пакете: у находки
    «режима ChatGPT» дата ссылки — это дата события по версии модели, публикационной даты у неё нет.
    publisher_type — тип источника из справочника источников (Media, Company…); у находки без статьи
    корпуса его нет."""
    from oiltech_digest.signal_research import EVIDENCE_SOURCE

    ids = sorted({int(value) for value in signal_ids})
    if not ids:
        return []
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT s.id AS signal_id, s.origin, s.theme, s.signal_category AS category,
                   'article:' || a.id AS publication, a.title, a.raw_text AS text,
                   COALESCE(a.published_at, a.collected_at) AS published,
                   src.name AS publisher, src.source_type AS publisher_type
            FROM signals s
            JOIN articles a ON a.id = s.origin_article_id
            JOIN sources src ON src.id = a.source_id
            WHERE s.id = ANY(%(ids)s) AND s.origin = '{SIGNAL_ORIGIN_FEED}'
            """,
            {"ids": ids},
        )
        rows = cur.fetchall()
        cur.execute(
            f"""
            SELECT DISTINCT ON (s.id)
                   s.id AS signal_id, s.origin, s.theme, s.signal_category AS category,
                   'evidence:' || e.id AS publication,
                   COALESCE(a.title, e.title) AS title, COALESCE(a.raw_text, e.extracted_fact) AS text,
                   CASE WHEN a.id IS NOT NULL THEN COALESCE(a.published_at, a.collected_at)
                        WHEN e.raw_payload_json->>'evidence_source' IS DISTINCT FROM %(model_dated)s
                        THEN e.published_at END AS published,
                   COALESCE(src.name, e.publisher) AS publisher, src.source_type AS publisher_type
            FROM signals s
            JOIN signal_evidence e ON e.signal_id = s.id
            LEFT JOIN articles a ON a.id = e.article_id
            LEFT JOIN sources src ON src.id = a.source_id
            WHERE s.id = ANY(%(ids)s) AND {radar_origin_sql("s")}
            ORDER BY s.id,
                     (length(btrim(COALESCE(a.raw_text, ''))) > 0
                      OR e.raw_payload_json->>'full_text_fetched' = 'true') DESC,
                     e.strength DESC, e.id
            """,
            {"ids": ids, "model_dated": EVIDENCE_SOURCE},
        )
        rows += cur.fetchall()
    return sorted(rows, key=lambda row: row["signal_id"])


# Сколько задач шаг планировщика ставит одному сигналу: вторая — на случай сбоя модели или смены
# справочников посреди задачи; дальше сигнал добирает прогон по истории (#98), а не каждые полчаса.
DAILY_ATTEMPTS = 2


def new_signal_ids(*, days: int, limit: int) -> list[int]:
    """Сигналы для шага планировщика: заведены за `days` дней, без карточки, не дубли, не в задаче
    агента, которая ещё идёт, и не исчерпали попыток (DAILY_ATTEMPTS). Дальше «Отбора» (#130) идёт
    только принятое: отклонённое им и сигнал без его решения (сбой) карточку не получают."""
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
            WHERE s.card_recipe IS NULL AND s.merged_into_signal_id IS NULL
              AND s.created_at >= now() - make_interval(days => %(days)s)
              AND s.otbor_decision IS DISTINCT FROM '{OTBOR_REJECT}'
              AND NOT (s.otbor_decision IS NULL AND s.otbor_failure IS NOT NULL)
              AND NOT EXISTS (SELECT 1 FROM taken t WHERE t.id = s.id)
            ORDER BY s.id
            LIMIT %(limit)s
            """,
            {"days": int(days), "window": int(days) + 1, "limit": int(limit), "attempts": DAILY_ATTEMPTS},
        ).fetchall()
    return [int(row[0]) for row in rows]


def history_signals(*, since: date, window: FeedWindow) -> list[dict[str, Any]]:
    """Сигналы прогона по истории (#98, спека #93): ленты — принятые статьи, собранные с `since` (сигнал не в
    архиве, а статью показывает лента — как у витрины «бизнес»: за карточку, которую никто не увидит, не платим),
    и все находки радара; дубли — мимо. first — радар или статья в текущем окне ленты (window): их карточки
    ставятся первыми; pending — сигнал в задаче агента, которая ещё идёт. По порядку постановки."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            WITH pending AS (
              SELECT DISTINCT (jsonb_array_elements_text(j.payload_json->'signal_ids'))::bigint AS id
              FROM background_jobs j
              WHERE j.kind = '{KIND}' AND j.status IN ('queued', 'running', 'finalizing')
                AND jsonb_typeof(j.payload_json->'signal_ids') = 'array'
            )
            SELECT s.id, s.card_recipe,
                   ({radar_origin_sql("s")} OR COALESCE({window.sql("a")}, FALSE)) AS first,
                   EXISTS (SELECT 1 FROM pending p WHERE p.id = s.id) AS pending
            FROM signals s
            LEFT JOIN articles a ON a.id = s.origin_article_id AND s.origin = '{SIGNAL_ORIGIN_FEED}'
            WHERE s.merged_into_signal_id IS NULL
              AND ({radar_origin_sql("s")}
                   OR (a.collected_at >= %(since)s AND s.archived_at IS NULL AND {_FEED_ARTICLE_SHOWN_SQL}))
            ORDER BY first DESC, s.id
            """,
            {"since": since},
        )
        return cur.fetchall()


def write_card(signal_id: int, values: dict[str, Any], *, recipe: dict[str, Any]) -> bool:
    """Записать проверенные значения карточки и её рецепт. False — сигнала нет.

    Агент пишет поле, если оно его (прошлый прогон агента) или пустое. Значение человека не
    перезаписывается никогда — даже пустое: человек мог убрать лишнее намеренно. Чужое непустое
    значение — судьи радара (дата события, связь с нефтесервисом, применение, «почему интересно») —
    тоже остаётся: у находки радара агент заполняет только пустые поля."""
    primary = ", ".join(columns[0] for columns in CARD_COLUMNS.values())
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(f"SELECT card_set_by, {primary} FROM signals WHERE id = %s FOR UPDATE", (int(signal_id),))
        row = cur.fetchone()
        if row is None:
            return False
        set_by = dict(row["card_set_by"] or {})
        updates: dict[str, Any] = {}
        for key, columns in CARD_COLUMNS.items():
            if columns[0] not in values or not _writable(set_by.get(key), row[columns[0]]):
                continue
            for column in columns:
                value = values.get(column)
                updates[column] = Json(value) if column in _JSON_COLUMNS else value
            set_by[key] = CARD_SET_BY_AGENT
        updates["card_set_by"] = Json(set_by)
        updates["card_recipe"] = Json(recipe)
        assignments = ", ".join(f"{column} = %s" for column in updates)
        cur.execute(
            f"UPDATE signals SET {assignments}, updated_at = now() WHERE id = %s",
            [*updates.values(), int(signal_id)],
        )
        conn.commit()
    return True


def record_usage(job_id: int | None, rows: list[dict[str, Any]]) -> None:
    """Расход агента — в ai_processing_runs под его именем, строка на (задача, сигнал): повторное
    применение итога той же задачи счёт не удваивает (частичный уникальный индекс по signal_id)."""
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


def judge_values(signal_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """Дата события и связь с нефтесервисом сигналов радара — для сравнения судьи с экспертом (#98): кто
    поставил значение — в card_set_by; дубль отдаёт свои значения и номер главной карточки."""
    ids = sorted({int(value) for value in signal_ids})
    if not ids:
        return {}
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT id, event_date, oilfield_relevance, card_set_by, merged_into_signal_id FROM signals "
            "WHERE id = ANY(%s)",
            (ids,),
        )
        rows = cur.fetchall()
    return {int(row["id"]): {
        "event_date": row["event_date"].isoformat() if row["event_date"] else None,
        "oilfield_relevance": row["oilfield_relevance"],
        "set_by": {key: value for key, value in (row["card_set_by"] or {}).items()
                   if key in ("event_date", "oilfield_relevance")},
        "merged_into_signal_id": row["merged_into_signal_id"],
    } for row in rows}


def _writable(set_by: str | None, current: Any) -> bool:
    if set_by == CARD_SET_BY_HUMAN:
        return False
    return set_by == CARD_SET_BY_AGENT or current is None or current == "" or current == []
