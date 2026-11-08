"""Радар после 655cdbe (21.09): что роняло прогон или портило данные.

Каждый тест падает на коде до правки: пустая страница роняла тему, докачка шла без
heartbeat, 0 в настройках превращался в умолчание, итог задачи не показывал новые шаги,
дата страницы не доезжала до ядра."""

from pathlib import Path

import pytest
import yaml
from lxml import etree

from oiltech_digest import background_jobs, signal_discovery, signal_research
from oiltech_digest.db import repository
from oiltech_digest.ingestion import publisher_page, request_parser
from oiltech_digest.processing.openai_client import AIResponse

SNIPPET = "Field trial of drilling automation for an oil and gas operator."


def _web_item(url: str) -> dict:
    return {
        "source_url": url,
        "title": "Oil and gas operator deploys drilling automation",
        "extracted_fact": SNIPPET,
        "raw_payload": {"snippet": SNIPPET},
    }


# --- 1. Пустая страница -------------------------------------------------------------


@pytest.mark.parametrize("body", [b"", b"   \n", "", b"<!-- -->"])
def test_parse_article_page_survives_empty_body(body):
    assert request_parser.parse_article_page(body, "Заголовок из листинга") == ("Заголовок из листинга", None, "")


def test_extract_candidate_links_survives_empty_listing():
    assert request_parser.extract_candidate_links({"id": 1}, "https://example.com/news", b"") == []


def test_empty_page_in_fulltext_falls_back_to_snippet(monkeypatch):
    """Ровно путь 21.09: 200 с пустым телом. Находка остаётся со сводкой и пометкой, а не роняет тему."""
    monkeypatch.setattr(signal_discovery, "_fetch_full_text", lambda url, fallback_title="", **kwargs: {
        "ok": True, "error": None, "raw_text": "", "title": "", "published_at": None})

    evidence, stats = signal_discovery._enrich_web_evidence_with_full_text(
        [_web_item("https://example.com/empty")], "Бурение"
    )

    assert stats["too_short"] == 1
    assert evidence[0]["extracted_fact"] == SNIPPET
    assert evidence[0]["raw_payload"]["full_text_fetched"] is False


def test_any_page_error_keeps_candidate_with_snippet(monkeypatch):
    def boom(url, fallback_title="", **kwargs):
        raise etree.ParserError("Document is empty")

    monkeypatch.setattr(signal_discovery, "_fetch_full_text", boom)

    evidence, stats = signal_discovery._enrich_web_evidence_with_full_text(
        [_web_item("https://example.com/broken")], "Бурение"
    )

    assert stats == {"attempted": 1, "fetched": 0, "too_short": 0, "failed": 1,
                     "via": dict.fromkeys(publisher_page.TOP_UP_FETCHERS, 0), "pdf": 0}
    assert evidence[0]["extracted_fact"] == SNIPPET
    assert "ParserError" in evidence[0]["raw_payload"]["full_text_error"]


def test_fetch_full_text_reads_the_publisher_page_with_the_top_up(monkeypatch):
    # Страница — условие находки: после запроса идёт добор (PDF, ядро РФ, браузер), страница-список — слабый заголовок.
    asked = []
    monkeypatch.setattr(publisher_page, "read", lambda url, title, **kwargs: asked.append((url, title, kwargs)) or {
        "ok": True, "raw_text": "текст", "title": title, "fetcher": "core"})

    result = signal_discovery._fetch_full_text("https://example.com/annual-report.pdf", "Годовой отчёт")

    assert result["fetcher"] == "core"
    assert asked == [("https://example.com/annual-report.pdf", "Годовой отчёт", {
        "timeout": signal_discovery.app_config.SIGNAL_DISCOVERY_FULLTEXT_TIMEOUT_SECONDS,
        "weak_title": signal_research.is_list_page})]


# --- 2. Аренда задачи: heartbeat перед каждой страницей ------------------------------


