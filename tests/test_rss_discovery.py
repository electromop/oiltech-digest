"""Тесты автообнаружения — на инлайн-фикстурах, без сети."""

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
