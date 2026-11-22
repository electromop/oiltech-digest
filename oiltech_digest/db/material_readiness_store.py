"""Хранение готовности материала выпуска (#199, спека #197): что правилу готовности (material_readiness) читать у сигнала
карточки и его статьи и какие задачи шагов конвейера у материала уже идут или были за сутки.

Пути шагов берутся у их хранилищ, а не переписываются: «Карточку» получает то, что прошло «Отбор»
(signal_card_store._PAST_OTBOR_SQL), «Суть и перевод» сигналов: принятые им находки и материалы
(signal_summary_store._ACCEPTED_SQL), баллы положены по signal_scores_store.SCORES_DUE_SQL. Соединение модуль получает
от вызывающего: состояние месяца читается одним соединением.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from psycopg.rows import dict_row

from oiltech_digest import feed_window
from oiltech_digest.db import marks
from oiltech_digest.db.repository import SIGNAL_ORIGIN_FEED
from oiltech_digest.db.showcase_cards import corrections_lateral
from oiltech_digest.db.signal_card_store import _PAST_OTBOR_SQL
from oiltech_digest.db.signal_scores_store import SCORES_DUE_SQL
from oiltech_digest.db.signal_summary_store import _ACCEPTED_SQL

# Задача шага ещё идёт: ждёт выдачи, выдана воркеру или пишет итог.
_PENDING = ("queued", "running", "finalizing")
# Окно суточного предела перепроверки: попытки шага за последние сутки.
_DAY_HOURS = 24
# Где в задаче шага номера материалов: у обработки ленты статьи (явный список и резерв выдачи, у пакета дня только
# резерв), у шагов сигналов их номера.
_KEYS = {"process_articles": ("article_ids", "reserved_article_ids")}
_SIGNAL_KEYS = ("signal_ids",)


def _present(column: str) -> str:
    return f"(COALESCE(btrim({column}), '') <> '')"


def signal_states(conn, signal_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """Сигналы карточек материалов: есть ли суть и русский заголовок (текст модели или правка людей, как на экране
    витрины), отработала ли «Карточка» (рецепт карточки), посчитаны ли баллы второго вызова и не «пересчитываются» ли,
    положены ли они (принято «Отбором», бизнес или технология); на какие шаги сигнал идёт по своему пути; у сигнала ленты
    ещё его статья: принята ли, есть ли у неё суть и русский заголовок (их пишет обработка ленты, мост переносит их в
    сигнал). Нет сигнала: нет и ключа."""
    ids = sorted({int(one) for one in signal_ids})
    if not ids:
        return {}
    rows = conn.cursor(row_factory=dict_row).execute(
        f"""
        SELECT s.id, s.origin, s.otbor_decision, s.summary, s.title_ru, s.card_set_by,
               CASE WHEN s.origin = '{SIGNAL_ORIGIN_FEED}' THEN s.origin_article_id END AS article_id,
               ({_present('corr.corrected_thesis')} OR {_present('s.summary')}) AS has_summary,
               ({_present('corr.corrected_title')} OR {_present('s.title_ru')}) AS has_title,
               s.card_recipe IS NOT NULL AS has_card,
               (s.scores_recipe IS NOT NULL AND s.scores_stale_since IS NULL) AS has_scores,
               COALESCE({SCORES_DUE_SQL}, FALSE) AS scores_due,
               COALESCE({_PAST_OTBOR_SQL}, FALSE) AS card_path,
               COALESCE({_ACCEPTED_SQL}, FALSE) AS summary_path,
               {_present('c.summary')} AS article_summary, {_present('c.title_ru')} AS article_title,
               COALESCE({feed_window.relevance_sql('a', 'c')}, FALSE) AS article_accepted
        FROM signals s
        {corrections_lateral('s')}
        LEFT JOIN articles a ON a.id = s.origin_article_id AND s.origin = '{SIGNAL_ORIGIN_FEED}'
        LEFT JOIN article_cards c ON c.article_id = a.id
        WHERE s.id = ANY(%s)
        """,
        (ids,),
    ).fetchall()
    return {int(row["id"]): row for row in rows}


def article_states(conn, article_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """Статьи выпуска без сигнала ленты (собраны до 01.08.2026): есть ли у статьи суть и русский заголовок и принята ли
    она; поля под теми же именами, что у статьи сигнала ленты в signal_states."""
    ids = sorted({int(one) for one in article_ids})
    if not ids:
        return {}
    rows = conn.cursor(row_factory=dict_row).execute(
        f"""
        SELECT a.id AS article_id, {_present('c.summary')} AS article_summary, {_present('c.title_ru')} AS article_title,
               COALESCE({feed_window.relevance_sql('a', 'c')}, FALSE) AS article_accepted
        FROM articles a
        LEFT JOIN article_cards c ON c.article_id = a.id
        WHERE a.id = ANY(%s)
        """,
        (ids,),
    ).fetchall()
    return {int(row["article_id"]): row for row in rows}


def _numbers_sql() -> str:
    """Номера материалов задачи шага по ключам задачи (k.key): строка на номер."""
    return ("SELECT j.id AS job, (jsonb_array_elements_text(j.payload_json->k.key))::bigint AS id"
            " FROM background_jobs j CROSS JOIN unnest(%(keys)s::text[]) AS k(key)"
            " WHERE j.kind = %(kind)s AND jsonb_typeof(j.payload_json->k.key) = 'array'")


def pending(conn, kind: str, ids: Sequence[int]) -> set[int]:
    """Из номеров: те, у кого задача шага вида kind ещё идёт (второй раз шаг не ставится)."""
    wanted = sorted({int(one) for one in ids})
    if not wanted:
        return set()
    rows = conn.execute(
        f"SELECT DISTINCT x.id FROM ({_numbers_sql()} AND j.status = ANY(%(statuses)s)) x WHERE x.id = ANY(%(ids)s)",
        {"keys": list(_KEYS.get(kind, _SIGNAL_KEYS)), "kind": kind, "statuses": list(_PENDING), "ids": wanted},
    ).fetchall()
    return {int(row[0]) for row in rows}


def attempts(conn, kind: str, ids: Sequence[int]) -> dict[int, int]:
    """Сколько задач шага вида kind у каждого номера поставлено за последние сутки, по любому поводу (суточный предел
    перепроверки готовности)."""
    wanted = sorted({int(one) for one in ids})
    if not wanted:
        return {}
    rows = conn.execute(
        f"SELECT x.id, count(DISTINCT x.job)::int FROM ({_numbers_sql()}"
        " AND j.created_at >= now() - make_interval(hours => %(hours)s)) x WHERE x.id = ANY(%(ids)s) GROUP BY x.id",
        {"keys": list(_KEYS.get(kind, _SIGNAL_KEYS)), "kind": kind, "hours": _DAY_HOURS, "ids": wanted},
    ).fetchall()
    return {int(row[0]): int(row[1]) for row in rows}


def marked(conn, article_ids: Sequence[int], signal_ids: Sequence[int]) -> tuple[set[int], set[int]]:
    """Из номеров: статьи и сигналы, которые сейчас отмечены «в дайджест» (у сигнала ленты отметка его статьи). По ним
    готовность ведёт цепочку шагов: снятая отметка больше не повод для прогона."""
    articles = sorted({int(one) for one in article_ids})
    signals = sorted({int(one) for one in signal_ids})
    found_articles = {int(row[0]) for row in conn.execute(
        f"SELECT a.id FROM articles a WHERE a.id = ANY(%s) AND {marks.article_marked_sql('a.id', marks.DIGEST_STATUS)}",
        (articles,)).fetchall()} if articles else set()
    found_signals = {int(row[0]) for row in conn.execute(
        f"SELECT s.id FROM signals s WHERE s.id = ANY(%s) AND ({marks.signal_marked_sql('s.id', marks.DIGEST_STATUS)}"
        f" OR (s.origin = '{SIGNAL_ORIGIN_FEED}'"
        f" AND {marks.article_marked_sql('s.origin_article_id', marks.DIGEST_STATUS)}))",
        (signals,)).fetchall()} if signals else set()
    return found_articles, found_signals
