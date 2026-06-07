from datetime import datetime, timedelta, timezone

from oiltech_digest import background_jobs
from oiltech_digest.db import connection
from oiltech_digest.db import repository


def test_background_job_run_records_success(monkeypatch, isolated_db):
    job = repository.create_background_job("test_success", {"value": 3})

    monkeypatch.setitem(
        background_jobs._HANDLERS,
        "test_success",
        lambda payload, job_id: {"doubled": payload["value"] * 2},
    )

    background_jobs.run(int(job["id"]))

    stored = repository.get_background_job(int(job["id"]))
    assert stored["status"] == "ok"
    assert stored["progress"] == 100
    assert stored["result_json"] == {"doubled": 6}
    assert stored["error_message"] is None
    assert stored["started_at"] is not None
    assert stored["finished_at"] is not None


def test_background_job_run_records_failure(monkeypatch, isolated_db):
    job = repository.create_background_job("test_failure", {}, max_attempts=1)

    def fail(payload, job_id):
        raise RuntimeError("boom")

    monkeypatch.setitem(background_jobs._HANDLERS, "test_failure", fail)

    background_jobs.run(int(job["id"]))

    stored = repository.get_background_job(int(job["id"]))
    assert stored["status"] == "failed"
    assert stored["error_message"] == "boom"
    assert stored["started_at"] is not None
    assert stored["finished_at"] is not None


def test_background_job_run_requeues_retryable_failure(monkeypatch, isolated_db):
    job = repository.create_background_job("test_retry", {}, max_attempts=3)

    def fail(payload, job_id):
        raise RuntimeError("temporary")

    monkeypatch.setattr(background_jobs.config, "BACKGROUND_JOB_RETRY_BASE_SECONDS", 5)
    monkeypatch.setitem(background_jobs._HANDLERS, "test_retry", fail)

    background_jobs.run(int(job["id"]))

    stored = repository.get_background_job(int(job["id"]))
    assert stored["status"] == "queued"
    assert stored["attempts"] == 1
    assert stored["error_message"] == "temporary"
    assert stored["run_after"] is not None


def test_process_job_without_article_ids_uses_each_stage_queue(monkeypatch):
    calls = []

    monkeypatch.setattr(background_jobs, "make_client", lambda offline: object())
    monkeypatch.setattr(background_jobs.repository, "update_background_job_progress", lambda job_id, progress: None)
    monkeypatch.setattr(
        background_jobs.repository,
        "get_articles_needing_summary",
        lambda limit: calls.append(("summary_queue", limit)) or [{"id": 1}],
    )
    monkeypatch.setattr(
        background_jobs.repository,
        "get_articles_needing_relevance",
        lambda limit: calls.append(("relevance_queue", limit)) or [{"id": 2}],
    )
    monkeypatch.setattr(
        background_jobs.repository,
        "get_articles_needing_tags",
        lambda limit: calls.append(("tags_queue", limit)) or [{"id": 3}],
    )
    monkeypatch.setattr(
        background_jobs.repository,
        "get_articles_needing_scores",
        lambda limit: calls.append(("scores_queue", limit)) or [{"id": 4}],
    )
    monkeypatch.setattr(background_jobs.repository, "get_articles_by_ids", lambda ids, include_summary: [])
    monkeypatch.setattr(background_jobs, "process_summary_articles", lambda articles, client: {"processed": len(articles)})
    monkeypatch.setattr(background_jobs, "process_relevance_articles", lambda articles, client: {"processed": len(articles)})
    monkeypatch.setattr(background_jobs, "process_tag_articles", lambda articles, client: {"processed": len(articles)})
    monkeypatch.setattr(background_jobs, "process_score_articles", lambda articles, client: {"processed": len(articles)})

    result = background_jobs._run_process_articles({"limit": 10}, job_id=123)

    assert calls == [
        ("summary_queue", 10),
        ("relevance_queue", 10),
        ("tags_queue", 10),
        ("scores_queue", 10),
    ]
    assert result["summary"] == {"processed": 1}
    assert result["relevance"] == {"processed": 1}
    assert result["tagging"] == {"processed": 1}
    assert result["scoring"] == {"processed": 1}


