"""HTTP API for the OilTech Digest admin frontend."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import hashlib
import hmac
import logging
from pathlib import Path
import secrets
import time
from typing import Any

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from psycopg.rows import dict_row
from psycopg.types.json import Json

from oiltech_digest import auth, background_jobs, config
from oiltech_digest.benchmarks import run_readiness_benchmark
from oiltech_digest.config import REPO_ROOT
from oiltech_digest.db.connection import get_connection
from oiltech_digest.db import repository
from oiltech_digest.logging_utils import setup_logging
from oiltech_digest.maintenance import maintenance_cleanup, maintenance_status
from oiltech_digest import network_policy
from oiltech_digest.processing.pipeline import (
    make_client,
    process_relevance_articles,
    process_score_articles,
    process_summary_articles,
    process_tag_articles,
)
from oiltech_digest.readiness import readiness_check
from oiltech_digest.ingestion import normalize, playwright_parser, request_parser
from oiltech_digest.ingestion import external_fetch
from oiltech_digest.ingestion.source_diagnostics import diagnose_source
from oiltech_digest.processing.digest import (
    build_digest_content,
    get_digest_branding,
    render_digest_email,
    save_digest_branding,
    save_digest_draft,
    write_digest_export,
)
from oiltech_digest.processing import external_ai

WEB_DIR = REPO_ROOT / "web"
FRONTEND_DIST_DIR = REPO_ROOT / "frontend" / "dist"

setup_logging("api")
logger = logging.getLogger(__name__)

app = FastAPI(title="OilTech Digest API")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
if (FRONTEND_DIST_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST_DIR / "assets"), name="frontend-assets")


@app.middleware("http")
async def log_requests(request, call_next):
    if request.url.path.startswith("/static") or request.url.path.startswith("/assets"):
        return await call_next(request)

    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - started) * 1000
    client = request.client.host if request.client else "-"
    logger.info(
        "request method=%s path=%s status=%s duration_ms=%.1f client=%s",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
        client,
    )
    return response


# Набор допустимых пер-юзерных статусов — единый источник правды в repository.ArticleStatus
# (там же кортеж для счётчиков). Колонки статуса — свободный TEXT без CHECK, поэтому
# валидация на границе API — единственное, что не даёт записать мусор: статья с
# неизвестным статусом молча пропадает из всех вкладок (фильтры перечисляют известный набор).
class ArticlePatch(BaseModel):
    status: repository.ArticleStatus | None = None
    selected_for_digest: bool | None = None
    analyst_comment: str | None = None


class SourcePatch(BaseModel):
    enabled: bool | None = None
    url: str | None = None
    rss_url: str | None = None
    parse_strategy: str | None = None
    update_frequency: str | None = None
    listing_url: str | None = None
    listing_strategy: str | None = None
    listing_selector: str | None = None
    article_link_selector: str | None = None
    article_date_selector: str | None = None
    network_region: str | None = None
    network_profile: str | None = None


class SourceCreate(BaseModel):
    name: str
    url: str | None = None
    rss_url: str = ""
    priority: float = 1.0
    category: str | None = None
    update_frequency: str | None = None


class ScoringCriterionIn(BaseModel):
    id: int | None = None
    name: str
    description: str | None = None
    weight: float
    keywords_json: list[str] = []
    keywords_en_json: list[str] = []
    sort_order: int = 0


class TagIn(BaseModel):
    id: int | None = None
    parent_name: str | None = None
    name: str
    name_en: str | None = None
    description: str | None = None
    keywords_json: list[str] = []
    keywords_en_json: list[str] = []
    negative_keywords_json: list[str] = []
    enabled: bool = True
    sort_order: int = 0


class ProcessRequest(BaseModel):
    article_ids: list[int] | None = None
    limit: int = 5
    offline: bool = False


class DigestRequest(BaseModel):
    month: str
    limit: int = 20
    min_score: float = 60
    max_score: float | None = None
    search: str = ""
    top_tag: str = ""


class MonthlyDigestItemIn(BaseModel):
    article_id: int
    section: str | None = None
    editor_note: str | None = None


class MonthlyDigestUpdateRequest(BaseModel):
    title: str | None = None
    status: str = "draft"
    items: list[MonthlyDigestItemIn]


class DigestExportJobRequest(BaseModel):
    month: str = ""
    export_format: str = "pdf"
    limit: int = 100
    min_score: float = 0
    max_score: float | None = None
    search: str = ""
    top_tag: str = ""


class MaintenanceCleanupRequest(BaseModel):
    background_job_days: int | None = None
    export_job_days: int | None = None


class ExternalWorkerClaimRequest(BaseModel):
    worker_id: str
    queues: list[str] = []
    capabilities: list[str] = []
    max_lease_seconds: int | None = None


class ExternalWorkerLeaseRequest(BaseModel):
    lease_token: str


class ExternalWorkerProgressRequest(ExternalWorkerLeaseRequest):
    progress: float
    lease_seconds: int | None = None


class ExternalWorkerHeartbeatRequest(ExternalWorkerLeaseRequest):
    lease_seconds: int | None = None


class ExternalWorkerCompleteRequest(ExternalWorkerLeaseRequest):
    result: dict[str, Any] = {}


class ExternalWorkerFailRequest(ExternalWorkerLeaseRequest):
    error: str
    retryable: bool = True
    retry_after_seconds: int | None = None


class DigestSocialIn(BaseModel):
    label: str
    accent: str
    text: str


class DigestHeaderBrandingIn(BaseModel):
    brand_text: str
    brand_suffix: str
    department_text: str


class DigestHeroBrandingIn(BaseModel):
    badge: str
    headline: str
    subtitle: str
    image_url: str = ""


class DigestIssueBrandingIn(BaseModel):
    title_template: str
    title_template_with_month: str
    period_label_all: str
    preheader: str
    intro_template: str
    intro_template_with_month: str
    highlights_title: str
    news_title: str
    read_more_label: str
    empty_summary_text: str
    preview_empty_text: str


class DigestFooterBrandingIn(BaseModel):
    contact_text: str
    contact_email: str
    note: str
    socials: list[DigestSocialIn] = []


class DigestHighlightsBrandingIn(BaseModel):
    analytics_source_keywords: list[str] = []
    analytics_category_keywords: list[str] = []
    business_category_keywords: list[str] = []
    cards: list[dict[str, str]] = []


class DigestBrandingIn(BaseModel):
    header: DigestHeaderBrandingIn
    hero: DigestHeroBrandingIn
    issue: DigestIssueBrandingIn
    footer: DigestFooterBrandingIn
    highlights: DigestHighlightsBrandingIn = DigestHighlightsBrandingIn()


class AuthPayload(BaseModel):
    email: str
    password: str


class UserCreate(BaseModel):
    email: str
    password: str
    role: str = "user"


class UserUpdate(BaseModel):
    role: str | None = None
    password: str | None = None


@app.get("/")
def index() -> FileResponse:
    if (FRONTEND_DIST_DIR / "index.html").exists():
        return FileResponse(FRONTEND_DIST_DIR / "index.html")
    return FileResponse(WEB_DIR / "app.html")


def require_user(session_token: str | None = Cookie(default=None, alias=config.AUTH_COOKIE_NAME)) -> dict[str, Any]:
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = repository.get_user_by_session(session_token)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_admin(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Доступ только администратору (настройка тегов/скоринга/источников, пользователи)."""
    if (user.get("role") or "user") != "admin":
        raise HTTPException(status_code=403, detail="Требуются права администратора")
    return user


