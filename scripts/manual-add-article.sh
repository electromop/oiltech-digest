#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/manual-add-article.sh URL [--source-id ID] [--no-process] [--offline]

Examples:
  scripts/manual-add-article.sh "https://www.slb.com/newsroom/press-release/2026/pr-2026-0714-slb-liberty-energy"
  scripts/manual-add-article.sh "https://example.com/news/item" --source-id 12
  scripts/manual-add-article.sh "https://example.com/news/item" --offline

What it does:
  1. Runs inside docker compose app container.
  2. Finds a matching source by article domain, or creates a fallback manual source.
  3. Fetches and parses the article page.
  4. Inserts the article if URL is new.
  5. Refreshes full text/image when possible.
  6. Runs AI pipeline for this article unless --no-process is passed.
USAGE
}

if [[ $# -lt 1 ]]; then
  usage
  exit 2
fi

URL=""
SOURCE_ID=""
PROCESS="1"
OFFLINE="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-id)
      SOURCE_ID="${2:-}"
      shift 2
      ;;
    --no-process)
      PROCESS="0"
      shift
      ;;
    --offline)
      OFFLINE="1"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      if [[ -z "$URL" ]]; then
        URL="$1"
        shift
      else
        echo "Unexpected argument: $1" >&2
        usage
        exit 2
      fi
      ;;
  esac
done

if [[ -z "$URL" ]]; then
  echo "URL is required" >&2
  usage
  exit 2
fi

docker compose run --rm app python - "$URL" "$SOURCE_ID" "$PROCESS" "$OFFLINE" <<'PY'
from __future__ import annotations

import sys
from urllib.parse import urlparse

from psycopg.rows import dict_row

from oiltech_digest.db import repository
from oiltech_digest.db.connection import get_connection
from oiltech_digest.ingestion import normalize
from oiltech_digest.ingestion.article_fetcher import extract_og_image, fetch_article_text
from oiltech_digest.ingestion.http_client import fetch
from oiltech_digest.ingestion.request_parser import parse_article_page


url = sys.argv[1].strip()
source_id_arg = sys.argv[2].strip()
should_process = sys.argv[3] == "1"
offline = sys.argv[4] == "1"


def fail(message: str) -> None:
    raise SystemExit(f"manual-add-article: {message}")


def article_by_url(article_url: str) -> dict | None:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.id, a.title, a.url, s.name AS source_name
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            WHERE a.url = %s
            """,
            (article_url,),
        )
        return cur.fetchone()


def host_tokens(article_url: str) -> tuple[str, str]:
    parsed = urlparse(article_url)
    host = (parsed.hostname or "").lower()
    if not parsed.scheme.startswith("http") or not host:
        fail("URL must be absolute http(s)")
    bare = host[4:] if host.startswith("www.") else host
    return host, bare


def find_or_create_source(article_url: str, explicit_source_id: str) -> dict:
    if explicit_source_id:
        source = repository.get_source(int(explicit_source_id))
        if source is None:
            fail(f"source_id={explicit_source_id} not found")
        return source

    host, bare = host_tokens(article_url)
    host_like = f"%{host}%"
    bare_like = f"%{bare}%"
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT *
            FROM sources
            WHERE enabled = TRUE
              AND (
                lower(coalesce(url, '')) LIKE %s
                OR lower(coalesce(url, '')) LIKE %s
                OR lower(coalesce(rss_url, '')) LIKE %s
                OR lower(coalesce(rss_url, '')) LIKE %s
                OR lower(coalesce(listing_url, '')) LIKE %s
                OR lower(coalesce(listing_url, '')) LIKE %s
              )
            ORDER BY priority DESC NULLS LAST, id
            LIMIT 1
            """,
            (host_like, bare_like, host_like, bare_like, host_like, bare_like),
        )
        source = cur.fetchone()
        if source is not None:
            return source

    source_id = repository.add_rss_source(
        name=f"Manual import: {bare}",
        rss_url="",
        source_type="manual",
        url=f"{urlparse(article_url).scheme}://{host}",
        parse_strategy="request",
    )
    source = repository.get_source(source_id)
    if source is None:
        fail("fallback source was not created")
    print(f"Created fallback source #{source_id}: {source['name']}")
    return source


def fetch_content(article_url: str) -> tuple[bytes | str, str]:
    content = fetch(article_url)
    if content:
        return content, "http"

    try:
        from oiltech_digest.ingestion.playwright_parser import fetch_rendered

        rendered = fetch_rendered(article_url, settle_ms=8000)
        if rendered:
            return rendered, "playwright"
    except Exception as exc:  # noqa: BLE001
        print(f"Playwright fallback failed: {exc}")

    fail("could not fetch article content")


def guess_language(text: str) -> str:
    sample = text[:3000].lower()
    return "ru" if any("а" <= ch <= "я" for ch in sample) else "en"


def refresh_full_text(article_id: int) -> None:
    article = repository.get_article(article_id)
    if article is None:
        return
    result = fetch_article_text(article, min_chars=500)
    repository.update_article_full_text(
        article_id,
        raw_text=result.text if result.status == "ok" else None,
        text_truncated=result.status != "ok",
        status=result.status,
        method=result.method,
        error=result.error,
        image_url=result.image_url,
    )
    print(f"Full text: {result.status} via {result.method} ({len(result.text)} chars)")


def process_article(article_id: int) -> None:
    from oiltech_digest.processing.pipeline import (
        make_client,
        process_relevance_articles,
        process_score_articles,
        process_summary_articles,
        process_tag_articles,
    )

    client = make_client(offline=offline)
    articles = repository.get_articles_by_ids([article_id], include_summary=False)
    if not articles:
        fail(f"article_id={article_id} not found for processing")

    summary = process_summary_articles(articles, client)
    articles_with_summary = repository.get_articles_by_ids([article_id], include_summary=True)
    relevance = process_relevance_articles(articles_with_summary, client)
    tag = process_tag_articles(articles_with_summary, client)
    score = process_score_articles(articles_with_summary, client)
    print(f"AI summary: {summary}")
    print(f"AI relevance: {relevance}")
    print(f"AI tag: {tag}")
    print(f"AI score: {score}")


existing = article_by_url(url)
if existing is not None:
    article_id = int(existing["id"])
    print(f"Duplicate URL: article_id={article_id} source={existing['source_name']}")
else:
    source = find_or_create_source(url, source_id_arg)
    content, fetch_method = fetch_content(url)
    title, published_at, raw_text = parse_article_page(content, "")
    title = (title or url).strip()
    raw_text = (raw_text or title).strip()
    image_url = extract_og_image(content) or None

    ok = repository.insert_article(
        {
            "source_id": int(source["id"]),
            "title": title[:500],
            "url": url,
            "published_at": published_at,
            "raw_text": raw_text,
            "text_truncated": normalize.is_truncated(raw_text),
            "language": guess_language(raw_text or title),
            "content_hash": normalize.compute_content_hash(title, url),
            "image_url": image_url,
        }
    )
    row = article_by_url(url)
    if row is None:
        fail("article insert did not return a row")
    article_id = int(row["id"])
    print(
        f"{'Inserted' if ok else 'Duplicate'} article_id={article_id} "
        f"source=#{source['id']} {source['name']} fetch={fetch_method} chars={len(raw_text)}"
    )
    print(f"Title: {title[:180]}")

refresh_full_text(article_id)

if should_process:
    process_article(article_id)
else:
    print(f"Skipped AI processing. Run later: python -m oiltech_digest.cli process-articles {article_id}")

print(f"Done: article_id={article_id}")
PY
