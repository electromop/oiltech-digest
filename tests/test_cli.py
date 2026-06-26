import argparse

import pytest

from oiltech_digest import cli


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


def test_jobs_requeue_stale_command_uses_config_default(monkeypatch, capsys):
    monkeypatch.setattr("oiltech_digest.config.BACKGROUND_JOB_STALE_MINUTES", 75)
    called = {}

    def fake_requeue(stale_minutes):
        called["stale_minutes"] = stale_minutes
        return 2

    monkeypatch.setattr("oiltech_digest.db.repository.requeue_stale_background_jobs", fake_requeue)

    cli.main(["jobs-requeue-stale"])

    assert called["stale_minutes"] == 75
    output = capsys.readouterr().out
    assert "requeued=2" in output
    assert "stale_minutes=75" in output


def test_jobs_requeue_stale_command_accepts_override(monkeypatch, capsys):
    monkeypatch.setattr(
        "oiltech_digest.db.repository.requeue_stale_background_jobs",
        lambda stale_minutes: stale_minutes // 30,
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
