from oiltech_digest.ingestion import request_parser


HOME_HTML = b"""
<html>
  <body>
    <a href="/about">About</a>
    <a href="/news/2026/05/field-automation-rollout">Field automation rollout improves wellsite performance</a>
    <a href="/media/article/drilling-analytics-platform">Drilling analytics platform expands to offshore assets</a>
    <a href="https://external.example.com/story">External story</a>
  </body>
</html>
"""

ARTICLE_HTML = b"""
<html>
  <head>
    <meta property="og:title" content="Field automation rollout improves wellsite performance">
    <meta property="article:published_time" content="2026-05-20T08:30:00Z">
  </head>
  <body>
    <article>
      <p>The company deployed a field automation stack across producing wells and surface facilities.</p>
      <p>The program reduced manual interventions, improved uptime and gave engineers better production control.</p>
      <p>Additional industrial context keeps this article above teaser length and suitable for downstream AI stages.</p>
    </article>
  </body>
</html>
"""


def test_extract_candidate_links_prefers_article_like_paths():
    items = request_parser.extract_candidate_links("https://example.com", HOME_HTML, limit=10)

    assert len(items) == 2
    assert items[0].url.startswith("https://example.com/")
    assert all("about" not in item.url for item in items)


def test_parse_article_page_extracts_title_date_and_body():
    title, published_at, raw_text = request_parser.parse_article_page(ARTICLE_HTML)

    assert "Field automation rollout" in title
    assert published_at is not None
    assert "reduced manual interventions" in raw_text


def test_parse_source_uses_listing_page_and_updates_last_seen(monkeypatch):
    fetched_urls = []
    inserted = []
    state = {}

    def fake_fetch(url):
        fetched_urls.append(url)
        if url == "https://example.com/news":
            return HOME_HTML
        if url == "https://example.com/news/2026/05/field-automation-rollout":
            return ARTICLE_HTML
        return None

    def fake_insert(article):
        inserted.append(article)
        return True

    monkeypatch.setattr(request_parser, "fetch", fake_fetch)
    monkeypatch.setattr(request_parser.repository, "insert_article", fake_insert)
    monkeypatch.setattr(request_parser.repository, "article_exists", lambda url: False)
    monkeypatch.setattr(request_parser.repository, "touch_last_parsed", lambda source_id: state.setdefault("touched", source_id))
    monkeypatch.setattr(
        request_parser.repository,
        "update_source_request_state",
        lambda source_id, **kwargs: state.update({"source_id": source_id, **kwargs}),
    )
    monkeypatch.setattr(
        request_parser,
        "extract_candidate_links",
        lambda source, listing_url, content, limit=12: [
            request_parser.CandidateLink(
                url="https://example.com/news/2026/05/field-automation-rollout",
                title="Field automation rollout improves wellsite performance",
                score=8,
                published_at=None,
            )
        ],
    )

    stats = request_parser.parse_source(
        {"id": 7, "url": "https://example.com", "listing_url": "https://example.com/news", "category": "международные"},
        article_limit=5,
    )

    assert stats["added"] == 1
    assert fetched_urls == [
        "https://example.com/news",
        "https://example.com/news/2026/05/field-automation-rollout",
    ]
    assert inserted[0]["url"] == "https://example.com/news/2026/05/field-automation-rollout"
    assert state["source_id"] == 7
    assert state["last_seen_article_url"] == "https://example.com/news/2026/05/field-automation-rollout"
