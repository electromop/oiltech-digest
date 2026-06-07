"""Playwright-based parser for JS-rendered listing and article pages.

Используется для источников с parse_strategy='playwright' — сайты, где контент
рендерится JavaScript или стоит WAF-проверка (Cloudflare challenge и т.п.),
которую lxml/requests не проходят.

Зависимость: playwright (опциональная).
  pip install playwright && playwright install chromium

На сервере с 1.9 ГБ RAM запускать строго последовательно (1 инстанс Chromium):
достаточно выставить PLAYWRIGHT_WORKERS=1 и не включать в общий thread-pool.

Логика извлечения ссылок и дедупликации наследует request_parser, но HTML
листинга и самих статей получается через headless Chromium.
"""

from __future__ import annotations

import logging
from typing import Any

from oiltech_digest.db import repository
from oiltech_digest.config import REQUEST_ARTICLE_LIMIT
from oiltech_digest.ingestion import normalize

logger = logging.getLogger(__name__)


def is_available() -> bool:
    """True если playwright установлен и Chromium доступен."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except ImportError:
        return False


def fetch_rendered(url: str, timeout_ms: int = 30_000, wait_until: str = "networkidle") -> bytes | None:
    """Загрузить страницу через headless Chromium, вернуть HTML как bytes."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("playwright не установлен: pip install playwright && playwright install chromium")
        return None

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            )
            page.goto(url, timeout=timeout_ms, wait_until=wait_until)
            html_content = page.content()
            browser.close()
        return html_content.encode("utf-8") if isinstance(html_content, str) else html_content
    except Exception as exc:  # noqa: BLE001
        logger.warning("playwright fetch failed for %s: %s", url, exc)
        return None


def parse_source(source: dict, max_age_days: int | None = None, article_limit: int = REQUEST_ARTICLE_LIMIT) -> dict:
    """Parse a JS-rendered listing page via Playwright, insert new articles.

    Delegates link extraction and dedup logic to request_parser after fetching
    the rendered DOM — same pipeline, different fetch backend.
    """
    if not is_available():
        logger.error(
            "Playwright not available — source %s (%s) skipped. "
            "Install: pip install playwright && playwright install chromium",
            source.get("name"),
            source.get("id"),
        )
        return _empty_stats()

    from oiltech_digest.ingestion.request_parser import (
        CandidateLink,
        extract_candidate_links,
        insert_candidates,
        parse_article_page,
    )

    listing_url = source.get("listing_url") or source.get("url")
    if not listing_url:
        return _empty_stats()

    content = fetch_rendered(listing_url)
    if content is None:
        return _empty_stats()

    candidates = extract_candidate_links(source, listing_url, content, limit=article_limit)
    if not candidates:
        logger.info("playwright: no candidates found for source %s (%s)", source.get("name"), listing_url)
        return _empty_stats()

    def fetch_rendered_article(candidate: CandidateLink, source: dict) -> dict | None:
        article_content = fetch_rendered(candidate.url)
        if article_content is None:
            return None
        title, published_at, raw_text = parse_article_page(article_content, candidate.title)
        final_published = published_at or candidate.published_at
        if not title or len(raw_text) < 200:
            return None
        return {
            "source_id": source["id"],
            "title": title[:500],
            "url": candidate.url,
            "published_at": final_published,
            "raw_text": raw_text,
            "text_truncated": normalize.is_truncated(raw_text),
            "language": _guess_language(source),
            "content_hash": normalize.compute_content_hash(title, candidate.url),
        }

    stats = insert_candidates(
        source,
        candidates,
        max_age_days=max_age_days,
        article_fetcher=fetch_rendered_article,
    )
    repository.touch_last_parsed(source["id"])
    return stats


def _empty_stats() -> dict[str, Any]:
    return {"added": 0, "attempted": 0, "skipped_old": 0,
            "skipped_irrelevant": 0, "skipped_known": 0}


def _guess_language(source: dict) -> str | None:
    category = (source.get("category") or "").lower()
    if any(marker in category for marker in ("рф", "снг", "россий", "telegram")):
        return "ru"
    if "международ" in category:
        return "en"
    return None
