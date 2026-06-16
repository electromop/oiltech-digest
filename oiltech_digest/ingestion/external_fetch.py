"""External-worker source scraping payloads and result application."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from oiltech_digest.config import REQUEST_ARTICLE_LIMIT
from oiltech_digest.db import repository
from oiltech_digest.ingestion.relevance_filter import should_keep_article


def build_scrape_source_payload(source_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    source = repository.get_source(source_id)
    if source is None:
        raise ValueError("Source not found")
    return {
        "kind": "scrape_source",
        "source": _jsonable_dict(source),
        "max_age_days": payload.get("max_age_days"),
        "article_limit": int(payload.get("article_limit") or REQUEST_ARTICLE_LIMIT),
    }


def process_payload(payload: dict[str, Any]) -> dict[str, Any]:
    source = payload["source"]
    strategy = source.get("parse_strategy")
    if strategy == "playwright":
        return _process_playwright(source, payload)
    if strategy == "request":
        return _process_request(source, payload)
    raise ValueError(f"Unsupported external scrape strategy: {strategy}")


def apply_scrape_result(result: dict[str, Any]) -> dict[str, Any]:
    source_id = int(result["source_id"])
    inserted = duplicates = 0
    for article in result.get("articles") or []:
        if repository.insert_article({**article, "source_id": source_id}):
            inserted += 1
        else:
            duplicates += 1
    repository.touch_last_parsed(source_id)
    repository.update_source_request_state(
        source_id,
        last_seen_article_url=result.get("last_seen_article_url"),
        last_seen_published_at=result.get("last_seen_published_at"),
        last_listing_hash=result.get("last_listing_hash"),
    )
    return {"inserted": inserted, "duplicates": duplicates, "source_id": source_id}


def _process_request(source: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from oiltech_digest.ingestion.request_parser import extract_candidate_links, fetch_article_candidate, _listing_hash
    from oiltech_digest.ingestion.http_client import fetch

    listing_url = source.get("listing_url") or source.get("url")
    content = fetch(listing_url) if listing_url else None
    candidates = extract_candidate_links(source, listing_url, content, limit=int(payload.get("article_limit") or REQUEST_ARTICLE_LIMIT)) if content else []
    return _articles_from_candidates(source, candidates, payload, fetch_article_candidate, _listing_hash)


def _process_playwright(source: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from oiltech_digest.ingestion import normalize
    from oiltech_digest.ingestion.playwright_parser import fetch_rendered
    from oiltech_digest.ingestion.request_parser import CandidateLink, extract_candidate_links, parse_article_page, _listing_hash

    listing_url = source.get("listing_url") or source.get("url")
    content = fetch_rendered(listing_url, settle_ms=5000) if listing_url else None
    candidates = extract_candidate_links(source, listing_url, content, limit=int(payload.get("article_limit") or REQUEST_ARTICLE_LIMIT)) if content else []

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

    return _articles_from_candidates(source, candidates, payload, fetch_rendered_article, _listing_hash)


def _articles_from_candidates(source: dict[str, Any], candidates: list, payload: dict[str, Any], article_fetcher, listing_hash_fn) -> dict[str, Any]:
    cutoff = None
    if payload.get("max_age_days") is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(payload["max_age_days"]))
    stats = {"attempted": 0, "skipped_old": 0, "skipped_irrelevant": 0, "failed_fetch": 0}
    articles: list[dict[str, Any]] = []
    newest_seen_url = candidates[0].url if candidates else None
    newest_seen_published = candidates[0].published_at if candidates else None
    for candidate in candidates:
        if cutoff is not None and candidate.published_at and candidate.published_at < cutoff:
            stats["skipped_old"] += 1
            continue
        article = article_fetcher(candidate, source)
        if article is None:
            stats["failed_fetch"] += 1
            continue
        if cutoff is not None and article.get("published_at") and article["published_at"] < cutoff:
            stats["skipped_old"] += 1
            continue
        pre_filter = should_keep_article(article["title"], article.get("raw_text") or "", source)
        if not pre_filter.keep:
            stats["skipped_irrelevant"] += 1
            continue
        stats["attempted"] += 1
        article = {key: value for key, value in article.items() if key != "source_id"}
        articles.append(_jsonable_dict(article))
    return {
        "external_fetch": True,
        "kind": "scrape_source",
        "source_id": int(source["id"]),
        "strategy": source.get("parse_strategy"),
        "stats": stats,
        "articles": articles,
        "last_seen_article_url": newest_seen_url,
        "last_seen_published_at": newest_seen_published,
        "last_listing_hash": listing_hash_fn(candidates),
    }


def _guess_language(source: dict) -> str | None:
    category = (source.get("category") or "").lower()
    if any(marker in category for marker in ("рф", "снг", "россий", "telegram")):
        return "ru"
    if "международ" in category:
        return "en"
    return None


def _jsonable_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _jsonable(value) for key, value in dict(row).items()}


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value