def test_fulltext_beats_before_every_page(monkeypatch):
    beats: list[int] = []
    fetched: list[tuple[str, int]] = []

    def fetch(url, fallback_title="", **kwargs):
        fetched.append((url, len(beats)))
        return {"ok": False, "error": "http_403", "raw_text": ""}

    monkeypatch.setattr(signal_discovery, "_fetch_full_text", fetch)

    signal_discovery._enrich_web_evidence_with_full_text(
        [_web_item(f"https://example.com/{index}") for index in range(3)],
        "Бурение",
        heartbeat=lambda: beats.append(1),
    )

    assert fetched == [("https://example.com/0", 1), ("https://example.com/1", 2), ("https://example.com/2", 3)]


def test_fulltext_fetches_the_page_of_every_finding(monkeypatch):
    # Срока на тему нет: находка без страницы во вход «Отбора» не идёт, и срезать её по времени значило бы потерять
    # её молча. Аренду держит признак продвижения перед каждой страницей.
    monkeypatch.setattr(signal_discovery, "_fetch_full_text",
                        lambda url, fallback_title="", **kwargs: {"ok": False, "error": "timeout", "raw_text": ""})

    evidence, stats = signal_discovery._enrich_web_evidence_with_full_text(
        [_web_item(f"https://example.com/{index}") for index in range(5)], "Бурение")

    assert (stats["attempted"], stats["failed"]) == (5, 5)
    assert len(evidence) == 5


class _Search:
    def __init__(self, events, item):
        self.events, self.item = events, item

    def research_json(self, instructions, user_input, schema, **kwargs):
        self.events.append("search")
        return AIResponse(data={"events": [self.item], "_cited_urls": [], "_web_search_calls": 1}, model="m")


def test_run_discovery_heartbeat_reaches_search_and_pages(monkeypatch):
    events: list[str] = []
    item = {"title": "Oil and gas operator deploys drilling automation", "company": "", "event_date": "",
            "summary": "", "why_important": "", "source_url": "https://example.com/a", "publisher": "",
            "source_kind": "primary", "technology": ""}
    monkeypatch.setattr(signal_research, "make_client", lambda offline=False: _Search(events, item))
    monkeypatch.setattr(
        signal_discovery,
        "_fetch_full_text",
        lambda url, fallback_title="", **kwargs: events.append("page") or {"ok": False, "error": "x", "raw_text": ""},
    )
    config = signal_discovery.SignalDiscoveryConfig(offline=True, dry_run=True, research_model="gpt-6.1-sol",
                                                    research_reasoning="low")

    signal_discovery.run_discovery(
        config, {"topics": [{"name": "Бурение", "query_seeds_json": []}], "tags": []}, heartbeat=lambda: events.append("beat")
    )

    assert events[events.index("search") - 1] == "beat"
    assert events[events.index("page") - 1] == "beat"


# --- 3. Ноль в настройках ------------------------------------------------------------


def test_config_keeps_explicit_zero_and_takes_core_settings_for_missing(monkeypatch):
    monkeypatch.setattr(signal_discovery.app_config, "SIGNAL_RESEARCH_EVENTS_PER_TOPIC", 12)
    monkeypatch.setattr(signal_discovery.app_config, "SIGNAL_RESEARCH_DAYS", 3)

    zero = signal_discovery.config_from_payload({"research_limit": 0, "research_days": 0})
    missing = signal_discovery.config_from_payload({})

    assert (zero.research_limit, zero.research_days) == (0, 0)
    # Задача, поставленная до #138, настроек поиска не несёт — берёт настройки ядра в момент выдачи.
    assert (missing.days, missing.research_limit, missing.research_days) == (14, 12, 14)


# --- 5. Балл интереса хранится ------------------------------------------------------


