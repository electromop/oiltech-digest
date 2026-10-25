"""Чтение базы для пакета эксперту «Отбора» (#129): кандидаты групп, ссылки находок, факты прежних строк.

Выборку и правила делает otbor_pack, здесь только запросы. Текст издателя и дата ссылки — по правилам пакета
карточки (signal_card_pack): у находки «режима ChatGPT» дата ссылки — дата события по версии модели поиска, в пакет
она не идёт.

ВНИМАНИЕ про тесты: соединение — `connection.get_connection()` атрибутом модуля (как в signal_card_store):
изоляция тестов держится на подмене этого атрибута.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from psycopg.rows import dict_row

from oiltech_digest import config, feed_window
from oiltech_digest.db import connection
from oiltech_digest.db.repository import _judged_sql, radar_origin_sql

# Дата ссылки находки: у статьи корпуса — её, у ссылки поиска «режима ChatGPT» — никакой (это дата события по
# версии модели поиска), у прочих ссылок — дата издателя.
LINK_PUBLISHED_SQL = (
    "CASE WHEN a.id IS NOT NULL THEN COALESCE(a.published_at, a.collected_at)"
    " WHEN e.raw_payload_json->>'evidence_source' IS DISTINCT FROM %(search_source)s THEN e.published_at END"
)
# Заголовок ссылки: у статьи корпуса — её, у страницы, которую докачали, и у выдачи обычного поиска — издателя. У
# ссылки «режима ChatGPT» без докачки заголовок написала модель поиска (бывает переводом: «SLB выиграла у Aramco…» у
# английской страницы, проверка пакета 22.10) — его нет: черновые данные поиска в решения агентов не идут (спека #128).
_LINK_TITLE_SQL = (
    "CASE WHEN a.id IS NULL AND e.raw_payload_json->>'evidence_source' = %(search_source)s"
    " AND e.raw_payload_json->>'full_text_fetched' IS DISTINCT FROM 'true' THEN NULL"
    " ELSE COALESCE(a.title, e.title) END"
)


def feed_candidates(since: str, until: str) -> list[dict[str, Any]]:
    """Статьи ленты, собранные в окне, о которых гейт уже решил: условие ленты без решения гейта
    (feed_window.ungated_visible_sql) — отклонённые лента не показывает, а пакету они нужны. Отметки людей выборку
    не трогают."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT a.id AS article_id, a.title, a.url, a.raw_text AS text, a.language,
                   COALESCE(a.published_at, a.collected_at) AS published, a.collected_at AS received,
                   s.name AS publisher, COALESCE(s.source_type, '') AS publisher_type, c.relevant
            FROM articles a
            JOIN article_cards c ON c.article_id = a.id
            JOIN sources s ON s.id = a.source_id
            WHERE c.relevant IS NOT NULL AND {feed_window.ungated_visible_sql('a', 's')}
              AND a.collected_at >= %(since)s::date AND a.collected_at < %(until)s::date
              AND a.full_text_status IS DISTINCT FROM 'mismatch'
              AND length(btrim(COALESCE(a.raw_text, ''))) >= %(min_chars)s
            ORDER BY a.id
            """,
            {"since": since, "until": until, "min_chars": config.MIN_ARTICLE_TEXT_CHARS},
        )
        return cur.fetchall()


def radar_candidates(until: str) -> list[dict[str, Any]]:
    """Находки поиска, поступившие до конца окна, с главной публикацией, текст которой написал издатель (статья
    корпуса или докачанная страница; без докачки текст находки — сводка модели). Вид: брак судьи (judge), брак
    ревью пачки (review), иной этап отсева (other_stage), смешанные ссылки (mixed), событие старше срока на день
    находки (old) — по дате судьи или человека, не агента карточки, как у правил показа; без вида — не отсеяна."""
    from oiltech_digest.signal_research import EVIDENCE_SOURCE

    age = int(config.SIGNAL_RADAR_MAX_EVENT_AGE_DAYS)
    old = f"WHEN {_judged_sql('s', 'event_date')} < s.first_seen_at::date - {age} THEN 'old'" if age > 0 else ""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT DISTINCT ON (s.id)
                   s.id AS signal_id, e.id AS evidence_id, a.id AS article_id, a.language,
                   COALESCE(a.title, e.title) AS title, COALESCE(a.url, e.source_url) AS url,
                   COALESCE(a.raw_text, e.extracted_fact) AS text, {LINK_PUBLISHED_SQL} AS published,
                   s.first_seen_at AS received, src.name AS publisher_source,
                   COALESCE(src.source_type, '') AS publisher_type,
                   CASE WHEN s.filter_stage IN ('judge', 'review') THEN s.filter_stage
                        WHEN s.filter_stage IS NOT NULL THEN 'other_stage'
                        WHEN s.mixed_events IS TRUE THEN 'mixed'
                        {old}
                   END AS kind,
                   (SELECT count(*) FROM signal_evidence x WHERE x.signal_id = s.id) AS link_count
            FROM signals s
            JOIN signal_evidence e ON e.signal_id = s.id
            LEFT JOIN articles a ON a.id = e.article_id
            LEFT JOIN sources src ON src.id = a.source_id
            WHERE {radar_origin_sql('s')} AND s.merged_into_signal_id IS NULL AND s.archived_at IS NULL
              AND s.first_seen_at < %(until)s::date
              AND (length(btrim(COALESCE(a.raw_text, ''))) >= %(min_chars)s
                   OR (a.id IS NULL AND e.raw_payload_json->>'full_text_fetched' = 'true'
                       AND length(btrim(COALESCE(e.extracted_fact, ''))) >= %(min_chars)s))
            ORDER BY s.id, e.strength DESC, e.id
            """,
            {"search_source": EVIDENCE_SOURCE, "until": until, "min_chars": config.MIN_ARTICLE_TEXT_CHARS},
        )
        return cur.fetchall()


