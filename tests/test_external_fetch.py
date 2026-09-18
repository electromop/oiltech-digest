from datetime import datetime, timezone

from oiltech_digest.ingestion import external_fetch
from oiltech_digest.ingestion.request_parser import CandidateLink


def test_external_fetch_request_payload_returns_articles(monkeypatch):
    source = {
        "id": 7,
        "name": "External Source",
        "parse_strategy": "request",
        "url": "https://example.com",
        "listing_url": "https://example.com/news",
        "category": "международные",
    }
    candidate = CandidateLink("https://example.com/a", "Article", 10, datetime(2026, 6, 1, tzinfo=timezone.utc))

    monkeypatch.setattr(external_fetch, "should_keep_article", lambda title, text, source: type("R", (), {"keep": True})())
    monkeypatch.setattr("oiltech_digest.ingestion.http_client.fetch", lambda url: b"<html></html>")
    monkeypatch.setattr("oiltech_digest.ingestion.request_parser.extract_candidate_links", lambda *args, **kwargs: [candidate])
    monkeypatch.setattr(
        "oiltech_digest.ingestion.request_parser.fetch_article_candidate",
        lambda candidate, source: {
            "source_id": source["id"],
            "title": candidate.title,
            "url": candidate.url,
            "published_at": candidate.published_at,
            "raw_text": "Long enough article text " * 20,
            "text_truncated": False,
            "language": "en",
            "content_hash": "hash",
        },
    )

    result = external_fetch.process_payload({"source": source, "article_limit": 5})

    assert result["external_fetch"] is True
    assert result["source_id"] == 7
    assert result["stats"]["attempted"] == 1
    assert result["articles"][0]["url"] == "https://example.com/a"
    assert isinstance(result["articles"][0]["published_at"], str)


def test_external_fetch_rss_payload_returns_articles(monkeypatch):
    source = {
        "id": 4,
        "name": "Endeavor Feed",
        "parse_strategy": "rss",
        "rss_url": "https://example.com/feed.xml",
        "category": "международные",
    }
    rec = {
        "source_id": 4,
        "title": "Feed Article",
        "url": "https://example.com/feed/a",
        "published_at": datetime(2026, 6, 2, tzinfo=timezone.utc),
        "raw_text": "summary",
        "text_truncated": False,
        "language": "en",
        "content_hash": "h",
        "image_url": None,
    }
    monkeypatch.setattr("oiltech_digest.ingestion.http_client.fetch", lambda url: b"<rss/>")
    monkeypatch.setattr(
        "oiltech_digest.ingestion.rss_parser.extract_articles_from_feed",
        lambda source, content, max_age_days=None: ([rec], {"skipped_old": 0, "skipped_irrelevant": 2}),
    )

    result = external_fetch.process_payload({"source": source})

    assert result["external_fetch"] is True
    assert result["strategy"] == "rss"
    assert result["source_id"] == 4
    assert result["stats"]["attempted"] == 1
    assert result["stats"]["skipped_irrelevant"] == 2
    assert result["articles"][0]["url"] == "https://example.com/feed/a"
    # source_id убран из article-rec (его проставит core при apply), published_at — строка
    assert "source_id" not in result["articles"][0]
    assert isinstance(result["articles"][0]["published_at"], str)
    assert result["last_listing_hash"] is None


def test_external_fetch_rss_payload_handles_fetch_failure(monkeypatch):
    source = {"id": 5, "parse_strategy": "rss", "rss_url": "https://example.com/feed.xml"}
    monkeypatch.setattr("oiltech_digest.ingestion.http_client.fetch", lambda url: None)

    result = external_fetch.process_payload({"source": source})

    assert result["external_fetch"] is True
    assert result["articles"] == []
    assert result["stats"]["attempted"] == 0


def test_external_fetch_apply_inserts_articles(monkeypatch):
    inserted = []
    touched = []
    state_updates = []
    monkeypatch.setattr(external_fetch.repository, "insert_article", lambda article: inserted.append(article) or True)
    monkeypatch.setattr(external_fetch.repository, "touch_last_parsed", lambda source_id: touched.append(source_id))
    monkeypatch.setattr(external_fetch.repository, "update_source_request_state", lambda source_id, **kwargs: state_updates.append((source_id, kwargs)))

    stats = external_fetch.apply_scrape_result(
        {
            "source_id": 7,
            "articles": [{"title": "A", "url": "https://example.com/a", "raw_text": "Text"}],
            "last_seen_article_url": "https://example.com/a",
            "last_seen_published_at": "2026-06-01T00:00:00+00:00",
            "last_listing_hash": "hash",
        }
    )

    assert stats == {"inserted": 1, "duplicates": 0, "source_id": 7}
    assert inserted[0]["source_id"] == 7
    assert touched == [7]
    assert state_updates[0][1]["last_listing_hash"] == "hash"



