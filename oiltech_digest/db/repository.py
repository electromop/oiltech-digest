"""CRUD and query helpers for sources, articles, auth and AI processing."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Json

from oiltech_digest import auth, config
from oiltech_digest.db.connection import get_connection

# ---------------------------------------------------------------------------
#  sources
# ---------------------------------------------------------------------------

def upsert_source(rec: dict) -> str:
    """Вставить/обновить источник по name (естественный ключ).

    Ключ — (name, source_type): один бренд может иметь сайт и Telegram-канал с
    одинаковым именем, но разным типом (это разные источники). При конфликте
    обновляются только описательные поля (url, category, priority). rss_url /
    parse_strategy / enabled НЕ трогаем — ими управляет discover-rss.
    Возвращает 'inserted' либо 'updated'.
    """
    with get_connection() as conn:
        rec = {**rec, "update_frequency": rec.get("update_frequency")}
        cur = conn.execute(
            """
            INSERT INTO sources (name, source_type, url, rss_url, enabled,
                                 parse_strategy, category, update_frequency, priority)
            VALUES (%(name)s, %(source_type)s, %(url)s, %(rss_url)s,
                    COALESCE(%(enabled)s, TRUE), %(parse_strategy)s,
                    %(category)s, %(update_frequency)s, COALESCE(%(priority)s, 1.0))
            ON CONFLICT (name, source_type) DO UPDATE SET
                url         = EXCLUDED.url,
                category    = EXCLUDED.category,
                update_frequency = EXCLUDED.update_frequency,
                priority    = EXCLUDED.priority,
                updated_at  = now()
            RETURNING (xmax = 0) AS inserted
            """,
            rec,
        )
        inserted = cur.fetchone()[0]
        conn.commit()
    return "inserted" if inserted else "updated"


def get_enabled_sources(strategy: str | None = None) -> list[dict]:
    """Включённые источники, опционально с фильтром по parse_strategy."""
    query = "SELECT * FROM sources WHERE enabled = TRUE"
    params: list = []
    if strategy is not None:
        query += " AND parse_strategy = %s"
        params.append(strategy)
    query += " ORDER BY id"
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(query, params)
        return cur.fetchall()


def get_sources_for_discovery(only_missing: bool = True,
                              source_id: int | None = None,
                              limit: int | None = None) -> list[dict]:
    """Источники-кандидаты на автообнаружение RSS (не Telegram и не Playwright).

    `playwright` — осознанно выставленная вручную стратегия (JS/WAF-сайты); discover-rss
    НЕ должен её сбрасывать в request, иначе оверрайды откатываются на каждом цикле.
    """
    query = "SELECT * FROM sources WHERE enabled = TRUE AND parse_strategy NOT IN ('telegram', 'playwright')"
    params: list = []
    if only_missing:
        query += " AND (rss_url IS NULL OR rss_url = '')"
    if source_id is not None:
        query += " AND id = %s"
        params.append(source_id)
    query += " ORDER BY id"
    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(query, params)
        return cur.fetchall()


def update_source_rss(source_id: int, rss_url: str | None, parse_strategy: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE sources SET rss_url = %s, parse_strategy = %s, updated_at = now() WHERE id = %s",
            (rss_url, parse_strategy, source_id),
        )
        conn.commit()


def set_source_enabled(source_id: int, enabled: bool) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE sources SET enabled = %s, updated_at = now() WHERE id = %s",
            (enabled, source_id),
        )
        conn.commit()


def add_rss_source(name: str, rss_url: str, source_type: str = "RSS",
                   url: str | None = None, priority: float = 1.0,
                   category: str | None = None,
                   update_frequency: str | None = None,
                   parse_strategy: str = "rss") -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO sources (name, source_type, url, rss_url, enabled,
                                 parse_strategy, category, update_frequency, priority)
            VALUES (%s, %s, %s, %s, TRUE, %s, %s, %s, %s)
            ON CONFLICT (name, source_type) DO UPDATE SET
                url = EXCLUDED.url,
                rss_url = EXCLUDED.rss_url,
                enabled = TRUE,
                parse_strategy = EXCLUDED.parse_strategy,
                category = EXCLUDED.category,
                update_frequency = EXCLUDED.update_frequency,
                priority = EXCLUDED.priority,
                updated_at = now()
            RETURNING id
            """,
            (name, source_type, url, rss_url, parse_strategy, category, update_frequency, priority),
        )
        source_id = cur.fetchone()[0]
        conn.commit()
        return source_id


def list_sources(search: str | None = None, limit: int = 50) -> list[dict]:
    query = "SELECT * FROM sources"
    params: list = []
    if search:
        query += " WHERE name ILIKE %s OR url ILIKE %s OR rss_url ILIKE %s OR listing_url ILIKE %s"
        like = f"%{search}%"
        params.extend([like, like, like, like])
    query += " ORDER BY enabled DESC, parse_strategy NULLS LAST, id LIMIT %s"
    params.append(limit)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(query, params)
        return cur.fetchall()


def get_source(source_id: int) -> dict | None:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT * FROM sources WHERE id = %s", (source_id,))
        return cur.fetchone()


def touch_last_parsed(source_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE sources SET last_parsed_at = now() WHERE id = %s", (source_id,)
        )
        conn.commit()


def set_sources_network_region(ids: list[int], region: str) -> int:
    """Проставить network_region (auto|ru|external) источникам по списку id.

    При уходе в/из external сбрасываем request-состояние (last_seen/hash), чтобы
    смена пути парсинга не коротила на старом хэше. Возвращает число обновлённых строк."""
    if not ids:
        return 0
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE sources SET network_region = %s, last_listing_hash = NULL, "
            "last_seen_article_url = NULL, last_seen_published_at = NULL, updated_at = now() "
            "WHERE id = ANY(%s)",
            (region, list(ids)),
        )
        conn.commit()
        return cur.rowcount


def article_exists(url: str) -> bool:
    with get_connection() as conn:
        row = conn.execute("SELECT 1 FROM articles WHERE url = %s", (url,)).fetchone()
        return row is not None


def update_source_request_state(
    source_id: int,
    *,
    last_seen_article_url: str | None = None,
    last_seen_published_at=None,
    last_listing_hash: str | None = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE sources
            SET last_seen_article_url = COALESCE(%s, last_seen_article_url),
                last_seen_published_at = COALESCE(%s, last_seen_published_at),
                last_listing_hash = COALESCE(%s, last_listing_hash),
                updated_at = now()
            WHERE id = %s
            """,
            (last_seen_article_url, last_seen_published_at, last_listing_hash, source_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
#  auth
# ---------------------------------------------------------------------------

def create_user(email: str, password: str, role: str = "user") -> dict:
    email = auth.normalize_email(email)
    role = role if role in ("admin", "user") else "user"
    salt_hex, password_hash = auth.hash_password(password)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        if cur.fetchone() is not None:
            raise ValueError("Пользователь с таким email уже существует")
        cur.execute(
            """
            INSERT INTO users (email, password_salt, password_hash, role)
            VALUES (%s, %s, %s, %s)
            RETURNING id, email, role, created_at
            """,
            (email, salt_hex, password_hash, role),
        )
        user = cur.fetchone()
        conn.commit()
        return user


def authenticate_user(email: str, password: str) -> dict | None:
    email = auth.normalize_email(email)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT id, email, role, password_salt, password_hash, created_at FROM users WHERE email = %s",
            (email,),
        )
        user = cur.fetchone()
        if user is None:
            return None
        if not auth.verify_password(password, user["password_salt"], user["password_hash"]):
            return None
        return {"id": user["id"], "email": user["email"], "role": user["role"], "created_at": user["created_at"]}


def create_user_session(user_id: int) -> str:
    token = auth.create_session_token()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_sessions (user_id, session_token, expires_at)
            VALUES (%s, %s, now() + %s::interval)
            """,
            (user_id, token, f"{config.AUTH_SESSION_DAYS} days"),
        )
        conn.commit()
    return token


