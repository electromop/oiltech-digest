"""Что агент «Отбор» видит о публикации (#130, #131, спека #128): статья ленты, находка радара или материал коллеги — те
же поля, что у строки пакета эксперту (otbor_pack), чтобы экзамен (#133) подавал агенту манифест без переделки. У
публикации, которую вернула «Карточка» (#132), — ещё её факт о старом событии.

ВНИМАНИЕ про тесты: соединение — `connection.get_connection()` атрибутом модуля (как в signal_card_store):
изоляция тестов держится на подмене этого атрибута.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from psycopg.rows import dict_row

from oiltech_digest.db import connection, feed_signals
from oiltech_digest.db.otbor_pack_store import LINK_PUBLISHED_SQL, evidence_links
from oiltech_digest.db.repository import SIGNAL_ORIGIN_MATERIAL, radar_origin_sql

# Ссылок находки во входе «Отбора» — не больше стольких (спека #128, как в пакете эксперту).
LINKS_LIMIT = 8

# Что нашла «Карточка» у возвращённой ею публикации (#132): факт идёт во вход «Отбора», пока решения нет.
_RETURN_FACT_SQL = "CASE WHEN {alias}.otbor_decision IS NULL THEN {alias}.otbor_return_fact END"


def article_inputs(article_ids: Sequence[int]) -> list[dict[str, Any]]:
    """Статьи так, как их видит модель, — те же поля, что у статьи ленты в пакете эксперту (otbor_pack_store
    .feed_candidates): экзамен (#133) подаст агенту манифест пакета без переделки. У статьи, которую вернула
    «Карточка» (#132), — её факт."""
    ids = sorted({int(value) for value in article_ids})
    if not ids:
        return []
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT a.id AS article_id, a.title, a.raw_text AS text, a.language,
                   COALESCE(a.published_at, a.collected_at) AS published, a.collected_at AS received,
                   src.name AS publisher, {_RETURN_FACT_SQL.format(alias="os")} AS card_fact
            FROM articles a
            JOIN sources src ON src.id = a.source_id
            LEFT JOIN signals os ON os.signal_key = '{feed_signals.FEED_KEY_PREFIX}' || a.id
            WHERE a.id = ANY(%(ids)s)
            ORDER BY a.id
            """,
            {"ids": ids},
        )
        return cur.fetchall()


def signal_inputs(signal_ids: Sequence[int]) -> list[dict[str, Any]]:
    """Находки и материалы так, как их видит модель (#131), — поля строки радара и материала в пакете эксперту
    (otbor_pack): у находки главная публикация с текстом издателя (статья корпуса или докачанная страница — раньше
    сводки модели поиска, затем по силе, как у «Карточки»), дата публикации — только издателя (у ссылки поиска
    «режима ChatGPT» её дала модель поиска, во вход она не идёт), поступление — день находки, ссылки находки — если их
    больше одной; у материала — строка материала как публикация (colleague_materials.as_publication)."""
    from oiltech_digest.colleague_materials import as_publication
    from oiltech_digest.signal_research import EVIDENCE_SOURCE

    ids = sorted({int(value) for value in signal_ids})
    if not ids:
        return []
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT DISTINCT ON (s.id)
                   s.id AS signal_id, s.origin, s.first_seen_at AS received, a.language,
                   COALESCE(a.title, e.title) AS title, COALESCE(a.url, e.source_url) AS url,
                   COALESCE(a.raw_text, e.extracted_fact) AS text, {LINK_PUBLISHED_SQL} AS published,
                   src.name AS publisher_source, {_RETURN_FACT_SQL.format(alias="s")} AS card_fact
            FROM signals s
            JOIN signal_evidence e ON e.signal_id = s.id
            LEFT JOIN articles a ON a.id = e.article_id
            LEFT JOIN sources src ON src.id = a.source_id
            WHERE s.id = ANY(%(ids)s) AND {radar_origin_sql('s')}
            ORDER BY s.id,
                     (length(btrim(COALESCE(a.raw_text, ''))) > 0
                      OR e.raw_payload_json->>'full_text_fetched' = 'true') DESC,
                     e.strength DESC, e.id
            """,
            {"ids": ids, "search_source": EVIDENCE_SOURCE},
        )
        rows = cur.fetchall()
        cur.execute(
            f"""
            SELECT s.id AS signal_id, s.origin, m.id AS material_id, m.title, m.description, m.url, m.file_path,
                   m.created_at AS published, m.created_at AS received,
                   {_RETURN_FACT_SQL.format(alias="s")} AS card_fact
            FROM signals s
            JOIN colleague_materials m ON m.signal_id = s.id
            WHERE s.id = ANY(%(ids)s) AND s.origin = '{SIGNAL_ORIGIN_MATERIAL}'
            """,
            {"ids": ids},
        )
        rows += [as_publication(row) for row in cur.fetchall()]
    links = evidence_links([int(row["signal_id"]) for row in rows if row["origin"] != SIGNAL_ORIGIN_MATERIAL])
    for row in rows:
        found = links.get(int(row["signal_id"])) or []
        row["links"] = found[:LINKS_LIMIT] if len(found) > 1 else []
    return sorted(rows, key=lambda row: row["signal_id"])


def links_in_input(signal_ids: Sequence[int]) -> dict[int, int]:
    """Сколько ссылок у каждой находки во входе «Отбора» (0 — ссылка одна): «разные события в одной находке» —
    причина только у находки с несколькими ссылками. Ядро считает сама, ответ воркера — недоверенный ввод."""
    found = evidence_links(sorted({int(value) for value in signal_ids}))
    return {int(signal_id): (min(len(rows), LINKS_LIMIT) if len(rows) > 1 else 0) for signal_id, rows in found.items()}
