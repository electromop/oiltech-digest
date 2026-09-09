"""Тесты автообнаружения — на инлайн-фикстурах, без сети."""

from oiltech_digest.db import connection
from oiltech_digest.ingestion import rss_discovery

HTML_WITH_LINK = b"""<html><head>
<link rel="alternate" type="application/rss+xml" href="/feed.xml">
</head><body>x</body></html>"""

HTML_NO_LINK = b"<html><head><title>x</title></head><body>y</body></html>"

RSS_SAMPLE = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>T</title>
<item><title>A</title><link>https://e.com/a</link>
<pubDate>Wed, 07 May 2026 12:00:00 +0000</pubDate></item>
</channel></rss>"""


def test_links_from_html_found():
    links = rss_discovery._links_from_html(HTML_WITH_LINK, "https://site.com")
    assert "https://site.com/feed.xml" in links


def test_links_from_html_absent():
    assert rss_discovery._links_from_html(HTML_NO_LINK, "https://site.com") == []


def test_links_from_html_broken_does_not_raise():
    assert rss_discovery._links_from_html(b"\x00\x01 not html", "https://site.com") == []


def test_looks_like_feed():
    assert rss_discovery._looks_like_feed(RSS_SAMPLE) is True
    assert rss_discovery._looks_like_feed(HTML_NO_LINK) is False
    assert rss_discovery._looks_like_feed(None) is False
    assert rss_discovery._looks_like_feed(b"") is False


def test_discovery_skips_sources_configured_by_overrides(isolated_db):
    """Источник с явным listing_url НЕ отдаётся автообнаружению RSS.

    Порядок на деплое: bootstrap применяет реестр оверрайдов, а первый же цикл планировщика
    запускает discover-rss (RUN_DISCOVER_ON_START=1 по умолчанию, дальше каждые 24 цикла).
    discover_feed пробует НЕ listing_url, а `url` — то есть главную страницу издания. Если
    главная рекламирует RSS (у федеральных СМИ рекламирует всегда), update_source_rss
    перезаписывает parse_strategy на 'rss' и подставляет ОБЩИЙ фид издания — ровно тот
    мусор, ради которого правился источник (#61). Оверрайд откатывался бы в том же деплое,
    до первого парса, и выглядело бы это как «починили, а в ленте всё так же кот Ларри».

    Тот же довод уже зафиксирован в докстринге get_sources_for_discovery для 'playwright';
    здесь он распространяется на request-источники с ручным листингом.
    """
    from oiltech_digest.db import repository

    with connection.get_connection() as conn:
        overridden_id = conn.execute(
            """INSERT INTO sources (name, source_type, url, parse_strategy, listing_url, enabled)
               VALUES ('РБК Энергетика', 'Media', 'https://www.rbc.ru', 'request',
                       'https://www.rbc.ru/tags/?tag=нефть+и+газ', TRUE) RETURNING id"""
        ).fetchone()[0]
        plain_id = conn.execute(
            """INSERT INTO sources (name, source_type, url, parse_strategy, enabled)
               VALUES ('Обычный источник', 'Media', 'https://example.com', 'request', TRUE)
               RETURNING id"""
        ).fetchone()[0]
        conn.commit()

    candidate_ids = {s["id"] for s in repository.get_sources_for_discovery(only_missing=True)}

    assert overridden_id not in candidate_ids, (
        "источник с ручным listing_url попал в автообнаружение — discover-rss перезапишет "
        "parse_strategy и вернёт общую ленту издания"
    )
    # Источник без ручной настройки автообнаружение по-прежнему видит.
    assert plain_id in candidate_ids