def get_user_by_session(session_token: str) -> dict | None:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT u.id, u.email, u.role, u.created_at
            FROM user_sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.session_token = %s
              AND s.expires_at > now()
            """,
            (session_token,),
        )
        user = cur.fetchone()
        if user is not None:
            conn.execute(
                "UPDATE user_sessions SET last_seen_at = now() WHERE session_token = %s",
                (session_token,),
            )
            conn.commit()
        return user


def delete_user_session(session_token: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM user_sessions WHERE session_token = %s", (session_token,))
        conn.commit()


def delete_expired_user_sessions() -> int:
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM user_sessions WHERE expires_at <= now()")
        conn.commit()
        return cur.rowcount or 0


def count_expired_user_sessions() -> int:
    with get_connection() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM user_sessions WHERE expires_at <= now()").fetchone()[0])


def count_users() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def list_users() -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT id, email, role, created_at FROM users ORDER BY id")
        return cur.fetchall()


def get_user_by_id(user_id: int) -> dict | None:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT id, email, role, created_at FROM users WHERE id = %s", (user_id,))
        return cur.fetchone()


def set_user_role(user_id: int, role: str) -> None:
    role = role if role in ("admin", "user") else "user"
    with get_connection() as conn:
        conn.execute("UPDATE users SET role = %s, updated_at = now() WHERE id = %s", (role, user_id))
        conn.commit()


def set_user_password(user_id: int, password: str) -> None:
    salt_hex, password_hash = auth.hash_password(password)
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET password_salt = %s, password_hash = %s, updated_at = now() WHERE id = %s",
            (salt_hex, password_hash, user_id),
        )
        conn.commit()


def delete_user(user_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM user_sessions WHERE user_id = %s", (user_id,))
        conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()


def count_admins() -> int:
    with get_connection() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0])


def set_user_article_status(user_id: int, article_id: int, status: str | None = None,
                            analyst_comment: str | None = None) -> None:
    """Пер-юзерный рабочий статус статьи. status=None — не трогаем (только коммент)."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_article_states (user_id, article_id, status, analyst_comment)
            VALUES (%s, %s, COALESCE(%s, 'new'), %s)
            ON CONFLICT (user_id, article_id) DO UPDATE SET
              status = COALESCE(%s, user_article_states.status),
              analyst_comment = COALESCE(%s, user_article_states.analyst_comment),
              updated_at = now()
            """,
            (user_id, article_id, status, analyst_comment, status, analyst_comment),
        )
        conn.commit()


def migrate_global_status_to_user(user_id: int) -> int:
    """Разовый перенос текущих ГЛОБАЛЬНЫХ статусов (article_cards.status != 'new')
    в личное состояние указанного пользователя — чтобы его дайджест/работа сохранились
    при переходе на пер-юзерную модель. Идемпотентно (не перетирает уже заданные)."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO user_article_states (user_id, article_id, status, analyst_comment)
            SELECT %s, c.article_id, c.status, c.analyst_comment
            FROM article_cards c
            WHERE COALESCE(c.status, 'new') <> 'new'
            ON CONFLICT (user_id, article_id) DO NOTHING
            """,
            (user_id,),
        )
        conn.commit()
        return cur.rowcount or 0


def ensure_admin_bootstrap() -> int | None:
    """Если админов нет, а пользователи есть — назначить админом самого первого
    (по id). Возвращает id назначенного админа или None. Идемпотентно."""
    with get_connection() as conn:
        has_admin = conn.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone()
        if has_admin:
            return None
        row = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        if not row:
            return None
        conn.execute("UPDATE users SET role = 'admin', updated_at = now() WHERE id = %s", (row[0],))
        conn.commit()
        return int(row[0])


def create_export_job(export_type: str, export_format: str) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO export_jobs (export_type, format, status, started_at)
            VALUES (%s, %s, 'running', now())
            RETURNING id
            """,
            (export_type, export_format),
        )
        job_id = cur.fetchone()[0]
        conn.commit()
        return job_id


def finish_export_job(job_id: int, status: str, file_path: str | None = None,
                      error_message: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE export_jobs
            SET status = %s,
                file_path = COALESCE(%s, file_path),
                error_message = %s,
                finished_at = now()
            WHERE id = %s
            """,
            (status, file_path, error_message, job_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
#  background jobs
# ---------------------------------------------------------------------------

def create_background_job(
    kind: str,
    payload: dict | None = None,
    *,
    queue_name: str = "default",
    execution_region: str = "ru",
    capability: str | None = None,
    max_attempts: int = 3,
) -> dict:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            INSERT INTO background_jobs (
                kind, queue_name, execution_region, capability,
                status, progress, max_attempts, payload_json
            )
            VALUES (%s, %s, %s, %s, 'queued', 0, %s, %s)
            RETURNING *
            """,
            (kind, queue_name, execution_region, capability, max_attempts, Json(_jsonable(payload or {}))),
        )
        job = cur.fetchone()
        conn.commit()
        return job


def get_background_job(job_id: int) -> dict | None:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT * FROM background_jobs WHERE id = %s", (job_id,))
        return cur.fetchone()


def claim_next_background_job(queue_names: list[str] | None = None) -> dict | None:
    """Atomically claim the oldest queued job for an external worker."""
    queue_names = queue_names or ["default"]
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            WITH next_job AS (
                SELECT id
                FROM background_jobs
                WHERE status = 'queued'
                  AND queue_name = ANY(%s)
                  AND run_after <= now()
                ORDER BY created_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE background_jobs j
            SET status = 'running',
                progress = CASE WHEN j.progress < 10 THEN 10 ELSE j.progress END,
                attempts = j.attempts + 1,
                started_at = COALESCE(j.started_at, now()),
                error_message = NULL
            FROM next_job
            WHERE j.id = next_job.id
            RETURNING j.*
            """,
            (queue_names,),
        )
        job = cur.fetchone()
        conn.commit()
        return job


def requeue_expired_external_leases() -> int:
    """Return external jobs with expired leases to the queue after worker loss."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE background_jobs
            SET status = 'queued',
                progress = 0,
                started_at = NULL,
                claimed_by = NULL,
                lease_token_hash = NULL,
                lease_expires_at = NULL,
                error_message = 'Requeued after external worker lease expired'
            WHERE status = 'running'
              AND execution_region = 'external'
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at < now()
            """
        )
        conn.commit()
        return cur.rowcount or 0


def claim_external_background_job(
    *,
    queue_names: list[str],
    capabilities: list[str],
    worker_id: str,
    lease_token_hash: str,
    lease_seconds: int,
) -> dict | None:
    """Atomically lease the oldest queued external job for a remote worker."""
    queue_names = queue_names or ["external-ai", "external-fetch", "external-playwright"]
    capabilities = capabilities or []
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            WITH next_job AS (
                SELECT id
                FROM background_jobs
                WHERE status = 'queued'
                  AND execution_region = 'external'
                  AND queue_name = ANY(%s)
                  AND (%s::text[] = '{}'::text[] OR capability IS NULL OR capability = ANY(%s))
                  AND run_after <= now()
                ORDER BY created_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE background_jobs j
            SET status = 'running',
                progress = CASE WHEN j.progress < 10 THEN 10 ELSE j.progress END,
                attempts = j.attempts + 1,
                started_at = COALESCE(j.started_at, now()),
                claimed_by = %s,
                lease_token_hash = %s,
                lease_expires_at = now() + (%s::text || ' seconds')::interval,
                last_heartbeat_at = now(),
                error_message = NULL
            FROM next_job
            WHERE j.id = next_job.id
            RETURNING j.*
            """,
            (queue_names, capabilities, capabilities, worker_id, lease_token_hash, lease_seconds),
        )
        job = cur.fetchone()
        conn.commit()
        return job


