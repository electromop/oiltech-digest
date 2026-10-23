"""Мост «лента → сигнал» (#95, ADR 0003): принятая отбором статья — сигнал категории «бизнес»
происхождения «лента», сама статья — его публикация; перепечатка — публикация сигнала главной.

Без вызовов модели: сигнал повторяет то, что у статьи уже записано, — заголовок, суть, тему,
балл. Его зовут те же записи, что пишут это у статьи (отбор, суть, перевод заголовка, тег,
балл, пересчёт баллов, пометка перепечатки), и разовая команда по истории. Одна функция на
все случаи: синхронизация идемпотентна, повторный вызов второй сигнал не создаёт.

Ссылка радара важнее ссылки ленты: адрес публикации в signal_evidence уникален, и если его
уже держит карточка радара, ссылка остаётся у неё — иначе сигналы ленты меняли бы радар.
Такое событие до агента дедупа — два сигнала; сигнал ленты виден по своей статье
(repository._BUSINESS_VISIBLE_SQL), а не по ссылке."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
import logging

from oiltech_digest import feed_window
from oiltech_digest.db import connection
from oiltech_digest.db.repository import SIGNAL_ORIGIN_FEED, SYSTEM_TAG_UNCLASSIFIED, signal_publication_count_sql

logger = logging.getLogger(__name__)

FEED_CATEGORY = "business"
# С какой даты сбора статьи мост заводит сигналы сам: история в единое хранилище — с августа
# 2026 (#93), и обслуживание старых статей (пересчёт баллов, перепроверка, ремонт заголовков)
# не должно заводить сигналы задним числом. Заведённый сигнал обновляется при любой дате.
FEED_SIGNALS_SINCE = date(2026, 8, 1)
# Ключ сигнала ленты — по главной статье: тот же у повторной обработки и у разовой команды.
FEED_KEY_PREFIX = "feed:"
REPRINT_MERGE_REASON = "перепечатка статьи"


def _key_sql(article_sql: str) -> str:
    return f"('{FEED_KEY_PREFIX}' || {article_sql})"


# Принятая статья, которая не перепечатка, — свой сигнал. Тема — корневая тематика тега
# статьи (у дочернего тега — его корень), без тега — «не классифицировано»: тема у сигнала
# обязательна, а тег приходит позже отбора. Балл с подпунктами — как у статьи. Принятая — по
# решению агента «Отбор» (#130), без него — по гейту: отклонённую и ждущую «Отбора» статью
# заводит хранилище «Отбора» (otbor_store), а мост ей суть, тему и балл не пишет.
_UPSERT_SIGNALS = f"""
INSERT INTO signals (
  signal_key, origin, origin_article_id, signal_category, title, title_ru, theme, summary,
  maturity, score, score_profile, criteria_snapshot, score_items_json, first_seen_at, last_seen_at
)
SELECT {_key_sql('a.id')}, '{SIGNAL_ORIGIN_FEED}', a.id, '{FEED_CATEGORY}', a.title, c.title_ru,
       COALESCE(root.name, t.name, %(unclassified)s), c.summary,
       'watch', COALESCE(sc.total_score, 0), sc.profile, sc.criteria_snapshot, items.items,
       COALESCE(a.collected_at, now()), COALESCE(a.collected_at, now())
FROM feed_scope f
JOIN articles a ON a.id = f.id
JOIN article_cards c ON c.article_id = a.id
LEFT JOIN article_tags at ON at.article_id = a.id
LEFT JOIN tags t ON t.id = at.tag_id
LEFT JOIN tags root ON root.id = t.parent_id
LEFT JOIN article_scores sc ON sc.article_id = a.id
LEFT JOIN LATERAL (
  SELECT jsonb_agg(jsonb_build_object('criterion_id', i.criterion_id, 'final_score', i.final_score,
                                      'rationale', i.rationale) ORDER BY i.criterion_id) AS items
  FROM article_score_items i WHERE i.article_score_id = sc.id
) items ON TRUE
WHERE {feed_window.relevance_sql('a', 'c')} IS TRUE
  AND NOT EXISTS (SELECT 1 FROM article_reprints ar WHERE ar.article_id = a.id)
  AND (%(create_since)s::date IS NULL OR a.collected_at >= %(create_since)s::date
       OR EXISTS (SELECT 1 FROM signals x WHERE x.signal_key = {_key_sql('a.id')}))
