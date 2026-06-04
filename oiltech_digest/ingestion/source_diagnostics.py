"""Read-only source diagnostics for CLI/API troubleshooting."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

import feedparser
import requests

from oiltech_digest.config import REQUEST_TIMEOUT
from oiltech_digest.ingestion import request_parser, telegram_parser
from oiltech_digest.ingestion.http_client import _DEFAULT_HEADERS, _mask_proxy, _proxy_for
from oiltech_digest.ingestion.relevance_filter import should_keep_article


@dataclass(frozen=True)
class ProbeResult:
    url: str
    status: int | str
    bytes: int = 0
    seconds: float | None = None
    error: str | None = None
    proxy: str | None = None


def diagnose_source(source: dict, limit: int = 5) -> dict:
    """Return a read-only diagnostic snapshot for one source."""
    strategy = source.get("parse_strategy") or ""
    if strategy == "request":
        return diagnose_request_source(source, limit=limit)
    if strategy == "telegram":
        return diagnose_telegram_source(source, limit=limit)
    if strategy == "rss":
        return diagnose_rss_source(source, limit=limit)
    return {
        "source_id": source.get("id"),
        "source_name": source.get("name"),
        "strategy": strategy or None,
        "verdict": "unsupported_strategy",
    }


def diagnose_request_source(source: dict, limit: int = 5) -> dict:
    listing_url = source.get("listing_url") or source.get("url")
    base = _base_payload(source, "request", listing_url)
    if not listing_url:
        return {**base, "verdict": "missing_listing_url"}

    probe, content = probe_url(listing_url)
    payload = {**base, "listing_probe": asdict(probe)}
    if content is None:
        return {**payload, "verdict": "listing_fetch_failed", "candidates": []}

    candidates = request_parser.extract_candidate_links(source, listing_url, content, limit=limit)
    payload["candidate_count"] = len(candidates)
    payload["candidates"] = [
        {
            "url": item.url,
            "title": item.title,
            "score": item.score,
            "published_at": item.published_at,
        }
        for item in candidates
    ]
    if not candidates:
        return {**payload, "verdict": "no_candidates"}

    article_checks = []
    for candidate in candidates[:limit]:
        article_probe, article_content = probe_url(candidate.url)
        check = {"candidate_url": candidate.url, "article_probe": asdict(article_probe)}
        if article_content is None:
            check["verdict"] = "article_fetch_failed"
            article_checks.append(check)
            continue

        title, published_at, raw_text = request_parser.parse_article_page(article_content, candidate.title)
        pre_filter = should_keep_article(title, raw_text, source)
        check.update(
            {
                "verdict": "ok" if len(raw_text) >= 200 and pre_filter.keep else "article_not_insertable",
                "title": title,
                "published_at": published_at,
                "text_chars": len(raw_text),
                "prefilter_keep": pre_filter.keep,
                "prefilter_noise": pre_filter.matched_noise[:5],
                "prefilter_keywords": pre_filter.matched_keywords[:5],
            }
        )
        article_checks.append(check)

    return {
        **payload,
        "article_checks": article_checks,
        "verdict": "ok" if any(item.get("verdict") == "ok" for item in article_checks) else "no_insertable_articles",
    }


def diagnose_telegram_source(source: dict, limit: int = 5) -> dict:
    preview_url = telegram_parser.preview_url_for_source(source)
    base = _base_payload(source, "telegram", preview_url)
    if not preview_url:
        return {**base, "verdict": "missing_or_invalid_channel_url", "posts": []}

    probe, content = probe_url(preview_url)
    payload = {**base, "preview_probe": asdict(probe)}
    if content is None:
        return {**payload, "verdict": "preview_fetch_failed", "posts": []}

    posts = telegram_parser.extract_posts(content, limit=limit)
    return {
        **payload,
        "post_count": len(posts),
        "posts": [
            {
                "url": post.url,
                "title": post.title,
                "published_at": post.published_at,
                "text_chars": len(post.text),
            }
            for post in posts
        ],
        "verdict": "ok" if posts else "no_posts",
    }


def diagnose_rss_source(source: dict, limit: int = 5) -> dict:
    rss_url = source.get("rss_url")
    base = _base_payload(source, "rss", rss_url)
    if not rss_url:
        return {**base, "verdict": "missing_rss_url", "entries": []}

    probe, content = probe_url(rss_url)
    payload = {**base, "rss_probe": asdict(probe)}
    if content is None:
        return {**payload, "verdict": "rss_fetch_failed", "entries": []}

    feed = feedparser.parse(content)
    entries = []
    for entry in feed.entries[:limit]:
        entries.append({"title": entry.get("title", ""), "url": entry.get("link", "")})
    return {
        **payload,
        "entry_count": len(feed.entries),
        "entries": entries,
        "verdict": "ok" if feed.entries else "no_entries",
    }


def probe_url(url: str, timeout: int = REQUEST_TIMEOUT) -> tuple[ProbeResult, bytes | None]:
    """Single diagnostic GET. Returns HTTP metadata even when content is unusable."""
    host = (urlsplit(url).netloc or "").lower()
    proxies = _proxy_for(host)
    proxy_label = _mask_proxy(next(iter(proxies.values()))) if proxies else None
    try:
        response = requests.get(
            url,
            headers=_DEFAULT_HEADERS,
            timeout=timeout,
            allow_redirects=True,
            proxies=proxies,
        )
        result = ProbeResult(
            url=url,
            status=response.status_code,
            bytes=len(response.content),
            seconds=round(response.elapsed.total_seconds(), 2),
            proxy=proxy_label,
        )
        if response.status_code >= 400:
            return result, None
        return result, response.content
    except requests.RequestException as exc:
        return (
            ProbeResult(
                url=url,
                status="ERR",
                error=f"{type(exc).__name__}: {str(exc)[:160]}",
                proxy=proxy_label,
            ),
            None,
        )


def _base_payload(source: dict, strategy: str, url: str | None) -> dict:
    return {
        "source_id": source.get("id"),
        "source_name": source.get("name"),
        "strategy": strategy,
        "url": url,
    }