def list_background_jobs(
    *,
    status: str | None = None,
    kind: str | None = None,
    queue_name: str | None = None,
    limit: int = 50,
) -> list[dict]:
    clauses = []
    params: list = []
    if status:
        clauses.append("status = %s")
        params.append(status)
    if kind:
        clauses.append("kind = %s")
        params.append(kind)
    if queue_name:
        clauses.append("queue_name = %s")
        params.append(queue_name)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(limit)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT *
            FROM background_jobs
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            params,
        )
        return cur.fetchall()


def external_queue_status() -> dict:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE status = 'queued') AS queued,
              COUNT(*) FILTER (WHERE status = 'running') AS running,
              COUNT(*) FILTER (WHERE status = 'failed') AS failed,
              COUNT(*) FILTER (WHERE status = 'ok') AS ok,
              MIN(created_at) FILTER (WHERE status = 'queued') AS oldest_queued_at,
              MAX(last_heartbeat_at) FILTER (WHERE status = 'running') AS last_heartbeat_at,
              COUNT(*) FILTER (
                WHERE status = 'running'
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at < now()
              ) AS expired_leases
            FROM background_jobs
            WHERE execution_region = 'external'
            """
        )
        totals = cur.fetchone() or {}
        cur.execute(
            """
            SELECT queue_name,
                   COUNT(*) FILTER (WHERE status = 'queued') AS queued,
                   COUNT(*) FILTER (WHERE status = 'running') AS running,
                   COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                   COUNT(*) FILTER (WHERE status = 'ok') AS ok,
                   MIN(created_at) FILTER (WHERE status = 'queued') AS oldest_queued_at,
                   MAX(last_heartbeat_at) FILTER (WHERE status = 'running') AS last_heartbeat_at
            FROM background_jobs
            WHERE execution_region = 'external'
            GROUP BY queue_name
            ORDER BY queue_name
            """
        )
        queues = cur.fetchall()
    return {
        "totals": dict(totals),
        "queues": [dict(row) for row in queues],
    }


def mark_background_job_running(job_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE background_jobs
            SET status = 'running',
                progress = CASE WHEN progress < 10 THEN 10 ELSE progress END,
                attempts = attempts + 1,
                started_at = COALESCE(started_at, now()),
                error_message = NULL
            WHERE id = %s
            """,
            (job_id,),
        )
        conn.commit()


def update_background_job_progress(job_id: int, progress: float) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE background_jobs SET progress = %s WHERE id = %s",
            (progress, job_id),
        )
        conn.commit()


def update_external_background_job_progress(
    job_id: int,
    *,
    lease_token_hash: str,
    progress: float,
    lease_seconds: int | None = None,
) -> bool:
    with get_connection() as conn:
        if lease_seconds is None:
            cur = conn.execute(
                """
                UPDATE background_jobs
                SET progress = %s,
                    last_heartbeat_at = now()
                WHERE id = %s
                  AND status = 'running'
                  AND execution_region = 'external'
                  AND lease_token_hash = %s
                  AND lease_expires_at > now()
                """,
                (progress, job_id, lease_token_hash),
            )
        else:
            cur = conn.execute(
                """
                UPDATE background_jobs
                SET progress = %s,
                    last_heartbeat_at = now(),
                    lease_expires_at = now() + (%s::text || ' seconds')::interval
                WHERE id = %s
                  AND status = 'running'
                  AND execution_region = 'external'
                  AND lease_token_hash = %s
                  AND lease_expires_at > now()
                """,
                (progress, lease_seconds, job_id, lease_token_hash),
            )
        conn.commit()
        return bool(cur.rowcount)


def heartbeat_external_background_job(job_id: int, *, lease_token_hash: str, lease_seconds: int) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE background_jobs
            SET last_heartbeat_at = now(),
                lease_expires_at = now() + (%s::text || ' seconds')::interval
            WHERE id = %s
              AND status = 'running'
              AND execution_region = 'external'
              AND lease_token_hash = %s
              AND lease_expires_at > now()
            """,
            (lease_seconds, job_id, lease_token_hash),
        )
        conn.commit()
        return bool(cur.rowcount)


def external_background_job_lease_is_active(job_id: int, *, lease_token_hash: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM background_jobs
            WHERE id = %s
              AND status = 'running'
              AND execution_region = 'external'
              AND lease_token_hash = %s
              AND lease_expires_at > now()
            """,
            (job_id, lease_token_hash),
        ).fetchone()
        return row is not None


def finish_background_job(job_id: int, result: dict | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE background_jobs
            SET status = 'ok',
                progress = 100,
                result_json = %s,
                error_message = NULL,
                finished_at = now()
            WHERE id = %s
            """,
            (Json(_jsonable(result or {})), job_id),
        )
        conn.commit()


def begin_external_background_job_finalize(job_id: int, *, lease_token_hash: str) -> bool:
    """Атомарно перевести внешнюю задачу running→finalizing под защитой лиза (баг T2).

    Закрывает окно двойного AI-расхода: применять результат и биллить ai_processing_runs
    можно ТОЛЬКО после успешного перехода в 'finalizing'. Пока задача 'finalizing',
    requeue_expired_external_leases (он трогает лишь status='running') её НЕ переотдаст,
    поэтому другой воркер не прогонит AI повторно. Возвращает True, если лиз ещё валиден
    и задача застолблена именно за этим воркером (его lease_token_hash)."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE background_jobs
            SET status = 'finalizing',
                last_heartbeat_at = now()
            WHERE id = %s
              AND status = 'running'
              AND execution_region = 'external'
              AND lease_token_hash = %s
              AND lease_expires_at > now()
            """,
            (job_id, lease_token_hash),
        )
        conn.commit()
        return bool(cur.rowcount)


def release_external_background_job_finalize(job_id: int, *, lease_token_hash: str) -> bool:
    """Откатить finalizing→running, если применение результата упало (баг T2, восстановление).

    Без отката задача залипла бы в 'finalizing' навсегда. После отката её подберёт обычный
    путь восстановления (requeue_expired по истечении лиза / requeue_stale)."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE background_jobs
            SET status = 'running'
            WHERE id = %s
              AND status = 'finalizing'
              AND lease_token_hash = %s
            """,
            (job_id, lease_token_hash),
        )
        conn.commit()
        return bool(cur.rowcount)


def finish_external_background_job(job_id: int, *, lease_token_hash: str, result: dict | None = None) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE background_jobs
            SET status = 'ok',
                progress = 100,
                result_json = %s,
                error_message = NULL,
                claimed_by = NULL,
                lease_token_hash = NULL,
                lease_expires_at = NULL,
                finished_at = now()
            WHERE id = %s
              AND execution_region = 'external'
              AND lease_token_hash = %s
              AND (status = 'finalizing'
                   OR (status = 'running' AND lease_expires_at > now()))
            """,
            (Json(_jsonable(result or {})), job_id, lease_token_hash),
        )
        conn.commit()
        return bool(cur.rowcount)


def fail_background_job(job_id: int, error_message: str, *, retry_delay_seconds: int | None = None) -> None:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT attempts, max_attempts FROM background_jobs WHERE id = %s",
            (job_id,),
        )
        row = cur.fetchone()
        should_retry = (
            retry_delay_seconds is not None
            and row is not None
            and int(row["attempts"] or 0) < int(row["max_attempts"] or 0)
        )
        if should_retry:
            conn.execute(
                """
                UPDATE background_jobs
                SET status = 'queued',
                    progress = 0,
                    run_after = now() + (%s::text || ' seconds')::interval,
                    error_message = %s,
                    started_at = NULL
                WHERE id = %s
                """,
                (retry_delay_seconds, error_message, job_id),
            )
        else:
            conn.execute(
            """
            UPDATE background_jobs
            SET status = 'failed',
                error_message = %s,
                finished_at = now()
            WHERE id = %s
            """,
                (error_message, job_id),
            )
        conn.commit()


