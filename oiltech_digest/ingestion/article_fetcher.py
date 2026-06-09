"""Fetch full article pages and extract readable main text.

RSS feeds often contain only a short teaser. This module upgrades those
records by downloading the linked article page and replacing ``raw_text`` when
the extracted body is clearly better than the RSS snippet.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re

from lxml import html

from oiltech_digest.db import repository
from oiltech_digest.ingestion import normalize
from oiltech_digest.ingestion.http_client import fetch

logger = logging.getLogger(__name__)

MIN_FULL_TEXT_CHARS = 800
MIN_GAIN_RATIO = 2.0

_DROP_XPATH = (
    ".//script", ".//style", ".//noscript", ".//svg", ".//iframe",
    ".//nav", ".//footer", ".//header", ".//aside", ".//form",
)
_BAD_CLASS_RE = re.compile(
    r"(nav|menu|footer|header|cookie|banner|share|social|subscribe|"
    r"newsletter|advert|promo|related|sidebar|breadcrumb|comment)",
    re.I,
)
_GOOD_CLASS_RE = re.compile(r"(article|content|story|post|entry|body|main|text)", re.I)


@dataclass(frozen=True)
class ExtractionResult:
    text: str
    status: str
    method: str = "lxml"
    error: str | None = None
    image_url: str = ""


_OG_IMAGE_XPATHS = (
    "//meta[@property='og:image']/@content",
    "//meta[@property='og:image:url']/@content",
    "//meta[@property='og:image:secure_url']/@content",
    "//meta[@name='og:image']/@content",
    "//meta[@name='twitter:image']/@content",
    "//meta[@name='twitter:image:src']/@content",
    "//link[@rel='image_src']/@href",
)


def extract_og_image(content: bytes | str) -> str:
    """Best-effort lead image for a news card: og:image / twitter:image / image_src."""
    if not content:
        return ""
    try:
        doc = html.fromstring(content)
    except (ValueError, TypeError):
        return ""
    for xpath in _OG_IMAGE_XPATHS:
        for value in doc.xpath(xpath):
            url = (value or "").strip()
            if url.startswith("http"):
                return url
    return ""


def fetch_full_text(limit: int = 50, min_chars: int = MIN_FULL_TEXT_CHARS,
                    retry_too_short: bool = False) -> dict:
    """Fetch and store full text for truncated articles.

    retry_too_short=True re-attempts articles previously marked too_short,
    useful after adding a new extraction backend (e.g. trafilatura).
    """
    stats = {"processed": 0, "updated": 0, "failed": 0, "too_short": 0}
    articles = repository.get_articles_needing_full_text(limit=limit, retry_too_short=retry_too_short)
    for article in articles:
        stats["processed"] += 1
        try:
            result = fetch_article_text(article, min_chars=min_chars)
            if result.status == "ok":
                repository.update_article_full_text(
                    int(article["id"]),
                    raw_text=result.text,
                    text_truncated=False,
                    status=result.status,
                    method=result.method,
                    error=None,
                    image_url=result.image_url,
                )
                stats["updated"] += 1
            else:
                repository.update_article_full_text(
                    int(article["id"]),
                    raw_text=None,
                    text_truncated=True,
                    status=result.status,
                    method=result.method,
                    error=result.error,
                    image_url=result.image_url,
                )
                stats["too_short" if result.status == "too_short" else "failed"] += 1
        except Exception as exc:  # noqa: BLE001 - batch should continue
            logger.warning("Full text fetch failed for article %s: %s", article.get("id"), exc)
            repository.update_article_full_text(
                int(article["id"]),
                raw_text=None,
                text_truncated=True,
                status="failed",
                method="lxml",
                error=str(exc)[:500],
            )
            stats["failed"] += 1
    return stats


def backfill_images(limit: int = 200) -> dict:
    """Дозаполнить image_url (og:image) у статей без картинки — для дайджеста.

    fetch-full-text обрабатывает только статьи без полного текста; уже обработанные
    остаются без картинки. Здесь перефетчим страницу и берём og:image/twitter:image.
    """
    from oiltech_digest.ingestion.http_client import fetch

    stats = {"processed": 0, "updated": 0, "no_image": 0, "failed": 0}
    for article in repository.get_articles_missing_image(limit=limit):
        stats["processed"] += 1
        try:
            content = fetch(article["url"])
            if not content:
                stats["failed"] += 1
                continue
            image_url = extract_og_image(content)
            if image_url and repository.set_article_image(int(article["id"]), image_url):
                stats["updated"] += 1
            else:
                stats["no_image"] += 1
        except Exception as exc:  # noqa: BLE001 - batch should continue
            logger.warning("backfill image failed for article %s: %s", article.get("id"), exc)
            stats["failed"] += 1
    return stats


def fetch_article_text(article: dict, min_chars: int = MIN_FULL_TEXT_CHARS) -> ExtractionResult:
    url = article.get("url")
    if not url:
        return ExtractionResult("", "failed", error="missing article url")
    content = fetch(url)
    if content is None:
        return ExtractionResult("", "failed", error="download failed")
    current = article.get("raw_text") or ""
    image_url = extract_og_image(content)

    extracted = extract_main_text(content)
    if _is_better_text(extracted, current, min_chars=min_chars):
        return ExtractionResult(extracted, "ok", method="lxml", image_url=image_url)

    # Fallback: trafilatura often handles cluttered pages better than lxml heuristics.
    traf = _trafilatura_extract(content)
    if traf and _is_better_text(traf, current, min_chars=min_chars):
        return ExtractionResult(traf, "ok", method="trafilatura", image_url=image_url)

    best = traf if len(traf) > len(extracted) else extracted
    return ExtractionResult(
        best,
        "too_short",
        method="trafilatura" if traf and len(traf) > len(extracted) else "lxml",
        error=f"extracted={len(best)} chars, current={len(current)} chars",
        image_url=image_url,
    )


def extract_main_text(content: bytes | str) -> str:
    """Extract readable text from an article HTML page.

    This is deliberately conservative: it prefers semantic article/main nodes,
    removes navigation/ads, and returns a clean text block only when there is
    enough paragraph-like content.
    """
    if not content:
        return ""
    try:
        doc = html.fromstring(content)
    except (ValueError, TypeError):
        return ""

    structured_text = _json_ld_article_text(doc)

    for xpath in _DROP_XPATH:
        for node in doc.xpath(xpath):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)

    candidates = doc.xpath("//article|//main")
    candidates.extend(
        doc.xpath(
            "//*[self::div or self::section][contains(translate(@class,"
            " 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'article')"
            " or contains(translate(@class,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'content')"
            " or contains(translate(@class,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'story')"
            " or contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'article')"
            " or contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'content')]"
        )
    )
    if not candidates:
        candidates = [doc]

    best_text = structured_text
    best_score = len(structured_text) + 300 if len(structured_text) >= 120 else -1
    for node in candidates:
        text = _node_text(node)
        if len(text) < 120:
            continue
        score = _score_node(node, text)
        if score > best_score:
            best_score = score
            best_text = text
    return best_text


def _json_ld_article_text(doc) -> str:
    """Extract articleBody/text from JSON-LD structured data when present."""
    texts: list[str] = []
    for node in doc.xpath("//script[contains(translate(@type, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'ld+json')]"):
        raw = (node.text or "").strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        texts.extend(_json_ld_text_values(payload))
    if not texts:
        return ""
    return max((normalize.clean_html(text) for text in texts), key=len, default="")


def _json_ld_text_values(payload) -> list[str]:
    if isinstance(payload, list):
        values: list[str] = []
        for item in payload:
            values.extend(_json_ld_text_values(item))
        return values
    if not isinstance(payload, dict):
        return []

    values = []
    for key in ("articleBody", "text"):
        value = payload.get(key)
        if isinstance(value, str) and len(value.strip()) >= 120:
            values.append(value)
    graph = payload.get("@graph")
    if graph:
        values.extend(_json_ld_text_values(graph))
    return values


def _node_text(node) -> str:
    chunks = []
    for item in node.xpath(".//p|.//li|.//h2|.//h3|.//blockquote"):
        class_id = " ".join(filter(None, [item.get("class"), item.get("id")]))
        if _BAD_CLASS_RE.search(class_id):
            continue
        text = normalize.clean_html(item.text_content())
        if len(text) >= 30:
            chunks.append(text)
    text = "\n\n".join(_dedupe_preserve_order(chunks))
    if len(text) < 200:
        # Вёрстка без значимых <p> (текст лежит в <div>/таблицах — частый случай
        # у CMS вроде EnergyLand): берём очищенный текст самого узла-кандидата.
        node_text = normalize.clean_html(node.text_content())
        if len(node_text) > len(text):
            text = node_text
    return text


def _score_node(node, text: str) -> float:
    class_id = " ".join(filter(None, [node.get("class"), node.get("id")]))
    good_bonus = 250 if _GOOD_CLASS_RE.search(class_id) else 0
    bad_penalty = 500 if _BAD_CLASS_RE.search(class_id) else 0
    paragraph_count = max(1, text.count("\n\n") + 1)
    link_text = " ".join(normalize.clean_html(a.text_content()) for a in node.xpath(".//a"))
    link_penalty = min(400, len(link_text) * 0.4)
    return len(text) + paragraph_count * 30 + good_bonus - bad_penalty - link_penalty


def _is_better_text(extracted: str, current: str, min_chars: int) -> bool:
    extracted_len = len(extracted or "")
    current_len = len(current or "")
    if extracted_len < min_chars:
        return False
    if current_len and extracted_len < current_len * MIN_GAIN_RATIO:
        return False
    return True


def _trafilatura_extract(content: bytes | str) -> str:
    try:
        import trafilatura  # optional dep — not available in all envs
    except ImportError:
        return ""
    try:
        text = trafilatura.extract(
            content,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
        )
        return (text or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
