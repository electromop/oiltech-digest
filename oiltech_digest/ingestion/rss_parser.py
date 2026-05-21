"""RSS-парсер: ленты enabled-источников (strategy='rss') → нормализация → articles.

Адаптация прототипного `fetch_feed`/`fetch_all_feeds` под нормализованную схему
(пишем по source_id в таблицу articles). Тематический фильтр НЕ применяется
(релевантность — будущий скоринг). Окно свежести опционально (по умолчанию выкл).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import feedparser

from oiltech_digest.config import MAX_WORKERS
from oiltech_digest.db import repository
from oiltech_digest.ingestion import normalize
from oiltech_digest.ingestion.http_client import fetch

logger = logging.getLogger(__name__)


def _guess_language(source: dict) -> str | None:
    """Грубая эвристика языка по группе источника. Точное определение — будущий этап."""
    category = (source.get("category") or "").lower()
    if any(marker in category for marker in ("рф", "снг", "россий", "telegram")):
        return "ru"
    if "международ" in category:
        return "en"
    return None  # неизвестно → NULL


def parse_source(source: dict, max_age_days: int | None = None) -> dict:
    """Скачать и распарсить одну ленту, вставить новые статьи. Вернуть метрики источника."""
    rss_url = source.get("rss_url")
    if not rss_url:
        return {"added": 0, "attempted": 0, "skipped_old": 0}

    content = fetch(rss_url)
    if content is None:
        return {"added": 0, "attempted": 0, "skipped_old": 0}

    feed = feedparser.parse(content)
    cutoff = None
    if max_age_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    language = _guess_language(source)
    added = attempted = skipped_old = 0

    for entry in feed.entries:
        title = normalize.clean_html(entry.get("title", ""))
        url = entry.get("link", "")
        if not title or not url:
            continue

        published = normalize.parse_date(entry)
        if cutoff is not None and published is not None and published < cutoff:
            skipped_old += 1
            continue

        summary = normalize.clean_html(entry.get("summary", entry.get("description", "")))
        rec = {
            "source_id": source["id"],
            "title": title[:500],
            "url": url,
            "published_at": published,
            "raw_text": summary or None,
            "language": language,
            "content_hash": normalize.compute_content_hash(title, url),
        }
        attempted += 1
        if repository.insert_article(rec):
            added += 1

    repository.touch_last_parsed(source["id"])
    return {"added": added, "attempted": attempted, "skipped_old": skipped_old}


def parse_all(max_age_days: int | None = None, workers: int = MAX_WORKERS,
              source_id: int | None = None) -> dict:
    """Параллельно обойти все RSS-источники. duplicates = attempted - added."""
    sources = repository.get_enabled_sources("rss")
    if source_id is not None:
        sources = [s for s in sources if s["id"] == source_id]

    stats = {"added": 0, "duplicates": 0, "skipped_old": 0, "sources_ok": 0, "errors": 0}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(parse_source, s, max_age_days): s for s in sources}
        for fut in as_completed(futures):
            src = futures[fut]
            try:
                r = fut.result()
                stats["added"] += r["added"]
                stats["duplicates"] += r["attempted"] - r["added"]
                stats["skipped_old"] += r["skipped_old"]
                stats["sources_ok"] += 1
            except Exception as e:  # noqa: BLE001 - падение одного источника не валит прогон
                logger.error("Ошибка парсинга %s: %s", src.get("name"), e)
                stats["errors"] += 1

    return stats
