from oiltech_digest.source_discovery import readiness


def test_source_discovery_readiness_blocks_missing_brave_key(monkeypatch):
    monkeypatch.setattr(readiness.config, "SOURCE_DISCOVERY_SEARCH_PROVIDER", "brave")
    monkeypatch.setattr(readiness.config, "BRAVE_SEARCH_API_KEY", "")
    monkeypatch.setattr(readiness.config, "EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr(readiness.config, "AI_EXECUTION_REGION", "external")
    monkeypatch.setattr(readiness.config, "EXTERNAL_WORKER_TOKEN_HASH", "hash")
    monkeypatch.setattr(readiness.repository, "source_candidate_triage_report", lambda limit=20: [{"id": 1}])
    monkeypatch.setattr(readiness.repository, "query_memory_report", lambda **kwargs: [{"query": "q"}])
    monkeypatch.setattr(readiness.repository, "background_job_status_counts", lambda **kwargs: {})
    monkeypatch.setattr(readiness.repository, "source_discovery_daily_usage", lambda: {"loop_runs": 0, "candidates_created": 0, "candidate_evaluations": 0})

    report = readiness.source_discovery_readiness()

    assert report["ok"] is False
    assert report["status"] == "blocked"
    assert any(issue["code"] == "brave_key_missing" for issue in report["issues"])


def test_source_discovery_readiness_allows_seed_url_mode(monkeypatch):
    monkeypatch.setattr(readiness.config, "SOURCE_DISCOVERY_SEARCH_PROVIDER", "none")
    monkeypatch.setattr(readiness.config, "EXTERNAL_WORKERS_ENABLED", False)
    monkeypatch.setattr(readiness.config, "AI_EXECUTION_REGION", "ru")
    monkeypatch.setattr(readiness.repository, "source_candidate_triage_report", lambda limit=20: [{"id": 1}])
    monkeypatch.setattr(readiness.repository, "query_memory_report", lambda **kwargs: [{"query": "q"}])
    monkeypatch.setattr(readiness.repository, "background_job_status_counts", lambda **kwargs: {})
    monkeypatch.setattr(readiness.repository, "source_discovery_daily_usage", lambda: {"loop_runs": 0, "candidates_created": 0, "candidate_evaluations": 0})

    report = readiness.source_discovery_readiness()

    assert report["ok"] is True
    assert report["status"] == "degraded"
    assert any(issue["code"] == "search_provider_disabled" for issue in report["issues"])


def test_source_discovery_readiness_reports_loop_scheduler_mode(monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    monkeypatch.setenv("SOURCE_DISCOVERY_MODE", "loop")
    monkeypatch.setenv("SOURCE_DISCOVERY_MAX_ITERATIONS", "4")
    monkeypatch.setattr(readiness.config, "SOURCE_DISCOVERY_SEARCH_PROVIDER", "none")
    monkeypatch.setattr(readiness.config, "EXTERNAL_WORKERS_ENABLED", False)
    monkeypatch.setattr(readiness.config, "AI_EXECUTION_REGION", "ru")
    monkeypatch.setattr(readiness.repository, "source_candidate_triage_report", lambda limit=20: [{"id": 1}])
    monkeypatch.setattr(readiness.repository, "query_memory_report", lambda **kwargs: [{"query": "q"}])
    monkeypatch.setattr(readiness.repository, "background_job_status_counts", lambda **kwargs: {})
    monkeypatch.setattr(readiness.repository, "source_discovery_daily_usage", lambda: {"loop_runs": 1, "candidates_created": 2, "candidate_evaluations": 3})

    report = readiness.source_discovery_readiness()

    assert report["checks"]["scheduler"]["mode"] == "loop"
    assert report["checks"]["scheduler"]["max_iterations"] == 4
    assert report["checks"]["scheduler"]["ok"] is True
    assert report["checks"]["budget"]["usage"]["candidates_created"] == 2
