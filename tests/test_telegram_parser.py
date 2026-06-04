from oiltech_digest.ingestion import rss_parser, telegram_parser


TELEGRAM_HTML = """
<html>
  <body>
    <div class="tgme_widget_message" data-post="oiltechnews/42">
      <div class="tgme_widget_message_text js-message_text">
        Новая система автоматизации бурения внедрена на месторождении.
        Проект снизил время сервисных операций.
      </div>
      <a class="tgme_widget_message_date" href="https://t.me/oiltechnews/42">
        <time datetime="2026-06-03T10:15:00+00:00"></time>
      </a>
    </div>
    <div class="tgme_widget_message" data-post="oiltechnews/41">
      <div class="tgme_widget_message_text js-message_text">
        НПЗ запустил новую установку переработки нефти.
      </div>
      <a class="tgme_widget_message_date" href="https://t.me/oiltechnews/41">
        <time datetime="2026-06-02T08:00:00+00:00"></time>
      </a>
    </div>
  </body>
</html>
"""


def test_channel_from_url_supports_common_telegram_forms():
    assert telegram_parser.channel_from_url("https://t.me/oiltechnews") == "oiltechnews"
    assert telegram_parser.channel_from_url("https://t.me/s/oiltechnews") == "oiltechnews"
    assert telegram_parser.channel_from_url("@oiltechnews") == "oiltechnews"
    assert telegram_parser.channel_from_url("oiltechnews") == "oiltechnews"
    assert telegram_parser.channel_from_url("https://example.com/oiltechnews") is None


def test_extract_posts_from_public_preview_html():
    posts = telegram_parser.extract_posts(TELEGRAM_HTML)

    assert len(posts) == 2
    assert posts[0].url == "https://t.me/oiltechnews/42"
    assert posts[0].published_at is not None
    assert "автоматизации бурения" in posts[0].text
    assert posts[0].title.startswith("Новая система")


def test_parse_source_fetches_preview_and_inserts_new_posts(monkeypatch):
    inserted = []
    state = {}
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        return TELEGRAM_HTML

    def fake_insert(article):
        inserted.append(article)
        return True

    monkeypatch.setattr(telegram_parser, "fetch", fake_fetch)
    monkeypatch.setattr(telegram_parser.repository, "article_exists", lambda url: False)
    monkeypatch.setattr(telegram_parser.repository, "insert_article", fake_insert)
    monkeypatch.setattr(telegram_parser.repository, "touch_last_parsed", lambda source_id: state.setdefault("touched", source_id))
    monkeypatch.setattr(
        telegram_parser.repository,
        "update_source_request_state",
        lambda source_id, **kwargs: state.update({"source_id": source_id, **kwargs}),
    )

    stats = telegram_parser.parse_source(
        {"id": 15, "name": "OilTech TG", "url": "https://t.me/oiltechnews", "category": "Telegram"},
    )

    assert fetched == ["https://t.me/s/oiltechnews"]
    assert stats["added"] == 2
    assert inserted[0]["url"] == "https://t.me/oiltechnews/42"
    assert inserted[0]["language"] == "ru"
    assert state["source_id"] == 15
    assert state["last_seen_article_url"] == "https://t.me/oiltechnews/42"


def test_parse_all_dispatches_telegram_sources(monkeypatch):
    seen = []

    monkeypatch.setattr(
        rss_parser.repository,
        "get_enabled_sources",
        lambda: [{"id": 15, "parse_strategy": "telegram", "url": "https://t.me/oiltechnews"}],
    )
    monkeypatch.setattr(
        rss_parser.telegram_parser,
        "parse_source",
        lambda source, max_age_days=None: (
            seen.append((source["id"], max_age_days))
            or {"added": 1, "attempted": 1, "skipped_old": 0, "skipped_irrelevant": 0}
        ),
    )

    stats = rss_parser.parse_all(max_age_days=7, workers=1)

    assert seen == [(15, 7)]
    assert stats["added"] == 1
    assert stats["sources_ok"] == 1