ON CONFLICT (signal_key) DO UPDATE SET
  -- Категорию не трогаем: её поменяет человек через сервис ОС, а повторная обработка статьи
  -- не должна возвращать сигнал в «бизнес».
  title = EXCLUDED.title,
  title_ru = EXCLUDED.title_ru,
  theme = EXCLUDED.theme,
  summary = EXCLUDED.summary,
  score = EXCLUDED.score,
  score_profile = EXCLUDED.score_profile,
  criteria_snapshot = EXCLUDED.criteria_snapshot,
  score_items_json = EXCLUDED.score_items_json,
  updated_at = now()
WHERE (signals.title, signals.title_ru, signals.theme, signals.summary, signals.score,
       signals.score_profile, signals.criteria_snapshot, signals.score_items_json)
  IS DISTINCT FROM
      (EXCLUDED.title, EXCLUDED.title_ru, EXCLUDED.theme, EXCLUDED.summary, EXCLUDED.score,
       EXCLUDED.score_profile, EXCLUDED.criteria_snapshot, EXCLUDED.score_items_json)
RETURNING (xmax = 0) AS inserted
"""

# Пометку перепечатки сняли — статья снова главная своего сигнала.
_UNMERGE_FORMER_REPRINTS = f"""
UPDATE signals s
SET merged_into_signal_id = NULL, merge_reason = NULL, updated_at = now()
FROM feed_scope f
WHERE s.signal_key = {_key_sql('f.id')}
  AND s.merged_into_signal_id IS NOT NULL
  AND s.merge_reason LIKE '{REPRINT_MERGE_REASON}%%'
  AND NOT EXISTS (SELECT 1 FROM article_reprints ar WHERE ar.article_id = f.id)
"""

# Свой сигнал у статьи, ставшей перепечаткой, — дубль сигнала главной (тот же механизм
# склейки, что у радара: merged_into_signal_id, дубль не удаляется).
_MERGE_REPRINTS = f"""
UPDATE signals d
SET merged_into_signal_id = p.id,
    merge_reason = '{REPRINT_MERGE_REASON} ' || ar.primary_id,
    updated_at = now()
FROM feed_scope f
JOIN article_reprints ar ON ar.article_id = f.id
JOIN signals p ON p.signal_key = {_key_sql('ar.primary_id')}
WHERE d.signal_key = {_key_sql('f.id')}
  AND p.id <> d.id
  AND d.merged_into_signal_id IS DISTINCT FROM p.id
"""

# Публикация: статья — к своему сигналу, перепечатка — к сигналу главной. Ссылку, которую
# держит карточка радара, не забираем и не переписываем (см. описание модуля).
_UPSERT_PUBLICATIONS = f"""
INSERT INTO signal_evidence (
  signal_id, article_id, source_url, title, title_ru, publisher, published_at,
  evidence_type, summary_ru, strength, raw_payload_json
)
SELECT DISTINCT ON (a.url)
       target.id, a.id, a.url, a.title, c.title_ru, src.name, a.published_at,
       'feed_article', c.summary, CASE WHEN ar.article_id IS NULL THEN 1 ELSE 0.5 END,
       jsonb_build_object('evidence_source', 'feed', 'article_id', a.id)
FROM feed_scope f
JOIN articles a ON a.id = f.id
JOIN sources src ON src.id = a.source_id
LEFT JOIN article_cards c ON c.article_id = a.id
LEFT JOIN article_reprints ar ON ar.article_id = a.id
JOIN signals target ON target.signal_key = {_key_sql('COALESCE(ar.primary_id, a.id)')}
WHERE COALESCE(a.url, '') <> ''
  AND (ar.article_id IS NULL OR {feed_window.selected_sql('a', 'c')})
ORDER BY a.url, (ar.article_id IS NULL) DESC, a.id
ON CONFLICT (source_url) DO UPDATE SET
  signal_id = EXCLUDED.signal_id,
  article_id = EXCLUDED.article_id,
  title = EXCLUDED.title,
  title_ru = EXCLUDED.title_ru,
  publisher = EXCLUDED.publisher,
  published_at = EXCLUDED.published_at,
  evidence_type = EXCLUDED.evidence_type,
  summary_ru = EXCLUDED.summary_ru,
  strength = EXCLUDED.strength,
  updated_at = now()
