"""Страница LinkedIn как источник: форма «Добавить источник», команды, исключения из чужих шагов, строка «Статистики».

Страница заводится без пробы сайта (LinkedIn с ядра закрыт), не ищет RSS и не идёт в дозагрузку текста: пост приходит
целиком. Расход сервиса виден в «Статистике» своей строкой. Страницы выдуманные.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest

from oiltech_digest import api, cli
from oiltech_digest.db import connection, linkedin_store, repository

ROOT = Path(__file__).resolve().parents[1]
MSK = ZoneInfo("Europe/Moscow")


def _post_source(payload: dict, monkeypatch):
    def no_probe(url):
        raise AssertionError("страницу LinkedIn не пробуют как сайт")

    monkeypatch.setattr(api, "probe_strategies", no_probe)
    app = api.app
    app.dependency_overrides[api.require_admin] = lambda: {"id": 1, "email": "admin@example.com", "role": "admin"}
    try:
        return TestClient(app).post("/api/sources", json=payload)
    finally:
        app.dependency_overrides.clear()


def _row(source_id: int) -> tuple:
    with connection.get_connection() as conn:
        return conn.execute("SELECT parse_strategy, source_type, url, listing_url, network_region, enabled, collect_since"
                            " FROM sources WHERE id = %s", (source_id,)).fetchone()


def test_form_adds_a_linkedin_page_without_probing_the_site(isolated_db, monkeypatch):
    response = _post_source({"name": "Acme Drilling (LinkedIn)",
                             "url": "https://uk.linkedin.com/company/acme-drilling/posts/?lipi=x"}, monkeypatch)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["parse_strategy"] == "linkedin" and body["probe"] is None
    strategy, source_type, url, listing_url, region, enabled, since = _row(body["id"])
    assert (strategy, source_type, url, listing_url, region, enabled) == (
        "linkedin", "LinkedIn", "https://www.linkedin.com/company/acme-drilling/",
        "https://www.linkedin.com/company/acme-drilling/", "auto", True)
    assert since is not None  # дата начала сбора как у любого нового источника

    again = _post_source({"name": "Другое имя", "url": "https://www.linkedin.com/company/acme-drilling/"}, monkeypatch)
    assert again.status_code == 409 and "уже заведена" in again.json()["detail"]


def test_profile_address_is_a_page_too(isolated_db, monkeypatch):
    response = _post_source({"name": "Jane Doe (LinkedIn)", "url": "https://www.linkedin.com/in/jane-doe-12ab34/"},
                            monkeypatch)
    assert response.status_code == 200, response.text
    assert _row(response.json()["id"])[2] == "https://www.linkedin.com/in/jane-doe-12ab34/"


def test_linkedin_pages_skip_rss_search_and_full_text(isolated_db):
    page = linkedin_store.add_page("https://www.linkedin.com/company/acme-drilling/", name="Acme Drilling (LinkedIn)")
    site = repository.add_rss_source(name="Acme news", rss_url="", url="https://acme.example/", parse_strategy="request")
    for source_id, url in ((page["id"], "https://www.linkedin.com/posts/acme_x-activity-1-a"),
                           (site, "https://acme.example/news/1")):
        assert repository.insert_article({"source_id": source_id, "title": f"Short post {source_id}", "url": url,
                                          "published_at": datetime(2026, 11, 20, tzinfo=timezone.utc),
                                          "raw_text": "Short text of a post.", "text_truncated": False,
                                          "language": "en", "content_hash": url})
    assert [row["url"] for row in repository.get_articles_needing_full_text(limit=10)] == ["https://acme.example/news/1"]
    # Без listing_url поиск RSS берёт источник: страницу LinkedIn он не трогает и тогда.
    with connection.get_connection() as conn:
        conn.execute("UPDATE sources SET listing_url = NULL")
        conn.commit()
    found = {row["id"] for row in repository.get_sources_for_discovery(only_missing=False)}
    assert site in found and page["id"] not in found


def test_statistics_shows_apify_spend_as_its_own_area_and_not_as_ai(isolated_db):
    from oiltech_digest.db import analytics

    linkedin_store.record_run(stage="linkedin_apify", model="harvestapi/linkedin-company-posts", cost_usd=0.07, ok=True)
    with connection.get_connection() as conn:
        conn.execute("INSERT INTO ai_processing_runs (stage, model, cost_usd) VALUES ('otbor', 'gpt-6-sol', 1.5)")
        conn.commit()
    areas = {(row["area"], row["model"]): float(row["cost_usd"]) for row in repository.monthly_ai_cost(1)}
    assert areas == {("linkedin_apify", "harvestapi/linkedin-company-posts"): pytest.approx(0.07),
                     ("otbor", "gpt-6-sol"): 1.5}
    month = analytics.monthly_analytics(1, include_cost=True)["ai_cost"][-1]
    assert float(month["cost_usd"]) == 1.5  # расход сервиса не раздувает расход ИИ


def test_diagnostics_says_the_service_collects_the_page(isolated_db):
    from oiltech_digest.ingestion import source_diagnostics

    page = linkedin_store.add_page("https://www.linkedin.com/company/acme-drilling/", name="Acme Drilling (LinkedIn)")
    report = source_diagnostics.diagnose_source(repository.get_source(page["id"]))
    assert report["verdict"] == "collected_by_service" and "linkedin-collect" in report["detail"]


def test_nl_refetch_skips_linkedin_even_when_its_region_is_changed(isolated_db):
    page = linkedin_store.add_page("https://www.linkedin.com/company/acme-drilling/", name="Acme Drilling (LinkedIn)")
    site = repository.add_rss_source(name="Acme west", rss_url="", url="https://acme.example/", parse_strategy="request")
    repository.set_sources_network_region([page["id"], site], "external")
    for source_id, url in ((page["id"], "https://www.linkedin.com/posts/acme_y-activity-2-b"),
                           (site, "https://acme.example/news/2")):
        assert repository.insert_article({"source_id": source_id, "title": f"Teaser {source_id}", "url": url,
                                          "published_at": datetime(2026, 11, 20, tzinfo=timezone.utc),
                                          "raw_text": "Teaser only.", "text_truncated": True, "language": "en",
                                          "content_hash": url})
    assert [row["url"] for row in repository.external_refetch_candidates(limit=10)] == ["https://acme.example/news/2"]


def test_region_change_keeps_the_paid_boundary_of_a_linkedin_page(isolated_db):
    page = linkedin_store.add_page("https://www.linkedin.com/company/acme-drilling/", name="Acme Drilling (LinkedIn)")
    seen = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)
    repository.update_source_request_state(page["id"], last_seen_article_url="https://www.linkedin.com/posts/a",
                                           last_seen_published_at=seen)
    repository.set_sources_network_region([page["id"]], "external")
    assert repository.get_source(page["id"])["last_seen_published_at"] == seen


def test_add_command_takes_the_start_date_by_moscow_time(isolated_db, capsys):
    args = cli.build_parser().parse_args(["add-linkedin-source", "https://www.linkedin.com/in/jane-doe-12ab34/",
                                          "--name", "Jane Doe (LinkedIn)", "--collect-since", "2026-11-01"])
    args.func(args)
    assert "профиль эксперта" in capsys.readouterr().out
    with connection.get_connection() as conn:
        since = conn.execute("SELECT collect_since FROM sources WHERE name = 'Jane Doe (LinkedIn)'").fetchone()[0]
    assert since == datetime(2026, 11, 1, tzinfo=MSK)
    with pytest.raises(SystemExit, match="уже заведена"):
        args.func(args)
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["add-linkedin-source", "x", "--collect-since", "01.11.2026"])


def test_collect_command_dry_run_prints_the_plan(isolated_db, capsys):
    linkedin_store.add_page("https://www.linkedin.com/company/acme-drilling/", name="Acme Drilling (LinkedIn)")
    args = cli.build_parser().parse_args(["linkedin-collect", "--dry-run"])
    args.func(args)
    out = capsys.readouterr().out
    assert "Acme Drilling (LinkedIn) (company): посты с" in out and "к сбору 1" in out


def test_scheduler_collects_linkedin_before_otbor():
    script = (ROOT / "scripts" / "docker-scheduler.sh").read_text()
    assert script.index('run_step "linkedin-collect"') < script.index('run_step "enqueue-otbor"')