def fail_external_background_job(
    job_id: int,
    *,
    lease_token_hash: str,
    error_message: str,
    retryable: bool,
    retry_delay_seconds: int | None = None,
) -> bool:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT attempts, max_attempts
            FROM background_jobs
            WHERE id = %s
              AND status = 'running'
              AND execution_region = 'external'
              AND lease_token_hash = %s
              AND lease_expires_at > now()
            """,
            (job_id, lease_token_hash),
        )
        row = cur.fetchone()
        if row is None:
            return False
        should_retry = retryable and int(row["attempts"] or 0) < int(row["max_attempts"] or 0)
        if should_retry:
            delay = retry_delay_seconds if retry_delay_seconds is not None else 60
            conn.execute(
                """
                UPDATE background_jobs
                SET status = 'queued',
                    progress = 0,
                    run_after = now() + (%s::text || ' seconds')::interval,
                    error_message = %s,
                    started_at = NULL,
                    claimed_by = NULL,
                    lease_token_hash = NULL,
                    lease_expires_at = NULL
                WHERE id = %s
                """,
                (delay, error_message, job_id),
            )
        else:
            conn.execute(
                """
                UPDATE background_jobs
                SET status = 'failed',
                    error_message = %s,
                    claimed_by = NULL,
                    lease_token_hash = NULL,
                    lease_expires_at = NULL,
                    finished_at = now()
                WHERE id = %s
                """,
                (error_message, job_id),
            )
        conn.commit()
        return True


def requeue_stale_background_jobs(stale_minutes: int) -> int:
    """Move old running/finalizing jobs back to queued after a worker/core crash/restart.

    'finalizing' (баг T2) — переходный статус на время применения результата внешней задачи.
    В норме он живёт секунды; если задача в нём дольше stale-таймаута (по last_heartbeat_at,
    выставленному при входе в finalize), значит core упал между застолблением и финишем —
    возвращаем в очередь. lease-поля чистим, чтобы задачу можно было переотдать заново."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE background_jobs
            SET status = 'queued',
                progress = 0,
                started_at = NULL,
                claimed_by = NULL,
                lease_token_hash = NULL,
                lease_expires_at = NULL,
                error_message = 'Requeued after stale running/finalizing timeout'
            WHERE (status = 'running'
                   AND started_at < now() - (%s::text || ' minutes')::interval)
               OR (status = 'finalizing'
                   AND last_heartbeat_at < now() - (%s::text || ' minutes')::interval)
            """,
            (stale_minutes, stale_minutes),
        )
        conn.commit()
        return cur.rowcount or 0


def count_stale_running_background_jobs(stale_minutes: int) -> int:
    with get_connection() as conn:
        return int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM background_jobs
                WHERE status = 'running'
                  AND started_at < now() - (%s::text || ' minutes')::interval
                """,
                (stale_minutes,),
            ).fetchone()[0]
        )


def cleanup_finished_background_jobs(retention_days: int) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            DELETE FROM background_jobs
            WHERE status IN ('ok', 'failed')
              AND COALESCE(finished_at, started_at, created_at)
                  < now() - (%s::text || ' days')::interval
            """,
            (retention_days,),
        )
        conn.commit()
        return cur.rowcount or 0


def count_finished_background_jobs_eligible_for_cleanup(retention_days: int) -> int:
    with get_connection() as conn:
        return int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM background_jobs
                WHERE status IN ('ok', 'failed')
                  AND COALESCE(finished_at, started_at, created_at)
                      < now() - (%s::text || ' days')::interval
                """,
                (retention_days,),
            ).fetchone()[0]
        )


def cleanup_finished_export_jobs(retention_days: int) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            DELETE FROM export_jobs
            WHERE status IN ('ok', 'failed')
              AND COALESCE(finished_at, started_at)
                  < now() - (%s::text || ' days')::interval
            """,
            (retention_days,),
        )
        conn.commit()
        return cur.rowcount or 0


def count_finished_export_jobs_eligible_for_cleanup(retention_days: int) -> int:
    with get_connection() as conn:
        return int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM export_jobs
                WHERE status IN ('ok', 'failed')
                  AND COALESCE(finished_at, started_at)
                      < now() - (%s::text || ' days')::interval
                """,
                (retention_days,),
            ).fetchone()[0]
        )


def _jsonable(value):
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


# ---------------------------------------------------------------------------
#  articles
# ---------------------------------------------------------------------------

def insert_article(rec: dict) -> bool:
    """Вставить статью. Дубликаты по url игнорируются (ON CONFLICT DO NOTHING).
    Возвращает True, если строка реально вставлена."""
    rec = {**rec, "image_url": rec.get("image_url")}
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO articles (source_id, title, url, published_at,
                                  raw_text, text_truncated, language, content_hash, image_url)
            VALUES (%(source_id)s, %(title)s, %(url)s, %(published_at)s,
                    %(raw_text)s, COALESCE(%(text_truncated)s, FALSE), %(language)s,
                    %(content_hash)s, %(image_url)s)
            ON CONFLICT (url) DO NOTHING
            RETURNING id
            """,
            rec,
        )
        row = cur.fetchone()
        conn.commit()
    return row is not None


def get_articles_missing_image(limit: int = 200) -> list[dict]:
    """Статьи без картинки (image_url пуст) — для бэкфилла og:image в дайджест.
    fetch-full-text трогает только статьи без полного текста, поэтому уже
    обработанные статьи остаются без image_url, и их добирает эта выборка."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.id, a.url
            FROM articles a
            WHERE a.url IS NOT NULL AND COALESCE(a.image_url, '') = ''
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def set_article_image(article_id: int, image_url: str) -> bool:
    """Проставить image_url, только если его ещё нет. True, если обновлено."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE articles SET image_url = %s, updated_at = now() "
            "WHERE id = %s AND COALESCE(image_url, '') = ''",
            (image_url, article_id),
        )
        conn.commit()
        return cur.rowcount > 0


def get_articles_needing_full_text(limit: int = 50, retry_too_short: bool = False) -> list[dict]:
    """Articles whose RSS body is likely only a teaser and needs URL extraction.

    retry_too_short=True also includes articles previously marked too_short so they
    can be re-attempted (e.g. after trafilatura is added to the extraction chain).
    """
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        status_filter = (
            "AND (a.full_text_status IS NULL OR a.full_text_status = 'too_short')"
            if retry_too_short
            else "AND a.full_text_status IS NULL"
        )
        cur.execute(
            f"""
            SELECT a.*, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            WHERE a.url IS NOT NULL
              {status_filter}
              AND (
                COALESCE(a.text_truncated, FALSE) = TRUE
                OR length(COALESCE(a.raw_text, '')) < 800
              )
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def update_article_full_text(article_id: int, raw_text: str | None, text_truncated: bool,
                             status: str, method: str, error: str | None = None,
                             image_url: str | None = None) -> None:
    """Store full-text extraction result without losing the RSS teaser on failure."""
    with get_connection() as conn:
        if image_url:
            # Заполняем картинку, только если её ещё нет — RSS-media в приоритете.
            conn.execute(
                "UPDATE articles SET image_url = %s "
                "WHERE id = %s AND COALESCE(image_url, '') = ''",
                (image_url, article_id),
            )
        if raw_text is not None:
            conn.execute(
                """
                UPDATE articles
                SET raw_text = %s,
                    text_truncated = %s,
                    full_text_fetched_at = now(),
                    full_text_status = %s,
                    full_text_error = %s,
                    extraction_method = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (raw_text, text_truncated, status, error, method, article_id),
            )
        else:
            conn.execute(
                """
                UPDATE articles
                SET text_truncated = %s,
                    full_text_fetched_at = now(),
                    full_text_status = %s,
                    full_text_error = %s,
                    extraction_method = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (text_truncated, status, error, method, article_id),
            )
        conn.commit()


# ---------------------------------------------------------------------------
#  Диагностика (для stats)
# ---------------------------------------------------------------------------

def count_sources() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]


def count_articles() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]