def _set_session_cookie(response: Response, session_token: str) -> None:
    response.set_cookie(
        key=config.AUTH_COOKIE_NAME,
        value=session_token,
        httponly=True,
        samesite="lax",
        max_age=config.AUTH_SESSION_DAYS * 24 * 60 * 60,
    )


@app.get("/api/auth/me")
def auth_me(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return {"ok": True, "user": _clean(user)}


@app.post("/api/auth/register")
def auth_register(payload: AuthPayload, response: Response) -> dict[str, Any]:
    email = auth.normalize_email(payload.email)
    if not auth.validate_email(email):
        raise HTTPException(status_code=400, detail="Некорректный email")
    if not auth.validate_password(payload.password):
        raise HTTPException(status_code=400, detail="Пароль должен быть не короче 8 символов")
    try:
        user = repository.create_user(email, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    session_token = repository.create_user_session(int(user["id"]))
    _set_session_cookie(response, session_token)
    return {"ok": True, "user": _clean(user)}


@app.post("/api/auth/login")
def auth_login(payload: AuthPayload, response: Response) -> dict[str, Any]:
    user = repository.authenticate_user(payload.email, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
    session_token = repository.create_user_session(int(user["id"]))
    _set_session_cookie(response, session_token)
    return {"ok": True, "user": _clean(user)}


@app.post("/api/auth/logout")
def auth_logout(
    response: Response,
    session_token: str | None = Cookie(default=None, alias=config.AUTH_COOKIE_NAME),
) -> dict[str, Any]:
    if session_token:
        repository.delete_user_session(session_token)
    response.delete_cookie(config.AUTH_COOKIE_NAME)
    return {"ok": True}


@app.get("/api/users")
def list_users_endpoint(user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    return {"users": [_clean(u) for u in repository.list_users()]}


@app.post("/api/users")
def create_user_endpoint(payload: UserCreate, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    email = auth.normalize_email(payload.email)
    if not auth.validate_email(email):
        raise HTTPException(status_code=400, detail="Некорректный email")
    if not auth.validate_password(payload.password):
        raise HTTPException(status_code=400, detail="Пароль должен быть не короче 8 символов")
    try:
        created = repository.create_user(email, payload.password, payload.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "user": _clean(created)}


@app.patch("/api/users/{user_id}")
def update_user_endpoint(user_id: int, payload: UserUpdate, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    target = repository.get_user_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if payload.role is not None:
        new_role = payload.role if payload.role in ("admin", "user") else "user"
        if target["role"] == "admin" and new_role != "admin" and repository.count_admins() <= 1:
            raise HTTPException(status_code=400, detail="Нельзя снять роль у последнего администратора")
        repository.set_user_role(user_id, new_role)
    if payload.password is not None:
        if not auth.validate_password(payload.password):
            raise HTTPException(status_code=400, detail="Пароль должен быть не короче 8 символов")
        repository.set_user_password(user_id, payload.password)
    return {"ok": True, "user": _clean(repository.get_user_by_id(user_id))}


@app.delete("/api/users/{user_id}")
def delete_user_endpoint(user_id: int, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    target = repository.get_user_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if int(user["id"]) == user_id:
        raise HTTPException(status_code=400, detail="Нельзя удалить собственную учётную запись")
    if target["role"] == "admin" and repository.count_admins() <= 1:
        raise HTTPException(status_code=400, detail="Нельзя удалить последнего администратора")
    repository.delete_user(user_id)
    return {"ok": True}


@app.get("/api/health")
def health() -> dict[str, Any]:
    with get_connection() as conn:
        article_count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    return {"ok": True, "articles": article_count}


@app.get("/api/readiness")
def readiness() -> JSONResponse:
    try:
        payload = readiness_check()
    except Exception as exc:  # noqa: BLE001 - readiness must return a clear 503 payload
        return JSONResponse(
            status_code=503,
            content={"ok": False, "database": {"ok": False}, "error": str(exc)},
        )
    return JSONResponse(status_code=200 if payload["ok"] else 503, content=_clean(payload))


@app.get("/api/articles")
def list_articles(
    search: str | None = None,
    source: str | None = None,
    tag: str | None = None,
    status: str | None = None,
    language: str | None = None,
    min_score: float | None = None,
    max_score: float | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = Query("score_desc", pattern="^(date_desc|score_desc|score_asc)$"),
    changed_only: bool = False,
    limit: int = Query(1000, ge=1, le=5000),
    user: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if search:
        clauses.append(
            "LOWER(a.title || ' ' || COALESCE(a.raw_text, '') || ' ' || COALESCE(c.summary, '')) LIKE %s"
        )
        params.append(f"%{search.lower()}%")
    if source:
        clauses.append("s.name = %s")
        params.append(source)
    if tag:
        clauses.append("(t.name = %s OR parent.name = %s)")
        params.extend([tag, tag])
    if status:
        clauses.append("COALESCE(uas.status, 'new') = %s")
        params.append(status)
    if changed_only:
        clauses.append("COALESCE(uas.status, 'new') <> 'new'")
    if language:
        clauses.append("a.language = %s")
        params.append(language)
    if min_score is not None:
        # Ещё НЕ оценённые статьи (нет строки в article_scores) — это не «низкобалльные»:
        # балла у них попросту нет, и COALESCE(...,0) выдавал бы за 0, отсекая свежий приток
        # порогом. Порог применяем только к УЖЕ оценённым — иначе новые статьи исчезают из
        # ленты до прохода ИИ и она выглядит замороженной. Тот же принцип, что строкой ниже
        # для c.relevant IS NULL: необработанное не прячем.
        clauses.append("(sc.total_score IS NULL OR sc.total_score >= %s)")
        params.append(min_score)
    if max_score is not None:
        clauses.append("COALESCE(sc.total_score, 0) <= %s")
        params.append(max_score)
    if date_from:
        clauses.append("COALESCE(a.published_at::date, a.collected_at::date) >= %s")
        params.append(date_from)
    if date_to:
        clauses.append("COALESCE(a.published_at::date, a.collected_at::date) <= %s")
        params.append(date_to)
    # Скрываем отклонённые гейтом релевантности статьи (relevant=false), как это уже
    # делает дайджест. relevant IS NULL (ещё не проверенные) остаются видны.
    clauses.append("c.relevant IS NOT FALSE")
    # Скрываем помеченные на удаление (recheck --mark): исчезают из ленты, но физически
    # ещё в БД (восстановимы recheck-unmark до recheck-purge).
    clauses.append("NOT a.pending_deletion")
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    order_by = {
        "date_desc": "a.published_at DESC NULLS LAST, COALESCE(sc.total_score, 0) DESC, a.id DESC",
        "score_asc": "COALESCE(sc.total_score, 0) ASC, a.published_at DESC NULLS LAST, a.id DESC",
        "score_desc": "COALESCE(sc.total_score, 0) DESC, a.published_at DESC NULLS LAST, a.id DESC",
    }[sort]
    # user_id — первый %s (для LEFT JOIN user_article_states), затем where-параметры, затем limit.
    params.insert(0, int(user["id"]))
    params.append(limit)

    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT a.id, COALESCE(c.title_ru, a.title) AS title, a.url, a.language, length(a.raw_text) AS raw_text_chars,
                   a.published_at,
                   a.collected_at, a.text_truncated, s.name AS source_name,
                   COALESCE(c.summary, '') AS summary,
                   COALESCE(uas.status, 'new') AS status,
                   c.relevant, c.relevance_reason,
                   (COALESCE(uas.status, 'new') = 'digest') AS selected_for_digest,
                   sc.total_score, sc.score_label, sc.explanation AS score_explanation,
                   t.name AS tag_name, parent.name AS parent_tag_name,
                   at.confidence AS tag_confidence, at.rationale AS tag_rationale
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN user_article_states uas ON uas.article_id = a.id AND uas.user_id = %s
            LEFT JOIN article_cards c ON c.article_id = a.id
            LEFT JOIN article_scores sc ON sc.article_id = a.id
            LEFT JOIN article_tags at ON at.article_id = a.id
            LEFT JOIN tags t ON t.id = at.tag_id
            LEFT JOIN tags parent ON parent.id = t.parent_id
            {where}
            ORDER BY {order_by}
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()
        score_items = _score_items_by_article(conn, [row["id"] for row in rows])
    payloads = [_article_payload(row) for row in rows]
    for payload in payloads:
        payload["score_items"] = score_items.get(payload["id"], [])
    return payloads


@app.get("/api/stats")
def dashboard_stats(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Authoritative dashboard counters, computed over the full database."""
    return _clean(repository.dashboard_stats(int(user["id"])))


@app.patch("/api/articles/{article_id}")
def update_article(article_id: int, patch: ArticlePatch, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    # Статус и выбор в дайджест — ПЕР-ЮЗЕРНЫЕ (#12). selected_for_digest сводится к статусу.
    target_status = patch.status
    if target_status is None and patch.selected_for_digest is not None:
        target_status = "digest" if patch.selected_for_digest else "review"
    with get_connection() as conn:
        exists = conn.execute("SELECT 1 FROM articles WHERE id = %s", (article_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Article not found")
    repository.set_user_article_status(
        int(user["id"]), article_id, status=target_status, analyst_comment=patch.analyst_comment
    )
    return {"ok": True}


@app.get("/api/sources")
def list_sources(
    search: str | None = None,
    limit: int = Query(300, ge=1, le=1000),
    user: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    return [_clean(row) for row in repository.list_sources(search=search, limit=limit)]


@app.get("/api/source-health")
def source_health(
    stale_days: int = Query(3, ge=1, le=30),
    limit: int = Query(500, ge=1, le=1000),
    verdict: str | None = Query(None, pattern="^(ok|stale|no_articles|disabled)$"),
    user: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    return [_clean(row) for row in repository.source_health_report(stale_days=stale_days, limit=limit, verdict=verdict)]


@app.post("/api/sources")
def create_source(payload: SourceCreate, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    # Пользователь вставляет просто ссылку на источник — система сама ищет RSS-ленту.
    # Нашла → parse_strategy='rss' с найденным фидом; не нашла → 'request' (скрейп
    # страницы новостей). RSS можно передать и явно (тогда discover пропускается).
    site_url = (payload.url or payload.rss_url or "").strip()
    rss_url = (payload.rss_url or "").strip()
    parse_strategy = "rss"
    if not rss_url and site_url:
        from oiltech_digest.ingestion.rss_discovery import discover_feed
        found = discover_feed(site_url)
        if found:
            rss_url = found
        else:
            parse_strategy = "request"
    source_id = repository.add_rss_source(
        name=payload.name,
        rss_url=rss_url,
        url=site_url or rss_url,
        priority=payload.priority,
        category=payload.category,
        update_frequency=payload.update_frequency,
        parse_strategy=parse_strategy,
    )
    return {"ok": True, "id": source_id, "rss_url": rss_url or None, "parse_strategy": parse_strategy}


@app.patch("/api/sources/{source_id}")
def update_source(source_id: int, patch: SourcePatch, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    updates = patch.model_dump(exclude_unset=True)
    if not updates:
        return {"ok": True}
    allowed = {
        "enabled",
        "url",
        "rss_url",
        "parse_strategy",
        "update_frequency",
        "listing_url",
        "listing_strategy",
        "listing_selector",
        "article_link_selector",
        "article_date_selector",
        "network_region",
        "network_profile",
    }
    fields = [key for key in updates if key in allowed]
    if not fields:
        return {"ok": True}
    values = [updates[field] for field in fields]
    set_clause = ", ".join(f"{field} = %s" for field in fields)
    with get_connection() as conn:
        cur = conn.execute(
            f"UPDATE sources SET {set_clause}, updated_at = now() WHERE id = %s RETURNING id",
            [*values, source_id],
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Source not found")
        conn.commit()
    return {"ok": True}


@app.post("/api/sources/{source_id}/scrape")
def scrape_source(
    source_id: int,
    background: bool = False,
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    source = repository.get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    strategy = source.get("parse_strategy")
    if strategy not in {"request", "playwright"}:
        raise HTTPException(status_code=400, detail="Скраппер доступен только для request/playwright-источников")
    if background:
        decision = network_policy.route_source_task(source, task_kind="scrape")
        job = background_jobs.enqueue(
            "scrape_source",
            {"source_id": source_id},
            user_id=int(user["id"]),
            queue_name=decision.queue_name,
            execution_region=decision.execution_region,
            capability=decision.capability,
        )
        return {"ok": True, "job": _job_payload(job)}
    stats = playwright_parser.parse_source(source) if strategy == "playwright" else request_parser.parse_source(source)
    return {"ok": True, "stats": _clean(stats)}


@app.get("/api/sources/{source_id}/diagnose")
def diagnose_source_endpoint(
    source_id: int,
    limit: int = Query(5, ge=1, le=20),
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    source = repository.get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return _clean(diagnose_source(source, limit=limit))


@app.post("/api/sources/{source_id}/diagnose")
def diagnose_source_with_overrides(
    source_id: int,
    patch: SourcePatch,
    limit: int = Query(5, ge=1, le=20),
    background: bool = False,
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    source = repository.get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    overrides = patch.model_dump(exclude_unset=True)
    if background:
        decision = network_policy.route_source_task({**source, **overrides}, task_kind="diagnose")
        job = background_jobs.enqueue(
            "diagnose_source",
            {"source_id": source_id, "overrides": overrides, "limit": limit},
            user_id=int(user["id"]),
            queue_name=decision.queue_name,
            execution_region=decision.execution_region,
            capability=decision.capability,
        )
        return {"ok": True, "job": _job_payload(job)}
    return _clean(diagnose_source({**source, **overrides}, limit=limit))


@app.get("/api/tags")
def list_tags(user: dict[str, Any] = Depends(require_user)) -> list[dict[str, Any]]:
    return [_clean(row) for row in repository.list_enabled_tags()]


@app.put("/api/tags")
def save_tags(items: list[TagIn], user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    result = repository.save_tags([i.model_dump() for i in items])
    return {"ok": True, **result}


@app.delete("/api/tags/{tag_id}")
def delete_tag(tag_id: int, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    repository.delete_tag(tag_id)
    return {"ok": True}


@app.get("/api/scoring-criteria")
def list_scoring_criteria(user: dict[str, Any] = Depends(require_user)) -> list[dict[str, Any]]:
    return [_clean(row) for row in repository.list_enabled_scoring_criteria()]


@app.put("/api/scoring-criteria")
def save_scoring_criteria(items: list[ScoringCriterionIn], user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    try:
        result = repository.save_scoring_criteria([i.model_dump() for i in items])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, **result}


@app.delete("/api/scoring-criteria/{criterion_id}")
def delete_scoring_criterion(criterion_id: int, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    repository.delete_scoring_criterion(criterion_id)
    return {"ok": True}


@app.get("/api/reports/ai-cost")
def ai_cost(user: dict[str, Any] = Depends(require_user)) -> list[dict[str, Any]]:
    return [_clean(row) for row in repository.ai_cost_report()]


@app.get("/api/reports/ai-article-cost")
def ai_article_cost(
    limit: int = Query(20, ge=1, le=200),
    include_partial: bool = False,
    user: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    return [_clean(row) for row in repository.ai_article_cost_report(limit=limit, complete_only=not include_partial)]


@app.get("/api/digest-content")
def digest_content(month: str = "", limit: int = Query(100, ge=1, le=500),
                   min_score: float = 0,
                   max_score: float | None = None,
                   search: str = "",
                   top_tag: str = "",
                   user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return _clean(
        build_digest_content(
            month=month,
            limit=limit,
            min_score=min_score,
            max_score=max_score,
            search=search.strip() or None,
            top_tag=top_tag.strip() or None,
            user_id=int(user["id"]),
        )
    )


@app.get("/api/digest-branding")
def digest_branding(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return _clean(get_digest_branding())


@app.put("/api/digest-branding")
def update_digest_branding(payload: DigestBrandingIn, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return {"ok": True, "branding": _clean(save_digest_branding(payload.model_dump()))}


@app.post("/api/monthly-digests")
def create_monthly_digest(payload: DigestRequest, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return _clean(
        save_digest_draft(
            month=payload.month,
            limit=payload.limit,
            min_score=payload.min_score,
            max_score=payload.max_score,
            search=payload.search.strip() or None,
            top_tag=payload.top_tag.strip() or None,
            user_id=int(user["id"]),
        )
    )


@app.get("/api/monthly-digests/{month}")
def get_monthly_digest(month: str, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    digest = repository.get_monthly_digest(month, user_id=int(user["id"]))
    if digest is None:
        raise HTTPException(status_code=404, detail="Digest not found")
    return _clean(digest)


@app.put("/api/monthly-digests/{month}")
def update_monthly_digest(month: str, payload: MonthlyDigestUpdateRequest, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    saved = repository.save_monthly_digest(
        month=month,
        title=payload.title or f"Нефтесервисный дайджест · {month}",
        items=[item.model_dump() for item in payload.items],
        status=payload.status,
        user_id=int(user["id"]),
    )
    return _clean(saved)


@app.get("/api/digest-email", response_class=HTMLResponse)
def digest_email(month: str = "", limit: int = Query(100, ge=1, le=500),
                 min_score: float = 0,
                 max_score: float | None = None,
                 search: str = "",
                 top_tag: str = "",
                 user: dict[str, Any] = Depends(require_user)) -> HTMLResponse:
    content = build_digest_content(
        month=month,
        limit=limit,
        min_score=min_score,
        max_score=max_score,
        search=search.strip() or None,
        top_tag=top_tag.strip() or None,
        user_id=int(user["id"]),
    )
    return HTMLResponse(render_digest_email(content))


@app.get("/api/jobs")
def list_jobs(
    status: str | None = Query(None, pattern="^(queued|running|finalizing|ok|failed)$"),
    kind: str | None = None,
    queue_name: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    user: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    user_id = None if (user.get("role") or "user") == "admin" else int(user["id"])
    return [
        _job_payload(row)
        for row in repository.list_background_jobs(
            status=status,
            kind=kind,
            queue_name=queue_name,
            user_id=user_id,
            limit=limit,
        )
    ]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    job = _get_scoped_background_job(job_id, user)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_payload(job)


@app.get("/api/jobs/{job_id}/download")
def download_job_result(job_id: int, user: dict[str, Any] = Depends(require_user)) -> FileResponse:
    job = _get_scoped_background_job(job_id, user)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "ok":
        raise HTTPException(status_code=409, detail="Job is not finished")
    path = background_jobs.job_download_path(job)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="Job result file not found")
    result = job.get("result_json") or {}
    return FileResponse(
        str(path),
        media_type=result.get("media_type") or "application/octet-stream",
        filename=result.get("filename") or path.name,
    )


@app.post("/api/jobs/digest-export")
def enqueue_digest_export(payload: DigestExportJobRequest, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    if payload.export_format not in {"pdf", "doc", "docx", "html", "json"}:
        raise HTTPException(status_code=400, detail="Unsupported export format")
    if payload.limit < 1 or payload.limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    decision = network_policy.route_digest_export(payload.export_format)
    job = background_jobs.enqueue(
        "digest_export",
        {**payload.model_dump(), "user_id": int(user["id"])},  # дайджест пер-юзерный (#12)
        user_id=int(user["id"]),
        queue_name=decision.queue_name,
        execution_region=decision.execution_region,
        capability=decision.capability,
    )
    return {"ok": True, "job": _job_payload(job)}


@app.post("/api/jobs/process")
def enqueue_process_articles(payload: ProcessRequest, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    if payload.limit < 1 or payload.limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    decision = network_policy.route_ai_processing()
    job = background_jobs.enqueue(
        "process_articles",
        payload.model_dump(),
        user_id=int(user["id"]),
        queue_name=decision.queue_name,
        execution_region=decision.execution_region,
        capability=decision.capability,
    )
    return {"ok": True, "job": _job_payload(job)}


def require_external_worker(authorization: str | None = Header(default=None)) -> None:
    if not config.EXTERNAL_WORKER_TOKEN_HASH:
        raise HTTPException(status_code=503, detail="External worker auth is not configured")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="External worker token required")
    if not hmac.compare_digest(_sha256_hex(token), config.EXTERNAL_WORKER_TOKEN_HASH):
        raise HTTPException(status_code=401, detail="Invalid external worker token")


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _lease_seconds(value: int | None) -> int:
    requested = value or config.EXTERNAL_WORKER_DEFAULT_LEASE_SECONDS
    return min(max(int(requested), 30), 3600)


@app.post("/api/external-worker/claim")
def external_worker_claim(
    payload: ExternalWorkerClaimRequest,
    _: None = Depends(require_external_worker),
) -> dict[str, Any]:
    repository.requeue_expired_external_leases()
    lease_token = secrets.token_urlsafe(32)
    job = repository.claim_external_background_job(
        queue_names=payload.queues,
        capabilities=payload.capabilities,
        worker_id=payload.worker_id,
        lease_token_hash=_sha256_hex(lease_token),
        lease_seconds=_lease_seconds(payload.max_lease_seconds),
    )
    if job is None:
        return {"job": None}
    return {"job": {**_job_payload(job), "payload": _external_worker_payload(job), "lease_token": lease_token}}


@app.post("/api/external-worker/jobs/{job_id}/progress")
def external_worker_progress(
    job_id: int,
    payload: ExternalWorkerProgressRequest,
    _: None = Depends(require_external_worker),
) -> dict[str, Any]:
    if payload.progress < 0 or payload.progress > 100:
        raise HTTPException(status_code=400, detail="progress must be between 0 and 100")
    ok = repository.update_external_background_job_progress(
        job_id,
        lease_token_hash=_sha256_hex(payload.lease_token),
        progress=payload.progress,
        lease_seconds=_lease_seconds(payload.lease_seconds) if payload.lease_seconds is not None else None,
    )
    if not ok:
        raise HTTPException(status_code=409, detail="Job lease is not active")
    return {"ok": True}


@app.post("/api/external-worker/jobs/{job_id}/heartbeat")
def external_worker_heartbeat(
    job_id: int,
    payload: ExternalWorkerHeartbeatRequest,
    _: None = Depends(require_external_worker),
) -> dict[str, Any]:
    ok = repository.heartbeat_external_background_job(
        job_id,
        lease_token_hash=_sha256_hex(payload.lease_token),
        lease_seconds=_lease_seconds(payload.lease_seconds),
    )
    if not ok:
        raise HTTPException(status_code=409, detail="Job lease is not active")
    return {"ok": True}


@app.post("/api/external-worker/jobs/{job_id}/complete")
def external_worker_complete(
    job_id: int,
    payload: ExternalWorkerCompleteRequest,
    _: None = Depends(require_external_worker),
) -> dict[str, Any]:
    result = payload.result
    job = repository.get_background_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    lease_token_hash = _sha256_hex(payload.lease_token)
    # Баг T2 (двойной AI-расход): застолбить завершение АТОМАРНО до применения результата.
    # Пока идёт apply (запись карточек/скоринга + биллинг ai_processing_runs), задача в статусе
    # 'finalizing', и requeue_expired_external_leases (только status='running') её НЕ переотдаст —
    # значит другой воркер не прогонит AI повторно. Лиз истёк/переотдан → 409, ничего не применяем.
    if not repository.begin_external_background_job_finalize(job_id, lease_token_hash=lease_token_hash):
        raise HTTPException(status_code=409, detail="Job lease is not active")
    try:
        if job.get("kind") == "process_articles" and result.get("external_ai"):
            result = {**result, "applied": external_ai.apply_process_result(result, job_id=job_id)}
        if job.get("kind") == "recheck_relevance" and result.get("recheck_relevance"):
            # ИМЕННО payload_json: job приходит из get_background_job (SELECT *), поэтому ключи —
            # это колонки таблицы (schema.sql:304). Ключа "payload" в строке НЕТ, и чтение его
            # молча давало {} → mark/dry_run/force всегда False → recheck удалял статьи ФИЗИЧЕСКИ
            # вопреки запрошенному мягкому режиму (баг T3, так уже потеряли ~2000 статей).
            job_payload = job.get("payload_json") or {}
            force = bool(job_payload.get("force", False))
            dry_run = bool(job_payload.get("dry_run", False))
            mark = bool(job_payload.get("mark", False))
            result = {**result, "applied": external_ai.apply_recheck_result(result, force=force, dry_run=dry_run, mark=mark, job_id=job_id)}
        if job.get("kind") == "translate_titles" and result.get("translate_titles"):
            result = {**result, "applied": external_ai.apply_translate_result(result, job_id=job_id)}
        if job.get("kind") == "scrape_source" and result.get("external_fetch"):
            result = {**result, "applied": external_fetch.apply_scrape_result(result)}
    except Exception:
        # apply упал — снять 'finalizing', чтобы задача не залипла (вернётся в очередь по лизу/stale)
        repository.release_external_background_job_finalize(job_id, lease_token_hash=lease_token_hash)
        raise
    ok = repository.finish_external_background_job(
        job_id,
        lease_token_hash=lease_token_hash,
        result=result,
    )
    if not ok:
        raise HTTPException(status_code=409, detail="Job lease is not active")
    return {"ok": True}


@app.post("/api/external-worker/jobs/{job_id}/fail")
def external_worker_fail(
    job_id: int,
    payload: ExternalWorkerFailRequest,
    _: None = Depends(require_external_worker),
) -> dict[str, Any]:
    ok = repository.fail_external_background_job(
        job_id,
        lease_token_hash=_sha256_hex(payload.lease_token),
        error_message=payload.error,
        retryable=payload.retryable,
        retry_delay_seconds=payload.retry_after_seconds,
    )
    if not ok:
        raise HTTPException(status_code=409, detail="Job lease is not active")
    return {"ok": True}


@app.get("/api/maintenance/status")
def get_maintenance_status(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return _clean(maintenance_status())


@app.post("/api/maintenance/cleanup")
def run_maintenance_cleanup(
    payload: MaintenanceCleanupRequest,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    if payload.background_job_days is not None and payload.background_job_days < 1:
        raise HTTPException(status_code=400, detail="background_job_days must be >= 1")
    if payload.export_job_days is not None and payload.export_job_days < 1:
        raise HTTPException(status_code=400, detail="export_job_days must be >= 1")
    return {"ok": True, "result": _clean(maintenance_cleanup(**payload.model_dump()))}


@app.get("/api/maintenance/benchmark")
def get_maintenance_benchmark(
    iterations: int = Query(3, ge=1, le=10),
    articles_limit: int = Query(200, ge=1, le=2000),
    source_limit: int = Query(150, ge=1, le=1000),
    jobs_limit: int = Query(100, ge=1, le=1000),
    digest_limit: int = Query(100, ge=1, le=500),
    min_score: float = 0,
    warn_ms: float = Query(800, gt=0, le=10_000),
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    return _clean(
        run_readiness_benchmark(
            iterations=iterations,
            articles_limit=articles_limit,
            source_limit=source_limit,
            jobs_limit=jobs_limit,
            digest_limit=digest_limit,
            min_score=min_score,
            warn_ms=warn_ms,
        )
    )


@app.get("/api/digest-export")
def digest_export(
    month: str = "",
    export_format: str = Query("pdf", pattern="^(pdf|docx?|html|json)$"),
    limit: int = Query(100, ge=1, le=500),
    min_score: float = 0,
    max_score: float | None = None,
    search: str = "",
    top_tag: str = "",
    user: dict[str, Any] = Depends(require_user),
) -> FileResponse:
    job_id = repository.create_export_job("monthly_digest", export_format)
    try:
        result = write_digest_export(
            month=month,
            export_format=export_format,
            limit=limit,
            min_score=min_score,
            max_score=max_score,
            search=search.strip() or None,
            top_tag=top_tag.strip() or None,
            user_id=int(user["id"]),
        )
        repository.finish_export_job(job_id, "ok", result["path"])
    except RuntimeError as exc:  # PDF без Chromium и т.п. — понятное сообщение, не 500
        repository.finish_export_job(job_id, "failed", error_message=str(exc))
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        repository.finish_export_job(job_id, "failed", error_message=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))
    return FileResponse(
        result["path"],
        media_type=result["media_type"],
        filename=result["filename"],
    )


@app.post("/api/process")
def process_articles(payload: ProcessRequest, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    client = make_client(payload.offline)
    if payload.article_ids:
        articles = repository.get_articles_by_ids(payload.article_ids, include_summary=False)
    else:
        articles = repository.get_articles_needing_summary(payload.limit)
    summaries = process_summary_articles(articles, client)

    ids = [int(article["id"]) for article in articles]
    relevance_articles = (
        repository.get_articles_by_ids(ids, include_summary=True)
        if payload.article_ids
        else repository.get_articles_needing_relevance(payload.limit)
    )
    relevance = process_relevance_articles(relevance_articles, client)

    if payload.article_ids:
        with_summary = repository.get_articles_by_ids(ids, include_summary=True)
    else:
        with_summary = repository.get_articles_needing_tags(payload.limit)
    tags = process_tag_articles(with_summary, client)

    if payload.article_ids:
        with_summary = repository.get_articles_by_ids(ids, include_summary=True)
    else:
        with_summary = repository.get_articles_needing_scores(payload.limit)
    scores = process_score_articles(with_summary, client)
    return {"summary": summaries, "relevance": relevance, "tagging": tags, "scoring": scores}


def _job_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "kind": row["kind"],
        "queue": row.get("queue_name") or "default",
        "execution_region": row.get("execution_region") or "ru",
        "capability": row.get("capability"),
        "status": row["status"],
        "progress": float(row.get("progress") or 0),
        "attempts": int(row.get("attempts") or 0),
        "max_attempts": int(row.get("max_attempts") or 0),
        "payload": _clean(row.get("payload_json") or {}),
        "result": _clean(row.get("result_json") or {}),
        "error": row.get("error_message"),
        "run_after": _clean(row.get("run_after")),
        "created_at": _clean(row.get("created_at")),
        "started_at": _clean(row.get("started_at")),
        "finished_at": _clean(row.get("finished_at")),
    }


def _get_scoped_background_job(job_id: int, user: dict[str, Any]) -> dict[str, Any] | None:
    if (user.get("role") or "user") == "admin":
        return repository.get_background_job(job_id)
    return repository.get_background_job(job_id, user_id=int(user["id"]))


def _external_worker_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row.get("payload_json") or {})
    if row.get("kind") == "process_articles" and row.get("queue_name") == "external-ai":
        return _clean(external_ai.build_process_articles_payload(payload))
    if row.get("kind") == "recheck_relevance" and row.get("queue_name") == "external-ai":
        return _clean(external_ai.build_recheck_payload(payload))
    if row.get("kind") == "translate_titles" and row.get("queue_name") == "external-ai":
        return _clean(external_ai.build_translate_payload(payload))
    if row.get("kind") == "scrape_source" and str(row.get("queue_name") or "").startswith("external-"):
        return _clean(external_fetch.build_scrape_source_payload(int(payload["source_id"]), payload))
    return _clean(payload)


def _score_items_by_article(conn, article_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Per-criterion scoring breakdown grouped by article id."""
    if not article_ids:
        return {}
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        SELECT s.article_id, sc.name, sc.weight, asi.final_score, asi.ai_score,
               asi.keyword_score, asi.rationale
        FROM article_score_items asi
        JOIN article_scores s ON s.id = asi.article_score_id
        JOIN scoring_criteria sc ON sc.id = asi.criterion_id
        WHERE s.article_id = ANY(%s)
        ORDER BY sc.sort_order, sc.id
        """,
        (article_ids,),
    )
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in cur.fetchall():
        grouped.setdefault(int(row["article_id"]), []).append(
            {
                "name": row["name"],
                "weight": float(row["weight"]) if row["weight"] is not None else 0.0,
                "final_score": float(row["final_score"]) if row["final_score"] is not None else 0.0,
                "ai_score": float(row["ai_score"]) if row["ai_score"] is not None else None,
                "keyword_score": float(row["keyword_score"]) if row["keyword_score"] is not None else None,
                "rationale": row["rationale"],
            }
        )
    return grouped


def _article_payload(row: dict[str, Any]) -> dict[str, Any]:
    tag = row.get("tag_name") or "Без тега"
    if row.get("parent_tag_name"):
        tag = f"{row['parent_tag_name']} / {tag}"
    return {
        "id": row["id"],
        "title": row["title"],
        "url": row["url"],
        "source": row["source_name"],
        "language": row.get("language"),
        "date": _date(row.get("published_at") or row.get("collected_at")),
        "published_at": _date(row.get("published_at")),
        "collected": _date(row.get("collected_at")),
        "future_date": normalize.is_future_date(row.get("published_at")),
        "summary": row.get("summary") or "",
        "tag": tag,
        "score": float(row["total_score"]) if row.get("total_score") is not None else 0,
        "rating": row.get("score_label") or "Без оценки",
        "status": row.get("status") or "new",
        "digest": (row.get("status") or "new") == "digest",
        "tag_confidence": float(row["tag_confidence"]) if row.get("tag_confidence") is not None else None,
        "tag_rationale": row.get("tag_rationale"),
        "score_explanation": row.get("score_explanation"),
        "raw_text_chars": int(row.get("raw_text_chars") or 0),
        "text_truncated": bool(row.get("text_truncated")),
        "relevant": row.get("relevant"),
        "relevance_reason": row.get("relevance_reason"),
    }


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Json):
        return value.obj
    return value


def _date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)
