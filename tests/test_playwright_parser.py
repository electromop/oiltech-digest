from oiltech_digest.ingestion import playwright_parser


LISTING_HTML = b"""
<html>
  <body>
    <a href="/news/2026/06/js-rendered-drilling-automation">
      JS rendered drilling automation platform improves oilfield operations
    </a>
  </body>
</html>
"""

ARTICLE_HTML = b"""
<html>
  <head>
    <meta property="og:title" content="JS rendered drilling automation platform improves oilfield operations">
    <meta property="article:published_time" content="2026-06-05T08:30:00Z">
  </head>
  <body>
    <article>
      <p>The company deployed a drilling automation platform across oilfield service crews.</p>
      <p>The system improves well construction, equipment uptime, and production operations.</p>
      <p>Additional industrial context keeps this article above teaser length for downstream AI stages.</p>
    </article>
  </body>
</html>
"""


def test_parse_source_renders_listing_and_article_pages(monkeypatch):
    rendered_urls = []
    inserted = []
    state = {}

    def fake_fetch_rendered(url, **kwargs):
        rendered_urls.append(url)
        if url == "https://example.com/news":
            return LISTING_HTML
        if url == "https://example.com/news/2026/06/js-rendered-drilling-automation":
            return ARTICLE_HTML
        return None

    monkeypatch.setattr(playwright_parser, "is_available", lambda: True)
    monkeypatch.setattr(playwright_parser, "fetch_rendered", fake_fetch_rendered)
    monkeypatch.setattr(playwright_parser.repository, "insert_article", lambda article: inserted.append(article) or True)
    monkeypatch.setattr(playwright_parser.repository, "article_exists", lambda url: False)
    monkeypatch.setattr(playwright_parser.repository, "touch_last_parsed", lambda source_id: state.setdefault("touched", source_id))
    monkeypatch.setattr(
        playwright_parser.repository,
        "update_source_request_state",
        lambda source_id, **kwargs: state.update({"source_id": source_id, **kwargs}),
    )

    stats = playwright_parser.parse_source(
        {
            "id": 17,
            "name": "Rendered Example",
            "parse_strategy": "playwright",
            "listing_url": "https://example.com/news",
            "category": "международные",
        },
        article_limit=5,
    )

    assert stats["added"] == 1
    assert rendered_urls == [
        "https://example.com/news",
        "https://example.com/news/2026/06/js-rendered-drilling-automation",
    ]
    assert inserted[0]["url"] == "https://example.com/news/2026/06/js-rendered-drilling-automation"
    assert inserted[0]["language"] == "en"
    assert state["source_id"] == 17
    assert state["last_seen_article_url"] == "https://example.com/news/2026/06/js-rendered-drilling-automation"


def test_parse_source_reports_unavailable_without_rendering(monkeypatch):
    monkeypatch.setattr(playwright_parser, "is_available", lambda: False)
    monkeypatch.setattr(
        playwright_parser,
        "fetch_rendered",
        lambda url: (_ for _ in ()).throw(AssertionError("fetch_rendered should not be called")),
    )

    stats = playwright_parser.parse_source({"id": 18, "name": "No browser", "listing_url": "https://example.com/news"})

    assert stats["added"] == 0
    assert stats["attempted"] == 0