WHERE (signal_evidence.signal_id IS NULL
       OR EXISTS (SELECT 1 FROM signals owner
                  WHERE owner.id = signal_evidence.signal_id AND owner.origin = '{SIGNAL_ORIGIN_FEED}'))
  AND (signal_evidence.signal_id, signal_evidence.article_id, signal_evidence.title,
       signal_evidence.title_ru, signal_evidence.publisher, signal_evidence.published_at,
       signal_evidence.summary_ru, signal_evidence.strength)
  IS DISTINCT FROM
      (EXCLUDED.signal_id, EXCLUDED.article_id, EXCLUDED.title, EXCLUDED.title_ru,
       EXCLUDED.publisher, EXCLUDED.published_at, EXCLUDED.summary_ru, EXCLUDED.strength)
"""

_REFRESH_COUNTS = f"""
UPDATE signals s
SET evidence_count = n.count, updated_at = now()
FROM (
  SELECT s2.id, {signal_publication_count_sql('s2')} AS count
  FROM signals s2
  JOIN feed_scope f ON s2.signal_key = {_key_sql('f.id')}
) n
WHERE s.id = n.id AND s.evidence_count <> n.count
"""


def sync_feed_signals(
    article_ids: Sequence[int] | None = None,
    *,
    collected_since: date | str | None = None,
    collected_until: date | str | None = None,
    create_since: date | None = None,
    dry_run: bool = False,
) -> dict:
    """Сигналы ленты для статей — по номерам или по дате сбора [since, until).

    В охват входят и связанные перепечатками статьи: принятая главная забирает публикации
    своих копий, копия — к сигналу главной. create_since — новые сигналы только статьям,
    собранным с этой даты (уже заведённые обновляются всегда). dry_run — всё в одной
    транзакции и откат: числа те же, что дал бы настоящий прогон, но в базе ничего не меняется."""
    if article_ids is None and collected_since is None and collected_until is None:
        raise ValueError("Нужны номера статей или период сбора")
    clauses: list[str] = []
    params: dict = {"unclassified": SYSTEM_TAG_UNCLASSIFIED, "create_since": create_since}
    if article_ids is not None:
        ids = sorted({int(value) for value in article_ids})
        if not ids:
            return {"signals_created": 0, "signals_updated": 0, "reprints_merged": 0,
                    "reprints_unmerged": 0, "publications": 0, "dry_run": dry_run}
        clauses.append("a.id = ANY(%(ids)s)")
        params["ids"] = ids
    if collected_since is not None:
        clauses.append("a.collected_at >= %(since)s::date")
        params["since"] = str(collected_since)
    if collected_until is not None:
        clauses.append("a.collected_at < %(until)s::date")
        params["until"] = str(collected_until)
    with connection.get_connection() as conn:
        conn.execute(
            f"CREATE TEMP TABLE feed_scope ON COMMIT DROP AS "
            f"SELECT a.id FROM articles a WHERE {' AND '.join(clauses)}",
            params,
        )
        conn.execute(
            """
            INSERT INTO feed_scope
            SELECT ar.article_id FROM article_reprints ar JOIN feed_scope f ON f.id = ar.primary_id
            UNION
            SELECT ar.primary_id FROM article_reprints ar JOIN feed_scope f ON f.id = ar.article_id
            EXCEPT
            SELECT id FROM feed_scope
            """
        )
        upserted = conn.execute(_UPSERT_SIGNALS, params).fetchall()
        unmerged = conn.execute(_UNMERGE_FORMER_REPRINTS).rowcount
        merged = conn.execute(_MERGE_REPRINTS).rowcount
        publications = conn.execute(_UPSERT_PUBLICATIONS).rowcount
        conn.execute(_REFRESH_COUNTS)
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    return {
        "signals_created": sum(1 for row in upserted if row[0]),
        "signals_updated": sum(1 for row in upserted if not row[0]),
        "reprints_merged": int(merged or 0),
        "reprints_unmerged": int(unmerged or 0),
        "publications": int(publications or 0),
        "dry_run": dry_run,
    }


def sync_quietly(article_ids: Sequence[int]) -> None:
    """Синхронизация из записи статьи: сбой моста не роняет обработку ленты — статья уже
    записана, а сигнал доведёт следующая запись или разовая команда (`sync-feed-signals`)."""
    try:
        sync_feed_signals(article_ids, create_since=FEED_SIGNALS_SINCE)
    except Exception:  # noqa: BLE001 - мост вторичен по отношению к записи статьи
        logger.exception("feed_signal_sync_failed article_ids=%s", list(article_ids)[:20])
