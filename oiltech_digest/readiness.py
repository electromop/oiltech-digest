"""Production readiness and schema checks."""

from __future__ import annotations

from typing import Any

from oiltech_digest import config
from oiltech_digest.db.connection import get_connection

REQUIRED_TABLES = (
    "articles",
    "article_cards",
    "article_scores",
    "article_tags",
    "background_jobs",
    "sources",
    "tags",
    "users",
    "user_sessions",
)


def schema_check() -> dict[str, Any]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            """
        ).fetchall()
        present = {row[0] for row in rows}
        missing = [name for name in REQUIRED_TABLES if name not in present]
        return {
            "ok": not missing,
            "required_tables": list(REQUIRED_TABLES),
            "present_tables": sorted(present),
            "missing_tables": missing,
        }


def readiness_check() -> dict[str, Any]:
    schema = schema_check()
    with get_connection() as conn:
        article_count = int(conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0])
        queued_jobs = int(
            conn.execute(
                "SELECT COUNT(*) FROM background_jobs WHERE status IN ('queued', 'running')"
            ).fetchone()[0]
        )
        stale_running_jobs = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM background_jobs
                WHERE status = 'running'
                  AND started_at < now() - (%s::text || ' minutes')::interval
                """,
                (config.BACKGROUND_JOB_STALE_MINUTES,),
            ).fetchone()[0]
        )

    return {
        "ok": bool(schema["ok"] and stale_running_jobs == 0),
        "database": {"ok": True},
        "schema": schema,
        "jobs": {
            "queued_or_running": queued_jobs,
            "stale_running": stale_running_jobs,
            "stale_minutes": config.BACKGROUND_JOB_STALE_MINUTES,
        },
        "articles": article_count,
    }