def evidence_links(signal_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Ссылки находок по силе: у ссылки на статью корпуса заголовок, адрес и издатель — статьи; у ссылки «режима
    ChatGPT» без докачки заголовка нет (_LINK_TITLE_SQL)."""
    from oiltech_digest.signal_research import EVIDENCE_SOURCE

    links: dict[int, list[dict[str, Any]]] = defaultdict(list)
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT e.signal_id, {_LINK_TITLE_SQL} AS title, COALESCE(a.url, e.source_url) AS url,
                   src.name AS publisher_source, {LINK_PUBLISHED_SQL} AS published
            FROM signal_evidence e
            LEFT JOIN articles a ON a.id = e.article_id
            LEFT JOIN sources src ON src.id = a.source_id
            WHERE e.signal_id = ANY(%(signals)s::bigint[])
            ORDER BY e.signal_id, e.strength DESC, e.id
            """,
            {"search_source": EVIDENCE_SOURCE, "signals": signal_ids},
        )
        for row in cur.fetchall():
            links[int(row["signal_id"])].append(row)
    return links


def card_row_facts(article_ids: list[int], signal_ids: list[int]) -> tuple[dict[int, dict[str, Any]], dict[int, Any]]:
    """Для строк прежней разметки (в её манифесте этого нет): язык и день сбора статьи, день появления находки."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT id, language, collected_at FROM articles WHERE id = ANY(%s::bigint[])", (article_ids,))
        articles = {int(row["id"]): row for row in cur.fetchall()}
        cur.execute("SELECT id, first_seen_at FROM signals WHERE id = ANY(%s::bigint[])", (signal_ids,))
        signals = {int(row["id"]): row["first_seen_at"] for row in cur.fetchall()}
    return articles, signals


def material_candidates(until: str) -> list[dict[str, Any]]:
    """Материалы коллег со статусом «принят» (сигналом ещё не стали), присланные до конца окна."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT id, title, description, url, file_path, created_at FROM colleague_materials"
                    " WHERE signal_id IS NULL AND created_at < %s::date ORDER BY id", (until,))
        return cur.fetchall()
