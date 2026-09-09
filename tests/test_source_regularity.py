from datetime import datetime, timezone

from oiltech_digest.source_discovery.source_regularity import assess_source_regularity, regularity_comment


NOW = datetime(2026, 8, 24, tzinfo=timezone.utc)


def test_assess_source_regularity_detects_regular_flow():
    result = assess_source_regularity(
        {"url": "https://example.com/news"},
        {"articles": [
            {"url": "https://example.com/news/1", "published_at": "2026-08-20T10:00:00+00:00"},
            {"url": "https://example.com/news/2", "published_at": "2026-08-12T10:00:00+00:00"},
            {"url": "https://example.com/news/3", "published_at": "2026-08-01T10:00:00+00:00"},
            {"url": "https://example.com/news/4", "published_at": "2026-07-10T10:00:00+00:00"},
        ]},
        [],
        now=NOW,
    )

    assert result["regularity_label"] == "regular"
    assert result["is_regular"] is True
    assert result["articles_last_30_days"] == 3
    assert result["articles_last_90_days"] == 4
    assert result["archive_suspected"] is False
    assert "регулярный поток" in regularity_comment(result)


def test_assess_source_regularity_detects_archive():
    result = assess_source_regularity(
        {"url": "https://example.com/archive/2024"},
        {},
        [
            {"url": "https://example.com/archive/old", "title": "Archive item", "published_at": "2024-05-01"},
            {"url": "https://example.com/archive/older", "published_at": "2024-01-12"},
        ],
        now=NOW,
    )

    assert result["regularity_label"] == "archive"
    assert result["is_regular"] is False
    assert result["archive_suspected"] is True
    assert result["articles_last_90_days"] == 0


def test_assess_source_regularity_handles_missing_dates():
    result = assess_source_regularity(
        {"url": "https://example.com/news"},
        {"articles": [{"url": "https://example.com/news/1", "title": "Fresh title"}]},
        [],
        now=NOW,
    )

    assert result["regularity_label"] == "unknown"
    assert result["dated_articles"] == 0
    assert result["section_updates"] is False
    assert "дат мало" in regularity_comment(result)
