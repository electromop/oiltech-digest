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


# --- Защита от подмены текста статьи (задача №24) ---------------------------------
# Дефект 28.07: у 10.4% видимой ленты (у Neftegaz.ru — 37.7%) тело было от ДРУГОЙ
# новости. Принадлежность не проверялась вообще: `_is_better_text` смотрел только
# на длину, поэтому листинг/пейвол/«избранный» материал из JSON-LD побеждали статью
# и записывались со статусом ok — то есть порча помечалась успехом.

FOREIGN_BODY_HTML = b"""
<html><body><article>
  <p>Researchers at Perm Polytech developed a digital model of a check valve heater
     for arctic fields, calculating the heating power required to prevent icing at
     temperatures ranging from minus thirty to minus seventy degrees Celsius.</p>
  <p>For several heater ratings the study established critical boundaries: sixty watts
     down to minus forty four degrees, seventy two watts down to minus fifty two degrees,
     eighty four watts down to minus sixty degrees and ninety six watts to minus seventy.</p>
  <p>The practical effect is lower energy consumption and fewer unplanned shutdowns
     across remote arctic production sites during the cold season, because operators can
     tune valve heating precisely instead of running heaters at maximum power all winter.</p>
  <p>Validation so far is limited to laboratory simulation without field trials, so the
     economic estimate remains indicative rather than confirmed, and the authors note that
     industrial deployment would require additional qualification on operating equipment.</p>
</article></body></html>
"""


def _article(**over):
    base = {"id": 1, "source_id": 7, "url": "https://example.com/a", "title": "", "raw_text": ""}
    base.update(over)
    return base


def test_fetch_article_text_rejects_body_that_does_not_match_title(monkeypatch):
    """Реальный кейс владельца 28.07: заголовок про суд над TotalEnergies,
    а тело — про цифровую модель клапана Пермского Политеха."""
    monkeypatch.setattr(article_fetcher, "fetch", lambda url: FOREIGN_BODY_HTML)
    monkeypatch.setattr(article_fetcher.repository, "body_hash_belongs_to_other_article",
                        lambda *a, **k: False)

    result = article_fetcher.fetch_article_text(_article(
        title="TotalEnergies обжалует решение суда Франции о климатических целях компании"))

    assert result.status == "mismatch"
    assert result.text == ""          # чужой текст НЕ отдаётся на запись
    assert "rejected" in (result.error or "")


def test_fetch_article_text_accepts_body_matching_title(monkeypatch):
    """Контроль: страж не должен резать здоровые статьи — цена ложного отказа выше."""
    monkeypatch.setattr(article_fetcher, "fetch", lambda url: FOREIGN_BODY_HTML)
    monkeypatch.setattr(article_fetcher.repository, "body_hash_belongs_to_other_article",
                        lambda *a, **k: False)

    result = article_fetcher.fetch_article_text(_article(
        title="Perm Polytech researchers model check valve heater power for arctic fields"))

    assert result.status == "ok"
    assert "Perm Polytech" in result.text


def test_fetch_article_text_rejects_body_already_stored_for_another_article(monkeypatch):
    """Второй рубеж: сайт стабильно отдаёт одну страницу на все URL (пейвол).
    Заголовок может формально пересечься, поэтому ловим по совпадению тела."""
    monkeypatch.setattr(article_fetcher, "fetch", lambda url: FOREIGN_BODY_HTML)
    seen = {}

    def already_seen(source_id, body_hash, exclude_article_id=None):
        seen["args"] = (source_id, body_hash, exclude_article_id)
        return True

    monkeypatch.setattr(article_fetcher.repository,
                        "body_hash_belongs_to_other_article", already_seen)

    result = article_fetcher.fetch_article_text(_article(
        title="Perm Polytech researchers model check valve heater power for arctic fields"))

    assert result.status == "mismatch"
    assert "identical body" in (result.error or "")
    assert seen["args"][0] == 7 and seen["args"][2] == 1   # проверка идёт по паре и без self


def test_short_title_is_not_judged(monkeypatch):
    """«SOCAR» или «May 2026» — заголовков в 1-2 слова не хватает для суждения,
    и страж обязан пропускать их, а не резать вслепую."""
    monkeypatch.setattr(article_fetcher, "fetch", lambda url: FOREIGN_BODY_HTML)
    monkeypatch.setattr(article_fetcher.repository, "body_hash_belongs_to_other_article",
                        lambda *a, **k: False)

    result = article_fetcher.fetch_article_text(_article(title="SOCAR"))

    assert result.status == "ok"


def test_mismatch_keeps_previous_text_and_is_counted_separately(monkeypatch):
    """mismatch НЕ должен идти в failed: иначе не видно, работает ли страж.
    И прежний (свой, пусть короткий) текст обязан остаться нетронутым."""
    monkeypatch.setattr(article_fetcher, "fetch", lambda url: FOREIGN_BODY_HTML)
    monkeypatch.setattr(article_fetcher.repository, "body_hash_belongs_to_other_article",
                        lambda *a, **k: False)
    monkeypatch.setattr(article_fetcher.repository, "get_articles_needing_full_text",
                        lambda **k: [_article(title="TotalEnergies обжалует решение суда Франции о климате")])
    written = {}

    def capture(article_id, **kwargs):
        written.update(kwargs)

    monkeypatch.setattr(article_fetcher.repository, "update_article_full_text", capture)

    stats = article_fetcher.fetch_full_text(limit=1)

    assert stats["mismatch"] == 1
    assert stats["failed"] == 0
    assert written["raw_text"] is None        # прежний текст не затирается