def dashboard_stats(user_id: int | None = None) -> dict:
    """Aggregate counters for the admin dashboard cards.

    Computed over the FULL database (not the loaded page), so the numbers stay
    correct regardless of how many articles the UI fetches. ``avg_score`` is the
    mean over scored articles only — unscored articles do not drag it to zero.
    ``selected_for_digest`` — ПЕР-ЮЗЕРНО (выбор в дайджест личный, #12).
    """
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM articles) AS total_articles,
              (SELECT COUNT(*) FROM article_cards
                 WHERE COALESCE(summary, '') <> '') AS with_summary,
              (SELECT COUNT(*)
                 FROM article_cards c
                WHERE COALESCE(c.summary, '') <> ''
                  AND c.relevant IS NOT NULL
                  AND (
                    c.relevant IS FALSE
                    OR EXISTS (
                      SELECT 1 FROM article_tags at
                      WHERE at.article_id = c.article_id
                    )
                    OR EXISTS (
                      SELECT 1 FROM article_scores sc
                      WHERE sc.article_id = c.article_id
                    )
                  )) AS processed_articles,
              (SELECT COUNT(*) FROM user_article_states
                 WHERE user_id = %(user_id)s AND status = 'digest') AS selected_for_digest,
              (SELECT ROUND(AVG(total_score)) FROM article_scores) AS avg_score,
              (SELECT COUNT(*) FROM sources) AS sources
            """,
            {"user_id": user_id},
        )
        row = cur.fetchone()
    return {
        "total_articles": int(row["total_articles"] or 0),
        "with_summary": int(row["with_summary"] or 0),
        "processed_articles": int(row["processed_articles"] or 0),
        "selected_for_digest": int(row["selected_for_digest"] or 0),
        "avg_score": int(row["avg_score"] or 0),
        "sources": int(row["sources"] or 0),
    }


def clear_future_published_dates(tolerance_days: int = 2) -> int:
    """Обнулить недостоверные даты публикации из будущего (анонсы-события календаря).

    Статья сохраняется — убирается только ошибочная дата, после чего она
    сортируется/показывается по реальному `collected_at` и перестаёт помечаться
    как «дата в будущем». Идемпотентно. Возвращает число затронутых строк.
    """
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE articles SET published_at = NULL, updated_at = now() "
            "WHERE published_at > now() + make_interval(days => %s)",
            (tolerance_days,),
        )
        conn.commit()
        return cur.rowcount


def sources_by_strategy() -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT parse_strategy, COUNT(*) AS n FROM sources "
            "GROUP BY parse_strategy ORDER BY n DESC"
        )
        return cur.fetchall()


def source_health_report(stale_days: int = 3, limit: int = 300, verdict: str | None = None) -> list[dict]:
    """Per-source article coverage verdict for operations diagnostics."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            WITH src AS (
              SELECT s.id, s.name, s.enabled, s.parse_strategy, s.source_type,
                     s.url, s.rss_url, s.listing_url,
                     COUNT(a.id) AS articles,
                     MAX(a.collected_at) AS last_article_at
              FROM sources s
              LEFT JOIN articles a ON a.source_id = s.id
              GROUP BY s.id
            ),
            verdicts AS (
              SELECT *,
                     CASE
                       WHEN NOT enabled THEN 'disabled'
                       WHEN articles = 0 THEN 'no_articles'
                       WHEN last_article_at < now() - (%s::text || ' days')::interval THEN 'stale'
                       ELSE 'ok'
                     END AS verdict
              FROM src
            )
            SELECT *
            FROM verdicts
            WHERE (%s::text IS NULL OR verdict = %s)
            ORDER BY
              CASE
                WHEN NOT enabled THEN 4
                WHEN articles = 0 THEN 1
                WHEN last_article_at < now() - (%s::text || ' days')::interval THEN 2
                ELSE 3
              END,
              articles ASC,
              last_article_at NULLS FIRST,
              name
            LIMIT %s
            """,
            (stale_days, verdict, verdict, stale_days, limit),
        )
        return cur.fetchall()


def top_sources(limit: int = 10) -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT s.name, COUNT(a.id) AS n FROM sources s "
            "JOIN articles a ON a.source_id = s.id "
            "GROUP BY s.id, s.name ORDER BY n DESC LIMIT %s",
            (limit,),
        )
        return cur.fetchall()


def cross_dup_candidates() -> int:
    """Сколько content_hash встречается более чем у одного URL (кандидаты-перепечатки)."""
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT content_hash FROM articles "
            "  WHERE content_hash IS NOT NULL "
            "  GROUP BY content_hash HAVING COUNT(DISTINCT url) > 1"
            ") t"
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
#  AI processing: articles, cards, tags, scoring, metrics
# ---------------------------------------------------------------------------

def get_article(article_id: int) -> dict | None:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.*, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            WHERE a.id = %s
            """,
            (article_id,),
        )
        return cur.fetchone()


def get_articles_by_ids(article_ids: list[int], include_summary: bool = False) -> list[dict]:
    if not article_ids:
        return []
    summary_select = ", c.summary" if include_summary else ""
    summary_join = "LEFT JOIN article_cards c ON c.article_id = a.id" if include_summary else ""
    placeholders = ", ".join(["%s"] * len(article_ids))
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT a.*, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
                   {summary_select}
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            {summary_join}
            WHERE a.id IN ({placeholders})
            ORDER BY array_position(%s::bigint[], a.id)
            """,
            [*article_ids, article_ids],
        )
        return cur.fetchall()


def delete_article(article_id: int, *, force: bool = False) -> bool:
    """Физически удалить статью и все её зависимые строки. Возвращает True, если удалена.

    FK на articles в основном БЕЗ ON DELETE CASCADE, поэтому удаляем детей вручную
    в правильном порядке (user_article_states каскадится сам). По умолчанию НЕ удаляем
    статью, входящую в сохранённый месячный дайджест (monthly_digest_items) — чтобы не
    рвать историю; force=True снимает защиту (удалит и ссылки дайджеста)."""
    with get_connection() as conn:
        if not force:
            in_digest = conn.execute(
                "SELECT 1 FROM monthly_digest_items WHERE article_id = %s LIMIT 1",
                (article_id,),
            ).fetchone()
            if in_digest:
                return False
        conn.execute(
            """
            DELETE FROM article_score_items
            WHERE article_score_id IN (SELECT id FROM article_scores WHERE article_id = %s)
            """,
            (article_id,),
        )
        conn.execute("DELETE FROM article_scores WHERE article_id = %s", (article_id,))
        conn.execute("DELETE FROM article_tags WHERE article_id = %s", (article_id,))
        conn.execute("DELETE FROM ai_processing_runs WHERE article_id = %s", (article_id,))
        conn.execute("DELETE FROM article_cards WHERE article_id = %s", (article_id,))
        if force:
            conn.execute("DELETE FROM monthly_digest_items WHERE article_id = %s", (article_id,))
        cur = conn.execute("DELETE FROM articles WHERE id = %s RETURNING id", (article_id,))
        deleted = cur.fetchone() is not None
        conn.commit()
    return deleted


def mark_article_for_deletion(article_id: int, reason: str | None, *, force: bool = False) -> str:
    """Пометить статью на удаление (мягко, без физического DELETE). Возвращает
    'marked' либо 'skipped_in_digest' (статья в сохранённом дайджесте, force=False)."""
    with get_connection() as conn:
        if not force:
            in_digest = conn.execute(
                "SELECT 1 FROM monthly_digest_items WHERE article_id = %s LIMIT 1", (article_id,)
            ).fetchone()
            if in_digest:
                return "skipped_in_digest"
        conn.execute(
            "UPDATE articles SET pending_deletion = TRUE, deletion_reason = %s, "
            "marked_for_deletion_at = now(), updated_at = now() WHERE id = %s",
            (reason, article_id),
        )
        conn.commit()
    return "marked"


def count_pending_deletion() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT count(*) FROM articles WHERE pending_deletion").fetchone()[0]


def list_pending_deletion(limit: int = 100) -> list[dict]:
    """Помеченные на удаление — заголовок/источник/причина (для просмотра перед purge)."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT a.id, a.title, s.name AS source_name, a.deletion_reason "
            "FROM articles a JOIN sources s ON s.id = a.source_id "
            "WHERE a.pending_deletion ORDER BY a.id LIMIT %s",
            (limit,),
        )
        return cur.fetchall()


