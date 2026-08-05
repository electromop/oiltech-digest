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
  6. Enqueues AI pipeline for this article unless --no-process is passed.
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

from oiltech_digest import background_jobs, network_policy
from oiltech_digest.ingestion.manual_import import ManualImportError, import_article


url = sys.argv[1].strip()
source_id_arg = sys.argv[2].strip()
should_process = sys.argv[3] == "1"
offline = sys.argv[4] == "1"


def fail(message: str) -> None:
    raise SystemExit(f"manual-add-article: {message}")


try:
    result = import_article(url, explicit_source_id=int(source_id_arg) if source_id_arg else None)
except ManualImportError as exc:
    fail(str(exc))

article_id = result.article_id
print(
    f"{'Duplicate' if result.duplicate else 'Inserted'} article_id={article_id} "
    f"source=#{result.source_id} {result.source_name} fetch={result.fetch_method}"
)
print(f"Title: {result.title[:180]}")
if result.full_text_status:
    print(
        f"Full text: {result.full_text_status} via {result.full_text_method} "
        f"({result.full_text_chars} chars)"
    )

if should_process:
    decision = network_policy.route_ai_processing()
    job = background_jobs.enqueue(
        "process_articles",
        {"article_ids": [article_id], "limit": 1, "offline": offline},
        queue_name=decision.queue_name,
        execution_region=decision.execution_region,
        capability=decision.capability,
    )
    print(
        f"Queued AI processing: job_id={job['id']} queue={job.get('queue_name')} "
        f"region={job.get('execution_region')}"
    )
else:
    print(f"Skipped AI processing. Run later: python -m oiltech_digest.cli process-articles {article_id}")

print(f"Done: article_id={article_id}")
PY
