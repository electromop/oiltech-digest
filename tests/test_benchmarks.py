from __future__ import annotations

from datetime import datetime, timezone

from oiltech_digest import benchmarks
from oiltech_digest.db import connection


def test_percentile_handles_single_and_multiple_values():
    assert benchmarks._percentile([12.0], 0.95) == 12.0
    assert benchmarks._percentile([10.0, 20.0, 30.0], 0.5) == 20.0
    assert benchmarks._percentile([], 0.95) == 0.0


def test_readiness_benchmark_runs_safe_read_queries(isolated_db):
    now = datetime.now(timezone.utc)
    with connection.get_connection() as conn:
        source_id = conn.execute(
            """
            INSERT INTO sources (name, source_type, url, enabled, parse_strategy)
            VALUES ('Benchmark Source', 'News', 'https://example.com', TRUE, 'rss')
            RETURNING id
            """
        ).fetchone()[0]
        article_id = conn.execute(
            """
            INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language, content_hash)
            VALUES (%s, 'Benchmark signal', 'https://example.com/a', %s, %s, 'Body text', 'en', 'bench-hash')
            RETURNING id
            """,
            (source_id, now, now),
        ).fetchone()[0]
        tag_id = conn.execute(
            "INSERT INTO tags (name, enabled, sort_order) VALUES ('Бурение', TRUE, 1) RETURNING id"
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO article_cards (article_id, summary, relevant, status, selected_for_digest)
            VALUES (%s, 'Compact summary', TRUE, 'digest', TRUE)
            """,
            (article_id,),
        )
        conn.execute(
            """
            INSERT INTO article_tags (article_id, tag_id, confidence, rationale)
            VALUES (%s, %s, 0.8, 'benchmark')
            """,
            (article_id, tag_id),
        )
        conn.execute(
            """
            INSERT INTO article_scores (article_id, model, total_score, score_label, explanation)
            VALUES (%s, 'offline', 77, 'Высокая', 'benchmark')
            """,
            (article_id,),
        )
        conn.execute(
            """
            INSERT INTO background_jobs (kind, queue_name, status, progress, payload_json)
            VALUES ('digest_export', 'exports', 'ok', 100, '{}'::jsonb)
            """
        )
        conn.commit()

    report = benchmarks.run_readiness_benchmark(
        iterations=1,
        articles_limit=10,
        source_limit=10,
        jobs_limit=10,
        month=now.strftime("%Y-%m"),
        digest_limit=10,
        min_score=0,
        warn_ms=10_000,
    )

    assert report["warnings"] == []
    assert report["counts"]["articles"] == 1
    assert report["counts"]["background_jobs"] == 1
    names = {item["name"] for item in report["benchmarks"]}
    assert names == {
        "table_counts",
        "dashboard_stats",
        "articles_list",
        "source_health",
        "digest_candidates",
        "jobs_list",
        "queue_summary",
    }
    assert all(item["p50_ms"] >= 0 for item in report["benchmarks"])
    assert "articles_list" in benchmarks.format_benchmark_report(report)
