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
from urllib.parse import unquote, urlsplit

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


def _playwright_proxy_for(url: str) -> dict[str, str] | None:
    """Прокси в playwright-формате ({server, username, password}) для URL, или None.

    Переиспользует логику http_client (PROXY_HOST_OVERRIDES имеет приоритет над
    глобальным PROXY_URL): через прокси идут только хосты из overrides — это и есть
    точечный канал для Hard-WAF (Cloudflare/Akamai), чтобы не гонять платный
    резидентский трафик для всех playwright-источников. Headless Chromium с
    резидентским IP нужен сайтам, блокирующим и по JS-challenge, и по IP-репутации.
    """
    from oiltech_digest.ingestion.http_client import _host, _proxy_for

    proxies = _proxy_for(_host(url))
    if not proxies:
        return None
    raw = proxies.get("https") or proxies.get("http")
    if not raw:
        return None
    parts = urlsplit(raw)
    if not parts.hostname:
        return None
    server = f"{parts.scheme or 'http'}://{parts.hostname}"
    if parts.port:
        server += f":{parts.port}"
    pw_proxy: dict[str, str] = {"server": server}
    if parts.username:
        pw_proxy["username"] = unquote(parts.username)
    if parts.password:
        pw_proxy["password"] = unquote(parts.password)
    return pw_proxy


_BLOCK_STATUSES = {403, 429, 503}


def fetch_rendered(url: str, timeout_ms: int = 30_000, wait_until: str = "domcontentloaded",
                   settle_ms: int = 3500) -> bytes | None:
    """Загрузить страницу через headless Chromium, вернуть HTML как bytes.

    wait_until='domcontentloaded' (НЕ 'networkidle'): networkidle зависает на сайтах
    с постоянной сетевой активностью (аналитика/реклама/websockets) и упирается в
    timeout (наблюдалось на bcg.com). После загрузки даём JS дорендериться фиксированной
    паузой settle_ms (важно для JS-листингов и прохождения лёгких challenge).
    Возвращает None при блокировке (403/429/503) — чтобы не разбирать challenge-страницу.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("playwright не установлен: pip install playwright && playwright install chromium")
        return None

    try:
        with sync_playwright() as pw:
            # --no-sandbox: Chromium под root в Docker; --disable-dev-shm-usage: малый /dev/shm
            # на сервере 1.9 ГБ RAM (иначе краши вкладок). Те же флаги, что в PDF-экспорте.
            launch_kwargs: dict[str, Any] = {
                "headless": True,
                "args": ["--no-sandbox", "--disable-dev-shm-usage"],
            }
            proxy = _playwright_proxy_for(url)
            if proxy:
                launch_kwargs["proxy"] = proxy
                logger.info("playwright %s — через прокси %s", url, proxy.get("server"))
            browser = pw.chromium.launch(**launch_kwargs)
            try:
                page = browser.new_page(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    )
                )
                response = page.goto(url, timeout=timeout_ms, wait_until=wait_until)
                if response is not None and response.status in _BLOCK_STATUSES:
                    logger.warning("playwright %s — статус %s (WAF/блок), пропуск", url, response.status)
                    return None
                if settle_ms:
                    page.wait_for_timeout(settle_ms)
                html_content = page.content()
            finally:
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

    content = fetch_rendered(listing_url, settle_ms=5000)
    candidates = extract_candidate_links(source, listing_url, content, limit=article_limit) if content else []
    if not candidates:
        # JS-листинг мог не успеть дорендерить ссылки за первый проход (наблюдалось на
        # bakerhughes.com: «то 6, то 0 кандидатов»). Даём ещё одну попытку с большим settle.
        logger.info("playwright: 0 кандидатов на первом проходе для %s — ретрай с большим settle", source.get("name"))
        content = fetch_rendered(listing_url, settle_ms=12000)
        candidates = extract_candidate_links(source, listing_url, content, limit=article_limit) if content else []
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
