"""Listing-page scraper for sources that do not expose RSS."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import re
from urllib.parse import urljoin, urlsplit

from dateutil import parser as dateparser
from lxml import html

from oiltech_digest.config import REQUEST_ARTICLE_LIMIT
from oiltech_digest.db import repository
from oiltech_digest.ingestion import normalize
from oiltech_digest.ingestion.article_fetcher import extract_main_text
from oiltech_digest.ingestion.http_client import fetch
from oiltech_digest.ingestion.relevance_filter import should_keep_article

logger = logging.getLogger(__name__)

_ARTICLE_HINT_RE = re.compile(
    r"(news|press|media|article|articles|blog|post|posts|story|stories|"
    r"insight|insights|publication|publications|release|releases|updates?)",
    re.I,
)
_BAD_LINK_RE = re.compile(
    r"(contact|about|privacy|terms|career|job|vacan|event|webinar|podcast|"
    r"subscribe|signin|login|register|mailto:|javascript:|#)",
    re.I,
)
_DATE_HINT_RE = re.compile(r"/20\d{2}/\d{1,2}/\d{1,2}/")
_DATE_TEXT_RE = re.compile(r"\b(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})\b")


@dataclass(frozen=True)
class CandidateLink:
    url: str
    title: str
    score: int
    published_at: datetime | None = None


def parse_source(source: dict, max_age_days: int | None = None, article_limit: int = REQUEST_ARTICLE_LIMIT) -> dict:
    listing_url = source.get("listing_url") or source.get("url")
    if not listing_url:
        return _empty_stats()

    content = fetch(listing_url)
    if content is None:
        return _empty_stats()

    candidates = extract_candidate_links(source, listing_url, content, limit=article_limit)
    listing_hash = _listing_hash(candidates)
    if candidates and source.get("last_listing_hash") and listing_hash == source.get("last_listing_hash"):
        repository.touch_last_parsed(source["id"])
        return {
            "added": 0,
            "attempted": 0,
            "skipped_old": 0,
            "skipped_irrelevant": 0,
            "skipped_known": len(candidates),
        }
    cutoff = None
    if max_age_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    last_seen_url = source.get("last_seen_article_url") or ""
    last_seen_published = source.get("last_seen_published_at")
    if isinstance(last_seen_published, str):
        last_seen_published = _parse_datetime(last_seen_published)

    added = attempted = skipped_old = skipped_irrelevant = skipped_known = 0
    known_streak = 0
    newest_seen_url: str | None = None
    newest_seen_published: datetime | None = None

    for candidate in candidates:
        if newest_seen_url is None:
            newest_seen_url = candidate.url
            newest_seen_published = candidate.published_at

        if last_seen_url and candidate.url == last_seen_url:
            known_streak += 1
            skipped_known += 1
            break
        if repository.article_exists(candidate.url):
            known_streak += 1
            skipped_known += 1
            if known_streak >= 3:
                break
            continue
        known_streak = 0

        if cutoff is not None and candidate.published_at and candidate.published_at < cutoff:
            skipped_old += 1
            continue
        if last_seen_published and candidate.published_at and candidate.published_at <= last_seen_published:
            skipped_old += 1
            continue

        article = fetch_article_candidate(candidate, source)
        if article is None:
            continue
        if cutoff is not None and article.get("published_at") and article["published_at"] < cutoff:
            skipped_old += 1
            continue
        pre_filter = should_keep_article(article["title"], article.get("raw_text") or "", source)
        if not pre_filter.keep:
            skipped_irrelevant += 1
            continue

        attempted += 1
        if repository.insert_article(article):
            added += 1

    repository.touch_last_parsed(source["id"])
    repository.update_source_request_state(
        source["id"],
        last_seen_article_url=newest_seen_url,
        last_seen_published_at=newest_seen_published,
        last_listing_hash=listing_hash,
    )
    return {
        "added": added,
        "attempted": attempted,
        "skipped_old": skipped_old,
        "skipped_irrelevant": skipped_irrelevant,
        "skipped_known": skipped_known,
    }


def extract_candidate_links(source: dict | str, listing_url: str | bytes, content: bytes | str | None = None,
                            limit: int = 12) -> list[CandidateLink]:
    if isinstance(source, str):
        source_dict = {}
        home_url = source
        body = listing_url
    else:
        source_dict = source
        home_url = str(listing_url)
        body = content
    if body is None:
        return []

    try:
        doc = html.fromstring(body)
    except (ValueError, TypeError):
        return []

    explicit = _extract_candidates_with_selector(doc, home_url, source_dict)
    if explicit:
        return explicit[:limit]

    base_host = (urlsplit(home_url).netloc or "").lower()
    seen: set[str] = set()
    candidates: list[CandidateLink] = []

    for node in doc.xpath("//a[@href]"):
        item = _build_candidate_from_anchor(home_url, base_host, node)
        if item is None or item.url in seen:
            continue
        seen.add(item.url)
        candidates.append(item)

    candidates.sort(key=lambda item: (-item.score, item.published_at is None, item.published_at or datetime.min.replace(tzinfo=timezone.utc), item.url))
    return candidates[:limit]


def fetch_article_candidate(candidate: CandidateLink, source: dict) -> dict | None:
    content = fetch(candidate.url)
    if content is None:
        return None
    title, published_at, raw_text = parse_article_page(content, candidate.title)
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


def parse_article_page(content: bytes | str, fallback_title: str = "") -> tuple[str, datetime | None, str]:
    try:
        doc = html.fromstring(content)
    except (ValueError, TypeError):
        return fallback_title, None, ""

    title = _first_non_empty(
        doc.xpath("string(//meta[@property='og:title']/@content)"),
        doc.xpath("string(//meta[@name='twitter:title']/@content)"),
        doc.xpath("string(//h1[1])"),
        doc.xpath("string(//title)"),
        fallback_title,
    )
    title = normalize.clean_html(title)[:500]

    published_at = _parse_datetime(
        _first_non_empty(
            doc.xpath("string(//meta[@property='article:published_time']/@content)"),
            doc.xpath("string(//meta[@name='pubdate']/@content)"),
            doc.xpath("string(//time[1]/@datetime)"),
            _guess_date_from_text(doc.xpath("string(//time[1])")),
        )
    )

    raw_text = extract_main_text(content)
    if len(raw_text) < 200:
        raw_text = normalize.clean_html(doc.text_content())
    return title, published_at, raw_text


def _extract_candidates_with_selector(doc, listing_url: str, source: dict) -> list[CandidateLink]:
    listing_selector = source.get("listing_selector")
    link_selector = source.get("article_link_selector")
    date_selector = source.get("article_date_selector")
    if not any((listing_selector, link_selector, date_selector)):
        return []

    base_host = (urlsplit(listing_url).netloc or "").lower()
    nodes = _nodes_by_selector(doc, listing_selector) if listing_selector else []
    if not nodes:
        nodes = [doc]

    seen: set[str] = set()
    candidates: list[CandidateLink] = []
    for node in nodes:
        link_nodes = _nodes_by_selector(node, link_selector) if link_selector else node.xpath(".//a[@href]")
        for link in link_nodes:
            item = _build_candidate_from_anchor(listing_url, base_host, link)
            if item is None or item.url in seen:
                continue
            published_at = item.published_at
            if date_selector:
                date_nodes = _nodes_by_selector(node, date_selector)
                published_at = _parse_datetime(_first_non_empty(*[_node_text(n) for n in date_nodes])) or published_at
            seen.add(item.url)
            candidates.append(CandidateLink(item.url, item.title, item.score + 2, published_at))
    candidates.sort(key=lambda item: (-item.score, item.url))
    return candidates


def _build_candidate_from_anchor(home_url: str, base_host: str, node) -> CandidateLink | None:
    href = (node.get("href") or "").strip()
    if not href or _BAD_LINK_RE.search(href):
        return None
    url = urljoin(home_url, href)
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return None
    if (parts.netloc or "").lower() != base_host:
        return None
    clean_url = f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/")
    if not clean_url or parts.path in {"", "/"}:
        return None
    title = normalize.clean_html(node.text_content())
    if len(title) < 18:
        return None
    published_at = _parse_datetime(
        _first_non_empty(
            node.get("datetime"),
            node.get("content"),
            node.get("data-date"),
            _guess_date_from_text(_node_text(node.getparent()) if node.getparent() is not None else ""),
        )
    )
    score = _score_candidate(parts.path, title)
    if published_at is not None:
        score += 2
    if score <= 0:
        return None
    return CandidateLink(clean_url, title[:500], score, published_at)


def _score_candidate(path: str, title: str) -> int:
    path_lower = (path or "").lower()
    score = 0
    if _ARTICLE_HINT_RE.search(path_lower):
        score += 4
    if _DATE_HINT_RE.search(path_lower):
        score += 3
    if path_lower.count("/") >= 2:
        score += 1
    if len(title) >= 40:
        score += 2
    if len(title) >= 80:
        score += 1
    if _BAD_LINK_RE.search(path_lower):
        score -= 5
    return score


def _nodes_by_selector(node, selector: str | None) -> list:
    if not selector:
        return []
    selector = selector.strip()
    if not selector:
        return []
    try:
        if selector.startswith(("/", ".//", "(")):
            return list(node.xpath(selector))
        return list(node.cssselect(selector))
    except Exception:
        return []


def _node_text(node) -> str:
    try:
        return normalize.clean_html(node.text_content())
    except Exception:
        return ""


def _first_non_empty(*values: str) -> str:
    for value in values:
        if value and str(value).strip():
            return str(value).strip()
    return ""


def _guess_date_from_text(raw: str) -> str:
    if not raw:
        return ""
    match = _DATE_TEXT_RE.search(raw)
    return match.group(1) if match else ""


def _parse_datetime(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = dateparser.parse(raw)
    except (ValueError, TypeError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _listing_hash(candidates: list[CandidateLink]) -> str | None:
    if not candidates:
        return None
    basis = "\n".join(item.url for item in candidates[:10])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _guess_language(source: dict) -> str | None:
    category = (source.get("category") or "").lower()
    if any(marker in category for marker in ("рф", "снг", "россий", "telegram")):
        return "ru"
    if "международ" in category:
        return "en"
    return None


def _empty_stats() -> dict:
    return {"added": 0, "attempted": 0, "skipped_old": 0, "skipped_irrelevant": 0, "skipped_known": 0}
