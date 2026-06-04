from oiltech_digest.ingestion import source_diagnostics
from oiltech_digest.ingestion.source_diagnostics import ProbeResult


LISTING_HTML = b"""
<html>
  <body>
    <a href="/news/2026/06/drilling-automation-platform">
      Drilling automation platform expands well service efficiency
    </a>
  </body>
</html>
"""

ARTICLE_HTML = b"""
<html>
  <head>
    <meta property="og:title" content="Drilling automation platform expands well service efficiency">
    <meta property="article:published_time" content="2026-06-03T10:00:00Z">
  </head>
  <body>
    <article>
      <p>The company deployed drilling automation software for oilfield service crews.</p>
      <p>The platform improves well construction, equipment uptime, and production operations.</p>
      <p>Extra industrial context keeps the extracted article body long enough for insertion diagnostics.</p>
    </article>
  </body>
</html>
"""

TELEGRAM_HTML = """
<div class="tgme_widget_message" data-post="oiltechnews/42">
  <div class="tgme_widget_message_text js-message_text">
    Новая система автоматизации бурения внедрена на месторождении.
  </div>
  <a class="tgme_widget_message_date">
    <time datetime="2026-06-03T10:15:00+00:00"></time>
  </a>
</div>
"""

RSS_XML = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>T</title>
<item><title>Automation news</title><link>https://example.com/a</link></item>
</channel></rss>"""


def test_diagnose_request_source_reports_insertable_candidate(monkeypatch):
    def fake_probe(url, timeout=20):
        if url == "https://example.com/news":
            return ProbeResult(url=url, status=200, bytes=len(LISTING_HTML)), LISTING_HTML
        if url == "https://example.com/news/2026/06/drilling-automation-platform":
            return ProbeResult(url=url, status=200, bytes=len(ARTICLE_HTML)), ARTICLE_HTML
        raise AssertionError(url)

    monkeypatch.setattr(source_diagnostics, "probe_url", fake_probe)

    result = source_diagnostics.diagnose_source(
        {"id": 7, "name": "Example", "parse_strategy": "request", "listing_url": "https://example.com/news"},
        limit=3,
    )

    assert result["verdict"] == "ok"
    assert result["candidate_count"] == 1
    assert result["article_checks"][0]["verdict"] == "ok"
    assert result["article_checks"][0]["prefilter_keep"] is True


def test_diagnose_request_source_reports_no_candidates(monkeypatch):
    monkeypatch.setattr(
        source_diagnostics,
        "probe_url",
        lambda url, timeout=20: (ProbeResult(url=url, status=200, bytes=13), b"<html></html>"),
    )

    result = source_diagnostics.diagnose_source(
        {"id": 8, "name": "Empty", "parse_strategy": "request", "url": "https://example.com"},
    )

    assert result["verdict"] == "no_candidates"
    assert result["candidate_count"] == 0


def test_diagnose_telegram_source_reports_posts(monkeypatch):
    monkeypatch.setattr(
        source_diagnostics,
        "probe_url",
        lambda url, timeout=20: (ProbeResult(url=url, status=200, bytes=len(TELEGRAM_HTML)), TELEGRAM_HTML),
    )

    result = source_diagnostics.diagnose_source(
        {"id": 15, "name": "TG", "parse_strategy": "telegram", "url": "https://t.me/oiltechnews"},
    )

    assert result["verdict"] == "ok"
    assert result["post_count"] == 1
    assert result["posts"][0]["url"] == "https://t.me/oiltechnews/42"


def test_diagnose_rss_source_reports_entries(monkeypatch):
    monkeypatch.setattr(
        source_diagnostics,
        "probe_url",
        lambda url, timeout=20: (ProbeResult(url=url, status=200, bytes=len(RSS_XML)), RSS_XML),
    )

    result = source_diagnostics.diagnose_source(
        {"id": 3, "name": "RSS", "parse_strategy": "rss", "rss_url": "https://example.com/feed.xml"},
    )

    assert result["verdict"] == "ok"
    assert result["entry_count"] == 1
    assert result["entries"][0]["title"] == "Automation news"
