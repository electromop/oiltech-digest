"""HTTP API for the OilTech Digest admin frontend."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from psycopg.rows import dict_row
from psycopg.types.json import Json

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

WEB_DIR = REPO_ROOT / "web"

app = FastAPI(title="OilTech Digest API")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


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


class ProcessRequest(BaseModel):
    article_ids: list[int] | None = None
    limit: int = 5
    offline: bool = False


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "app.html")


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
    limit: int = Query(200, ge=1, le=1000),
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
    return [_article_payload(row) for row in rows]


@app.patch("/api/articles/{article_id}")
def update_article(article_id: int, patch: ArticlePatch) -> dict[str, Any]:
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
            (article_id, patch.status, patch.selected_for_digest, patch.analyst_comment),
        )
        conn.commit()
    return {"ok": True}


@app.get("/api/sources")
def list_sources(search: str | None = None, limit: int = Query(300, ge=1, le=1000)) -> list[dict[str, Any]]:
    return [_clean(row) for row in repository.list_sources(search=search, limit=limit)]


@app.post("/api/sources")
def create_source(payload: SourceCreate) -> dict[str, Any]:
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
def update_source(source_id: int, patch: SourcePatch) -> dict[str, Any]:
    updates = patch.model_dump(exclude_unset=True)
    if not updates:
        return {"ok": True}
    allowed = {"enabled", "url", "rss_url", "parse_strategy", "update_frequency"}
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


@app.get("/api/tags")
def list_tags() -> list[dict[str, Any]]:
    return [_clean(row) for row in repository.list_enabled_tags()]


@app.get("/api/scoring-criteria")
def list_scoring_criteria() -> list[dict[str, Any]]:
    return [_clean(row) for row in repository.list_enabled_scoring_criteria()]


@app.put("/api/scoring-criteria")
def save_scoring_criteria(items: list[ScoringCriterionIn]) -> dict[str, Any]:
    try:
        result = repository.save_scoring_criteria([i.model_dump() for i in items])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, **result}


@app.delete("/api/scoring-criteria/{criterion_id}")
def delete_scoring_criterion(criterion_id: int) -> dict[str, Any]:
    repository.delete_scoring_criterion(criterion_id)
    return {"ok": True}


@app.get("/api/reports/ai-cost")
def ai_cost() -> list[dict[str, Any]]:
    return [_clean(row) for row in repository.ai_cost_report()]


@app.get("/api/reports/ai-article-cost")
def ai_article_cost(limit: int = Query(20, ge=1, le=200), include_partial: bool = False) -> list[dict[str, Any]]:
    return [_clean(row) for row in repository.ai_article_cost_report(limit=limit, complete_only=not include_partial)]


@app.post("/api/process")
def process_articles(payload: ProcessRequest) -> dict[str, Any]:
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
        "summary": row.get("summary") or "",
        "tag": tag,
        "score": float(row["total_score"]) if row.get("total_score") is not None else 0,
        "rating": row.get("score_label") or "Без оценки",
        "status": row.get("status") or "new",
        "digest": bool(row.get("selected_for_digest")),
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

