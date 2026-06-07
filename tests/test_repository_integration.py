from __future__ import annotations

from datetime import datetime, timedelta, timezone

from oiltech_digest.db import connection
from oiltech_digest.db import repository


def test_repository_dashboard_health_and_digest_queries_use_real_schema(isolated_db):
    now = datetime.now(timezone.utc)

    with connection.get_connection() as conn:
        source_rows = conn.execute(
            """
            INSERT INTO sources (name, source_type, url, enabled, parse_strategy, category)
            VALUES
              ('No Articles', 'News', 'https://example.com/empty', TRUE, 'request', 'международные'),
              ('Stale Source', 'News', 'https://example.com/stale', TRUE, 'rss', 'международные'),
              ('Healthy Source', 'News', 'https://example.com/ok', TRUE, 'playwright', 'международные'),
              ('Disabled Source', 'News', 'https://example.com/off', FALSE, 'telegram', 'telegram')
            RETURNING id, name
            """
        ).fetchall()
        source_ids = {name: source_id for source_id, name in source_rows}

        tag_parent = conn.execute(
            """
            INSERT INTO tags (name, enabled, sort_order)
            VALUES ('Технологии', TRUE, 1)
            RETURNING id
            """
        ).fetchone()[0]
        tag_child = conn.execute(
            """
            INSERT INTO tags (parent_id, name, enabled, sort_order)
            VALUES (%s, 'ГРП', TRUE, 1)
            RETURNING id
            """,
            (tag_parent,),
        ).fetchone()[0]

        article_rows = conn.execute(
            """
            INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language, content_hash)
            VALUES
              (%s, 'Old article', 'https://example.com/old', %s, %s, 'Old text', 'en', 'old-hash'),
              (%s, 'Digest candidate', 'https://example.com/digest', %s, %s, 'Electric frac text', 'en', 'digest-hash')
            RETURNING id, title
            """,
            (
                source_ids["Stale Source"],
                now - timedelta(days=10),
                now - timedelta(days=10),
                source_ids["Healthy Source"],
                now - timedelta(days=1),
                now - timedelta(days=1),
            ),
        ).fetchall()
        article_ids = {title: article_id for article_id, title in article_rows}

        conn.execute(
            """
            INSERT INTO article_cards (article_id, summary, relevant, status, selected_for_digest)
            VALUES
              (%s, 'Old summary', TRUE, 'new', FALSE),
              (%s, 'Digest candidate: Useful compact summary', TRUE, 'digest', TRUE)
            """,
            (article_ids["Old article"], article_ids["Digest candidate"]),
        )
        conn.execute(
            """
            INSERT INTO article_tags (article_id, tag_id, confidence, rationale)
            VALUES (%s, %s, 0.9, 'keyword match')
            """,
            (article_ids["Digest candidate"], tag_child),
        )
        conn.execute(
            """
            INSERT INTO scoring_criteria (name, weight, enabled, sort_order)
            VALUES ('Технологическая значимость', 100, TRUE, 1)
            RETURNING id
            """
        ).fetchone()
        conn.execute(
            """
            INSERT INTO article_scores (article_id, model, total_score, score_label, explanation)
            VALUES
              (%s, 'offline', 50, 'Средняя', 'old'),
              (%s, 'offline', 90, 'Высокая', 'strong')
            """,
            (article_ids["Old article"], article_ids["Digest candidate"]),
        )
        conn.commit()

    stats = repository.dashboard_stats()
    assert stats == {
        "total_articles": 2,
        "with_summary": 2,
        "processed_articles": 2,
        "selected_for_digest": 1,
        "avg_score": 70,
        "sources": 4,
    }

    health = repository.source_health_report(stale_days=3, limit=10)
    assert [row["verdict"] for row in health] == ["no_articles", "stale", "ok", "disabled"]
    assert health[0]["name"] == "No Articles"
    assert repository.source_health_report(stale_days=3, verdict="stale")[0]["name"] == "Stale Source"

    digest_rows = repository.digest_candidates(month=now.strftime("%Y-%m"), min_score=60)
    assert len(digest_rows) == 1
    assert digest_rows[0]["id"] == article_ids["Digest candidate"]
    assert digest_rows[0]["tag_name"] == "ГРП"
    assert digest_rows[0]["parent_tag_name"] == "Технологии"

    assert repository.digest_candidates(month=now.strftime("%Y-%m"), min_score=95) == []


def test_insert_article_is_idempotent_by_url_against_real_db(isolated_db):
    with connection.get_connection() as conn:
        source_id = conn.execute(
            """
            INSERT INTO sources (name, source_type, url, enabled, parse_strategy)
            VALUES ('Dedup Source', 'News', 'https://example.com', TRUE, 'request')
            RETURNING id
            """
        ).fetchone()[0]
        conn.commit()

    rec = {
        "source_id": source_id,
        "title": "First title",
        "url": "https://example.com/same-url",
        "published_at": datetime.now(timezone.utc),
        "raw_text": "Original article body",
        "text_truncated": False,
        "language": "en",
        "content_hash": "same-hash",
        "image_url": "https://example.com/image.jpg",
    }

    assert repository.insert_article(rec) is True
    assert repository.insert_article({**rec, "title": "Changed title"}) is False
    assert repository.article_exists("https://example.com/same-url") is True

    with connection.get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*), MIN(title) FROM articles WHERE url = %s",
            ("https://example.com/same-url",),
        ).fetchone()

    assert row == (1, "First title")
