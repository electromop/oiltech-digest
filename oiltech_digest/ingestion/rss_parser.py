"""RSS-парсер: ленты enabled-источников (strategy='rss') → нормализация → articles.

Адаптация прототипного `fetch_feed`/`fetch_all_feeds` под нормализованную схему
(пишем по source_id в таблицу articles). Перед вставкой применяется дешёвый
pre-filter очевидного шума; спорные материалы оставляет AI-релевантности.
Окно свежести опционально (по умолчанию выкл).
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
from oiltech_digest.ingestion import request_parser
from oiltech_digest.ingestion import telegram_parser
from oiltech_digest.ingestion import playwright_parser
from oiltech_digest.ingestion.relevance_filter import should_keep_article

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
        return {"added": 0, "attempted": 0, "skipped_old": 0, "skipped_irrelevant": 0}

    content = fetch(rss_url)
    if content is None:
        return {"added": 0, "attempted": 0, "skipped_old": 0, "skipped_irrelevant": 0}

    feed = feedparser.parse(content)
    cutoff = None
    if max_age_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    language = _guess_language(source)
    added = attempted = skipped_old = skipped_irrelevant = 0

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
        pre_filter = should_keep_article(title, summary, source)
        if not pre_filter.keep:
            skipped_irrelevant += 1
            logger.info(
                "RSS pre-filter skipped %s: %s (%s)",
                source.get("name"),
                title,
                ", ".join(pre_filter.matched_noise[:5]),
            )
            continue

        rec = {
            "source_id": source["id"],
            "title": title[:500],
            "url": url,
            "published_at": published,
            "raw_text": summary or None,
            "text_truncated": normalize.is_truncated(summary or ""),
            "language": language,
            "content_hash": normalize.compute_content_hash(title, url),
            "image_url": normalize.extract_image(entry) or None,
        }
        attempted += 1
        if repository.insert_article(rec):
            added += 1

    repository.touch_last_parsed(source["id"])
    return {
        "added": added,
        "attempted": attempted,
        "skipped_old": skipped_old,
        "skipped_irrelevant": skipped_irrelevant,
    }


def parse_all(max_age_days: int | None = None, workers: int = MAX_WORKERS,
              source_id: int | None = None) -> dict:
    """Параллельно обойти RSS и request-источники. duplicates = attempted - added."""
    sources = repository.get_enabled_sources()
    sources = [s for s in sources if s.get("parse_strategy") in {"rss", "request", "telegram", "playwright"}]
    if source_id is not None:
        sources = [s for s in sources if s["id"] == source_id]

    stats = {
        "added": 0,
        "duplicates": 0,
        "skipped_old": 0,
        "skipped_irrelevant": 0,
        "sources_ok": 0,
        "errors": 0,
    }

    threaded_sources = [s for s in sources if s.get("parse_strategy") != "playwright"]
    playwright_sources = [s for s in sources if s.get("parse_strategy") == "playwright"]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for source in threaded_sources:
            if source.get("parse_strategy") == "request":
                future = pool.submit(request_parser.parse_source, source, max_age_days)
            elif source.get("parse_strategy") == "telegram":
                future = pool.submit(telegram_parser.parse_source, source, max_age_days)
            else:
                future = pool.submit(parse_source, source, max_age_days)
            futures[future] = source
        for fut in as_completed(futures):
            src = futures[fut]
            try:
                r = fut.result()
                stats["added"] += r["added"]
                stats["duplicates"] += r["attempted"] - r["added"]
                stats["skipped_old"] += r["skipped_old"]
                stats["skipped_irrelevant"] += r["skipped_irrelevant"]
                stats["sources_ok"] += 1
            except Exception as e:  # noqa: BLE001 - падение одного источника не валит прогон
                logger.error("Ошибка парсинга %s: %s", src.get("name"), e)
                stats["errors"] += 1

    for source in playwright_sources:
        try:
            r = playwright_parser.parse_source(source, max_age_days)
            stats["added"] += r["added"]
            stats["duplicates"] += r["attempted"] - r["added"]
            stats["skipped_old"] += r["skipped_old"]
            stats["skipped_irrelevant"] += r["skipped_irrelevant"]
            stats["sources_ok"] += 1
        except Exception as e:  # noqa: BLE001 - падение одного источника не валит прогон
            logger.error("Ошибка playwright-парсинга %s: %s", source.get("name"), e)
            stats["errors"] += 1

    return stats
