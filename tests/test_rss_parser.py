from oiltech_digest.ingestion import rss_parser


def test_parse_all_runs_playwright_sources_after_threaded_sources(monkeypatch):
    calls = []
    sources = [
        {"id": 1, "name": "RSS", "parse_strategy": "rss", "rss_url": "https://example.com/feed.xml"},
        {"id": 2, "name": "Request", "parse_strategy": "request", "listing_url": "https://example.com/news"},
        {"id": 3, "name": "Telegram", "parse_strategy": "telegram", "url": "https://t.me/oiltechnews"},
        {"id": 4, "name": "Rendered", "parse_strategy": "playwright", "listing_url": "https://example.com/rendered"},
    ]

    monkeypatch.setattr(rss_parser.repository, "get_enabled_sources", lambda: sources)

    def stats(name):
        calls.append(name)
        return {"added": 1, "attempted": 2, "skipped_old": 0, "skipped_irrelevant": 0, "skipped_known": 0}

    monkeypatch.setattr(rss_parser, "parse_source", lambda source, max_age_days=None: stats("rss"))
    monkeypatch.setattr(rss_parser.request_parser, "parse_source", lambda source, max_age_days=None: stats("request"))
    monkeypatch.setattr(rss_parser.telegram_parser, "parse_source", lambda source, max_age_days=None: stats("telegram"))
    monkeypatch.setattr(rss_parser.playwright_parser, "parse_source", lambda source, max_age_days=None: stats("playwright"))

    result = rss_parser.parse_all(workers=1)

    assert calls == ["rss", "request", "telegram", "playwright"]
    assert result == {
        "added": 4,
        "duplicates": 4,
        "skipped_old": 0,
        "skipped_irrelevant": 0,
        "sources_ok": 4,
        "errors": 0,
    }


def test_parse_all_can_filter_single_playwright_source(monkeypatch):
    calls = []
    sources = [
        {"id": 1, "name": "RSS", "parse_strategy": "rss", "rss_url": "https://example.com/feed.xml"},
        {"id": 4, "name": "Rendered", "parse_strategy": "playwright", "listing_url": "https://example.com/rendered"},
    ]

    monkeypatch.setattr(rss_parser.repository, "get_enabled_sources", lambda: sources)
    monkeypatch.setattr(
        rss_parser,
        "parse_source",
        lambda source, max_age_days=None: (_ for _ in ()).throw(AssertionError("RSS should not run")),
    )
    monkeypatch.setattr(
        rss_parser.playwright_parser,
        "parse_source",
        lambda source, max_age_days=None: calls.append(source["id"])
        or {"added": 1, "attempted": 1, "skipped_old": 0, "skipped_irrelevant": 0, "skipped_known": 0},
    )

    result = rss_parser.parse_all(source_id=4)

    assert calls == [4]
    assert result["added"] == 1
    assert result["sources_ok"] == 1
