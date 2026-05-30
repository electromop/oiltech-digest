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


def test_extract_main_text_prefers_article_content():
    text = article_fetcher.extract_main_text(ARTICLE_HTML)

    assert "new drilling technology" in text
    assert "field deployment" in text
    assert "Home Products Subscribe" not in text
    assert "Related links" not in text


def test_is_better_text_requires_meaningful_gain():
    current = "Short RSS teaser about drilling."
    extracted = "Full text. " * 40

    assert not article_fetcher._is_better_text(extracted, current, min_chars=800)
    assert article_fetcher._is_better_text(extracted * 3, current, min_chars=800)
