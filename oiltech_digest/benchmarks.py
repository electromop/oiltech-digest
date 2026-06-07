"""Read-only production readiness benchmarks.

The benchmark intentionally exercises admin read paths without creating jobs,
parsing sources or calling AI providers. It is safe to run against a production
database when a quick latency picture is needed.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from psycopg.rows import dict_row

from oiltech_digest.db import connection, repository


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percentile
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _measure(
    name: str,
    fn: Callable[[], Any],
    *,
    iterations: int,
    warn_ms: float,
) -> dict[str, Any]:
    durations: list[float] = []
    rows = 0
    last_result: Any = None
    for _ in range(iterations):
        started = time.perf_counter()
        last_result = fn()
        durations.append((time.perf_counter() - started) * 1000)

    if isinstance(last_result, list):
        rows = len(last_result)
    elif isinstance(last_result, dict):
        rows = len(last_result)
    elif last_result is not None:
        rows = 1

    p95 = _percentile(durations, 0.95)
    return {
        "name": name,
        "runs": iterations,
        "rows": rows,
        "p50_ms": round(_percentile(durations, 0.50), 2),
        "p95_ms": round(p95, 2),
        "max_ms": round(max(durations), 2),
        "status": "warn" if p95 > warn_ms else "ok",
    }


def _article_list_query(limit: int) -> list[dict]:
    """Mirror the heaviest article list joins without loading score item details."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.id, a.title, a.url, a.language, a.published_at,
                   a.collected_at, s.name AS source_name,
                   COALESCE(c.summary, '') AS summary,
                   COALESCE(c.status, 'new') AS status,
                   c.relevant,
                   COALESCE(c.selected_for_digest, FALSE) AS selected_for_digest,
                   sc.total_score, sc.score_label,
                   t.name AS tag_name, parent.name AS parent_tag_name
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN article_cards c ON c.article_id = a.id
            LEFT JOIN article_scores sc ON sc.article_id = a.id
            LEFT JOIN article_tags at ON at.article_id = a.id
            LEFT JOIN tags t ON t.id = at.tag_id
            LEFT JOIN tags parent ON parent.id = t.parent_id
            ORDER BY COALESCE(sc.total_score, 0) DESC,
                     a.published_at DESC NULLS LAST,
                     a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def _queue_summary_query() -> list[dict]:
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT queue_name, status, COUNT(*) AS jobs
            FROM background_jobs
            GROUP BY queue_name, status
            ORDER BY queue_name, status
            """
        )
        return cur.fetchall()


def _table_counts_query() -> dict[str, int]:
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM sources) AS sources,
              (SELECT COUNT(*) FROM articles) AS articles,
              (SELECT COUNT(*) FROM article_cards) AS article_cards,
              (SELECT COUNT(*) FROM article_tags) AS article_tags,
              (SELECT COUNT(*) FROM article_scores) AS article_scores,
              (SELECT COUNT(*) FROM background_jobs) AS background_jobs
            """
        )
        row = cur.fetchone()
    return {key: int(row[key] or 0) for key in row}


def run_readiness_benchmark(
    *,
    iterations: int = 5,
    articles_limit: int = 1000,
    source_limit: int = 300,
    jobs_limit: int = 100,
    month: str | None = None,
    digest_limit: int = 100,
    min_score: float = 0,
    warn_ms: float = 800,
) -> dict[str, Any]:
    """Run safe read-only benchmarks for the most important production paths."""
    if iterations < 1:
        raise ValueError("iterations must be >= 1")
    if min(articles_limit, source_limit, jobs_limit, digest_limit) < 1:
        raise ValueError("limits must be >= 1")

    checks: list[tuple[str, Callable[[], Any]]] = [
        ("table_counts", _table_counts_query),
        ("dashboard_stats", repository.dashboard_stats),
        ("articles_list", lambda: _article_list_query(articles_limit)),
        ("source_health", lambda: repository.source_health_report(limit=source_limit)),
        (
            "digest_candidates",
            lambda: repository.digest_candidates(
                month=month,
                limit=digest_limit,
                min_score=min_score,
            ),
        ),
        ("jobs_list", lambda: repository.list_background_jobs(limit=jobs_limit)),
        ("queue_summary", _queue_summary_query),
    ]
    benchmarks = [
        _measure(name, fn, iterations=iterations, warn_ms=warn_ms)
        for name, fn in checks
    ]
    return {
        "iterations": iterations,
        "warn_ms": warn_ms,
        "params": {
            "articles_limit": articles_limit,
            "source_limit": source_limit,
            "jobs_limit": jobs_limit,
            "month": month,
            "digest_limit": digest_limit,
            "min_score": min_score,
        },
        "benchmarks": _jsonable(benchmarks),
        "counts": _jsonable(_table_counts_query()),
        "warnings": [
            item["name"]
            for item in benchmarks
            if item["status"] == "warn"
        ],
    }


def format_benchmark_report(report: dict[str, Any]) -> str:
    lines = [
        "Production readiness benchmark (read-only)",
        f"iterations={report['iterations']}, warn_p95_ms={report['warn_ms']}",
        "",
        "Dataset:",
    ]
    for key, value in report["counts"].items():
        lines.append(f"  {key}: {value}")
    lines.extend(["", "Checks:"])
    for item in report["benchmarks"]:
        lines.append(
            "  "
            f"{item['name']}: {item['status']} "
            f"rows={item['rows']} "
            f"p50={item['p50_ms']}ms "
            f"p95={item['p95_ms']}ms "
            f"max={item['max_ms']}ms"
        )
    if report["warnings"]:
        lines.extend(["", "Warnings: " + ", ".join(report["warnings"])])
    return "\n".join(lines)
