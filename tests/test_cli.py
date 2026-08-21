import argparse

import pytest

from oiltech_digest import cli
from oiltech_digest.db import repository


def test_schema_check_command_reports_ok(monkeypatch, capsys):
    monkeypatch.setattr(
        "oiltech_digest.readiness.schema_check",
        lambda: {"ok": True, "required_tables": ["articles"], "missing_tables": []},
    )

    cli.cmd_schema_check(argparse.Namespace())

    assert "schema-check: ok" in capsys.readouterr().out


def test_schema_check_command_exits_non_zero_when_missing_tables(monkeypatch, capsys):
    monkeypatch.setattr(
        "oiltech_digest.readiness.schema_check",
        lambda: {"ok": False, "required_tables": ["articles"], "missing_tables": ["background_jobs"]},
    )

    with pytest.raises(SystemExit, match="1"):
        cli.cmd_schema_check(argparse.Namespace())

    assert "background_jobs" in capsys.readouterr().out


def test_enqueue_external_scrape_is_noop_when_contour_disabled(monkeypatch, capsys):
    monkeypatch.setattr("oiltech_digest.config.EXTERNAL_WORKERS_ENABLED", False)
    monkeypatch.setattr("oiltech_digest.config.FETCH_EXTERNAL_ENABLED", True)
    created = []
    monkeypatch.setattr("oiltech_digest.db.repository.create_background_job",
                        lambda *a, **k: created.append((a, k)))

    cli.cmd_enqueue_external_scrape(argparse.Namespace(max_age_days=None))

    assert created == []
    assert "выключен" in capsys.readouterr().out


