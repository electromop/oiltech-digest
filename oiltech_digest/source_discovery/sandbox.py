"""Sandbox processing for source candidates.

Candidate articles live outside the main `articles` table. This lets us test
new sources with the real AI pipeline without polluting the production feed.
"""

from __future__ import annotations

import time
from typing import Any

from oiltech_digest import config
from oiltech_digest.db import repository
from oiltech_digest.ingestion import request_parser
from oiltech_digest.ingestion.relevance_filter import should_keep_article
from oiltech_digest.ingestion.source_diagnostics import probe_url
from oiltech_digest.processing import pipeline
from oiltech_digest.source_discovery.agent import (
    recommend_source_action,
    _name_from_domain,
    _status_for_recommendation,
)
from oiltech_digest.source_discovery.learning import apply_candidate_learning


def evaluate_source_candidate(
    candidate_id: int,
    *,
    article_limit: int = 5,
    offline: bool = True,
    collect: bool = True,
    process: bool = True,
) -> dict[str, Any]:
    started = time.monotonic()
    candidate = repository.get_source_candidate(candidate_id)
    if candidate is None:
        raise ValueError(f"source candidate id={candidate_id} not found")

    task_id = repository.create_agent_task(
        "evaluate_source_candidate",
        topic=candidate.get("topic"),
        payload={
            "candidate_id": candidate_id,
            "url": candidate["url"],
            "article_limit": article_limit,
            "offline": offline,
            "collect": collect,
            "process": process,
        },
        budget={"max_articles": article_limit, "writes_only_to_sandbox": True},
        status="running",
    )

    collected = collect_candidate_articles(candidate, article_limit=article_limit) if collect else {
        "inserted_or_updated": 0,
        "errors": 0,
        "articles": [],
    }
    processed = process_candidate_articles(candidate_id, limit=article_limit, offline=offline) if process else {
        "processed": 0,
        "relevant": 0,
        "rejected": 0,
        "errors": 0,
    }
    metrics = repository.source_candidate_article_metrics(candidate_id)
    evidence = repository.list_source_candidate_articles(candidate_id, limit=article_limit)
    recommendation = recommend_source_action(metrics, offline=offline, evidence=evidence)
    next_status = _status_for_recommendation(recommendation["recommended_action"])
    repository.update_source_candidate_assessment(
        candidate_id,
        status=next_status,
        tested_articles=metrics["tested_articles"],
        relevant_articles=metrics["relevant_articles"],
        avg_score=metrics["avg_score"],
        duplicate_count=metrics["duplicate_count"],
        noise_count=metrics["noise_count"],
        recommended_action=recommendation["recommended_action"],
        review_comment=recommendation["reason"],
    )
    learning = None
    if recommendation["recommended_action"] in {"add", "test_more", "reject"}:
        learning = apply_candidate_learning(
            candidate_id,
            event_type="evaluated",
            status=next_status,
            recommended_action=recommendation["recommended_action"],
            review_comment=recommendation["reason"],
            metrics=metrics,
        )
    result = {
        "candidate_id": candidate_id,
        "task_id": task_id,
        "url": candidate["url"],
        "collected": collected,
        "processed": processed,
        "metrics": metrics,
        "recommended_action": recommendation["recommended_action"],
        "next_status": next_status,
        "review_comment": recommendation["reason"],
        "learning": learning,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    repository.record_agent_action(
        task_id,
        "evaluate_source_candidate_finished",
        input_payload={"candidate_id": candidate_id},
        output_payload=result,
        duration_ms=result["duration_ms"],
    )
    return result


def collect_candidate_articles(candidate: dict, *, article_limit: int = 5) -> dict[str, Any]:
    source = _candidate_as_source(candidate)
    listing_probe, listing_content = probe_url(str(candidate["url"]))
    result: dict[str, Any] = {
        "listing_probe": {
            "status": listing_probe.status,
            "bytes": listing_probe.bytes,
            "seconds": listing_probe.seconds,
            "error": listing_probe.error,
        },
        "inserted_or_updated": 0,
        "errors": 0,
        "articles": [],
    }
    if listing_content is None:
        result["errors"] = 1
        result["error"] = "listing_fetch_failed"
        return result

    links = request_parser.extract_candidate_links(
        source,
        str(candidate["url"]),
        listing_content,
        limit=article_limit,
    )
    seen: set[str] = set()
    for link in links[:article_limit]:
        if link.url in seen:
            continue
        seen.add(link.url)
        article_probe, article_content = probe_url(link.url)
        if article_content is None:
            result["errors"] += 1
            result["articles"].append({
                "url": link.url,
                "title": link.title,
                "status": "fetch_failed",
                "error": article_probe.error,
            })
            continue
        title, published_at, raw_text = request_parser.parse_article_page(article_content, link.title)
        pre_filter = should_keep_article(title, raw_text, source)
        article_id = repository.upsert_source_candidate_article(
            int(candidate["id"]),
            {
                "title": title,
                "url": link.url,
                "published_at": published_at,
                "raw_text": raw_text,
                "language": "unknown",
                "prefilter_keep": pre_filter.keep and len(raw_text or "") >= config.MIN_ARTICLE_TEXT_CHARS,
                "prefilter_reason": pre_filter.reason,
            },
        )
        result["inserted_or_updated"] += 1
        result["articles"].append({
            "id": article_id,
            "url": link.url,
            "title": title,
            "text_chars": len(raw_text or ""),
            "prefilter_keep": pre_filter.keep,
            "prefilter_reason": pre_filter.reason,
        })
    return result


def process_candidate_articles(candidate_id: int, *, limit: int = 5, offline: bool = True) -> dict[str, int]:
    client = pipeline.make_client(offline)
    tags = repository.list_enabled_tags()
    if not tags:
        raise ValueError("Нет активных тегов. Запустите seed-tags.")
    criteria = repository.list_enabled_scoring_criteria()
    pipeline._validate_weights(criteria)
    stats = {"processed": 0, "relevant": 0, "rejected": 0, "errors": 0}
    articles = repository.list_source_candidate_articles(candidate_id, limit=limit, only_unprocessed=True)
    for article in articles:
        stats["processed"] += 1
        try:
            blocked_reason = pipeline._negative_keyword_block(article, tags)
            if blocked_reason:
                _save_rejected(article, blocked_reason, "negative-keyword")
                stats["rejected"] += 1
                continue

            rel_resp = pipeline.relevance_article(article, client)
            relevant = bool(rel_resp.data.get("relevant"))
            if not relevant:
                _save_rejected(article, rel_resp.data.get("reason"), rel_resp.model)
                stats["rejected"] += 1
                continue

            summary_resp = pipeline.summarize_article(article, client)
            article = {**article, "summary": summary_resp.data.get("summary")}
            title_ru, translate_resp = pipeline.title_ru_for_article(article, client)
            tag_resp = pipeline.tag_article(article, tags, client)
            tag_id = pipeline._valid_tag_id(tag_resp.data.get("tag_id"), tags)
            if tag_id == 0:
                tag_id = pipeline.keyword_tag(article, tags)["tag_id"]
            score_resp = pipeline.score_article(article, criteria, client)
            score_payload = pipeline.normalize_score_payload(article, criteria, score_resp.data)
            repository.update_source_candidate_article_result(
                int(article["id"]),
                {
                    "relevant": True,
                    "relevance_reason": rel_resp.data.get("reason"),
                    "relevance_model": rel_resp.model,
                    "summary": summary_resp.data.get("summary"),
                    "summary_model": summary_resp.model,
                    "title_ru": title_ru,
                    "tag_id": tag_id,
                    "tag_confidence": tag_resp.data.get("confidence"),
                    "tag_rationale": tag_resp.data.get("rationale"),
                    "tag_model": tag_resp.model,
                    "total_score": score_payload["total_score"],
                    "score_label": score_payload["score_label"],
                    "score_explanation": score_payload["explanation"],
                    "score_items": score_payload["items"],
                    "score_model": score_resp.model,
                    "processing_status": "ok",
                    "error_message": None,
                },
            )
            if translate_resp is not None:
                article["title_ru"] = title_ru
            stats["relevant"] += 1
        except Exception as exc:  # noqa: BLE001 - keep batch running
            repository.update_source_candidate_article_result(
                int(article["id"]),
                _error_payload(str(exc)),
            )
            stats["errors"] += 1
    return stats


def _candidate_as_source(candidate: dict) -> dict:
    domain = repository.normalize_domain(str(candidate["url"]))
    return {
        "id": int(candidate["id"]),
        "name": candidate.get("name") or _name_from_domain(domain),
        "url": candidate["url"],
        "listing_url": candidate["url"],
        "category": candidate.get("topic") or "source-discovery",
        "source_type": candidate.get("candidate_type") or "candidate",
    }


def _save_rejected(article: dict, reason: str | None, model: str | None) -> None:
    repository.update_source_candidate_article_result(
        int(article["id"]),
        {
            "relevant": False,
            "relevance_reason": reason,
            "relevance_model": model,
            "summary": article.get("summary"),
            "summary_model": None,
            "title_ru": article.get("title_ru"),
            "tag_id": None,
            "tag_confidence": None,
            "tag_rationale": None,
            "tag_model": None,
            "total_score": None,
            "score_label": None,
            "score_explanation": None,
            "score_items": [],
            "score_model": None,
            "processing_status": "rejected",
            "error_message": None,
        },
    )


def _error_payload(error: str) -> dict:
    return {
        "relevant": None,
        "relevance_reason": None,
        "relevance_model": None,
        "summary": None,
        "summary_model": None,
        "title_ru": None,
        "tag_id": None,
        "tag_confidence": None,
        "tag_rationale": None,
        "tag_model": None,
        "total_score": None,
        "score_label": None,
        "score_explanation": None,
        "score_items": [],
        "score_model": None,
        "processing_status": "error",
        "error_message": error[:1000],
    }