def purge_pending_deletion(*, force: bool = False) -> int:
    """Физически удалить все помеченные (pending_deletion) статьи. Возвращает число удалённых.
    Использует delete_article (тот же каскад + защита дайджеста при force=False)."""
    with get_connection() as conn:
        ids = [row[0] for row in conn.execute(
            "SELECT id FROM articles WHERE pending_deletion ORDER BY id"
        ).fetchall()]
    deleted = 0
    for article_id in ids:
        if delete_article(article_id, force=force):
            deleted += 1
    return deleted


def unmark_all_pending_deletion() -> int:
    """Снять пометку «на удаление» со всех статей (вернуть в строй). Возвращает число."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE articles SET pending_deletion = FALSE, deletion_reason = NULL, "
            "marked_for_deletion_at = NULL, updated_at = now() WHERE pending_deletion"
        )
        conn.commit()
        return cur.rowcount


def all_article_ids() -> list[int]:
    """Все id статей по возрастанию — для батч-перепрогона релевантности."""
    with get_connection() as conn:
        cur = conn.execute("SELECT id FROM articles ORDER BY id ASC")
        return [row[0] for row in cur.fetchall()]


def article_ids_needing_title_ru() -> list[int]:
    """id статей без русского заголовка — для батч-бэкфилла перевода через воркер."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            SELECT a.id FROM articles a
            LEFT JOIN article_cards c ON c.article_id = a.id
            WHERE c.title_ru IS NULL
            ORDER BY a.id ASC
            """
        )
        return [row[0] for row in cur.fetchall()]


def get_articles_for_recheck(after_id: int = 0, limit: int = 100) -> list[dict]:
    """Все статьи по возрастанию id после чекпоинта — для локального перепрогона
    релевантности на сыром тексте (независимо от наличия карточки)."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.*, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            WHERE a.id > %s
            ORDER BY a.id ASC
            LIMIT %s
            """,
            (after_id, limit),
        )
        return cur.fetchall()


def get_articles_needing_summary(limit: int = 20) -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.*, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN article_cards c ON c.article_id = a.id
            WHERE c.summary IS NULL
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def get_articles_needing_relevance(limit: int = 20) -> list[dict]:
    """Статьи с готовой сутью, но без проверки релевантности (card.relevant IS NULL)."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.*, c.summary, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            JOIN article_cards c ON c.article_id = a.id
            WHERE c.summary IS NOT NULL AND c.relevant IS NULL
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def set_article_relevance(article_id: int, relevant: bool, reason: str | None,
                          model: str | None = None) -> None:
    """Записать вердикт релевантности (UPSERT). Нерелевантные → status='rejected'.

    Раньше был чистый UPDATE — но после перестановки «релевантность ПЕРВОЙ» у
    отклонённой статьи карточки ещё нет (она создаётся на этапе суммаризации),
    и UPDATE по 0 строк терял вердикт. UPSERT создаёт карточку при необходимости."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO article_cards (article_id, relevant, relevance_reason, relevance_model, status)
            VALUES (%(id)s, %(rel)s, %(reason)s, %(model)s,
                    CASE WHEN %(rel)s THEN 'new' ELSE 'rejected' END)
            ON CONFLICT (article_id) DO UPDATE SET
                relevant = EXCLUDED.relevant,
                relevance_reason = EXCLUDED.relevance_reason,
                relevance_model = EXCLUDED.relevance_model,
                status = CASE WHEN EXCLUDED.relevant THEN article_cards.status ELSE 'rejected' END,
                updated_at = now()
            """,
            {"id": article_id, "rel": relevant, "reason": reason, "model": model},
        )
        conn.commit()


def get_articles_needing_tags(limit: int = 20) -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.*, c.summary, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            JOIN article_cards c ON c.article_id = a.id
            LEFT JOIN article_tags at ON at.article_id = a.id
            WHERE c.summary IS NOT NULL AND c.relevant IS TRUE AND at.id IS NULL
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def get_articles_needing_scores(limit: int = 20) -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.*, c.summary, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            JOIN article_cards c ON c.article_id = a.id
            LEFT JOIN article_scores sc ON sc.article_id = a.id
            WHERE c.summary IS NOT NULL AND c.relevant IS TRUE AND sc.id IS NULL
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def find_article_candidates(query: str, limit: int = 20) -> list[dict]:
    terms = [term.strip().lower() for term in query.split() if term.strip()]
    if not terms:
        return []
    conditions = []
    params: list[str] = []
    for term in terms:
        conditions.append("LOWER(a.title || ' ' || COALESCE(a.raw_text, '')) LIKE %s")
        params.append(f"%{term}%")
    where = " OR ".join(conditions)
    params.append(limit)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT a.id, a.title, a.language, a.published_at, s.name AS source_name,
                   LEFT(COALESCE(a.raw_text, ''), 240) AS snippet
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            WHERE {where}
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            params,
        )
        return cur.fetchall()


