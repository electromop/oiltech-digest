"""Страница LinkedIn и разбор поста сервиса Apify в статью: адреса, вид страницы, текст, заголовок, картинка.

Посты выдуманные (компания Acme Drilling, эксперт Jane Doe), по форме ответа сервиса: content, postedAt, linkedinUrl,
у репоста repostedBy и repostedAt, у цитаты repost, у статьи LinkedIn article, у документа document.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from oiltech_digest.ingestion import linkedin_posts
from oiltech_digest.ingestion.linkedin_posts import COMPANY, PROFILE, Page

POST_URL = "https://www.linkedin.com/posts/acme-drilling_drilling-activity-7000000000000000001-AbCd"


def _item(**overrides):
    item = {
        "type": "post",
        "linkedinUrl": POST_URL,
        "content": "Acme Drilling completed the first coiled tubing drilling job in the region. The rig cut "
                   "the well time by 30%.\n\n#drilling #oilandgas",
        "postedAt": {"timestamp": 1794000000000, "date": "2026-11-11T21:20:00.000Z"},
        "author": {"name": "Acme Drilling", "type": "company"},
        "postImages": [{"url": "https://media.licdn.com/dms/image/v2/abc/feedshare/0/1?e=1&v=beta", "width": 1200,
                        "height": 627}],
    }
    item.update(overrides)
    return item


@pytest.mark.parametrize("raw, section, slug", [
    ("https://www.linkedin.com/company/acme-drilling/", "company", "acme-drilling"),
    ("https://uk.linkedin.com/company/acme-drilling/posts/?feedView=all", "company", "acme-drilling"),
    ("linkedin.com/company/acme-drilling?lipi=urn%3Ali%3Apage", "company", "acme-drilling"),
    ("https://www.linkedin.com/in/jane-doe-12ab34/", "in", "jane-doe-12ab34"),
    ("www.linkedin.com/in/jane-doe-12ab34/recent-activity/all/", "in", "jane-doe-12ab34"),
])
def test_page_from_url_takes_company_showcase_and_profile_addresses(raw, section, slug):
    page = linkedin_posts.page_from_url(raw)
    assert page == Page(section, slug)
    assert page.url == f"https://www.linkedin.com/{section}/{slug}/"


@pytest.mark.parametrize("raw", [
    "", None, "acme-drilling", "https://example.com/company/acme-drilling/",
    "https://www.linkedin.com/posts/acme-drilling_x-activity-1-a", "https://www.linkedin.com/search/results/all/",
    "https://www.linkedin.com/company/", "https://evil.example/?u=linkedin.com/company/acme",
    "https://www.linkedin.com.evil.example/company/acme/", "https://www.linkedin.com/showcase/acme-subsea/",
])
def test_page_from_url_refuses_what_is_not_a_page(raw):
    assert linkedin_posts.page_from_url(raw) is None


def test_page_kind_is_company_or_profile():
    assert Page("company", "a").kind == COMPANY
    assert Page("in", "a").kind == PROFILE


def test_post_url_drops_tails_and_refuses_other_hosts():
    assert linkedin_posts.post_url(POST_URL + "/?utm_source=share&rcm=x") == POST_URL
    assert linkedin_posts.post_url(POST_URL.replace("www.", "uk.")) == POST_URL
    assert linkedin_posts.post_url("https://example.com/posts/x") is None
    assert linkedin_posts.post_url("https://www.linkedin.com/company/acme/") is None
    assert linkedin_posts.post_url("javascript:alert(1)//www.linkedin.com/posts/x") is None


def test_clean_text_turns_math_bold_letters_into_plain_ones():
    bold = "\U0001d5d4 \U0001d5f4\U0001d5ff\U0001d5fc\U0001d602\U0001d5fb\U0001d5f1\U0001d5ef\U0001d5ff\U0001d5f2\U0001d5ee\U0001d5f8"
    assert linkedin_posts.clean_text(bold + " tool\n\n\n\nnext") == "A groundbreak tool\n\nnext"


def test_title_is_the_first_sentence_without_links():
    text = "Read our new field report on stuck pipe prevention >> https://lnkd.in/abc123\n\nMore text here."
    assert linkedin_posts.post_title(text) == "Read our new field report on stuck pipe prevention"
    assert linkedin_posts.post_title("") == linkedin_posts.FALLBACK_TITLE


def test_own_post_becomes_an_article_record():
    post = linkedin_posts.parse_post(_item())
    assert post.url == POST_URL
    assert post.title == "Acme Drilling completed the first coiled tubing drilling job in the region."
    assert post.text.startswith("Acme Drilling completed")
    assert post.published_at == datetime(2026, 11, 11, 21, 20, tzinfo=timezone.utc)
    assert post.seen_at == post.published_at
    assert post.language == "en"
    assert post.image_source.startswith("https://media.licdn.com/")
    record = post.record(7, "https://platform.example/media/post-images/x.jpg")
    assert record["source_id"] == 7 and record["url"] == POST_URL and record["text_truncated"] is False
    assert record["image_url"] == "https://platform.example/media/post-images/x.jpg"
    assert record["content_hash"]


def test_repost_keeps_the_original_date_and_is_seen_at_the_repost_time():
    post = linkedin_posts.parse_post(_item(repostedBy={"name": "Jane Doe"},
                                           repostedAt={"date": "2026-11-20T08:00:00.000Z"}))
    assert post.published_at == datetime(2026, 11, 11, 21, 20, tzinfo=timezone.utc)
    assert post.seen_at == datetime(2026, 11, 20, 8, 0, tzinfo=timezone.utc)


def test_quote_post_carries_the_quoted_text_and_author():
    post = linkedin_posts.parse_post(_item(
        content="Proud to see this rolled out offshore!",
        repost={"content": "Our automated drilling system cut connection time by half on a deepwater well.",
                "author": {"name": "Acme Subsea"}}))
    assert post.title == "Proud to see this rolled out offshore!"
    assert "Quoted post by Acme Subsea:\nOur automated drilling system cut connection time" in post.text


def test_article_and_document_titles_join_the_text_once():
    post = linkedin_posts.parse_post(_item(
        content="New issue of our newsletter is out.",
        article={"title": "How automation reshapes well delivery", "description": "Five lessons from 2026.",
                 "image": {"url": "https://media.licdn.com/article-cover.jpg"}},
        document={"title": "Field results deck"}, postImages=[]))
    assert post.text == ("New issue of our newsletter is out.\n\nHow automation reshapes well delivery\n\n"
                         "Five lessons from 2026.\n\nField results deck")
    assert post.image_source == "https://media.licdn.com/article-cover.jpg"


def test_image_falls_back_to_video_frame_and_document_cover():
    video = linkedin_posts.parse_post(_item(postImages=[], postVideo={"thumbnailUrl": "https://media.licdn.com/v.jpg"}))
    assert video.image_source == "https://media.licdn.com/v.jpg"
    cover = linkedin_posts.parse_post(_item(postImages=[], document={
        "title": "Deck", "coverPages": [{"imageUrls": ["https://media.licdn.com/cover-1.jpg"]}]}))
    assert cover.image_source == "https://media.licdn.com/cover-1.jpg"
    nothing = linkedin_posts.parse_post(_item(postImages=[{"url": "http://media.licdn.com/plain-http.jpg"}]))
    assert nothing.image_source is None


def test_seen_at_is_taken_even_from_an_unreadable_post():
    assert linkedin_posts.seen_at({"postedAt": {"date": "2026-11-21T10:00:00.000Z"}, "content": ""}) == datetime(
        2026, 11, 21, 10, 0, tzinfo=timezone.utc)
    assert linkedin_posts.seen_at({"repostedAt": {"date": "2026-11-22T10:00:00.000Z"},
                                   "postedAt": {"date": "2026-11-01T10:00:00.000Z"}}).day == 22
    assert linkedin_posts.seen_at("not a dict") is None


def test_time_falls_back_to_the_millisecond_stamp():
    post = linkedin_posts.parse_post(_item(postedAt={"timestamp": 1794000000000}))
    assert post.published_at == datetime.fromtimestamp(1794000000, tz=timezone.utc)


def test_cyrillic_post_is_russian():
    post = linkedin_posts.parse_post(_item(content="Компания завершила бурение первой скважины на шельфе."))
    assert post.language == "ru"


@pytest.mark.parametrize("item", [
    {"content": "No address"},
    {"linkedinUrl": POST_URL, "content": "   "},
    {"linkedinUrl": "https://example.com/posts/x", "content": "Wrong host"},
    "not a dict",
])
def test_post_without_address_or_text_is_skipped(item):
    assert linkedin_posts.parse_post(item) is None
