"""Страница компании LinkedIn без входа: посты из разметки schema.org, сбор только через NL.

Страницы здесь синтетические, по устройству настоящих (снято 12.11 у Halliburton и Baker Hughes)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from oiltech_digest import cli, config
from oiltech_digest.db import repository
from oiltech_digest.db import connection
from oiltech_digest.ingestion import external_fetch, linkedin_parser


def _posting(slug, activity, text, date, author=None, headline=None, host="www"):
    node = {
        "@type": "SocialMediaPosting",
        "author": {"@type": "Organization", "name": slug, "url": f"https://www.linkedin.com/company/{author or slug}"},
        "datePublished": date,
        "text": text,
        "url": f"https://{host}.linkedin.com/posts/{slug}_post-activity-{activity}-AbCd?utm_source=share",
    }
    if headline is not None:
        node["headline"] = headline
    return node


def _page(*nodes, base="https://www.linkedin.com/company/halliburton/", title="Halliburton | LinkedIn"):
    graph = {"@context": "http://schema.org", "@graph": [*nodes, {"@type": "Organization", "name": "x"}]}
    return (f'<html><head><base href="{base}"><title>{title}</title>'
            f'<script type="application/ld+json">{json.dumps(graph, ensure_ascii=False)}</script></head><body></body></html>')


TECH = ("What if you could understand cement condition before mobilizing resources >> https://lnkd.in/eNAy\n\n"
        "Halliburton introduces the IntelliTTCE service, a through-tubing cement evaluation for P&A.")
PAGE = _page(
    _posting("halliburton", 2, "An internship in Colombia was just the beginning.", "2026-11-09T14:00:22.858Z"),
    _posting("halliburton", 3, TECH, "2026-11-11T13:12:08.493Z", headline=""),
    _posting("halliburton", 1, "Proud to share our partner's news", "2026-11-10T10:00:00Z", author="partnerco"),
)


def test_posts_come_from_markup_newest_first_without_reposts():
    posts = linkedin_parser.extract_posts(PAGE, "halliburton")

    assert [post.url for post in posts] == [
        "https://www.linkedin.com/posts/halliburton_post-activity-3-AbCd",
        "https://www.linkedin.com/posts/halliburton_post-activity-2-AbCd",
    ]
    first = posts[0]
    assert first.title == "What if you could understand cement condition before mobilizing resources"
    assert first.published_at == datetime(2026, 11, 11, 13, 12, 8, 493000, tzinfo=timezone.utc)
    assert "IntelliTTCE" in first.text and "https://lnkd.in/eNAy" in first.text


def test_rendered_bytes_are_utf8():
    page = _page(_posting("halliburton", 7, "Erika Tovar’s career — «ГРП» across six countries", "2026-11-09T14:00:00Z"))

    assert linkedin_parser.extract_posts(page.encode("utf-8"), "halliburton")[0].text.startswith("Erika Tovar’s career — «ГРП»")


def test_unicode_bold_becomes_plain_letters():
    page = _page(_posting("welltec", 8, "𝗔 𝗴𝗿𝗼𝘂𝗻𝗱𝗯𝗿𝗲𝗮𝗸𝗶𝗻𝗴 𝘀𝗼𝗹𝘂𝘁𝗶𝗼𝗻 𝗳𝗼𝗿 𝗶𝗻𝗳𝗹𝗼𝘄 𝗰𝗼𝗻𝘁𝗿𝗼𝗹.\n\nWelltec® 𝘪𝘯𝘧𝘭𝘰𝘸 valve",
                          "2026-10-22T09:00:00Z"))

    post = linkedin_parser.extract_posts(page, "welltec")[0]

    assert post.title == "A groundbreaking solution for inflow control."
    assert "Welltec® inflow valve" in post.text


def test_headline_wins_and_one_post_under_two_hosts_is_one():
    page = _page(
        _posting("bakerhughes", 5, "Getting more from existing assets.", "2026-11-02T10:00:00Z",
                 headline="Leucipa: Turning Production Data into Operational Action"),
        _posting("bakerhughes", 5, "Getting more from existing assets.", "2026-11-02T10:00:00Z", host="uk"),
    )

    posts = linkedin_parser.extract_posts(page, "bakerhughes")

    assert [post.title for post in posts] == ["Leucipa: Turning Production Data into Operational Action"]


@pytest.mark.parametrize(("content", "status"), [
    (PAGE, "ok"),
    (_page(base="https://www.linkedin.com/authwall?trk=bf", title="Sign Up | LinkedIn"), "blocked:authwall"),
    ('<html><head><title>Just a moment...</title></head></html>', "blocked:challenge"),
    (None, "empty"),
])
def test_page_status_tells_a_wall_from_no_news(content, status):
    assert linkedin_parser.page_status(content) == status


@pytest.mark.parametrize(("raw", "slug"), [
    ("halliburton", "halliburton"),
    ("https://uk.linkedin.com/company/slbglobal/posts/?feedView=all", "slbglobal"),
    ("linkedin.com/showcase/bh-leucipa", "bh-leucipa"),
    ("https://t.me/halliburton", None),
    ("https://www.linkedin.com/in/someone/", None),
])
def test_company_slug(raw, slug):
    assert linkedin_parser.company_slug(raw) == slug


def test_post_record_is_complete_text_with_language():
    record = linkedin_parser.post_record(linkedin_parser.extract_posts(PAGE, "halliburton")[0])

    assert record["text_truncated"] is False
    assert record["language"] == "en"
    assert linkedin_parser.guess_language("Газпром нефть испытала новый роторно-управляемый комплекс") == "ru"


def test_due_sources_respects_interval_pause_and_cycle_limit():
    now = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
    sources = [
        {"id": 1, "last_parsed_at": now - timedelta(hours=2)},                      # заходили недавно
        {"id": 2, "last_parsed_at": now - timedelta(hours=30)},
        {"id": 3, "last_parsed_at": None},                                           # не заходили ни разу
        {"id": 4, "last_parsed_at": now - timedelta(hours=50)},
        {"id": 5, "last_parsed_at": None, "external_cooldown_until": now + timedelta(hours=5)},  # пауза после блока
    ]

    due = linkedin_parser.due_sources(sources, now=now, interval_hours=20, per_cycle=2)

    assert [source["id"] for source in due] == [3, 4]


def _source(**extra):
    return {"id": 9, "name": "halliburton (LinkedIn)", "parse_strategy": "linkedin", "network_region": "external",
            "url": "https://www.linkedin.com/company/halliburton/", "category": "международные", **extra}


def _keep_all(monkeypatch):
    monkeypatch.setattr(external_fetch, "should_keep_article", lambda title, text, source: type("R", (), {"keep": True})())


def test_worker_renders_the_company_page_once_and_returns_posts(monkeypatch):
    _keep_all(monkeypatch)
    rendered = []
    monkeypatch.setattr("oiltech_digest.ingestion.playwright_parser.fetch_rendered",
                        lambda url, **kwargs: rendered.append(url) or PAGE.encode())
    beats = []

    result = external_fetch.process_payload(
        {"source": _source(), "known_urls": ["https://www.linkedin.com/posts/halliburton_post-activity-2-AbCd"]},
        heartbeat=lambda: beats.append(1))

    assert rendered == ["https://www.linkedin.com/company/halliburton/"]
    assert beats
    assert [article["url"] for article in result["articles"]] == [
        "https://www.linkedin.com/posts/halliburton_post-activity-3-AbCd"]
    assert isinstance(result["articles"][0]["published_at"], str)
    assert result["stats"]["skipped_known"] == 1
    assert result["stats"]["listing"] == "ok" and result["strategy"] == "linkedin"
    json.dumps(result)


def test_worker_reports_the_wall_instead_of_no_news(monkeypatch):
    _keep_all(monkeypatch)
    wall = _page(base="https://www.linkedin.com/authwall?trk=bf", title="Sign Up | LinkedIn")
    monkeypatch.setattr("oiltech_digest.ingestion.playwright_parser.fetch_rendered", lambda url, **kwargs: wall.encode())

    result = external_fetch.process_payload({"source": _source()})

    assert result["articles"] == []
    assert result["stats"]["listing"] == "blocked:authwall"


def test_worker_skips_posts_before_collect_since(monkeypatch):
    _keep_all(monkeypatch)
    monkeypatch.setattr("oiltech_digest.ingestion.playwright_parser.fetch_rendered", lambda url, **kwargs: PAGE.encode())

    result = external_fetch.process_payload({"source": _source(collect_since="2026-11-10T00:00:00+00:00")})

    assert len(result["articles"]) == 1 and result["stats"]["skipped_old"] == 1


def test_only_a_build_that_knows_linkedin_gets_the_job():
    from oiltech_digest import contract

    assert contract.CONTRACT >= 12
    assert contract.required_contract("scrape_source", {"source": _source()}) == 12
    assert contract.required_contract("scrape_source", {"source": {**_source(), "parse_strategy": "rss"}}) == 0


def test_linkedin_goes_to_the_browser_lane_on_nl(monkeypatch):
    from oiltech_digest import network_policy

    monkeypatch.setattr(config, "EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr(config, "FETCH_EXTERNAL_ENABLED", True)

    decision = network_policy.route_source_task(_source(), task_kind="scrape")

    assert (decision.queue_name, decision.capability) == ("external-playwright", "playwright")


# --- ядро: база ---------------------------------------------------------------

def _add(company):
    cli.main(["add-linkedin-source", company])
    with connection.get_connection() as conn:
        return conn.execute("SELECT id FROM sources WHERE source_type = 'LinkedIn' ORDER BY id DESC LIMIT 1").fetchone()[0]


def test_add_source_is_external_linkedin_and_idempotent(isolated_db, capsys):
    first = _add("https://uk.linkedin.com/company/halliburton/posts")
    second = _add("halliburton")

    source = repository.get_source(first)
    assert first == second
    assert (source["parse_strategy"], source["network_region"], source["enabled"]) == ("linkedin", "external", True)
    assert source["listing_url"] == "https://www.linkedin.com/company/halliburton/"
    assert source["collect_since"] is not None  # новый источник не тащит архив ленты
    with pytest.raises(SystemExit):
        cli.main(["add-linkedin-source", "https://example.com/halliburton"])


def _jobs():
    with connection.get_connection() as conn:
        return conn.execute(
            "SELECT payload_json->>'source_id', queue_name, max_attempts FROM background_jobs "
            "WHERE kind = 'scrape_source' ORDER BY id").fetchall()


def _delays():
    with connection.get_connection() as conn:
        rows = conn.execute("SELECT extract(epoch FROM run_after - created_at) FROM background_jobs "
                            "WHERE kind = 'scrape_source' ORDER BY id").fetchall()
    return [round(float(row[0]) / 60) for row in rows]


def test_enqueue_takes_few_companies_per_cycle_and_never_twice(isolated_db, monkeypatch, capsys):
    monkeypatch.setattr(config, "EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr(config, "FETCH_EXTERNAL_ENABLED", True)
    monkeypatch.setattr(config, "LINKEDIN_SOURCES_PER_CYCLE", 2)
    ids = [_add(company) for company in ("halliburton", "bakerhughes", "slbglobal")]

    cli.main(["enqueue-external-scrape"])
    cli.main(["enqueue-external-scrape"])  # следующий цикл: две уже в очереди, третья

    jobs = _jobs()
    assert sorted(int(source_id) for source_id, _, _ in jobs) == sorted(ids)
    assert {(queue, attempts) for _, queue, attempts in jobs} == {("external-playwright", 1)}
    # Браузер NL один поток: без разноса компании шли бы подряд, а шестой заход подряд LinkedIn закрывает.
    assert _delays() == [0, config.LINKEDIN_SPACING_MINUTES, 0]


def test_a_wall_pauses_every_linkedin_company(isolated_db, monkeypatch, capsys):
    monkeypatch.setattr(config, "EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr(config, "FETCH_EXTERNAL_ENABLED", True)
    ids = [_add(company) for company in ("halliburton", "bakerhughes")]

    applied = external_fetch.apply_scrape_result({
        "source_id": ids[0], "strategy": "linkedin", "articles": [],
        "stats": {"listing": "blocked:challenge"},
    })

    assert applied["paused_sources"] == 2
    cli.main(["enqueue-external-scrape"])
    assert _jobs() == []
    # Задача, поставленная до блока и ждавшая очереди, на паузе не выдаётся.
    from oiltech_digest.processing.external_ai import InvalidJobPayload
    with pytest.raises(InvalidJobPayload, match="паузе"):
        external_fetch.build_scrape_source_payload(ids[1], {"source_id": ids[1]})


def test_posts_are_inserted_and_not_sent_to_refetch(isolated_db, monkeypatch):
    _keep_all(monkeypatch)
    source_id = _add("halliburton")
    monkeypatch.setattr("oiltech_digest.ingestion.playwright_parser.fetch_rendered", lambda url, **kwargs: PAGE.encode())
    source = repository.get_source(source_id)
    with connection.get_connection() as conn:
        conn.execute("UPDATE sources SET collect_since = NULL WHERE id = %s", (source_id,))
        conn.commit()

    result = external_fetch.process_payload({"source": external_fetch._jsonable_dict({**source, "collect_since": None})})
    applied = external_fetch.apply_scrape_result(result)

    assert applied["inserted"] == 2
    assert repository.external_refetch_candidates() == []  # посты короткие, но полные