def upsert_article_card(article_id: int, summary: str, model: str | None = None,
                        title_ru: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO article_cards (article_id, summary, summary_model, title_ru, summary_generated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (article_id) DO UPDATE SET
                summary = EXCLUDED.summary,
                summary_model = EXCLUDED.summary_model,
                title_ru = COALESCE(EXCLUDED.title_ru, article_cards.title_ru),
                summary_generated_at = now(),
                updated_at = now()
            """,
            (article_id, summary, model, title_ru),
        )
        conn.commit()


def set_article_title_ru(article_id: int, title_ru: str) -> None:
    """Проставить русский заголовок отдельной стадией перевода. Создаёт карточку,
    если её ещё нет (статья могла не пройти суммаризацию), не трогая summary."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO article_cards (article_id, title_ru)
            VALUES (%s, %s)
            ON CONFLICT (article_id) DO UPDATE SET
                title_ru = EXCLUDED.title_ru,
                updated_at = now()
            """,
            (article_id, title_ru),
        )
        conn.commit()


def get_articles_needing_title_ru(limit: int = 20) -> list[dict]:
    """Статьи без русского заголовка (card.title_ru IS NULL ИЛИ карточки ещё нет).
    Бэкфилл отдельной стадии перевода по всей базе."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.*, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN article_cards c ON c.article_id = a.id
            WHERE c.title_ru IS NULL
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def upsert_tag(rec: dict) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            SELECT id FROM tags
            WHERE name = %s AND (
                (parent_id IS NULL AND %s::bigint IS NULL) OR parent_id = %s::bigint
            )
            """,
            (rec.get("name"), rec.get("parent_id"), rec.get("parent_id")),
        )
        row = cur.fetchone()
        if row:
            tag_id = row[0]
            conn.execute(
                """
                UPDATE tags
                SET name_en = %(name_en)s,
                    description = %(description)s,
                    keywords_json = %(keywords_json)s,
                    keywords_en_json = %(keywords_en_json)s,
                    sort_order = %(sort_order)s,
                    updated_at = now()
                WHERE id = %(id)s
                """,
                {
                    **rec,
                    "id": tag_id,
                    "keywords_json": Json(rec.get("keywords_json") or []),
                    "keywords_en_json": Json(rec.get("keywords_en_json") or []),
                },
            )
            conn.commit()
            return tag_id

        cur = conn.execute(
            """
            INSERT INTO tags (parent_id, name, name_en, description, keywords_json,
                              keywords_en_json, enabled, sort_order)
            VALUES (%(parent_id)s, %(name)s, %(name_en)s, %(description)s,
                    %(keywords_json)s, %(keywords_en_json)s, TRUE, %(sort_order)s)
            RETURNING id
            """,
            {
                **rec,
                "keywords_json": Json(rec.get("keywords_json") or []),
                "keywords_en_json": Json(rec.get("keywords_en_json") or []),
            },
        )
        tag_id = cur.fetchone()[0]
        conn.commit()
        return tag_id


def list_enabled_tags() -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT child.*, parent.name AS parent_name, parent.name_en AS parent_name_en
            FROM tags child
            LEFT JOIN tags parent ON parent.id = child.parent_id
            WHERE child.enabled = TRUE
            ORDER BY child.parent_id NULLS FIRST, child.sort_order, child.id
            """
        )
        return cur.fetchall()


def save_tags(items: list[dict]) -> dict:
    """Bulk-сохранение дерева тегов из UI. Родители обрабатываются раньше детей
    (parent резолвится по имени). Отсутствующие в списке — отключаются (soft delete,
    чтобы не рвать FK на article_tags)."""
    name_to_id: dict[str, int] = {}
    keep: list[int] = []
    # сначала корневые, затем дочерние — чтобы parent_id уже был известен
    ordered = [i for i in items if not i.get("parent_name")] + [i for i in items if i.get("parent_name")]
    with get_connection() as conn:
        for it in ordered:
            parent_id = name_to_id.get(it.get("parent_name")) if it.get("parent_name") else None
            payload = {
                "parent_id": parent_id,
                "name": it["name"],
                "name_en": it.get("name_en"),
                "description": it.get("description"),
                "keywords_json": Json(it.get("keywords_json") or []),
                "keywords_en_json": Json(it.get("keywords_en_json") or []),
                "negative_keywords_json": Json(it.get("negative_keywords_json") or []),
                "enabled": bool(it.get("enabled", True)),
                "sort_order": it.get("sort_order") or 0,
            }
            if it.get("id"):
                conn.execute(
                    """
                    UPDATE tags SET parent_id=%(parent_id)s, name=%(name)s, name_en=%(name_en)s,
                        description=%(description)s, keywords_json=%(keywords_json)s,
                        keywords_en_json=%(keywords_en_json)s, negative_keywords_json=%(negative_keywords_json)s,
                        enabled=%(enabled)s, sort_order=%(sort_order)s, updated_at=now()
                    WHERE id=%(id)s
                    """,
                    {**payload, "id": int(it["id"])},
                )
                tag_id = int(it["id"])
            else:
                cur = conn.execute(
                    """
                    INSERT INTO tags (parent_id, name, name_en, description, keywords_json,
                                      keywords_en_json, negative_keywords_json, enabled, sort_order)
                    VALUES (%(parent_id)s, %(name)s, %(name_en)s, %(description)s,
                            %(keywords_json)s, %(keywords_en_json)s, %(negative_keywords_json)s, %(enabled)s, %(sort_order)s)
                    RETURNING id
                    """,
                    payload,
                )
                tag_id = int(cur.fetchone()[0])
            name_to_id[it["name"]] = tag_id
            keep.append(tag_id)
        if keep:
            conn.execute("UPDATE tags SET enabled=FALSE WHERE id <> ALL(%s)", (keep,))
        conn.commit()
    return {"saved": len(items)}


def delete_tag(tag_id: int) -> None:
    """Мягкое удаление тега и его подтегов (enabled=FALSE) — FK на article_tags не рвём."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE tags SET enabled=FALSE, updated_at=now() WHERE id=%s OR parent_id=%s",
            (tag_id, tag_id),
        )
        conn.commit()


def upsert_article_tag(article_id: int, tag_id: int, confidence: float,
                       rationale: str | None, model: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO article_tags (article_id, tag_id, confidence, rationale, model)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (article_id) DO UPDATE SET
                tag_id = EXCLUDED.tag_id,
                confidence = EXCLUDED.confidence,
                rationale = EXCLUDED.rationale,
                model = EXCLUDED.model,
                created_at = now()
            """,
            (article_id, tag_id, confidence, rationale, model),
        )
        conn.commit()


def upsert_scoring_criterion(rec: dict) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO scoring_criteria (name, description, weight, keywords_json,
                                          keywords_en_json, enabled, sort_order)
            VALUES (%(name)s, %(description)s, %(weight)s, %(keywords_json)s,
                    %(keywords_en_json)s, TRUE, %(sort_order)s)
            ON CONFLICT (name) DO UPDATE SET
                description = EXCLUDED.description,
                weight = EXCLUDED.weight,
                keywords_json = EXCLUDED.keywords_json,
                keywords_en_json = EXCLUDED.keywords_en_json,
                enabled = TRUE,
                sort_order = EXCLUDED.sort_order,
                updated_at = now()
            RETURNING id
            """,
            {
                **rec,
                "keywords_json": Json(rec.get("keywords_json") or []),
                "keywords_en_json": Json(rec.get("keywords_en_json") or []),
            },
        )
        criterion_id = cur.fetchone()[0]
        conn.commit()
        return criterion_id


def list_enabled_scoring_criteria() -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT *
            FROM scoring_criteria
            WHERE enabled = TRUE
            ORDER BY sort_order, id
            """
        )
        return cur.fetchall()


def delete_scoring_criterion(criterion_id: int) -> None:
    """Мягкое удаление критерия (enabled=FALSE) — не рвём FK на article_score_items."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE scoring_criteria SET enabled = FALSE, updated_at = now() WHERE id = %s",
            (criterion_id,),
        )
        conn.commit()


def save_scoring_criteria(items: list[dict]) -> dict:
    """Bulk-сохранение профиля критериев (как кнопка «Сохранить» в мокапе).

    Валидирует сумму весов = 100. Существующие обновляются по id, новые вставляются,
    отсутствующие в списке — отключаются (soft delete, чтобы не рвать FK).
    """
    total = round(sum(float(i.get("weight") or 0) for i in items), 2)
    if total != 100:
        raise ValueError(f"Сумма весов критериев должна быть 100, сейчас {total}")

    keep_ids: list[int] = []
    with get_connection() as conn:
        for it in items:
            payload = {
                "name": it["name"],
                "description": it.get("description"),
                "weight": it["weight"],
                "keywords_json": Json(it.get("keywords_json") or []),
                "keywords_en_json": Json(it.get("keywords_en_json") or []),
                "sort_order": it.get("sort_order") or 0,
            }
            if it.get("id"):
                conn.execute(
                    """
                    UPDATE scoring_criteria SET name=%(name)s, description=%(description)s,
                        weight=%(weight)s, keywords_json=%(keywords_json)s,
                        keywords_en_json=%(keywords_en_json)s, sort_order=%(sort_order)s,
                        enabled=TRUE, updated_at=now()
                    WHERE id=%(id)s
                    """,
                    {**payload, "id": int(it["id"])},
                )
                keep_ids.append(int(it["id"]))
            else:
                cur = conn.execute(
                    """
                    INSERT INTO scoring_criteria (name, description, weight, keywords_json,
                                                  keywords_en_json, enabled, sort_order)
                    VALUES (%(name)s, %(description)s, %(weight)s, %(keywords_json)s,
                            %(keywords_en_json)s, TRUE, %(sort_order)s)
                    ON CONFLICT (name) DO UPDATE SET description=EXCLUDED.description,
                        weight=EXCLUDED.weight, keywords_json=EXCLUDED.keywords_json,
                        keywords_en_json=EXCLUDED.keywords_en_json, enabled=TRUE, updated_at=now()
                    RETURNING id
                    """,
                    payload,
                )
                keep_ids.append(int(cur.fetchone()[0]))
        if keep_ids:
            conn.execute(
                "UPDATE scoring_criteria SET enabled=FALSE, updated_at=now() WHERE id <> ALL(%s)",
                (keep_ids,),
            )
        conn.commit()
    return {"saved": len(items), "weight_sum": total}


def replace_article_score(article_id: int, total_score: float, score_label: str,
                          explanation: str, items: list[dict],
                          model: str | None = None) -> None:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO article_scores (article_id, model, total_score, score_label, explanation)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (article_id) DO UPDATE SET
                model = EXCLUDED.model,
                total_score = EXCLUDED.total_score,
                score_label = EXCLUDED.score_label,
                explanation = EXCLUDED.explanation,
                updated_at = now()
            RETURNING id
            """,
            (article_id, model, total_score, score_label, explanation),
        )
        score_id = cur.fetchone()[0]
        conn.execute("DELETE FROM article_score_items WHERE article_score_id = %s", (score_id,))
        for item in items:
            conn.execute(
                """
                INSERT INTO article_score_items
                  (article_score_id, criterion_id, keyword_score, ai_score, final_score, rationale)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    score_id,
                    item["criterion_id"],
                    item.get("keyword_score"),
                    item.get("ai_score"),
                    item.get("final_score"),
                    item.get("rationale"),
                ),
            )
        conn.commit()


def insert_ai_run(rec: dict) -> None:
    # job_id по умолчанию NULL — для локального пути и старых вызовов (не дедуплицируются).
    # ON CONFLICT DO NOTHING (баг H1/T2): повторное применение результата задачи не двоит
    # биллинг — одна строка на (job_id, article_id, stage).
    rec = {"job_id": None, **rec}
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO ai_processing_runs
              (job_id, article_id, stage, provider, model, language, input_tokens, output_tokens,
               total_tokens, cost_usd, status, error_message)
            VALUES (%(job_id)s, %(article_id)s, %(stage)s, %(provider)s, %(model)s, %(language)s,
                    %(input_tokens)s, %(output_tokens)s, %(total_tokens)s, %(cost_usd)s,
                    %(status)s, %(error_message)s)
            ON CONFLICT (job_id, article_id, stage) DO NOTHING
            """,
            rec,
        )
        conn.commit()