def test_upsert_signal_keeps_interest_when_next_run_has_none(isolated_db):
    base = {"signal_key": "k-interest", "title": "T", "theme": "Бурение", "maturity": "watch", "score": 70}
    signal_id = repository.upsert_signal({**base, "interest_score": 88, "why_interesting": "первое внедрение"})
    # Карточка без единой ссылки в список не попадает (сигнал 97) — даём ей ссылку.
    repository.upsert_signal_evidence(signal_id, {"source_url": "https://example.com/interest", "title": "T"})
    repository.upsert_signal(dict(base))  # тема из одного кандидата ревью пачки не проходит

    row = next(item for item in repository.list_signals(limit=10) if item["id"] == signal_id)

    assert float(row["interest_score"]) == 88.0
    assert row["why_interesting"] == "первое внедрение"


# --- 8. Итог задачи показывает новые шаги -------------------------------------------


def test_external_summary_shows_the_search_and_page_counters(monkeypatch):
    monkeypatch.setattr(repository, "create_signal_generation_run", lambda **kwargs: None)
    fulltext = {"attempted": 5, "fetched": 3, "too_short": 1, "failed": 1, "via": {"core": 1, "browser": 1}, "pdf": 1}
    result = {
        "signal_discovery": True,
        "config": {"offline": False, "dry_run": True},
        "run": {"topics": [{
            "topic": "Бурение",
            "web_search": {"status": "ok", "provider": "openai_web_search", "evidence_count": 2,
                           "research": {"status": "ok", "events": 5, "evidence_count": 2, "attempts": 1,
                                        "unverified": [{"url": "x"}], "dropped": [], "fulltext": fulltext,
                                        "already_collected": [{"url": "y"}]}},
            "candidates": [],
        }]},
    }

    summary = signal_discovery.apply_external_result(result, job_id=1)

    topic = summary["topics"][0]
    assert topic["provider"] == "openai_web_search"
    assert topic["research"] == {"status": "ok", "events": 5, "evidence_count": 2, "attempts": 1, "unverified": 1,
                                 "dropped": 0, "fulltext": fulltext, "already_collected": 1}


# --- 10. Внешняя очередь не исполняется на месте, конвейер — под профилем -------------


def test_enqueue_never_runs_external_queue_inline(monkeypatch, isolated_db):
    """20.09: планировщик без BACKGROUND_JOB_INLINE=0 выполнил радар из external-ai на РФ."""
    submitted = []
    monkeypatch.setattr(background_jobs.config, "BACKGROUND_JOB_INLINE", True)
    monkeypatch.setattr(background_jobs._executor, "submit", lambda *args, **kwargs: submitted.append(args))
    monkeypatch.setitem(background_jobs._HANDLERS, "test_queued", lambda payload, job_id: {"ok": True})

    external = background_jobs.enqueue("scrape_source", {"source_id": 1}, queue_name="external-fetch", execution_region="external")
    local = background_jobs.enqueue("test_queued", {}, queue_name="default")

    assert repository.get_background_job(int(external["id"]))["status"] == "queued"
    assert [args[1] for args in submitted] == [int(local["id"])]


def test_daily_radar_routed_abroad_is_not_executed_by_the_enqueuing_process(monkeypatch, isolated_db):
    from oiltech_digest import network_policy

    submitted = []
    monkeypatch.setattr(background_jobs.config, "BACKGROUND_JOB_INLINE", True)
    monkeypatch.setattr(background_jobs.config, "SIGNAL_DISCOVERY_DAILY_ENABLED", True)
    monkeypatch.setattr(background_jobs._executor, "submit", lambda *args, **kwargs: submitted.append(args))
    monkeypatch.setattr(
        network_policy,
        "route_ai_processing",
        lambda: network_policy.ExecutionDecision("external-ai", "external", "openai", "ai_external_enabled"),
    )

    result = background_jobs.enqueue_daily_signal_discovery()

    assert result["enqueued"] is True
    assert result["job"]["queue_name"] == "external-agents"
    assert submitted == []