def test_enqueue_can_skip_inline_execution(monkeypatch, isolated_db):
    submitted = []
    monkeypatch.setattr(background_jobs.config, "BACKGROUND_JOB_INLINE", False)
    monkeypatch.setattr(background_jobs._executor, "submit", lambda *args, **kwargs: submitted.append(args))
    monkeypatch.setitem(background_jobs._HANDLERS, "test_queued", lambda payload, job_id: {"ok": True})

    job = background_jobs.enqueue("test_queued", {"x": 1})

    stored = repository.get_background_job(int(job["id"]))
    assert stored["status"] == "queued"
    assert submitted == []


def test_claim_next_background_job_marks_oldest_queued_job_running(isolated_db):
    first = repository.create_background_job("test_first", {}, queue_name="default")
    repository.create_background_job("test_second", {}, queue_name="playwright")

    claimed = repository.claim_next_background_job(queue_names=["default"])

    assert claimed["id"] == first["id"]
    assert claimed["status"] == "running"
    assert claimed["started_at"] is not None
    assert repository.get_background_job(int(first["id"]))["status"] == "running"
    assert repository.get_background_job(int(first["id"]))["attempts"] == 1


def test_claim_next_background_job_filters_by_queue(isolated_db):
    repository.create_background_job("test_default", {}, queue_name="default")
    playwright = repository.create_background_job("test_playwright", {}, queue_name="playwright")

    claimed = repository.claim_next_background_job(queue_names=["playwright"])

    assert claimed["id"] == playwright["id"]
    assert claimed["queue_name"] == "playwright"


def test_claim_next_background_job_skips_delayed_retry(isolated_db):
    job = repository.create_background_job("test_delayed", {}, queue_name="default")
    with connection.get_connection() as conn:
        conn.execute(
            "UPDATE background_jobs SET run_after = now() + interval '10 minutes' WHERE id = %s",
            (job["id"],),
        )
        conn.commit()

    assert repository.claim_next_background_job(queue_names=["default"]) is None


def test_requeue_stale_background_jobs_recovers_stuck_running_job(isolated_db):
    job = repository.create_background_job("test_stale", {}, queue_name="default")
    claimed = repository.claim_next_background_job(queue_names=["default"])
    assert claimed["status"] == "running"

    stale_started_at = datetime.now(timezone.utc) - timedelta(hours=2)
    with connection.get_connection() as conn:
        conn.execute(
            "UPDATE background_jobs SET started_at = %s WHERE id = %s",
            (stale_started_at, job["id"]),
        )
        conn.commit()

    requeued = repository.requeue_stale_background_jobs(stale_minutes=60)
    stored = repository.get_background_job(int(job["id"]))

    assert requeued == 1
    assert stored["status"] == "queued"
    assert stored["progress"] == 0
    assert stored["started_at"] is None
    assert stored["error_message"] == "Requeued after stale running timeout"


def test_worker_loop_once_processes_queued_jobs(monkeypatch, isolated_db):
    first = repository.create_background_job("test_worker", {"value": 2})
    second = repository.create_background_job("test_worker", {"value": 4})

    monkeypatch.setitem(
        background_jobs._HANDLERS,
        "test_worker",
        lambda payload, job_id: {"value": payload["value"]},
    )

    background_jobs.worker_loop(once=True, poll_seconds=0, stale_minutes=60)

    assert repository.get_background_job(int(first["id"]))["status"] == "ok"
    assert repository.get_background_job(int(first["id"]))["result_json"] == {"value": 2}
    assert repository.get_background_job(int(second["id"]))["status"] == "ok"
    assert repository.get_background_job(int(second["id"]))["result_json"] == {"value": 4}
