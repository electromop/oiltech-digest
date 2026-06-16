"""Maintenance helpers for service tables and queue hygiene."""

from __future__ import annotations

from typing import Any

from oiltech_digest import config
from oiltech_digest.db import repository


def maintenance_status(
    *,
    stale_minutes: int | None = None,
    background_job_days: int | None = None,
    export_job_days: int | None = None,
) -> dict[str, Any]:
    stale_minutes = config.BACKGROUND_JOB_STALE_MINUTES if stale_minutes is None else stale_minutes
    background_job_days = (
        config.BACKGROUND_JOB_RETENTION_DAYS
        if background_job_days is None
        else background_job_days
    )
    export_job_days = config.EXPORT_JOB_RETENTION_DAYS if export_job_days is None else export_job_days

    return {
        "retention": {
            "stale_minutes": stale_minutes,
            "background_job_days": background_job_days,
            "export_job_days": export_job_days,
        },
        "expired_sessions": repository.count_expired_user_sessions(),
        "stale_running_jobs": repository.count_stale_running_background_jobs(stale_minutes),
        "cleanup_candidates": {
            "background_jobs": repository.count_finished_background_jobs_eligible_for_cleanup(
                background_job_days
            ),
            "export_jobs": repository.count_finished_export_jobs_eligible_for_cleanup(export_job_days),
        },
        "external_queues": repository.external_queue_status(),
    }


def maintenance_cleanup(
    *,
    background_job_days: int | None = None,
    export_job_days: int | None = None,
) -> dict[str, Any]:
    background_job_days = (
        config.BACKGROUND_JOB_RETENTION_DAYS
        if background_job_days is None
        else background_job_days
    )
    export_job_days = config.EXPORT_JOB_RETENTION_DAYS if export_job_days is None else export_job_days

    return {
        "expired_sessions": repository.delete_expired_user_sessions(),
        "background_jobs": repository.cleanup_finished_background_jobs(background_job_days),
        "background_job_days": background_job_days,
        "export_jobs": repository.cleanup_finished_export_jobs(export_job_days),
        "export_job_days": export_job_days,
    }
