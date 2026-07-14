"""Small persistent background-job runner for heavy API operations.

This is intentionally lightweight: one process-local executor plus database
state. It gives the API stable job contracts now and leaves room to swap the
executor for Redis/Celery later without changing frontend-facing endpoints.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import time
from pathlib import Path
from typing import Any, Callable

from oiltech_digest import config
from oiltech_digest.db import repository
from oiltech_digest.ingestion import playwright_parser, request_parser
from oiltech_digest.ingestion.source_diagnostics import diagnose_source
from oiltech_digest.processing.digest import write_digest_export
from oiltech_digest.processing.pipeline import (
    make_client,
    process_relevance_articles,
    process_score_articles,
    process_summary_articles,
    process_tag_articles,
)

_executor = ThreadPoolExecutor(max_workers=max(1, config.BACKGROUND_JOB_WORKERS))
logger = logging.getLogger(__name__)


def enqueue(
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    user_id: int | None = None,
    queue_name: str = "default",
    execution_region: str = "ru",
    capability: str | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Create a persistent job and submit it to the local executor."""
    if kind not in _HANDLERS:
        raise ValueError(f"Unknown background job kind: {kind}")
    job = repository.create_background_job(
        kind,
        payload or {},
        user_id=user_id,
        queue_name=queue_name,
        execution_region=execution_region,
        capability=capability,
        max_attempts=max_attempts,
    )
    if config.BACKGROUND_JOB_INLINE:
        _executor.submit(run, int(job["id"]))
    return job


def run(job_id: int) -> None:
    """Execute a queued job and persist terminal state."""
    job = repository.get_background_job(job_id)
    if job is None:
        return
    _execute(job, mark_running=True)


def run_claimed(job: dict[str, Any]) -> None:
    """Execute a job already claimed by an external worker."""
    _execute(job, mark_running=False)


def worker_loop(
    *,
    poll_seconds: float | None = None,
    once: bool = False,
    stale_minutes: int | None = None,
    queue_names: list[str] | None = None,
) -> None:
    """Poll the DB queue and execute jobs in the current process."""
    poll_seconds = config.BACKGROUND_JOB_POLL_SECONDS if poll_seconds is None else poll_seconds
    stale_minutes = config.BACKGROUND_JOB_STALE_MINUTES if stale_minutes is None else stale_minutes
    queue_names = queue_names or config.BACKGROUND_JOB_QUEUES
    finalize_minutes = config.FINALIZE_STALE_MINUTES
    sweep_interval = max(poll_seconds, 30.0)  # переочередь зависших — не чаще раза в ~30с

    def _sweep_stale() -> None:
        requeued = repository.requeue_stale_background_jobs(stale_minutes, finalize_minutes)
        if requeued:
            logger.warning(
                "jobs_requeued_stale count=%s stale_minutes=%s finalize_minutes=%s queues=%s",
                requeued,
                stale_minutes,
                finalize_minutes,
                ",".join(queue_names),
            )

    last_sweep = 0.0
    while True:
        # Периодически вытаскиваем зависшие running/finalizing (раньше — только разово на старте,
        # из-за чего застрявший после краша 'finalizing' ждал рестарта воркача; баг T2/H2).
        now_mono = time.monotonic()
        if now_mono - last_sweep >= sweep_interval:
            _sweep_stale()
            last_sweep = now_mono
        job = repository.claim_next_background_job(queue_names=queue_names)
        if job is None:
            if once:
                return
            time.sleep(poll_seconds)
            continue
        logger.info(
            "background_job_started job_id=%s kind=%s queue=%s attempts=%s",
            job["id"],
            job["kind"],
            job.get("queue_name"),
            job.get("attempts"),
        )
        run_claimed(job)


def _execute(job: dict[str, Any], *, mark_running: bool) -> None:
    job_id = int(job["id"])
    handler = _HANDLERS.get(job["kind"])
    if handler is None:
        repository.fail_background_job(job_id, f"Unknown background job kind: {job['kind']}")
        return

    try:
        if mark_running:
            repository.mark_background_job_running(job_id)
            job = repository.get_background_job(job_id) or job
        result = handler(dict(job.get("payload_json") or {}), job_id)
        repository.finish_background_job(job_id, result)
        logger.info(
            "background_job_finished job_id=%s kind=%s queue=%s",
            job["id"],
            job["kind"],
            job.get("queue_name"),
        )
    except Exception as exc:  # noqa: BLE001 - terminal job errors must be recorded
        retry_delay = _retry_delay_seconds(job)
        repository.fail_background_job(int(job["id"]), str(exc), retry_delay_seconds=retry_delay)
        logger.exception(
            "background_job_failed job_id=%s kind=%s queue=%s retry_delay_seconds=%s",
            job["id"],
            job["kind"],
            job.get("queue_name"),
            retry_delay,
        )