def ai_cost_report() -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT stage, COALESCE(language, 'unknown') AS language,
                   COUNT(*) AS runs,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(total_tokens) AS total_tokens,
                   ROUND(SUM(cost_usd)::numeric, 6) AS cost_usd,
                   ROUND(AVG(total_tokens)::numeric, 2) AS avg_tokens_per_run
            FROM ai_processing_runs
            GROUP BY stage, COALESCE(language, 'unknown')
            ORDER BY stage, language
            """
        )
        return cur.fetchall()


def ai_article_cost_report(limit: int = 20, complete_only: bool = True) -> list[dict]:
    """Cost of processing one article through AI stages.

    complete_only=True returns articles that have successful summary, tagging and
    scoring runs, which is the cleanest estimate for one full processing cycle.
    """
    stages_filter = "HAVING COUNT(DISTINCT r.stage) = 3" if complete_only else ""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT
                a.id AS article_id,
                a.title,
                a.language,
                COUNT(*) FILTER (WHERE r.status = 'ok') AS runs,
                COUNT(DISTINCT r.stage) FILTER (WHERE r.status = 'ok') AS stages,
                SUM(r.input_tokens) FILTER (WHERE r.status = 'ok') AS input_tokens,
                SUM(r.output_tokens) FILTER (WHERE r.status = 'ok') AS output_tokens,
                SUM(r.total_tokens) FILTER (WHERE r.status = 'ok') AS total_tokens,
                ROUND(SUM(r.cost_usd) FILTER (WHERE r.status = 'ok')::numeric, 6) AS cost_usd
            FROM ai_processing_runs r
            JOIN articles a ON a.id = r.article_id
            WHERE r.stage IN ('summary', 'tagging', 'scoring')
            GROUP BY a.id, a.title, a.language
            {stages_filter}
            ORDER BY cost_usd DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def digest_candidates(month: str | None = None, limit: int = 20, min_score: float = 60,
                      user_id: int | None = None) -> list[dict]:
    """Статьи, выбранные в дайджест КОНКРЕТНЫМ пользователем (его user_article_states.status='digest').

    ``month`` опционален: пусто/None — фильтр периода снимается, возвращаются все
    выбранные (превью совпадает с экспортом). Будущие публикации всегда исключены.
    """
    params: dict = {"min_score": min_score, "limit": limit, "user_id": user_id}
    month_clause = ""
    if month:
        month_clause = "AND to_char(COALESCE(a.published_at, a.collected_at), 'YYYY-MM') = %(month)s"
        params["month"] = month
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT a.id, COALESCE(c.title_ru, a.title) AS title, a.url, a.published_at, a.language, a.image_url,
                   s.name AS source_name,
                   c.summary, TRUE AS selected_for_digest,
                   sc.total_score, sc.score_label,
                   t.name AS tag_name, parent.name AS parent_tag_name
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            JOIN user_article_states uas ON uas.article_id = a.id AND uas.user_id = %(user_id)s
            LEFT JOIN article_cards c ON c.article_id = a.id
            LEFT JOIN article_scores sc ON sc.article_id = a.id
            LEFT JOIN article_tags at ON at.article_id = a.id
            LEFT JOIN tags t ON t.id = at.tag_id
            LEFT JOIN tags parent ON parent.id = t.parent_id
            WHERE uas.status = 'digest'
              AND c.relevant IS NOT FALSE
              AND (a.published_at IS NULL OR a.published_at <= now() + interval '2 days')
              AND COALESCE(sc.total_score, 0) >= %(min_score)s
              {month_clause}
            ORDER BY sc.total_score DESC NULLS LAST,
                     a.published_at DESC NULLS LAST
            LIMIT %(limit)s
            """,
            params,
        )
        return cur.fetchall()


def save_monthly_digest(month: str, title: str, items: list[dict], status: str = "draft") -> dict:
    """Persist a monthly digest draft and replace its ordered item list."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO monthly_digests (month, title, status)
            VALUES (%s, %s, %s)
            ON CONFLICT (month) DO UPDATE SET
                title = EXCLUDED.title,
                status = EXCLUDED.status,
                updated_at = now()
            RETURNING id, month, title, status
            """,
            (month, title, status),
        )
        digest = cur.fetchone()
        digest_id = digest[0]
        conn.execute("DELETE FROM monthly_digest_items WHERE digest_id = %s", (digest_id,))
        for idx, item in enumerate(items, start=1):
            conn.execute(
                """
                INSERT INTO monthly_digest_items (digest_id, article_id, sort_order, section, editor_note)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    digest_id,
                    int(item["article_id"]),
                    idx,
                    item.get("section"),
                    item.get("editor_note"),
                ),
            )
        conn.commit()
        return {
            "id": digest[0],
            "month": digest[1],
            "title": digest[2],
            "status": digest[3],
            "items": len(items),
        }


def get_monthly_digest(month: str) -> dict | None:
    """Fetch a persisted monthly digest draft with ordered item article ids."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT id, month, title, status, created_at, updated_at
            FROM monthly_digests
            WHERE month = %s
            """,
            (month,),
        )
        digest = cur.fetchone()
        if digest is None:
            return None
        cur.execute(
            """
            SELECT article_id, sort_order, section, editor_note
            FROM monthly_digest_items
            WHERE digest_id = %s
            ORDER BY sort_order, id
            """,
            (digest["id"],),
        )
        return {**digest, "items": cur.fetchall()}


def max_article_id() -> int | None:
    """Return the current maximum article id, or None if the table is empty."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT MAX(id) FROM articles")
        row = cur.fetchone()
        return row[0] if row else None


def get_articles_needing_summary_after(after_id: int, limit: int = 20) -> list[dict]:
    """Articles that have no summary yet and whose id > after_id (streaming checkpoint)."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.*, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN article_cards c ON c.article_id = a.id
            WHERE a.id > %s AND c.summary IS NULL
            ORDER BY a.id ASC
            LIMIT %s
            """,
            (after_id, limit),
        )
        return cur.fetchall()
