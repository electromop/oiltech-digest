from oiltech_digest.ingestion import article_fetcher


ARTICLE_HTML = b"""
<html>
  <body>
    <nav>Home Products Subscribe Contact</nav>
    <article class="article-content">
      <h1>Ignored heading</h1>
      <p>First paragraph about a new drilling technology that improves operational reliability.</p>
      <p>Second paragraph explains field deployment, measurable production impact and constraints.</p>
      <p>Third paragraph gives enough context for summary, tagging and scoring without navigation.</p>
    </article>
    <aside>Related links and promos should not dominate extraction.</aside>
  </body>
</html>
"""

JSON_LD_HTML = """
<html>
  <head>
    <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": "Structured data article",
        "articleBody": "The oilfield services company introduced a drilling automation platform for well construction teams. The system improves equipment uptime, supports production engineers, and gives enough detailed operational context for downstream summary and scoring."
      }
    </script>
  </head>
  <body>
    <div class="page-shell">Subscribe and follow us</div>
  </body>
</html>
"""


def test_extract_main_text_prefers_article_content():
    text = article_fetcher.extract_main_text(ARTICLE_HTML)

    assert "new drilling technology" in text
    assert "field deployment" in text
    assert "Home Products Subscribe" not in text
    assert "Related links" not in text


def test_extract_main_text_uses_json_ld_article_body():
    text = article_fetcher.extract_main_text(JSON_LD_HTML)

    assert "drilling automation platform" in text
    assert "equipment uptime" in text
    assert "Subscribe and follow us" not in text


def test_is_better_text_requires_meaningful_gain():
    current = "Short RSS teaser about drilling."
    extracted = "Full text. " * 40

    assert not article_fetcher._is_better_text(extracted, current, min_chars=800)
    assert article_fetcher._is_better_text(extracted * 3, current, min_chars=800)


def test_extract_og_image_prefers_open_graph():
    html = b"""
    <html><head>
      <meta property="og:image" content="https://cdn.example.com/lead.jpg">
      <meta name="twitter:image" content="https://cdn.example.com/tw.jpg">
    </head><body></body></html>
    """
    assert article_fetcher.extract_og_image(html) == "https://cdn.example.com/lead.jpg"


def test_extract_og_image_falls_back_to_twitter_and_handles_missing():
    only_twitter = b'<html><head><meta name="twitter:image" content="https://c.example.com/t.png"></head></html>'
    assert article_fetcher.extract_og_image(only_twitter) == "https://c.example.com/t.png"
    # Нет картинок и относительный URL → пусто (в карточке будет фирменная заглушка)
    assert article_fetcher.extract_og_image(b"<html><head></head></html>") == ""
    assert article_fetcher.extract_og_image(b'<meta property="og:image" content="/local.png">') == ""
