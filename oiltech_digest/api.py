"""HTTP API for the OilTech Digest admin frontend."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from psycopg.rows import dict_row
from psycopg.types.json import Json

from oiltech_digest import auth, background_jobs, config
from oiltech_digest.config import REPO_ROOT
from oiltech_digest.db.connection import get_connection
from oiltech_digest.db import repository
from oiltech_digest.processing.pipeline import (
    make_client,
    process_relevance_articles,
    process_score_articles,
    process_summary_articles,
    process_tag_articles,
)
from oiltech_digest.ingestion import normalize, playwright_parser, request_parser
from oiltech_digest.ingestion.source_diagnostics import diagnose_source
from oiltech_digest.processing.digest import build_digest_content, render_digest_email, save_digest_draft, write_digest_export

WEB_DIR = REPO_ROOT / "web"
FRONTEND_DIST_DIR = REPO_ROOT / "frontend" / "dist"

app = FastAPI(title="OilTech Digest API")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
if (FRONTEND_DIST_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST_DIR / "assets"), name="frontend-assets")


class ArticlePatch(BaseModel):
    status: str | None = None
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


class SourceCreate(BaseModel):
    name: str
    rss_url: str
    url: str | None = None
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


class DigestExportJobRequest(BaseModel):
    month: str = ""
    export_format: str = "pdf"
    limit: int = 100
    min_score: float = 0


class AuthPayload(BaseModel):
    email: str
    password: str


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


@app.get("/api/health")
def health() -> dict[str, Any]:
    with get_connection() as conn:
        article_count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    return {"ok": True, "articles": article_count}


@app.get("/api/articles")
def list_articles(
    search: str | None = None,
    source: str | None = None,
    tag: str | None = None,
    status: str | None = None,
    min_score: float | None = None,
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
        clauses.append("COALESCE(c.status, 'new') = %s")
        params.append(status)
    if min_score is not None:
        clauses.append("COALESCE(sc.total_score, 0) >= %s")
        params.append(min_score)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(limit)

    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT a.id, a.title, a.url, a.language, a.raw_text, a.published_at,
                   a.collected_at, a.text_truncated, s.name AS source_name,
                   COALESCE(c.summary, '') AS summary,
                   COALESCE(c.status, 'new') AS status,
                   c.relevant, c.relevance_reason,
                   COALESCE(c.selected_for_digest, FALSE) AS selected_for_digest,
                   sc.total_score, sc.score_label, sc.explanation AS score_explanation,
                   t.name AS tag_name, parent.name AS parent_tag_name,
                   at.confidence AS tag_confidence, at.rationale AS tag_rationale
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN article_cards c ON c.article_id = a.id
            LEFT JOIN article_scores sc ON sc.article_id = a.id
            LEFT JOIN article_tags at ON at.article_id = a.id
            LEFT JOIN tags t ON t.id = at.tag_id
            LEFT JOIN tags parent ON parent.id = t.parent_id
            {where}
            ORDER BY COALESCE(sc.total_score, 0) DESC, a.published_at DESC NULLS LAST, a.id DESC
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
    return _clean(repository.dashboard_stats())


@app.patch("/api/articles/{article_id}")
def update_article(article_id: int, patch: ArticlePatch, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    selected_for_digest = patch.selected_for_digest
    if patch.status is not None:
        selected_for_digest = patch.status == "digest"
    with get_connection() as conn:
        exists = conn.execute("SELECT 1 FROM articles WHERE id = %s", (article_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Article not found")
        conn.execute(
            """
            INSERT INTO article_cards (article_id, status, selected_for_digest, analyst_comment)
            VALUES (%s, COALESCE(%s, 'new'), COALESCE(%s, FALSE), %s)
            ON CONFLICT (article_id) DO UPDATE SET
              status = COALESCE(EXCLUDED.status, article_cards.status),
              selected_for_digest = COALESCE(EXCLUDED.selected_for_digest, article_cards.selected_for_digest),
              analyst_comment = COALESCE(EXCLUDED.analyst_comment, article_cards.analyst_comment),
              updated_at = now()
            """,
            (article_id, patch.status, selected_for_digest, patch.analyst_comment),
        )
        conn.commit()
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
def create_source(payload: SourceCreate, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    source_id = repository.add_rss_source(
        name=payload.name,
        rss_url=payload.rss_url,
        url=payload.url,
        priority=payload.priority,
        category=payload.category,
        update_frequency=payload.update_frequency,
    )
    return {"ok": True, "id": source_id}


@app.patch("/api/sources/{source_id}")
def update_source(source_id: int, patch: SourcePatch, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
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
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    source = repository.get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    strategy = source.get("parse_strategy")
    if strategy not in {"request", "playwright"}:
        raise HTTPException(status_code=400, detail="Скраппер доступен только для request/playwright-источников")
    if background:
        queue_name = "playwright" if strategy == "playwright" else "default"
        job = background_jobs.enqueue("scrape_source", {"source_id": source_id}, queue_name=queue_name)
        return {"ok": True, "job": _job_payload(job)}
    stats = playwright_parser.parse_source(source) if strategy == "playwright" else request_parser.parse_source(source)
    return {"ok": True, "stats": _clean(stats)}


@app.get("/api/sources/{source_id}/diagnose")
def diagnose_source_endpoint(
    source_id: int,
    limit: int = Query(5, ge=1, le=20),
    user: dict[str, Any] = Depends(require_user),
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
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    source = repository.get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    overrides = patch.model_dump(exclude_unset=True)
    return _clean(diagnose_source({**source, **overrides}, limit=limit))


@app.get("/api/tags")
def list_tags(user: dict[str, Any] = Depends(require_user)) -> list[dict[str, Any]]:
    return [_clean(row) for row in repository.list_enabled_tags()]


@app.put("/api/tags")
def save_tags(items: list[TagIn], user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    result = repository.save_tags([i.model_dump() for i in items])
    return {"ok": True, **result}


@app.delete("/api/tags/{tag_id}")
def delete_tag(tag_id: int, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    repository.delete_tag(tag_id)
    return {"ok": True}


@app.get("/api/scoring-criteria")
def list_scoring_criteria(user: dict[str, Any] = Depends(require_user)) -> list[dict[str, Any]]:
    return [_clean(row) for row in repository.list_enabled_scoring_criteria()]


@app.put("/api/scoring-criteria")
def save_scoring_criteria(items: list[ScoringCriterionIn], user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    try:
        result = repository.save_scoring_criteria([i.model_dump() for i in items])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, **result}


@app.delete("/api/scoring-criteria/{criterion_id}")
def delete_scoring_criterion(criterion_id: int, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
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
                   user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return _clean(build_digest_content(month=month, limit=limit, min_score=min_score))


@app.post("/api/monthly-digests")
def create_monthly_digest(payload: DigestRequest, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return _clean(save_digest_draft(month=payload.month, limit=payload.limit, min_score=payload.min_score))


@app.get("/api/monthly-digests/{month}")
def get_monthly_digest(month: str, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    digest = repository.get_monthly_digest(month)
    if digest is None:
        raise HTTPException(status_code=404, detail="Digest not found")
    return _clean(digest)


@app.get("/api/digest-email", response_class=HTMLResponse)
def digest_email(month: str = "", limit: int = Query(100, ge=1, le=500),
                 min_score: float = 0,
                 user: dict[str, Any] = Depends(require_user)) -> HTMLResponse:
    content = build_digest_content(month=month, limit=limit, min_score=min_score)
    return HTMLResponse(render_digest_email(content))


@app.get("/api/jobs")
def list_jobs(
    status: str | None = Query(None, pattern="^(queued|running|ok|failed)$"),
    kind: str | None = None,
    queue_name: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    user: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    return [
        _job_payload(row)
        for row in repository.list_background_jobs(status=status, kind=kind, queue_name=queue_name, limit=limit)
    ]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    job = repository.get_background_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_payload(job)


@app.get("/api/jobs/{job_id}/download")
def download_job_result(job_id: int, user: dict[str, Any] = Depends(require_user)) -> FileResponse:
    job = repository.get_background_job(job_id)
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
    if payload.export_format not in {"pdf", "doc", "html", "json"}:
        raise HTTPException(status_code=400, detail="Unsupported export format")
    if payload.limit < 1 or payload.limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    queue_name = "playwright" if payload.export_format == "pdf" else "default"
    job = background_jobs.enqueue("digest_export", payload.model_dump(), queue_name=queue_name)
    return {"ok": True, "job": _job_payload(job)}


@app.post("/api/jobs/process")
def enqueue_process_articles(payload: ProcessRequest, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    if payload.limit < 1 or payload.limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    job = background_jobs.enqueue("process_articles", payload.model_dump(), queue_name="ai")
    return {"ok": True, "job": _job_payload(job)}


@app.get("/api/digest-export")
def digest_export(
    month: str = "",
    export_format: str = Query("pdf", pattern="^(pdf|doc|html|json)$"),
    limit: int = Query(100, ge=1, le=500),
    min_score: float = 0,
    user: dict[str, Any] = Depends(require_user),
) -> FileResponse:
    job_id = repository.create_export_job("monthly_digest", export_format)
    try:
        result = write_digest_export(
            month=month,
            export_format=export_format,
            limit=limit,
            min_score=min_score,
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
        "raw_text_chars": len(row.get("raw_text") or ""),
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
