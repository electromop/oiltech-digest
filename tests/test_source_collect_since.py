"""Дата начала сбора у источника (решение владельца 06.11): новый источник не тащит архив своей ленты.

ФИПС 06.11: три ленты принесли при подключении 264 статьи с 2017 года (свежих за две недели 27), и все ушли в «Отбор»:
он берёт статьи по дате сбора, а «Карточка» возвращает только событие, старое к дате своей же публикации, поэтому четыре
публикации 2023 года вышли на радар. Граница: `sources.collect_since`. У нового источника база ставит её на две недели
раньше заведения, у прежних источников её нет. Для всех стратегий сбора это тот же рубеж OLD, что и окно свежести
прогона (`verdicts.old_cutoff`), а проба источника показывает то же, что делает сбор.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from oiltech_digest.db import connection, repository
from oiltech_digest.ingestion import (
    external_fetch,
    relevance_filter,
    request_parser,
    rss_parser,
    source_probe,
    telegram_parser,
    verdicts,
)
from oiltech_digest.ingestion.request_parser import CandidateLink
from oiltech_digest.ingestion.telegram_parser import TelegramPost

DRILLING = "Компания начала бурение новой скважины на месторождении и ввела буровую установку в работу. "


@pytest.fixture(autouse=True)
def _static_prefilter(monkeypatch):
    """Предфильтр на статическом словаре: тематики из других тестов не меняют вердикт."""
    monkeypatch.setattr(relevance_filter, "tag_keywords", lambda: ((), ()))


def _ago(days: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _near(left: datetime | None, right: datetime, seconds: float = 120) -> bool:
    return left is not None and abs((left - right).total_seconds()) < seconds


def _feed(items: list[tuple[str, str, datetime | None]]) -> bytes:
    body = "".join(
        f"<item><title>{title}</title><link>{link}</link><description>{title}. {DRILLING}</description>"
        + (f"<pubDate>{format_datetime(published)}</pubDate>" if published else "")
        + "</item>"
        for title, link, published in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>Лента</title>{body}</channel></rss>'.encode()


def _urls() -> set[str]:
    with connection.get_connection() as conn:
        return {row[0] for row in conn.execute("SELECT url FROM articles").fetchall()}


def _collect_since(source_id: int) -> datetime | None:
    with connection.get_connection() as conn:
        return conn.execute("SELECT collect_since FROM sources WHERE id = %s", (source_id,)).fetchone()[0]


def _source_id(name: str) -> int:
    with connection.get_connection() as conn:
        return int(conn.execute("SELECT id FROM sources WHERE name = %s", (name,)).fetchone()[0])


# --- граница рубежа OLD


def test_cutoff_is_the_later_of_run_window_and_source_start():
    since = _ago(14)
    assert verdicts.old_cutoff({}, None) is None
    assert verdicts.old_cutoff({"collect_since": None}, None) is None
    assert verdicts.old_cutoff({"collect_since": since}, None) == since
    assert _near(verdicts.old_cutoff({}, 30), _ago(30))
    assert verdicts.old_cutoff({"collect_since": since}, 30) == since, "дата начала позже окна прогона"
    assert _near(verdicts.old_cutoff({"collect_since": _ago(60)}, 30), _ago(30)), "окно прогона позже даты начала"


def test_cutoff_reads_source_start_from_worker_payload():
    """Воркер NL получает источник JSON-ом: дата начала приходит строкой ISO."""
    since = datetime(2026, 10, 23, 8, 38, 45, tzinfo=timezone.utc)
    assert verdicts.old_cutoff({"collect_since": since.isoformat()}, None) == since
    assert verdicts.old_cutoff({"collect_since": "2026-10-23T08:38:45Z"}, None) == since
    assert verdicts.old_cutoff({"collect_since": datetime(2026, 10, 23, 8, 38, 45)}, None) == since, "без пояса: UTC"


# --- дата начала ставится при заведении источника любым путём


def test_new_source_starts_two_weeks_before_it_is_added_by_any_path(isolated_db):
    screen = repository.add_rss_source(name="ФИПС (новости)", rss_url="https://fips.example/rss",
                                       source_type="Government")
    repository.upsert_source({"name": "Из таблицы", "source_type": "Journal", "url": "https://journal.example",
                              "rss_url": None, "enabled": True, "parse_strategy": "request", "category": None,
                              "priority": None})
    candidate = repository.upsert_source_candidate({"url": "https://agent.example/news", "name": "Находка агента"})
    agent = repository.approve_source_candidate(candidate)

    # Сверка по часам базы, а не по часам теста: часы ВМ colima после сна Mac отстают на минуты (07.11 на 15).
    with connection.get_connection() as conn:
        starts = conn.execute("SELECT id, collect_since, created_at FROM sources WHERE id = ANY(%s)",
                              ([screen, _source_id("Из таблицы"), agent],)).fetchall()
    assert len(starts) == 3
    for source_id, since, created in starts:
        assert since == created - timedelta(days=14), source_id


def test_adding_existing_source_again_keeps_its_start(isolated_db):
    """Повторное заведение (ON CONFLICT) дату не трогает: источник, заведённый до колонки, не получает её задним
    числом, а у нового она не сдвигается вперёд."""
    old = repository.add_rss_source(name="Прежний", rss_url="https://old.example/rss")
    with connection.get_connection() as conn:
        conn.execute("UPDATE sources SET collect_since = NULL WHERE id = %s", (old,))
        conn.commit()
    fresh = repository.add_rss_source(name="Новый", rss_url="https://new.example/rss")
    start = _collect_since(fresh)

    repository.add_rss_source(name="Прежний", rss_url="https://old.example/rss2")
    repository.add_rss_source(name="Новый", rss_url="https://new.example/rss2")

    assert _collect_since(old) is None
    assert _collect_since(fresh) == start


# --- сбор и проба по стратегиям


def _archive_feed(base: str) -> bytes:
    return _feed([
        ("Свежая новость о бурении скважины", f"{base}/fresh", _ago(2)),
        ("Новость без даты о бурении скважины", f"{base}/undated", None),
        ("Новость месячной давности о бурении", f"{base}/month", _ago(30)),
        ("Архивная новость 2017 года о бурении", f"{base}/2017", datetime(2017, 3, 10, tzinfo=timezone.utc)),
    ])


def test_new_rss_source_skips_its_archive_and_probe_says_the_same(isolated_db, monkeypatch):
    source = repository.get_source(repository.add_rss_source(name="Архивная лента", rss_url="https://feed.example/rss",
                                                             url="https://feed.example"))
    base = "https://feed.example/news"
    feed = _archive_feed(base)
    monkeypatch.setattr(rss_parser, "fetch", lambda url: feed)

    report = source_probe.probe_source(source)
    stats = rss_parser.parse_source(source)

    assert {row["url"]: row["verdict"] for row in report["rows"]} == {
        f"{base}/fresh": verdicts.WOULD_INSERT, f"{base}/undated": verdicts.WOULD_INSERT,
        f"{base}/month": verdicts.OLD, f"{base}/2017": verdicts.OLD,
    }
    assert (stats["added"], stats["skipped_old"]) == (2, 2)
    assert _urls() == {f"{base}/fresh", f"{base}/undated"}, "статья без даты собирается: судить её не по чему"
    assert any(note.startswith("дата начала сбора ") for note in report["notes"]), "проба называет причину вердикта old"


def test_source_without_start_collects_as_before(isolated_db, monkeypatch):
    source_id = repository.add_rss_source(name="Давний источник", rss_url="https://feed.example/rss")
    with connection.get_connection() as conn:
        conn.execute("UPDATE sources SET collect_since = NULL WHERE id = %s", (source_id,))
        conn.commit()
    base = "https://feed.example/news"
    feed = _archive_feed(base)
    monkeypatch.setattr(rss_parser, "fetch", lambda url: feed)

    stats = rss_parser.parse_source(repository.get_source(source_id))

    assert (stats["added"], stats["skipped_old"]) == (4, 0)


def test_listing_candidate_older_than_start_is_not_even_fetched(monkeypatch):
    monkeypatch.setattr(request_parser.repository, "article_exists", lambda url: False)
    fetched: list[str] = []

    def fetcher(candidate, source):
        fetched.append(candidate.url)
        published = _ago(40) if candidate.url.endswith("undated") else candidate.published_at
        return {"source_id": source["id"], "title": candidate.title, "url": candidate.url, "published_at": published,
                "raw_text": DRILLING * 5, "text_truncated": False, "language": "ru", "content_hash": candidate.url}

    source = {"id": 1, "name": "Сайт", "collect_since": _ago(14)}
    old = CandidateLink("https://site.example/old", "Старая новость о бурении скважины", 5, _ago(40))
    undated = CandidateLink("https://site.example/undated", "Новость без даты в листинге о бурении", 5)
    fresh = CandidateLink("https://site.example/fresh", "Свежая новость о бурении скважины", 5, _ago(1))

    steps = list(request_parser.candidate_steps(source, [old, undated, fresh], None, fetcher))

    assert [step.stage for step in steps] == [verdicts.OLD, verdicts.OLD, verdicts.READY]
    assert fetched == [undated.url, fresh.url], "старую по листингу страницу не качают; без даты судят по странице"


def test_telegram_post_older_than_start_is_old(monkeypatch):
    monkeypatch.setattr(telegram_parser.repository, "article_exists", lambda url: False)
    source = {"id": 1, "name": "Канал", "collect_since": _ago(14)}
    posts = [TelegramPost("https://t.me/chan/2", "Свежий пост о бурении", DRILLING, _ago(1)),
             TelegramPost("https://t.me/chan/1", "Старый пост о бурении", DRILLING, _ago(40))]

    assert [step.stage for step in telegram_parser.post_steps(source, posts)] == [verdicts.READY, verdicts.OLD]


# --- зарубежный воркер и ядро


def test_worker_skips_listing_older_than_source_start_from_payload(monkeypatch):
    fetched: list[str] = []
    monkeypatch.setattr(external_fetch, "should_keep_article", lambda title, text, source: type("R", (), {"keep": True})())
    monkeypatch.setattr("oiltech_digest.ingestion.http_client.fetch", lambda url: b"<html></html>")
    old = CandidateLink("https://example.com/old", "Old article about drilling the well", 5, _ago(40))
    fresh = CandidateLink("https://example.com/fresh", "Fresh article about drilling the well", 5, _ago(1))
    monkeypatch.setattr("oiltech_digest.ingestion.request_parser.extract_candidate_links", lambda *a, **k: [old, fresh])

    def fake_fetch(candidate, source):
        fetched.append(candidate.url)
        return {"source_id": source["id"], "title": candidate.title, "url": candidate.url,
                "published_at": candidate.published_at, "raw_text": "text " * 50, "text_truncated": False,
                "language": "en", "content_hash": candidate.url}
    monkeypatch.setattr("oiltech_digest.ingestion.request_parser.fetch_article_candidate", fake_fetch)
    source = {"id": 7, "name": "S", "parse_strategy": "request", "url": "https://example.com",
              "collect_since": _ago(14).isoformat()}

    result = external_fetch.process_payload({"source": source})

    assert fetched == [fresh.url]
    assert result["stats"]["skipped_old"] == 1
    assert [article["url"] for article in result["articles"]] == [fresh.url]


def test_worker_rss_skips_feed_entries_older_than_source_start(monkeypatch):
    base = "https://feed.example/news"
    feed = _archive_feed(base)
    monkeypatch.setattr("oiltech_digest.ingestion.http_client.fetch", lambda url: feed)
    monkeypatch.setattr(external_fetch, "_fill_bodies_from_source", lambda source, recs, heartbeat=None: recs)
    source = {"id": 7, "name": "S", "parse_strategy": "rss", "rss_url": "https://feed.example/rss",
              "collect_since": _ago(14).isoformat()}

    result = external_fetch.process_payload({"source": source})

    assert sorted(article["url"] for article in result["articles"]) == [f"{base}/fresh", f"{base}/undated"]
    assert result["stats"]["skipped_old"] == 2


def test_core_does_not_insert_archive_sent_by_old_worker_build(monkeypatch):
    """Сборка NL до даты начала сбора пришлёт архив: ядро его не вставляет, а считает старым."""
    inserted: list[str] = []
    monkeypatch.setattr(external_fetch.repository, "get_source", lambda source_id: {"id": 7, "collect_since": _ago(14)})
    monkeypatch.setattr(external_fetch.repository, "insert_article", lambda article: inserted.append(article["url"]) or True)
    monkeypatch.setattr(external_fetch.repository, "touch_last_parsed", lambda source_id: None)
    monkeypatch.setattr(external_fetch.repository, "update_source_request_state", lambda source_id, **kwargs: None)

    stats = external_fetch.apply_scrape_result({"source_id": 7, "articles": [
        {"url": "https://example.com/old", "title": "Old", "published_at": _ago(40).isoformat()},
        {"url": "https://example.com/fresh", "title": "Fresh", "published_at": _ago(1).isoformat()},
        {"url": "https://example.com/undated", "title": "Undated", "published_at": None},
    ]})

    assert inserted == ["https://example.com/fresh", "https://example.com/undated"]
    assert stats == {"inserted": 2, "duplicates": 0, "skipped_before_start": 1, "source_id": 7}
