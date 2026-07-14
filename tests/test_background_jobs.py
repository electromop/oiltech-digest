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


def test_background_job_records_execution_metadata(isolated_db):
    job = repository.create_background_job(
        "test_external",
        {"value": 1},
        queue_name="external-ai",
        execution_region="external",
        capability="openai",
    )

    stored = repository.get_background_job(int(job["id"]))

    assert stored["queue_name"] == "external-ai"
    assert stored["execution_region"] == "external"
    assert stored["capability"] == "openai"


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


def test_claim_external_background_job_sets_lease_metadata(isolated_db):
    default = repository.create_background_job("test_default", {}, queue_name="default")
    external = repository.create_background_job(
        "test_external",
        {},
        queue_name="external-ai",
        execution_region="external",
        capability="openai",
    )

    claimed = repository.claim_external_background_job(
        queue_names=["external-ai"],
        capabilities=["openai"],
        worker_id="eu-worker-1",
        lease_token_hash="hash1",
        lease_seconds=600,
    )

    assert claimed["id"] == external["id"]
    assert claimed["status"] == "running"
    assert claimed["claimed_by"] == "eu-worker-1"
    assert claimed["lease_token_hash"] == "hash1"
    assert claimed["lease_expires_at"] is not None
    assert repository.get_background_job(int(default["id"]))["status"] == "queued"


def test_external_job_progress_complete_and_wrong_lease(isolated_db):
    job = repository.create_background_job(
        "test_external",
        {},
        queue_name="external-ai",
        execution_region="external",
        capability="openai",
    )
    claimed = repository.claim_external_background_job(
        queue_names=["external-ai"],
        capabilities=["openai"],
        worker_id="eu-worker-1",
        lease_token_hash="hash1",
        lease_seconds=600,
    )

    assert repository.update_external_background_job_progress(int(claimed["id"]), lease_token_hash="wrong", progress=50) is False
    assert repository.external_background_job_lease_is_active(int(claimed["id"]), lease_token_hash="wrong") is False
    assert repository.external_background_job_lease_is_active(int(claimed["id"]), lease_token_hash="hash1") is True
    assert repository.update_external_background_job_progress(int(claimed["id"]), lease_token_hash="hash1", progress=50) is True
    assert repository.finish_external_background_job(int(claimed["id"]), lease_token_hash="wrong", result={"ok": True}) is False
    assert repository.finish_external_background_job(int(claimed["id"]), lease_token_hash="hash1", result={"ok": True}) is True

    stored = repository.get_background_job(int(job["id"]))
    assert stored["status"] == "ok"
    assert stored["progress"] == 100
    assert stored["result_json"] == {"ok": True}
    assert stored["lease_token_hash"] is None


def test_external_job_retryable_fail_requeues(isolated_db):
    job = repository.create_background_job(
        "test_external",
        {},
        queue_name="external-ai",
        execution_region="external",
        capability="openai",
        max_attempts=3,
    )
    repository.claim_external_background_job(
        queue_names=["external-ai"],
        capabilities=["openai"],
        worker_id="eu-worker-1",
        lease_token_hash="hash1",
        lease_seconds=600,
    )

    assert repository.fail_external_background_job(
        int(job["id"]),
        lease_token_hash="hash1",
        error_message="temporary",
        retryable=True,
        retry_delay_seconds=120,
    ) is True

    stored = repository.get_background_job(int(job["id"]))
    assert stored["status"] == "queued"
    assert stored["error_message"] == "temporary"
    assert stored["lease_token_hash"] is None


def test_requeue_expired_external_leases(isolated_db):
    job = repository.create_background_job(
        "test_external",
        {},
        queue_name="external-ai",
        execution_region="external",
        capability="openai",
    )
    repository.claim_external_background_job(
        queue_names=["external-ai"],
        capabilities=["openai"],
        worker_id="eu-worker-1",
        lease_token_hash="hash1",
        lease_seconds=600,
    )
    with connection.get_connection() as conn:
        conn.execute(
            "UPDATE background_jobs SET lease_expires_at = now() - interval '1 minute' WHERE id = %s",
            (job["id"],),
        )
        conn.commit()

    assert repository.requeue_expired_external_leases() == 1
    stored = repository.get_background_job(int(job["id"]))
    assert stored["status"] == "queued"
    assert stored["claimed_by"] is None
    assert stored["lease_token_hash"] is None


def test_external_queue_status_summarizes_external_jobs(isolated_db):
    repository.create_background_job(
        "test_external",
        {},
        queue_name="external-ai",
        execution_region="external",
        capability="openai",
    )
    repository.create_background_job("test_local", {}, queue_name="default")

    status = repository.external_queue_status()

    assert status["totals"]["queued"] == 1
    assert status["totals"]["running"] == 0
    assert status["queues"][0]["queue_name"] == "external-ai"
    assert status["queues"][0]["queued"] == 1


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
    assert stored["error_message"] == "Requeued after stale running/finalizing timeout"


def test_requeue_stale_does_not_rerun_local_ai_job_that_already_spent_money(isolated_db):
    """Зависшая ЛОКАЛЬНАЯ AI-обработка, уже начавшая жечь OpenAI, не перезапускается.

    requeue переиспользует тот же job_id, а get_articles_by_ids не пропускает уже
    обработанные статьи — повторный прогон означает повторный РЕАЛЬНЫЙ расход.
    Дедуп биллинга по (job_id, article_id, stage) тут не помог бы: он лишь СПРЯТАЛ бы
    второй, реально оплаченный вызов из отчёта о стоимости. Поэтому помечаем failed
    без авто-ретрая. Внешний контур (external-ai) не затрагиваем — у него свой lease.
    """
    stale_started_at = datetime.now(timezone.utc) - timedelta(hours=2)

    def make_stale_running(kind: str, queue_name: str, progress: int) -> dict:
        job = repository.create_background_job(kind, {}, queue_name=queue_name)
        repository.claim_next_background_job(queue_names=[queue_name])
        with connection.get_connection() as conn:
            conn.execute(
                "UPDATE background_jobs SET started_at = %s, progress = %s WHERE id = %s",
                (stale_started_at, progress, job["id"]),
            )
            conn.commit()
        return job

    burned = make_stale_running("process_articles", "default", progress=35)
    not_started = make_stale_running("process_articles", "default", progress=0)
    external = make_stale_running("process_articles", "external-ai", progress=35)

    repository.requeue_stale_background_jobs(stale_minutes=60)

    # Уже потратила деньги → failed, без авто-возврата в очередь.
    burned_stored = repository.get_background_job(int(burned["id"]))
    assert burned_stored["status"] == "failed"
    assert "дважды" in (burned_stored["error_message"] or "")

    # Упала ДО обращения к модели → безопасно перезапустить.
    assert repository.get_background_job(int(not_started["id"]))["status"] == "queued"

    # Внешний контур не задет — у него собственная защита (lease/finalize, T2/H1).
    assert repository.get_background_job(int(external["id"]))["status"] == "queued"


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
