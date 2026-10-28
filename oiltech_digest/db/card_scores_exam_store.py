"""Чтение базы для экзамена «Карточки» (#133): контроль у строк пакета и «сильные сверх пакета».

Экзамен идёт там, где доступна модель, а база — на ядре: card-scores-exam-control выгружает отсюда JSON
(card_scores_exam.control_values). Здесь только запросы. Текст и издатель находки — по правилам пакета «Отбора»
(otbor_pack_store): текст издателя (статья корпуса или докачанная страница), у ссылки «режима ChatGPT» нет даты.

ВНИМАНИЕ про тесты: соединение — `connection.get_connection()` атрибутом модуля (как в signal_card_store):
изоляция тестов держится на подмене этого атрибута.
"""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row

from oiltech_digest import config
from oiltech_digest.db import connection, marks
from oiltech_digest.db.otbor_pack_store import LINK_PUBLISHED_SQL, PUBLISHER_TEXT_SQL
from oiltech_digest.db.repository import radar_origin_sql

# Карточка радара, заведённая по списку заказчика (ТОП, 06-07.10): так помечена её ссылка.
CUSTOMER_LIST = "customer_list"
# Эталон заказчика в памяти радара (import-reference-signals, signal_reference.REFERENCE_ORIGIN).
REFERENCE_ORIGIN = "customer_reference"
# Ключ адреса, как signal_reference._url_key: без схемы, «www.», запроса, якоря и косой черты в конце.
_URL_KEY_SQL = ("rtrim(regexp_replace(split_part(split_part(regexp_replace(lower(btrim({url})), '^https?://', ''),"
                " '#', 1), '?', 1), '^www\\.', ''), '/')")


def feed_control(article_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Тег и балл ленты у статей: тег — как его поставила лента (корень находит вызывающий), балл — итог профиля."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.id AS article_id, t.tag_id, s.total_score AS score
            FROM articles a
            LEFT JOIN article_tags t ON t.article_id = a.id
            LEFT JOIN article_scores s ON s.article_id = a.id
            WHERE a.id = ANY(%s::bigint[])
            """,
            (article_ids,),
        )
        return {int(row["article_id"]): row for row in cur.fetchall()}


def radar_control(signal_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Тема и балл судьи у находок радара."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT id AS signal_id, theme, score FROM signals WHERE id = ANY(%s::bigint[])", (signal_ids,))
        return {int(row["signal_id"]): row for row in cur.fetchall()}


def reference_urls() -> list[str]:
    """Ссылки эталона заказчика в памяти радара."""
    with connection.get_connection() as conn:
        rows = conn.execute(
            "SELECT facts_json->>'source_url' FROM signal_agent_memory"
            " WHERE facts_json->>'origin' = %s AND COALESCE(facts_json->>'source_url', '') <> ''",
            (REFERENCE_ORIGIN,),
        ).fetchall()
    return [row[0] for row in rows]


def strong_radar(reference_keys: list[str]) -> list[dict[str, Any]]:
    """Находки радара (не дубли), которые человек выбрал «в дайджест» или которые входят в эталонный ТОП заказчика:
    заведены по его списку или их ссылка — среди ссылок эталона в памяти радара (reference_keys — ключи адресов).
    Главная публикация — самая сильная ссылка с текстом издателя; нет такой — поля публикации пустые (текст находки
    без докачки — сводка модели поиска, не издателя)."""
    from oiltech_digest.signal_research import EVIDENCE_SOURCE

    top = (f"EXISTS (SELECT 1 FROM signal_evidence x WHERE x.signal_id = s.id"
           f" AND (x.raw_payload_json->>'origin' = %(customer_list)s"
           f" OR {_URL_KEY_SQL.format(url='x.source_url')} = ANY(%(reference)s::text[])))")
    digest = marks.signal_marked_sql("s.id", "digest")
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT s.id AS signal_id, {digest} AS digest, {top} AS top, s.first_seen_at AS received,
                   s.signal_category, s.industry, s.oilfield_relevance, s.oilfield_application, m.*
            FROM signals s
            LEFT JOIN LATERAL (
                SELECT e.id AS evidence_id, a.id AS article_id, a.language, COALESCE(a.title, e.title) AS title,
                       COALESCE(a.url, e.source_url) AS url, COALESCE(a.raw_text, e.extracted_fact) AS text,
                       {LINK_PUBLISHED_SQL} AS published, src.name AS publisher_source,
                       COALESCE(src.source_type, '') AS publisher_type
                FROM signal_evidence e
                LEFT JOIN articles a ON a.id = e.article_id
                LEFT JOIN sources src ON src.id = a.source_id
                WHERE e.signal_id = s.id AND {PUBLISHER_TEXT_SQL}
                ORDER BY e.strength DESC, e.id
                LIMIT 1
            ) m ON TRUE
            WHERE {radar_origin_sql('s')} AND s.merged_into_signal_id IS NULL AND ({digest} OR {top})
            ORDER BY s.id
            """,
            {"search_source": EVIDENCE_SOURCE, "customer_list": CUSTOMER_LIST, "reference": reference_keys,
             "min_chars": config.MIN_ARTICLE_TEXT_CHARS},
        )
        return cur.fetchall()


def strong_feed() -> list[dict[str, Any]]:
    """Статьи ленты, выбранные «в дайджест», с текстом издателя; категория — их сигнала ленты, если он есть."""
    digest = marks.article_marked_sql("a.id", "digest")
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT a.id AS article_id, a.title, a.url, a.raw_text AS text, a.language,
                   COALESCE(a.published_at, a.collected_at) AS published, a.collected_at AS received,
                   src.name AS publisher_source, COALESCE(src.source_type, '') AS publisher_type,
                   fs.signal_category, fs.industry, fs.oilfield_relevance, fs.oilfield_application,
                   (a.full_text_status IS DISTINCT FROM 'mismatch'
                    AND length(btrim(COALESCE(a.raw_text, ''))) >= %(min_chars)s) AS has_text
            FROM articles a
            JOIN sources src ON src.id = a.source_id
            LEFT JOIN LATERAL (SELECT f.signal_category, f.industry, f.oilfield_relevance, f.oilfield_application
                               FROM signals f WHERE f.origin = 'feed' AND f.origin_article_id = a.id
                               ORDER BY f.id LIMIT 1) fs ON TRUE
            WHERE {digest}
            ORDER BY a.id
            """,
            {"min_chars": config.MIN_ARTICLE_TEXT_CHARS},
        )
        return cur.fetchall()
