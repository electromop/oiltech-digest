"""Fetch full article pages and extract readable main text.

RSS feeds often contain only a short teaser. This module upgrades those
records by downloading the linked article page and replacing ``raw_text`` when
the extracted body is clearly better than the RSS snippet.
"""

from __future__ import annotations

from dataclasses import dataclass
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


def fetch_full_text(limit: int = 50, min_chars: int = MIN_FULL_TEXT_CHARS) -> dict:
    """Fetch and store full text for truncated articles."""
    stats = {"processed": 0, "updated": 0, "failed": 0, "too_short": 0}
    articles = repository.get_articles_needing_full_text(limit=limit)
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


def fetch_article_text(article: dict, min_chars: int = MIN_FULL_TEXT_CHARS) -> ExtractionResult:
    url = article.get("url")
    if not url:
        return ExtractionResult("", "failed", error="missing article url")
    content = fetch(url)
    if content is None:
        return ExtractionResult("", "failed", error="download failed")
    extracted = extract_main_text(content)
    current = article.get("raw_text") or ""
    if not _is_better_text(extracted, current, min_chars=min_chars):
        return ExtractionResult(
            extracted,
            "too_short",
            error=f"extracted={len(extracted)} chars, current={len(current)} chars",
        )
    return ExtractionResult(extracted, "ok")


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

    best_text = ""
    best_score = -1
    for node in candidates:
        text = _node_text(node)
        if len(text) < 120:
            continue
        score = _score_node(node, text)
        if score > best_score:
            best_score = score
            best_text = text
    return best_text


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
