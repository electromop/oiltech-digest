"""Автообнаружение RSS/Atom-ленты по сайту источника.

Трёхступенчато: (1) `<link rel="alternate">` на главной → (2) перебор типичных
путей → (3) валидация feedparser (лента считается рабочей только если есть entries).
Зондирование — через http_client.probe (без ретраев, 404 ожидаемы).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlsplit

import feedparser
from lxml import html as lxml_html

from oiltech_digest.config import MAX_WORKERS, RSS_PROBE_TIMEOUT
from oiltech_digest.db import repository
from oiltech_digest.ingestion.http_client import probe

logger = logging.getLogger(__name__)

# Типичные пути RSS/Atom (порядок = приоритет проверки)
_CANDIDATE_PATHS = [
    "/feed", "/feed/", "/rss", "/rss/", "/rss.xml", "/feed.xml",
    "/atom.xml", "/index.xml", "/?feed=rss2", "/feeds/posts/default",
    "/en/rss", "/en/feed", "/news/rss", "/blog/feed", "/rss/all.xml",
    "/export/rss",
]


def _looks_like_feed(content: bytes | None) -> bool:
    """True, если содержимое парсится feedparser'ом и содержит хотя бы один entry."""
    if not content:
        return False
    parsed = feedparser.parse(content)
    return bool(getattr(parsed, "entries", None))


def _links_from_html(content: bytes, base_url: str) -> list[str]:
    """Извлечь href из <link rel="alternate" type="...rss/atom/xml...">."""
    try:
        doc = lxml_html.fromstring(content)
    except Exception:  # noqa: BLE001 - битый HTML не должен ронять обход
        return []
    out = []
    for link in doc.xpath('//link[@rel="alternate"]'):
        typ = (link.get("type") or "").lower()
        href = link.get("href")
        if href and ("rss" in typ or "atom" in typ or "xml" in typ):
            out.append(urljoin(base_url, href))
    return out


def discover_feed(site_url: str | None, timeout: int = RSS_PROBE_TIMEOUT) -> str | None:
    """Найти рабочую ленту по сайту источника. None, если не найдена."""
    if not site_url:
        return None

    # 1) <link rel="alternate"> на главной странице
    home = probe(site_url, timeout=timeout)
    if home:
        for cand in _links_from_html(home, site_url):
            if _looks_like_feed(probe(cand, timeout=timeout)):
                return cand

    # 2) перебор типичных путей от корня домена
    split = urlsplit(site_url)
    if split.scheme and split.netloc:
        base = f"{split.scheme}://{split.netloc}"
        for path in _CANDIDATE_PATHS:
            cand = urljoin(base, path)
            if _looks_like_feed(probe(cand, timeout=timeout)):
                return cand

    return None


def discover_all(only_missing: bool = True, source_id: int | None = None,
                 workers: int = MAX_WORKERS, dry_run: bool = False,
                 limit: int | None = None,
                 timeout: int = RSS_PROBE_TIMEOUT) -> dict:
    """Обойти источники-кандидаты, проставить rss_url/parse_strategy. Вернуть статистику."""
    sources = repository.get_sources_for_discovery(
        only_missing=only_missing,
        source_id=source_id,
        limit=limit,
    )
    stats = {"checked": 0, "rss": 0, "request": 0, "results": []}

    def work(src: dict):
        return src, discover_feed(src.get("url"), timeout=timeout)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, s): s for s in sources}
        for fut in as_completed(futures):
            src, rss = fut.result()
            stats["checked"] += 1
            strategy = "rss" if rss else "request"
            stats["rss" if rss else "request"] += 1
            stats["results"].append({"name": src["name"], "rss_url": rss, "strategy": strategy})
            if not dry_run:
                repository.update_source_rss(src["id"], rss, strategy)
            logger.info("%s → %s", src["name"], rss or "RSS не найден → request")

    return stats
