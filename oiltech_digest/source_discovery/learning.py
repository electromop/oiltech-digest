"""Learning helpers for source discovery feedback.

The agent learns lightweight policy signals in `agent_memory`. This is not model
fine-tuning: it updates scores for domains, topics, search queries and query
strategies based on operator decisions and sandbox results.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any

from oiltech_digest.db import repository


def apply_candidate_learning(
    candidate_id: int,
    *,
    event_type: str,
    status: str | None = None,
    recommended_action: str | None = None,
    review_comment: str | None = None,
    source_id: int | None = None,
    metrics: dict[str, Any] | None = None,
    run_id: int | None = None,
) -> dict[str, Any]:
    candidate = repository.get_source_candidate(candidate_id)
    if candidate is None:
        raise ValueError(f"source candidate id={candidate_id} not found")

    origin = _candidate_origin(candidate_id)
    domain = repository.normalize_domain(str(candidate.get("url") or ""))
    topic = str(candidate.get("topic") or "").strip()
    action = recommended_action or str(candidate.get("recommended_action") or "")
    candidate_status = status or str(candidate.get("status") or "")
    comment = review_comment if review_comment is not None else str(candidate.get("review_comment") or "")
    metrics = _normalize_metrics(metrics or _candidate_metrics(candidate))
    decision = _learning_decision(event_type, candidate_status, action, comment, metrics)

    updates = []
    if domain:
        updates.append(_upsert_memory_delta(
            memory_type="domain",
            subject=domain,
            delta=decision["domain_delta"],
            status=decision["domain_status"],
            facts={
                "candidate_id": candidate_id,
                "source_id": source_id,
                "event_type": event_type,
                "decision_kind": decision["kind"],
                "last_reason": decision["reason"],
                "topic": topic,
                "metrics": metrics,
            },
        ))
    if topic and decision["topic_delta"]:
        updates.append(_upsert_memory_delta(
            memory_type="topic",
            subject=topic,
            delta=decision["topic_delta"],
            status="active" if decision["topic_delta"] >= 0 else "muted",
            facts={
                "candidate_id": candidate_id,
                "event_type": event_type,
                "decision_kind": decision["kind"],
                "last_reason": decision["reason"],
                "metrics": metrics,
            },
        ))

    query = str(origin.get("query") or "").strip()
    if query:
        updates.append(_upsert_memory_delta(
            memory_type="query",
            subject=query,
            delta=decision["query_delta"],
            status="active" if decision["query_delta"] >= 0 else "muted",
            facts={
                "candidate_id": candidate_id,
                "event_type": event_type,
                "decision_kind": decision["kind"],
                "last_reason": decision["reason"],
                "topic": topic,
                "metrics": metrics,
            },
            key_extra=topic,
        ))

    strategy = str(origin.get("query_strategy") or "").strip().lower()
    if strategy:
        updates.append(_upsert_memory_delta(
            memory_type="strategy",
            subject=strategy,
            delta=decision["strategy_delta"],
            status="active" if decision["strategy_delta"] >= 0 else "muted",
            facts={
                "candidate_id": candidate_id,
                "event_type": event_type,
                "decision_kind": decision["kind"],
                "last_reason": decision["reason"],
                "topic": topic,
                "strategy": strategy,
                "metrics": metrics,
            },
            key_extra=topic,
        ))

    if topic and query and domain:
        combo_subject = f"{topic} | {query} | {domain}"
        updates.append(_upsert_memory_delta(
            memory_type="topic_query_domain",
            subject=combo_subject,
            delta=decision["combo_delta"],
            status="active" if decision["combo_delta"] >= 0 else "muted",
            facts={
                "candidate_id": candidate_id,
                "source_id": source_id,
                "event_type": event_type,
                "decision_kind": decision["kind"],
                "last_reason": decision["reason"],
                "topic": topic,
                "query": query,
                "domain": domain,
                "query_strategy": strategy,
                "metrics": metrics,
                "quality_funnel": _quality_funnel(metrics),
            },
            key_extra=f"{topic}|{query}|{domain}",
        ))

    result = {
        "candidate_id": candidate_id,
        "source_id": source_id,
        "event_type": event_type,
        "status": candidate_status,
        "recommended_action": action,
        "topic": topic,
        "domain": domain,
        "query": query,
        "query_strategy": strategy,
        "decision_kind": decision["kind"],
        "reason": decision["reason"],
        "quality_funnel": _quality_funnel(metrics),
        "score": sum(float(item.get("score") or 0) for item in updates),
        "updates": updates,
    }
    repository.record_agent_action(
        None,
        "source_candidate_learning",
        run_id=run_id,
        input_payload={"candidate_id": candidate_id, "event_type": event_type},
        output_payload=result,
    )
    return result


def _learning_decision(
    event_type: str,
    status: str,
    recommended_action: str,
    review_comment: str,
    metrics: dict[str, Any],
) -> dict[str, Any]:
    text = f"{status} {recommended_action} {review_comment}".lower()
    relevant = int(metrics.get("relevant_articles") or 0)
    tested = int(metrics.get("tested_articles") or 0)
    avg_score = float(metrics.get("avg_score") or 0)
    high_score = int(metrics.get("high_score_articles") or 0)
    if event_type == "approved" or status == "approved":
        return _decision("approved", "Оператор одобрил источник.", 30, 18, 24, 18, 30, "active")
    if "duplicate" in text or "дублик" in text:
        return _decision("duplicate", "Оператор отклонил кандидата как дубль.", -12, 0, -10, -6, -12, "muted")
    if "noise" in text or "шум" in text:
        return _decision("noise", "Оператор отклонил кандидата как шум.", -25, -8, -20, -10, -25, "muted")
    if status == "rejected" or recommended_action == "reject":
        return _decision("rejected", "Кандидат отклонен.", -18, -5, -14, -8, -18, "muted")
    if event_type == "evaluated" and (relevant > 0 or high_score > 0) and recommended_action in {"add", "test_more"}:
        strength = 22 if recommended_action == "add" or high_score >= 2 or avg_score >= 65 else 12
        return _decision("sandbox_positive", "Песочница нашла полезные материалы.", strength, 8, strength, 8, strength, "active")
    if event_type == "evaluated" and tested > 0 and relevant == 0:
        return _decision("sandbox_negative", "Песочница не нашла релевантных материалов.", -12, -4, -10, -5, -12, "muted")
    return _decision("neutral", "Недостаточно сигнала для сильного обучения.", 0, 0, 0, 0, 0, "active")


def _decision(
    kind: str,
    reason: str,
    domain_delta: float,
    topic_delta: float,
    query_delta: float,
    strategy_delta: float,
    combo_delta: float,
    domain_status: str,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "reason": reason,
        "domain_delta": domain_delta,
        "topic_delta": topic_delta,
        "query_delta": query_delta,
        "strategy_delta": strategy_delta,
        "combo_delta": combo_delta,
        "domain_status": domain_status,
    }


def _upsert_memory_delta(
    *,
    memory_type: str,
    subject: str,
    delta: float,
    status: str,
    facts: dict[str, Any],
    key_extra: str = "",
) -> dict[str, Any]:
    memory_key = _memory_key(memory_type, subject, key_extra)
    existing = _memory_by_key(memory_key)
    existing_facts = existing.get("facts_json") if existing else {}
    if not isinstance(existing_facts, dict):
        existing_facts = {}
    previous_score = float(existing.get("score") or 0) if existing else 0.0
    score = round(max(-100.0, min(100.0, previous_score + float(delta))), 2)
    events = list(existing_facts.get("events") or [])[-9:]
    events.append({
        "at": datetime.now(timezone.utc).isoformat(),
        "delta": delta,
        "status": status,
        "event_type": facts.get("event_type"),
        "candidate_id": facts.get("candidate_id"),
        "decision_kind": facts.get("decision_kind"),
    })
    merged_facts = {
        **existing_facts,
        **facts,
        "score_delta": delta,
        "previous_score": previous_score,
        "events": events,
    }
    memory_id = repository.upsert_agent_memory(
        memory_key=memory_key,
        memory_type=memory_type,
        subject=subject,
        status=status,
        score=score,
        facts=merged_facts,
    )
    return {
        "memory_id": memory_id,
        "memory_key": memory_key,
        "memory_type": memory_type,
        "subject": subject,
        "status": status,
        "score": score,
        "delta": delta,
    }


def _memory_by_key(memory_key: str) -> dict[str, Any] | None:
    try:
        rows = repository.list_agent_memory(status=None, limit=1000)
    except Exception:  # noqa: BLE001 - learning should still write new memory if read fails
        return None
    for row in rows:
        if str(row.get("memory_key") or "") == memory_key:
            return row
    return None


def _candidate_origin(candidate_id: int) -> dict[str, Any]:
    try:
        rows = repository.list_agent_actions(action_type="create_source_candidate", limit=500)
    except Exception:  # noqa: BLE001 - origin is optional learning context
        return {}
    for row in rows:
        output = row.get("output_json") or {}
        if int(output.get("candidate_id") or 0) != int(candidate_id):
            continue
        return {
            "query": output.get("discovery_query") or output.get("query"),
            "query_strategy": output.get("query_strategy"),
            "topic": output.get("topic"),
        }
    return {}


def _candidate_metrics(candidate: dict[str, Any]) -> dict[str, Any]:
    return _normalize_metrics({
        "tested_articles": int(candidate.get("tested_articles") or 0),
        "relevant_articles": int(candidate.get("relevant_articles") or 0),
        "avg_score": float(candidate["avg_score"]) if candidate.get("avg_score") is not None else None,
        "duplicate_count": int(candidate.get("duplicate_count") or 0),
        "noise_count": int(candidate.get("noise_count") or 0),
    })


def _normalize_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    tested = int(metrics.get("tested_articles") or 0)
    relevant = int(metrics.get("relevant_articles") or 0)
    scored = int(metrics.get("scored_articles") or 0)
    high_score = int(metrics.get("high_score_articles") or 0)
    parsed = int(metrics.get("parsed_articles") or tested)
    processed = int(metrics.get("processed_articles") or max(relevant, scored))
    kept = int(metrics.get("kept_by_prefilter") or 0)
    noise = int(metrics.get("noise_count") or 0)
    duplicate = int(metrics.get("duplicate_count") or 0)
    avg_score = metrics.get("avg_score")
    return {
        "tested_articles": tested,
        "parsed_articles": parsed,
        "processed_articles": processed,
        "kept_by_prefilter": kept,
        "relevant_articles": relevant,
        "scored_articles": scored,
        "high_score_articles": high_score,
        "avg_score": round(float(avg_score), 2) if avg_score is not None else None,
        "duplicate_count": duplicate,
        "noise_count": noise,
        "parse_rate": _rate(parsed, tested),
        "process_rate": _rate(processed, tested),
        "relevance_rate": _rate(relevant, max(processed, tested)),
        "high_score_rate": _rate(high_score, max(scored, tested)),
        "noise_rate": _rate(noise, tested),
        "duplicate_rate": _rate(duplicate, tested),
    }


def _quality_funnel(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "found": int(metrics.get("tested_articles") or 0),
        "parsed": int(metrics.get("parsed_articles") or 0),
        "prefilter_kept": int(metrics.get("kept_by_prefilter") or 0),
        "processed": int(metrics.get("processed_articles") or 0),
        "relevant": int(metrics.get("relevant_articles") or 0),
        "scored": int(metrics.get("scored_articles") or 0),
        "score_50_plus": int(metrics.get("high_score_articles") or 0),
        "noise": int(metrics.get("noise_count") or 0),
        "duplicates": int(metrics.get("duplicate_count") or 0),
    }


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(max(0.0, min(1.0, numerator / denominator)), 4)


def _memory_key(memory_type: str, subject: str, key_extra: str = "") -> str:
    raw = f"{memory_type}:{key_extra.strip().lower()}:{subject.strip().lower()}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    if memory_type == "domain":
        return f"domain:{subject.strip().lower()}"
    return f"{memory_type}:{digest}"