def _retry_delay_seconds(job: dict[str, Any]) -> int | None:
    attempts = int(job.get("attempts") or 0)
    max_attempts = int(job.get("max_attempts") or 0)
    if attempts >= max_attempts:
        return None
    return min(config.BACKGROUND_JOB_RETRY_BASE_SECONDS * (2 ** max(0, attempts - 1)), 1800)


def _run_digest_export(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    repository.update_background_job_progress(job_id, 25)
    result = write_digest_export(
        month=str(payload.get("month") or ""),
        export_format=str(payload.get("export_format") or "pdf"),
        limit=int(payload.get("limit") or 100),
        min_score=float(payload.get("min_score") or 0),
        max_score=float(payload["max_score"]) if payload.get("max_score") is not None else None,
        search=str(payload.get("search") or "") or None,
        top_tag=str(payload.get("top_tag") or "") or None,
        user_id=int(payload["user_id"]) if payload.get("user_id") is not None else None,
    )
    repository.update_background_job_progress(job_id, 90)
    return result


def _run_process_articles(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    client = make_client(bool(payload.get("offline", False)))
    article_ids = payload.get("article_ids") or []
    has_article_ids = bool(article_ids)
    limit = int(payload.get("limit") or 5)

    if has_article_ids:
        ids = [int(article_id) for article_id in article_ids]
        articles = repository.get_articles_by_ids(ids, include_summary=False)
    else:
        ids = []
        articles = repository.get_articles_needing_summary(limit)

    # Отметка «эта попытка НАЧАЛА жечь OpenAI» — до первого обращения к модели.
    # По ней requeue_stale_background_jobs отличает задачу, которую нельзя перезапускать
    # автоматически (повторный прогон = повторный реальный расход), от упавшей до AI.
    # Без этой отметки прогресс оставался 0, пока сгорали деньги на первой же стадии.
    repository.update_background_job_progress(job_id, 1)
    summaries = process_summary_articles(articles, client)
    repository.update_background_job_progress(job_id, 35)

    ids = ids or [int(article["id"]) for article in articles]
    relevance_articles = (
        repository.get_articles_by_ids(ids, include_summary=True)
        if has_article_ids
        else repository.get_articles_needing_relevance(limit)
    )
    relevance = process_relevance_articles(relevance_articles, client)
    repository.update_background_job_progress(job_id, 55)

    with_summary = (
        repository.get_articles_by_ids(ids, include_summary=True)
        if has_article_ids
        else repository.get_articles_needing_tags(limit)
    )
    tags = process_tag_articles(with_summary, client)
    repository.update_background_job_progress(job_id, 75)

    with_summary = (
        repository.get_articles_by_ids(ids, include_summary=True)
        if has_article_ids
        else repository.get_articles_needing_scores(limit)
    )
    scores = process_score_articles(with_summary, client)
    repository.update_background_job_progress(job_id, 95)

    return {"summary": summaries, "relevance": relevance, "tagging": tags, "scoring": scores}


def _run_scrape_source(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    source_id = int(payload["source_id"])
    source = repository.get_source(source_id)
    if source is None:
        raise ValueError("Source not found")

    strategy = source.get("parse_strategy")
    if strategy not in {"request", "playwright"}:
        raise ValueError("Скраппер доступен только для request/playwright-источников")

    repository.update_background_job_progress(job_id, 25)
    stats = playwright_parser.parse_source(source) if strategy == "playwright" else request_parser.parse_source(source)
    repository.update_background_job_progress(job_id, 90)
    return {"source_id": source_id, "stats": stats}


def _run_diagnose_source(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    source_id = int(payload["source_id"])
    source = repository.get_source(source_id)
    if source is None:
        raise ValueError("Source not found")
    overrides = payload.get("overrides") or {}
    limit = int(payload.get("limit") or 5)

    repository.update_background_job_progress(job_id, 20)
    result = diagnose_source({**source, **overrides}, limit=limit)
    repository.update_background_job_progress(job_id, 90)
    return result


def job_download_path(job: dict[str, Any]) -> Path | None:
    result = job.get("result_json") or {}
    path = result.get("path")
    return Path(path) if path else None


_HANDLERS: dict[str, Callable[[dict[str, Any], int], dict[str, Any]]] = {
    "digest_export": _run_digest_export,
    "process_articles": _run_process_articles,
    "scrape_source": _run_scrape_source,
    "diagnose_source": _run_diagnose_source,
}
