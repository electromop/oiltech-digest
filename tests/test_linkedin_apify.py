"""Сбор страниц LinkedIn через сервис Apify: шаг linkedin-collect на тестовой базе с подменённым сервисом.

Что проверяется снаружи: какие посты легли статьями страницы, о чём спросили сервис (модуль, граница, потолок), что
записано в учёт расхода и в состояние источника. Клиент сервиса проверяется на подменённой сети: он ждёт итоговое
списание, потому что сервис досчитывает его после конца запуска. Страницы и посты выдуманные.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import requests

from oiltech_digest import config
from oiltech_digest.db import connection, linkedin_store
from oiltech_digest.ingestion import linkedin_apify, relevance_filter
from oiltech_digest.ingestion.linkedin_apify import ApifyClient, RunResult

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
SINCE = datetime(2026, 11, 1, tzinfo=MSK)
COMPANY_URL = "https://www.linkedin.com/company/acme-drilling/"
PROFILE_URL = "https://www.linkedin.com/in/jane-doe-12ab34/"
DRILLING = "completed a coiled tubing drilling job on an offshore oil well and cut the drilling time by 30%."


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setattr(relevance_filter, "tag_keywords", lambda: ((), ()))
    monkeypatch.setattr(config, "LINKEDIN_APIFY_ENABLED", True)
    monkeypatch.setattr(config, "APIFY_TOKEN", "test-token")
    monkeypatch.setattr(config, "LINKEDIN_APIFY_RUN_MAX_USD", 1.0)
    monkeypatch.setattr(config, "LINKEDIN_APIFY_MONTH_MAX_USD", 5.0)
    monkeypatch.setattr(config, "LINKEDIN_MIN_INTERVAL_HOURS", 20.0)


class FakeClient:
    def __init__(self, *results: RunResult) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, dict, float]] = []

    def run(self, actor, run_input, max_charge_usd):
        self.calls.append((actor, run_input, max_charge_usd))
        return self.results.pop(0) if self.results else RunResult(True, [], 0.00105)


def _post(n: int, posted: str, text: str | None = None, **extra) -> dict:
    return {"type": "post", "linkedinUrl": f"https://www.linkedin.com/posts/acme-drilling_x-activity-70000000000{n:05d}-Ab",
            "content": text or f"Acme Drilling post {n} {DRILLING}", "postedAt": {"date": posted},
            "postImages": [{"url": f"https://media.licdn.com/dms/image/{n}.jpg"}], **extra}


def _page(url: str = COMPANY_URL, name: str = "Acme Drilling (LinkedIn)", since: datetime | None = SINCE) -> int:
    return int(linkedin_store.add_page(url, name=name, collect_since=since)["id"])


def _articles(source_id: int) -> list[tuple]:
    with connection.get_connection() as conn:
        return conn.execute("SELECT url, title, language, image_url, selector, published_at FROM articles"
                            " WHERE source_id = %s ORDER BY published_at", (source_id,)).fetchall()


def _source(source_id: int) -> dict:
    with connection.get_connection() as conn:
        row = conn.execute("SELECT last_parsed_at, last_seen_published_at, last_seen_article_url FROM sources"
                           " WHERE id = %s", (source_id,)).fetchone()
    return {"last_parsed_at": row[0], "last_seen_published_at": row[1], "last_seen_article_url": row[2]}


def _spend_rows() -> list[tuple]:
    with connection.get_connection() as conn:
        return conn.execute("SELECT stage, provider, model, cost_usd::float, status, error_message"
                            " FROM ai_processing_runs ORDER BY id").fetchall()


def _spend(cost: float, at: datetime) -> None:
    with connection.get_connection() as conn:
        conn.execute("INSERT INTO ai_processing_runs (stage, provider, model, cost_usd, created_at)"
                     " VALUES ('linkedin_apify', 'apify', 'harvestapi/linkedin-company-posts', %s, %s)", (cost, at))
        conn.commit()


def _make_due() -> None:
    """Время сбора база ставит по своим часам: тесты с замороженным «сейчас» сдвигают его назад явно."""
    with connection.get_connection() as conn:
        conn.execute("UPDATE sources SET last_parsed_at = NULL")
        conn.commit()


def _collect(client, **kwargs):
    return linkedin_apify.collect(now=kwargs.pop("now", NOW), client=client,
                                  save_image=kwargs.pop("save_image", lambda url: None), **kwargs)


def test_new_posts_become_articles_of_the_page_and_the_run_is_paid_once(isolated_db):
    source_id = _page()
    noise = _post(4, "2026-11-22T10:00:00.000Z", text="Our office football team won the city league cup this weekend!")
    client = FakeClient(RunResult(True, [
        _post(1, "2026-11-20T09:00:00.000Z"),
        _post(2, "2026-11-21T09:00:00.000Z"),
        _post(3, "2026-10-20T09:00:00.000Z"),  # раньше даты начала сбора 01.11
        noise,
    ], 0.00805))
    stats = _collect(client, save_image=lambda url: f"https://platform.example/media/post-images/{url[-5:]}")

    assert stats["added"] == 2 and stats["old"] == 1 and stats["prefilter"] == 1 and stats["posts"] == 4
    rows = _articles(source_id)
    assert [row[0][-12:] for row in rows] == ["000000001-Ab", "000000002-Ab"]
    assert rows[0][1].startswith("Acme Drilling post 1 completed") and rows[0][2] == "en"
    assert rows[0][3] == "https://platform.example/media/post-images/1.jpg"
    assert rows[0][4] == "otbor"  # новая статья идёт через «Отбор», как статья любого источника
    actor, request, cap = client.calls[0]
    assert actor == "harvestapi~linkedin-company-posts" and request["targetUrls"] == [COMPANY_URL]
    assert request["postedLimitDate"] == "2026-10-31T21:00:00.000Z"  # 01.11 по Москве
    assert request["includeReposts"] is True and request["scrapeComments"] is False and cap == 1.0
    assert _spend_rows() == [("linkedin_apify", "apify", "harvestapi/linkedin-company-posts", 0.00805, "ok", None)]
    state = _source(source_id)
    assert state["last_parsed_at"] is not None
    assert state["last_seen_published_at"] == datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)
    assert stats["cost_usd"] == pytest.approx(0.00805) and stats["month_spend_usd"] == pytest.approx(0.00805)


def test_next_run_asks_only_for_newer_posts_and_skips_known_ones(isolated_db):
    source_id = _page()
    _collect(FakeClient(RunResult(True, [_post(1, "2026-11-20T09:00:00.000Z")], 0.00205)))
    client = FakeClient(RunResult(True, [_post(1, "2026-11-20T09:00:00.000Z"), _post(5, "2026-11-23T09:00:00.000Z")],
                                  0.00405))
    _make_due()
    stats = _collect(client)

    assert client.calls[0][1]["postedLimitDate"] == "2026-11-20T09:00:01.000Z"  # последний виденный плюс секунда
    assert stats["known"] == 1 and stats["added"] == 1
    assert len(_articles(source_id)) == 2


def test_repost_moves_the_boundary_by_its_repost_time(isolated_db):
    source_id = _page()
    repost = _post(6, "2026-11-02T09:00:00.000Z", repostedBy={"name": "Acme Drilling"},
                   repostedAt={"date": "2026-11-24T15:30:00.000Z"})
    _collect(FakeClient(RunResult(True, [repost], 0.00205)))
    assert _source(source_id)["last_seen_published_at"] == datetime(2026, 11, 24, 15, 30, tzinfo=timezone.utc)
    assert _articles(source_id)[0][5] == datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


def test_profile_page_goes_to_the_profile_module(isolated_db):
    _page(PROFILE_URL, "Jane Doe (LinkedIn)")
    client = FakeClient()
    _collect(client)
    assert client.calls[0][0] == "harvestapi~linkedin-profile-posts"
    assert client.calls[0][1]["targetUrls"] == [PROFILE_URL]


def test_flag_off_no_token_and_dry_run_call_nothing(isolated_db, monkeypatch):
    source_id = _page()
    client = FakeClient()
    monkeypatch.setattr(config, "LINKEDIN_APIFY_ENABLED", False)
    assert "выключен" in _collect(client)["stopped"]
    plan = _collect(client, dry_run=True)
    assert plan["plan"][0]["source_id"] == source_id and plan["plan"][0]["since"] == "2026-10-31T21:00:00.000Z"
    monkeypatch.setattr(config, "LINKEDIN_APIFY_ENABLED", True)
    monkeypatch.setattr(config, "APIFY_TOKEN", "")
    assert "APIFY_TOKEN" in linkedin_apify.collect(now=NOW)["stopped"]
    assert client.calls == [] and _spend_rows() == [] and _source(source_id)["last_parsed_at"] is None


def test_page_waits_for_its_interval(isolated_db):
    _page()
    with connection.get_connection() as conn:
        conn.execute("UPDATE sources SET last_parsed_at = %s", (NOW - timedelta(hours=5),))
        conn.commit()
    client = FakeClient()
    _collect(client)
    assert client.calls == []
    _collect(client, now=NOW + timedelta(hours=16))  # 21 час после прошлого сбора
    assert len(client.calls) == 1


def test_month_cap_stops_collection_and_the_run_cap_is_the_remainder(isolated_db):
    _page()
    _spend(3.0, datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc))  # прошлый месяц не в счёт
    _spend(4.5, datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc))
    client = FakeClient()
    _collect(client)
    assert client.calls[0][2] == pytest.approx(0.5)
    _spend(0.6, datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc))
    _make_due()
    stats = _collect(client)
    assert len(client.calls) == 1 and "месячный потолок" in stats["stopped"]


def test_failed_run_keeps_the_page_due_and_the_rest_go_on(isolated_db):
    first = _page()
    second = _page("https://www.linkedin.com/company/beta-wells/", "Beta Wells (LinkedIn)")
    client = FakeClient(RunResult(False, cost_usd=0.00005, error="ApifyError: HTTP 402 not-enough-usage"),
                        RunResult(True, [_post(7, "2026-11-25T09:00:00.000Z")], 0.00205))
    stats = _collect(client)

    assert stats["failed"] == 1 and stats["pages"] == 1 and stats["added"] == 1
    assert _source(first)["last_parsed_at"] is None and _source(second)["last_parsed_at"] is not None
    assert [row[4] for row in _spend_rows()] == ["failed", "ok"]
    assert "402" in _spend_rows()[0][5]


def test_image_failure_keeps_the_post(isolated_db):
    source_id = _page()

    def broken(url):
        raise OSError("disk full")

    _collect(FakeClient(RunResult(True, [_post(8, "2026-11-25T09:00:00.000Z")], 0.00205)), save_image=broken)
    assert _articles(source_id)[0][3] is None


def test_same_page_is_not_added_twice(isolated_db):
    _page()
    with pytest.raises(linkedin_store.PageExists):
        linkedin_store.add_page("https://uk.linkedin.com/company/acme-drilling/posts/", name="Acme again")
    with pytest.raises(linkedin_store.PageExists):
        linkedin_store.add_page("https://www.linkedin.com/company/other-co/", name="Acme Drilling (LinkedIn)")
    with pytest.raises(ValueError):
        linkedin_store.add_page("https://example.com/company/acme/")


def test_a_post_that_fails_to_store_does_not_hold_the_boundary(isolated_db, monkeypatch):
    """За запуск уже заплачено: пост, который не записался, считается, а граница и время сбора сдвигаются, иначе
    следующий цикл оплатил бы те же посты снова."""
    source_id = _page()
    real_insert = linkedin_apify.repository.insert_article

    def insert(record):
        if record["url"].endswith("000000009-Ab"):
            raise RuntimeError("constraint")
        return real_insert(record)

    monkeypatch.setattr(linkedin_apify.repository, "insert_article", insert)
    unreadable = {"type": "post", "postedAt": {"date": "2026-11-26T08:00:00.000Z"}, "content": ""}
    stats = _collect(FakeClient(RunResult(True, [_post(9, "2026-11-24T09:00:00.000Z"),
                                                 _post(10, "2026-11-25T09:00:00.000Z"), unreadable], 0.00605)))

    assert stats["added"] == 1 and stats["post_errors"] == 1 and stats["unreadable"] == 1 and stats["pages"] == 1
    state = _source(source_id)
    assert state["last_parsed_at"] is not None
    # Самый новый пост нечитаемый, но оплачен: граница по его времени.
    assert state["last_seen_published_at"] == datetime(2026, 11, 26, 8, 0, tzinfo=timezone.utc)


def test_page_without_start_date_and_seen_posts_has_no_boundary(isolated_db):
    _page(since=None)
    with connection.get_connection() as conn:
        conn.execute("UPDATE sources SET collect_since = NULL")
        conn.commit()
    client = FakeClient()
    _collect(client)
    assert "postedLimitDate" not in client.calls[0][1]  # сервис отдаст последние LINKEDIN_APIFY_MAX_POSTS постов


# --- клиент сервиса на подменённой сети


class _Response:
    def __init__(self, status: int, payload) -> None:
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


class _Session:
    def __init__(self, *responses: _Response) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _client(session, **kwargs):
    clock = _Clock()
    return ApifyClient("secret-token", session=session, sleep=clock.sleep, clock=clock, **kwargs)


def _run(status: str, posts: int = 0, usage: float = 0.00005, **extra) -> _Response:
    return _Response(200, {"data": {"id": "run-1", "status": status, "defaultDatasetId": "ds-1",
                                    "usageTotalUsd": usage, "chargedEventCounts": {"post": posts}, **extra}})


def test_client_waits_for_the_final_charge():
    session = _Session(_run("READY"), _run("RUNNING"), _run("SUCCEEDED"), _Response(200, [{"type": "post"}]),
                       _run("SUCCEEDED", posts=1, usage=0.00205))
    result = _client(session).run("harvestapi~linkedin-company-posts", {"targetUrls": [COMPANY_URL]}, 1.0)

    assert result.ok and len(result.items) == 1 and result.cost_usd == pytest.approx(0.00205)
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("POST", "https://api.apify.com/v2/acts/harvestapi~linkedin-company-posts/runs")
    assert kwargs["headers"] == {"Authorization": "Bearer secret-token"}
    assert kwargs["params"] == {"maxTotalChargeUsd": "1.0000"} and kwargs["json"] == {"targetUrls": [COMPANY_URL]}
    assert all("secret-token" not in call[1] for call in session.calls)  # ключ только в заголовке


def test_counts_ahead_of_the_sum_do_not_cut_the_charge():
    """Счётчик постов уже верный, а сумма ещё только за начало запуска: клиент ждёт сумму, а не записывает начало."""
    session = _Session(_run("SUCCEEDED"), _Response(200, [{"type": "post"}] * 11),
                       _run("SUCCEEDED", posts=11, usage=0.00005), _run("SUCCEEDED", posts=11, usage=0.02205))
    assert _client(session).run("a~b", {}, 1.0).cost_usd == pytest.approx(0.02205)
    late = _Session(_run("SUCCEEDED"), _Response(200, [{"type": "post"}] * 11),
                    *[_run("SUCCEEDED", posts=11, usage=0.00005) for _ in range(4)])
    assert _client(late, settle_timeout=10).run("a~b", {}, 1.0).cost_usd == pytest.approx(11 * 0.002 + 0.00005)

def test_client_estimates_the_charge_when_the_service_is_late():
    session = _Session(_run("SUCCEEDED"), _Response(200, [{"type": "post"}, {"type": "post"}]),
                       *[_run("SUCCEEDED", posts=0) for _ in range(5)])
    result = _client(session, settle_timeout=10).run("a~b", {}, 1.0)
    assert result.cost_usd == pytest.approx(2 * 0.002 + 0.00005)


def test_empty_run_is_settled_by_its_no_result_charge():
    session = _Session(_run("SUCCEEDED"), _Response(200, []),
                       _run("SUCCEEDED", usage=0.00105, chargedEventCounts={"post": 0, "no-result": 1}))
    result = _client(session).run("a~b", {}, 1.0)
    assert result.ok and result.items == [] and result.cost_usd == pytest.approx(0.00105)


def test_client_turns_service_errors_into_a_failed_result():
    session = _Session(_Response(402, {"error": {"type": "not-enough-usage-to-run-paid-actor"}}))
    result = _client(session).run("a~b", {}, 1.0)
    assert not result.ok and "402" in result.error and "not-enough-usage" in result.error
    assert result.cost_usd == 0.0


def test_client_aborts_a_run_that_does_not_finish():
    session = _Session(_run("RUNNING"), *[_run("RUNNING") for _ in range(4)], _Response(200, {"data": {}}),
                       _run("ABORTED", usage=0.00005))
    result = _client(session, run_timeout=20, poll_seconds=5).run("a~b", {}, 1.0)
    assert not result.ok and "TIMED-OUT" in result.error
    assert any(method == "POST" and url.endswith("/actor-runs/run-1/abort") for method, url, _ in session.calls)


def test_client_error_text_never_carries_the_key():
    class _Leaky:
        def request(self, method, url, **kwargs):
            raise requests.exceptions.InvalidHeader(f"Invalid header value {kwargs['headers']['Authorization']!r}")

    result = _client(_Leaky()).run("a~b", {}, 1.0)
    assert not result.ok and "secret-token" not in result.error and "***" in result.error