def test_enqueue_external_scrape_enqueues_only_external_sources(monkeypatch, capsys):
    monkeypatch.setattr("oiltech_digest.config.EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr("oiltech_digest.config.FETCH_EXTERNAL_ENABLED", True)
    sources = [
        {"id": 22, "parse_strategy": "playwright", "network_region": "external"},
        {"id": 4, "parse_strategy": "rss", "network_region": "external"},
        {"id": 50, "parse_strategy": "request", "network_region": "auto"},      # локальный — пропуск
        {"id": 60, "parse_strategy": "telegram", "network_region": "external"}, # telegram — не трогаем
    ]
    monkeypatch.setattr("oiltech_digest.db.repository.get_enabled_sources", lambda: sources)
    jobs = []
    monkeypatch.setattr("oiltech_digest.db.repository.create_background_job",
                        lambda kind, payload, **k: jobs.append((kind, payload, k)))

    cli.cmd_enqueue_external_scrape(argparse.Namespace(max_age_days=7))

    enqueued_ids = {payload["source_id"] for _, payload, _ in jobs}
    assert enqueued_ids == {22, 4}
    queues = {k["queue_name"] for _, _, k in jobs}
    assert queues == {"external-playwright", "external-fetch"}
    assert "задач=2" in capsys.readouterr().out


def test_source_dump_listing_prints_anchors_with_container(monkeypatch, capsys):
    html = (
        b"<html><body>"
        b'<nav><a href="/about">About</a></nav>'
        b'<div class="news-list"><a href="/news/oil-deal">Big oil deal 2026</a></div>'
        b"</body></html>"
    )
    monkeypatch.setattr("oiltech_digest.db.repository.get_source",
                        lambda sid: {"id": 35, "name": "IoT World", "parse_strategy": "request",
                                     "listing_url": "https://example.com/news"})
    monkeypatch.setattr("oiltech_digest.ingestion.http_client.fetch", lambda url: html)

    cli.cmd_source_dump_listing(argparse.Namespace(source_id=35, limit=40))

    out = capsys.readouterr().out
    assert "news-list" in out                                  # контейнер статей виден
    assert "https://example.com/news/oil-deal" in out          # ссылки абсолютизированы
    assert "Big oil deal 2026" in out


def test_source_dump_listing_render_uses_playwright(monkeypatch, capsys):
    html = b'<html><body><div class="news"><a href="/n/1">Rendered SPA article</a></div></body></html>'
    monkeypatch.setattr("oiltech_digest.db.repository.get_source",
                        lambda sid: {"id": 99, "name": "Узбекнефтегаз", "parse_strategy": "request",
                                     "url": "https://www.ung.uz"})
    monkeypatch.setattr("oiltech_digest.ingestion.playwright_parser.is_available", lambda: True)
    rendered = {}
    def fake_render(url, settle_ms=3500):
        rendered["url"] = url
        rendered["settle"] = settle_ms
        return html
    monkeypatch.setattr("oiltech_digest.ingestion.playwright_parser.fetch_rendered", fake_render)

    cli.cmd_source_dump_listing(argparse.Namespace(source_id=99, limit=40, render=True))

    out = capsys.readouterr().out
    assert "playwright-render" in out
    assert "Rendered SPA article" in out
    assert rendered["url"] == "https://www.ung.uz"
    assert rendered["settle"] == 8000          # увеличенный settle для SPA


def test_source_dump_listing_exits_when_listing_unavailable(monkeypatch):
    monkeypatch.setattr("oiltech_digest.db.repository.get_source",
                        lambda sid: {"id": 9, "parse_strategy": "request", "url": "https://x.test"})
    monkeypatch.setattr("oiltech_digest.ingestion.http_client.fetch", lambda url: None)

    with pytest.raises(SystemExit):
        cli.cmd_source_dump_listing(argparse.Namespace(source_id=9, limit=10))


def test_jobs_requeue_stale_command_uses_config_default(monkeypatch, capsys):
    monkeypatch.setattr("oiltech_digest.config.BACKGROUND_JOB_STALE_MINUTES", 75)
    called = {}

    def fake_requeue(stale_minutes):
        called["stale_minutes"] = stale_minutes
        return repository.RequeueOutcome(requeued=2, exhausted=1)

    monkeypatch.setattr("oiltech_digest.db.repository.requeue_stale_background_jobs", fake_requeue)
    monkeypatch.setattr(
        "oiltech_digest.db.repository.requeue_expired_external_leases",
        lambda: repository.RequeueOutcome(requeued=0, exhausted=0),
    )

    cli.main(["jobs-requeue-stale"])

    assert called["stale_minutes"] == 75
    output = capsys.readouterr().out
    assert "requeued=2" in output
    assert "exhausted=1" in output
    assert "stale_minutes=75" in output


def test_agent_query_memory_command_prints_rows(monkeypatch, capsys):
    captured = {}
    monkeypatch.setattr(
        "oiltech_digest.db.repository.query_memory_report",
        lambda **kwargs: captured.update(kwargs) or [
            {
                "query": "robotic drilling automation newsroom",
                "topic": "бурение",
                "score": 76,
                "status": "active",
                "found_candidates": 3,
                "relevance_rate": 0.8,
                "empty_result": False,
            }
        ],
    )

    cli.cmd_agent_query_memory(argparse.Namespace(status="active", limit=5, json=False))

    out = capsys.readouterr().out
    assert "agent-query-memory: status=active rows=1" in out
    assert "robotic drilling automation newsroom" in out
    assert captured == {"status": "active", "limit": 5}


def test_agent_query_memory_command_all_status_passes_none(monkeypatch, capsys):
    captured = {}
    monkeypatch.setattr(
        "oiltech_digest.db.repository.query_memory_report",
        lambda **kwargs: captured.update(kwargs) or [],
    )

    cli.cmd_agent_query_memory(argparse.Namespace(status="all", limit=10, json=True))

    assert capsys.readouterr().out.strip() == "[]"
    assert captured == {"status": None, "limit": 10}


def test_agent_readiness_command_prints_issues(monkeypatch, capsys):
    monkeypatch.setattr(
        "oiltech_digest.source_discovery.readiness.source_discovery_readiness",
        lambda: {
            "ok": False,
            "status": "blocked",
            "checks": {"search": {"ok": False}},
            "issues": [{"severity": "blocker", "code": "brave_key_missing", "message": "BRAVE_SEARCH_API_KEY пустой"}],
            "recommendations": ["Заполните BRAVE_SEARCH_API_KEY"],
        },
    )

    cli.cmd_agent_readiness(argparse.Namespace(json=False))

    out = capsys.readouterr().out
    assert "agent-readiness: status=blocked ok=False" in out
    assert "brave_key_missing" in out
    assert "Заполните BRAVE_SEARCH_API_KEY" in out


def test_agent_loop_command_prints_summary(monkeypatch, capsys):
    monkeypatch.setattr(
        "oiltech_digest.source_discovery.loop.run_agent_loop",
        lambda config: {
            "run_id": 77,
            "iterations": [
                {
                    "iteration": 1,
                    "action_count": 1,
                    "auto_action_count": 1,
                    "human_review_count": 0,
                    "observations": [{"topic": "бурение", "candidate_count": 2, "query_strategy": "balanced", "search_status": "ok"}],
                }
            ],
            "total_candidates": 2,
            "terminal_reason": "max_iterations_reached",
        },
    )

    cli.cmd_agent_loop(argparse.Namespace(
        goal="найти",
        days=30,
        target_per_topic=10,
        topic_limit=5,
        candidate_limit=10,
        max_actions=5,
        max_iterations=1,
        offline=True,
        fetch_inspection=False,
        dry_run=False,
        evaluate=True,
        article_limit=5,
        no_memory=False,
        max_daily_loop_runs=4,
        max_daily_candidates=100,
        max_daily_evaluations=100,
        json=False,
    ))

    out = capsys.readouterr().out
    assert "agent-loop: run_id=77 iterations=1 candidates=2" in out
    assert "бурение: candidates=2 strategy=balanced search=ok" in out


def test_enqueue_agent_loop_command_creates_job(monkeypatch, capsys):
    captured = {}
    monkeypatch.setattr(
        "oiltech_digest.db.repository.background_job_status_counts",
        lambda **kwargs: {},
    )
    monkeypatch.setattr(
        "oiltech_digest.db.repository.create_background_job",
        lambda kind, payload, **kwargs: captured.update({"kind": kind, "payload": payload, **kwargs}) or {"id": 91},
    )

    cli.cmd_enqueue_agent_loop(argparse.Namespace(
        goal="найти",
        days=30,
        target_per_topic=10,
        topic_limit=5,
        candidate_limit=10,
        max_actions=4,
        max_iterations=2,
        offline=True,
        fetch_inspection=False,
        dry_run=False,
        evaluate=True,
        article_limit=5,
        no_memory=False,
        max_daily_loop_runs=4,
        max_daily_candidates=100,
        max_daily_evaluations=100,
    ))

    assert captured["kind"] == "source_discovery_loop"
    assert captured["payload"]["max_iterations"] == 2
    assert captured["capability"] == "source-discovery"
    assert "enqueue-agent-loop: job id=91" in capsys.readouterr().out


def test_enqueue_agent_loop_command_skips_when_loop_already_active(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(
        "oiltech_digest.db.repository.background_job_status_counts",
        lambda **kwargs: {"queued": 1},
    )
    monkeypatch.setattr(
        "oiltech_digest.db.repository.create_background_job",
        lambda *args, **kwargs: called.append((args, kwargs)) or {"id": 91},
    )

    cli.cmd_enqueue_agent_loop(argparse.Namespace(
        goal="найти",
        days=30,
        target_per_topic=10,
        topic_limit=5,
        candidate_limit=10,
        max_actions=4,
        max_iterations=2,
        offline=True,
        fetch_inspection=False,
        dry_run=False,
        evaluate=True,
        article_limit=5,
        no_memory=False,
        allow_parallel=False,
        max_daily_loop_runs=4,
        max_daily_candidates=100,
        max_daily_evaluations=100,
    ))

    assert called == []
    assert "enqueue-agent-loop: skipped active_jobs=1" in capsys.readouterr().out


def test_source_candidate_triage_command_prints_rows(monkeypatch, capsys):
    captured = {}
    monkeypatch.setattr(
        "oiltech_digest.db.repository.source_candidate_triage_report",
        lambda **kwargs: captured.update(kwargs) or [
            {
                "id": 7,
                "normalized_domain": "example.com",
                "url": "https://example.com/news",
                "status": "needs_human_review",
                "recommended_action": "add",
                "triage_priority": 120,
                "tested_articles": 5,
                "relevant_articles": 4,
                "avg_score": 80,
                "topic": "бурение",
            }
        ],
    )

    cli.cmd_source_candidate_triage(argparse.Namespace(limit=5, json=False))

    out = capsys.readouterr().out
    assert "source-candidate-triage: rows=1" in out
    assert "example.com" in out
    assert captured == {"limit": 5}


def test_jobs_requeue_stale_command_accepts_override(monkeypatch, capsys):
    monkeypatch.setattr(
        "oiltech_digest.db.repository.requeue_stale_background_jobs",
        lambda stale_minutes: repository.RequeueOutcome(
            requeued=stale_minutes // 30, exhausted=0
        ),
    )
    monkeypatch.setattr(
        "oiltech_digest.db.repository.requeue_expired_external_leases",
        lambda: repository.RequeueOutcome(requeued=0, exhausted=0),
    )

    cli.main(["jobs-requeue-stale", "--stale-minutes", "120"])

    output = capsys.readouterr().out
    assert "requeued=4" in output
    assert "stale_minutes=120" in output


def test_external_queues_status_command(monkeypatch, capsys):
    monkeypatch.setattr(
        "oiltech_digest.db.repository.external_queue_status",
        lambda: {
            "totals": {
                "queued": 3,
                "running": 1,
                "failed": 2,
                "ok": 0,
                "expired_leases": 0,
                "oldest_queued_at": None,
                "last_heartbeat_at": None,
            },
            "queues": [
                {
                    "queue_name": "external-ai",
                    "queued": 3,
                    "running": 1,
                    "failed": 2,
                    "ok": 0,
                    "oldest_queued_at": None,
                    "last_heartbeat_at": None,
                }
            ],
        },
    )

    cli.main(["external-queues-status"])

    output = capsys.readouterr().out
    assert "external-queues: queued=3" in output
    assert "external-ai: queued=3" in output


def test_maintenance_cleanup_command_uses_defaults(monkeypatch, capsys):
    monkeypatch.setattr("oiltech_digest.config.BACKGROUND_JOB_RETENTION_DAYS", 21)
    monkeypatch.setattr("oiltech_digest.config.EXPORT_JOB_RETENTION_DAYS", 14)
    monkeypatch.setattr("oiltech_digest.db.repository.delete_expired_user_sessions", lambda: 3)
    monkeypatch.setattr("oiltech_digest.db.repository.cleanup_finished_background_jobs", lambda days: days // 7)
    monkeypatch.setattr("oiltech_digest.db.repository.cleanup_finished_export_jobs", lambda days: days // 7)

    cli.main(["maintenance-cleanup"])

    output = capsys.readouterr().out
    assert "expired_sessions=3" in output
    assert "background_jobs=3" in output
    assert "background_job_days=21" in output
    assert "export_jobs=2" in output
    assert "export_job_days=14" in output


def test_maintenance_cleanup_command_accepts_overrides(monkeypatch, capsys):
    monkeypatch.setattr("oiltech_digest.db.repository.delete_expired_user_sessions", lambda: 1)
    monkeypatch.setattr("oiltech_digest.db.repository.cleanup_finished_background_jobs", lambda days: days)
    monkeypatch.setattr("oiltech_digest.db.repository.cleanup_finished_export_jobs", lambda days: days)

    cli.main(["maintenance-cleanup", "--background-job-days", "10", "--export-job-days", "5"])

    output = capsys.readouterr().out
    assert "expired_sessions=1" in output
    assert "background_jobs=10" in output
    assert "background_job_days=10" in output
    assert "export_jobs=5" in output
    assert "export_job_days=5" in output
