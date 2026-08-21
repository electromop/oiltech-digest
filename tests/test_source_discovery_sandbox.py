from oiltech_digest.ingestion.source_diagnostics import ProbeResult
from oiltech_digest.source_discovery import sandbox


def test_collect_candidate_articles_writes_sandbox_rows(monkeypatch):
    listing = b"""
    <html><body>
      <a href="/news/robotic-drilling">Robotic drilling system improves oilfield operations</a>
    </body></html>
    """
    article = b"""
    <html><head><title>Robotic drilling system improves oilfield operations</title></head>
    <body><article>
      <p>The company deployed a robotic drilling system for oilfield well construction.</p>
      <p>The technology improves uptime, safety and operational control for upstream teams.</p>
    </article></body></html>
    """
    rows = []

    def fake_probe(url, timeout=20):
        if url == "https://example.com/news":
            return ProbeResult(url=url, status=200, bytes=len(listing)), listing
        return ProbeResult(url=url, status=200, bytes=len(article)), article

    monkeypatch.setattr(sandbox, "probe_url", fake_probe)
    monkeypatch.setattr(
        sandbox.repository,
        "upsert_source_candidate_article",
        lambda candidate_id, rec: rows.append({"candidate_id": candidate_id, **rec}) or 101,
    )

    result = sandbox.collect_candidate_articles(
        {"id": 42, "url": "https://example.com/news", "name": "Example"},
        article_limit=3,
    )

    assert result["inserted_or_updated"] == 1
    assert rows[0]["candidate_id"] == 42
    assert rows[0]["url"] == "https://example.com/news/robotic-drilling"
    assert rows[0]["prefilter_keep"] is True


def test_process_candidate_articles_saves_ai_result(monkeypatch):
    saved = []
    article = {
        "id": 101,
        "candidate_id": 42,
        "title": "Robotic drilling system improves oilfield operations",
        "url": "https://example.com/news/robotic-drilling",
        "raw_text": "The company deployed a robotic drilling system for oilfield well construction.",
        "language": "en",
        "source_name": "Example",
        "source_category": "роботизация бурения",
    }
    tags = [{"id": 7, "name": "Бурение", "parent_id": None, "keywords_json": ["бурение"], "keywords_en_json": ["drilling"]}]
    criteria = [{"id": 9, "name": "Технологическая значимость", "weight": 100, "keywords_json": [], "keywords_en_json": ["drilling"]}]

    monkeypatch.setattr(sandbox.repository, "list_enabled_tags", lambda: tags)
    monkeypatch.setattr(sandbox.repository, "list_enabled_scoring_criteria", lambda: criteria)
    monkeypatch.setattr(
        sandbox.repository,
        "list_source_candidate_articles",
        lambda candidate_id, limit=5, only_unprocessed=True: [article],
    )
    monkeypatch.setattr(
        sandbox.repository,
        "update_source_candidate_article_result",
        lambda article_id, payload: saved.append({"article_id": article_id, **payload}),
    )

    stats = sandbox.process_candidate_articles(42, limit=5, offline=True)

    assert stats == {"processed": 1, "relevant": 1, "rejected": 0, "errors": 0}
    assert saved[0]["article_id"] == 101
    assert saved[0]["relevant"] is True
    assert saved[0]["processing_status"] == "ok"
    assert saved[0]["total_score"] == 33.33