def test_bare_compose_up_starts_only_core_services():
    services = yaml.safe_load((Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text())["services"]

    unprofiled = {name for name, service in services.items() if not service.get("profiles")}

    # Единый контур (D): стек MVP-1 — приложение `app` и Caddy; конвейер — профилем
    # `pipeline`, архивный трекер задач — профилем `archive`.
    assert unprofiled == {"db", "bootstrap", "app", "caddy"}
    for name in ("worker", "playwright-worker", "scheduler"):
        assert services[name]["profiles"] == ["pipeline"]
        assert services[name]["environment"]["BACKGROUND_JOB_INLINE"] == "0"
    assert services["tasks"]["profiles"] == ["archive"]
    assert services["tasks"]["environment"]["BACKGROUND_JOB_INLINE"] == "0"


def test_worker_result_does_not_carry_web_evidence_twice(monkeypatch):
    """Тексты страниц едут в находках; в блоке web_search темы — только счётчики."""
    page = {**_web_item("https://example.com/a"), "extracted_fact": "Contract signed for oil and gas drilling automation."}
    monkeypatch.setattr(
        signal_discovery,
        "_topic_web_evidence",
        lambda topic, topic_name, config, **kwargs: {"status": "ok", "provider": "openai_web_search", "research": {},
                                                     "evidence": [page]},
    )
    config = signal_discovery.SignalDiscoveryConfig(offline=True, dry_run=True)

    run = signal_discovery.run_discovery(config, {"topics": [{"name": "Бурение"}], "tags": []})

    web = run["topics"][0]["web_search"]
    assert "evidence" not in web
    assert web["evidence_count"] == 1
    assert run["topics"][0]["candidates"][0]["signal"]["evidence"][0]["source_url"] == "https://example.com/a"


# --- Итог воркера — JSON (поймано проверкой 4712 после пересборки NL 21.09) -----------


def test_fetched_page_date_travels_as_iso_string(monkeypatch):
    """Докачка клала published_at объектом datetime — итог воркера не уходил ядру."""
    from datetime import datetime, timezone

    monkeypatch.setattr(
        signal_discovery,
        "_fetch_full_text",
        lambda url, fallback_title="", **kwargs: {
            "ok": True, "error": None, "title": "Drilling automation contract",
            "raw_text": "Oil and gas operator signed a drilling automation contract. " * 10,
            "published_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        },
    )

    evidence, _ = signal_discovery._enrich_web_evidence_with_full_text(
        [_web_item("https://example.com/a")], "Бурение"
    )

    assert evidence[0]["published_at"] == "2026-09-01T00:00:00+00:00"


def test_worker_sends_result_with_dates_to_core(monkeypatch):
    """Граница с ядром: любой вид задачи отдаёт дату строкой ISO, а не падает на отправке
    (18.09 — сбор, 21.09 — радар: один и тот же класс дважды)."""
    import json as jsonlib
    from datetime import datetime, timezone

    from oiltech_digest import external_worker

    client = external_worker.ExternalWorkerClient(
        core_api_url="https://core.example", token="t", worker_id="w", queues=["external-ai"], capabilities=["openai"]
    )
    sent = {}

    class Response:
        def raise_for_status(self):
            return None

    def post(url, json=None, timeout=None):
        sent["body"] = jsonlib.dumps(json)  # как requests: без default
        return Response()

    monkeypatch.setattr(client.session, "post", post)

    client.complete(
        {"id": 1, "lease_token": "x"},
        {"run": {"evidence": [{"published_at": datetime(2026, 9, 1, tzinfo=timezone.utc)}]}},
    )

    assert '"published_at": "2026-09-01T00:00:00+00:00"' in sent["body"]


def test_core_stores_iso_published_at_from_worker(isolated_db):
    signal_id = repository.upsert_signal({"signal_key": "k-date", "title": "T", "theme": "Бурение", "score": 60})

    repository.upsert_signal_evidence(
        signal_id, {"source_url": "https://example.com/a", "title": "T", "published_at": "2026-09-01T00:00:00+00:00"}
    )

    stored = repository.list_signal_evidence(signal_id)[0]["published_at"]
    assert stored.isoformat().startswith("2026-09-01")