def _stub_request_path(monkeypatch, candidates, fetched):
    monkeypatch.setattr(external_fetch, "should_keep_article", lambda title, text, source: type("R", (), {"keep": True})())
    monkeypatch.setattr("oiltech_digest.ingestion.http_client.fetch", lambda url: b"<html></html>")
    monkeypatch.setattr("oiltech_digest.ingestion.request_parser.extract_candidate_links", lambda *a, **k: candidates)

    def fake_fetch(candidate, source):
        fetched.append(candidate.url)
        return {"source_id": source["id"], "title": candidate.title, "url": candidate.url,
                "published_at": None, "raw_text": "text " * 50, "text_truncated": False,
                "language": "en", "content_hash": candidate.url}
    monkeypatch.setattr("oiltech_digest.ingestion.request_parser.fetch_article_candidate", fake_fetch)


def test_worker_skips_urls_the_core_already_has(monkeypatch):
    """Базы у воркера нет: без списка от ядра он качал каждую статью листинга на
    каждом цикле, а ядро отбрасывало знакомые только при вставке."""
    known = CandidateLink("https://example.com/known", "Known article already in core database", 5)
    fresh = CandidateLink("https://example.com/fresh", "Fresh article the core has not seen", 5)
    fetched: list[str] = []
    _stub_request_path(monkeypatch, [known, fresh], fetched)
    source = {"id": 7, "name": "S", "parse_strategy": "request", "url": "https://example.com"}

    result = external_fetch.process_payload({"source": source, "known_urls": [known.url]})

    assert fetched == [fresh.url], "знакомую статью качать нельзя"
    assert result["stats"]["skipped_known"] == 1
    assert [a["url"] for a in result["articles"]] == [fresh.url]


def test_worker_heartbeats_for_request_and_playwright_not_only_rss(monkeypatch):
    """До 18.09 heartbeat получал только RSS-сбор; request и playwright шли без него
    при lease 600 с — пакет из десятка браузерных статей упирался в lease."""
    beats: list[str] = []
    candidates = [CandidateLink(f"https://example.com/a{i}", f"Article number {i} long enough title", 5) for i in range(3)]
    _stub_request_path(monkeypatch, candidates, [])
    source = {"id": 7, "name": "S", "parse_strategy": "request", "url": "https://example.com"}
    external_fetch.process_payload({"source": source}, heartbeat=lambda: beats.append("request"))
    assert beats.count("request") == 3

    monkeypatch.setattr("oiltech_digest.ingestion.playwright_parser.render_listing_candidates",
                        lambda source, url, limit=12: candidates)
    monkeypatch.setattr("oiltech_digest.ingestion.playwright_parser.rendered_article",
                        lambda candidate, source: {"source_id": 7, "title": candidate.title, "url": candidate.url,
                                                   "published_at": None, "raw_text": "t " * 100})
    source = {**source, "parse_strategy": "playwright"}
    external_fetch.process_payload({"source": source}, heartbeat=lambda: beats.append("playwright"))
    assert beats.count("playwright") == 3


def test_worker_result_is_json_serializable_when_candidates_have_dates(monkeypatch):
    """Регрессия 18.09: у кандидатов появились даты, и дата первого уходила в ответ
    ядру объектом datetime — задачи падали «Object of type datetime is not JSON
    serializable». Прежние тесты брали кандидатов без дат и этого не видели."""
    import json

    dated = CandidateLink("https://example.com/a", "Dated article with a real publication date", 5,
                          datetime(2026, 9, 17, tzinfo=timezone.utc))
    _stub_request_path(monkeypatch, [dated], [])
    source = {"id": 7, "name": "S", "parse_strategy": "request", "url": "https://example.com"}

    result = external_fetch.process_payload({"source": source})

    json.dumps(result)  # ровно то, что делает воркер, отправляя результат ядру
    assert result["last_seen_published_at"] == "2026-09-17T00:00:00+00:00"
