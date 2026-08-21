"""Deterministic planner for the source-discovery agent.

The planner turns database signals into concrete next actions. It deliberately
does not call an LLM: the first production version must be inspectable and
cheap enough to run on every scheduler cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import time
from typing import Any

from oiltech_digest.db import repository


@dataclass(frozen=True)
class PlannerConfig:
    days: int = 30
    target_per_topic: int = 10
    topic_limit: int = 5
    candidate_limit: int = 10
    max_actions: int = 5
    persist_memory: bool = True
    record_action: bool = True
    run_id: int | None = None


def build_plan(config: PlannerConfig | None = None) -> dict[str, Any]:
    """Build a source-discovery plan from topic gaps, feedback and candidate history."""
    config = config or PlannerConfig()
    started = time.monotonic()
    period_to = datetime.now(timezone.utc)
    period_from = period_to - timedelta(days=config.days)

    topic_gaps = repository.compute_topic_gap_rows(
        period_from,
        period_to,
        target_per_topic=config.target_per_topic,
        limit=max(config.topic_limit * 2, config.topic_limit),
    )
    source_quality = repository.compute_source_quality_rows(period_from, period_to)
    candidates = repository.list_source_candidates(limit=200)
    candidate_triage = _load_candidate_triage(candidates, limit=max(50, config.max_actions * 4))
    topic_memory = repository.list_agent_memory(memory_type="topic", status="active", limit=100)
    rejected_topic_memory = repository.list_agent_memory(memory_type="topic", status="rejected", limit=100)
    query_memory = repository.list_agent_memory(memory_type="query", status="active", limit=100)

    actions = _topic_discovery_actions(topic_gaps, candidates, topic_memory, query_memory, config, rejected_topic_memory)
    actions += _candidate_review_actions(candidate_triage, config)
    actions += _source_recheck_actions(source_quality, config)
    actions += _source_frequency_actions(source_quality, config)
    actions = sorted(actions, key=lambda item: item["priority"], reverse=True)[:config.max_actions]
    actions = [_apply_action_policy(action) for action in actions]
    policy = _policy_summary(actions)
    learning = _learning_summary(candidates)

    memory_updates = _memory_updates(topic_gaps, source_quality, candidates, actions)
    if config.persist_memory:
        for item in memory_updates:
            repository.upsert_agent_memory(
                memory_key=item["memory_key"],
                memory_type=item["memory_type"],
                subject=item["subject"],
                status=item.get("status") or "active",
                score=float(item.get("score") or 0),
                facts=item.get("facts") or {},
            )

    result = {
        "kind": "source_discovery_plan",
        "period": {"from": period_from.isoformat(), "to": period_to.isoformat(), "days": config.days},
        "inputs": {
            "topic_gaps": topic_gaps[: config.topic_limit],
            "source_quality_count": len(source_quality),
            "candidate_count": len(candidates),
            "candidate_triage_count": len(candidate_triage),
            "memory_count": len(topic_memory) + len(query_memory),
            "query_memory_count": len(query_memory),
        },
        "policy": policy,
        "learning": learning,
        "actions": actions,
        "memory_updates": memory_updates,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    if config.record_action:
        repository.record_agent_action(
            None,
            "source_discovery_plan_built",
            run_id=config.run_id,
            input_payload={
                "days": config.days,
                "target_per_topic": config.target_per_topic,
                "topic_limit": config.topic_limit,
                "candidate_limit": config.candidate_limit,
                "max_actions": config.max_actions,
            },
            output_payload=result,
            duration_ms=result["duration_ms"],
        )
    return result


def _load_candidate_triage(candidates: list[dict], *, limit: int) -> list[dict]:
    try:
        return repository.source_candidate_triage_report(limit=limit)
    except Exception:  # noqa: BLE001 - fallback keeps planning available during partial migrations
        return candidates


def enqueue_plan_actions(
    plan: dict[str, Any],
    *,
    offline: bool = True,
    evaluate: bool = True,
    run_id: int | None = None,
) -> dict[str, Any]:
    """Turn plan actions into background jobs that existing workers can execute."""
    jobs: list[dict[str, Any]] = []
    for action in plan.get("actions") or []:
        if action.get("policy_decision") != "auto":
            continue
        if action.get("action_type") != "discover_sources":
            continue
        job = repository.create_background_job(
            "discover_source_candidates",
            {
                "topics": [action["topic"]],
                "topic_limit": 1,
                "limit": int(action.get("limit") or 10),
                "offline": offline,
                "fetch_inspection": False,
                "auto_evaluate": evaluate,
                "article_limit": 5,
                "planner_reason": action.get("reason"),
                "agent_run_id": run_id,
            },
            queue_name="default",
            execution_region="ru",
            capability="source-discovery",
            max_attempts=1,
            agent_run_id=run_id,
        )
        jobs.append({
            "job_id": int(job["id"]),
            "kind": "discover_source_candidates",
            "topic": action["topic"],
            "priority": action["priority"],
        })
    return {"queued": len(jobs), "jobs": jobs}


def _topic_discovery_actions(
    topic_gaps: list[dict],
    candidates: list[dict],
    topic_memory: list[dict],
    query_memory: list[dict],
    config: PlannerConfig,
    rejected_topic_memory: list[dict] | None = None,
) -> list[dict[str, Any]]:
    seen_topics = {str(row.get("topic") or "").strip().lower() for row in candidates if row.get("topic")}
    memory_by_subject = {str(row.get("subject") or "").strip().lower(): row for row in topic_memory}
    rejected_topics = {str(row.get("subject") or "").strip().lower() for row in rejected_topic_memory or [] if row.get("subject")}
    query_hints_by_topic = _query_hints_by_topic(query_memory)
    feedback_by_topic = _candidate_feedback_by_topic(candidates)
    actions = []
    for row in topic_gaps[: config.topic_limit]:
        topic = str(row.get("topic") or "").strip()
        if not topic:
            continue
        topic_key = topic.lower()
        if topic_key in rejected_topics:
            continue
        gap = int(row.get("gap") or 0)
        if gap <= 0 and topic_key in seen_topics:
            continue
        feedback = feedback_by_topic.get(topic_key, {})
        memory_boost = float(memory_by_subject.get(topic_key, {}).get("score") or 0) * 0.1
        feedback_boost = _topic_feedback_boost(feedback)
        priority = min(100.0, max(0.0, float(row.get("priority") or 0) + memory_boost + feedback_boost))
        if topic_key not in seen_topics:
            priority = min(100.0, priority + 10)
        actions.append({
            "action_type": "discover_sources",
            "topic": topic,
            "priority": round(priority, 2),
            "limit": config.candidate_limit,
            "query_hints": query_hints_by_topic.get(topic_key, []),
            "reason": _topic_reason(row, topic_key in seen_topics, feedback),
        })
    return actions


def _candidate_review_actions(candidates: list[dict], config: PlannerConfig) -> list[dict[str, Any]]:
    actions = []
    for row in candidates:
        status = str(row.get("status") or "")
        recommended = str(row.get("recommended_action") or "")
        if status in {"approved", "rejected"} or recommended not in {"add", "test_more", "human_review"}:
            continue
        priority = float(row.get("triage_priority") or (80 if recommended == "add" else 55))
        actions.append({
            "action_type": "review_source_candidate",
            "candidate_id": int(row["id"]),
            "topic": row.get("topic"),
            "url": row.get("url"),
            "status": status,
            "priority": round(min(100.0, max(0.0, priority)), 2),
            "reason": (
                f"{row.get('triage_reason') or 'Кандидат ждёт решения оператора'}: "
                f"relevant={row.get('relevant_articles')}, "
                f"avg_score={row.get('avg_score') or '-'}, recommendation={recommended}."
            ),
        })
        if len(actions) >= config.max_actions:
            break
    return actions


def _source_recheck_actions(source_quality: list[dict], config: PlannerConfig) -> list[dict[str, Any]]:
    actions = []
    for row in source_quality:
        found = int(row.get("articles_found") or 0)
        quality = float(row.get("quality_score") or 0)
        noise = int(row.get("noise_count") or 0)
        duplicates = int(row.get("duplicate_count") or 0)
        if found < 5 or quality >= 20 or (noise + duplicates) == 0:
            continue
        actions.append({
            "action_type": "recheck_source",
            "source_id": int(row["source_id"]),
            "source_name": row.get("source_name"),
            "priority": round(min(70, 70 - quality), 2),
            "reason": (
                f"Источник дает слабое качество: quality={quality}, "
                f"noise={noise}, duplicates={duplicates}, articles={found}."
            ),
        })
        if len(actions) >= config.max_actions:
            break
    return actions


def _source_frequency_actions(source_quality: list[dict], config: PlannerConfig) -> list[dict[str, Any]]:
    actions = []
    for row in source_quality:
        found = int(row.get("articles_found") or 0)
        relevant = int(row.get("relevant_count") or 0)
        digest = int(row.get("digest_count") or 0)
        noise = int(row.get("noise_count") or 0)
        quality = float(row.get("quality_score") or 0)
        if found < 8:
            continue

        direction = None
        recommended_frequency = None
        priority = 0.0
        reason = ""
        if quality >= 65 and relevant >= max(4, found // 3):
            direction = "increase"
            recommended_frequency = "чаще"
            priority = min(68.0, 42 + quality * 0.35 + digest * 2)
            reason = (
                f"Источник стабильно полезен: quality={quality}, "
                f"relevant={relevant}/{found}, digest={digest}. Стоит проверять чаще."
            )
        elif quality <= 15 and noise >= max(3, found // 3):
            direction = "decrease"
            recommended_frequency = "реже"
            priority = min(58.0, 38 + noise * 2)
            reason = (
                f"Источник даёт много шума: quality={quality}, "
                f"noise={noise}/{found}, relevant={relevant}. Стоит проверять реже или поставить на паузу."
            )
        if not direction:
            continue

        actions.append({
            "action_type": "tune_source_frequency",
            "source_id": int(row["source_id"]),
            "source_name": row.get("source_name"),
            "priority": round(priority, 2),
            "direction": direction,
            "recommended_frequency": recommended_frequency,
            "reason": reason,
        })
        if len(actions) >= config.max_actions:
            break
    return actions


def _memory_updates(
    topic_gaps: list[dict],
    source_quality: list[dict],
    candidates: list[dict],
    actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = []
    action_topics = {str(item.get("topic") or "").strip().lower() for item in actions if item.get("topic")}
    for row in topic_gaps:
        topic = str(row.get("topic") or "").strip()
        if not topic:
            continue
        score = float(row.get("priority") or 0)
        if topic.lower() in action_topics:
            score = min(100.0, score + 10)
        updates.append({
            "memory_key": f"topic:{topic.lower()}",
            "memory_type": "topic",
            "subject": topic,
            "score": round(score, 2),
            "facts": {
                "signals": int(row.get("signals") or 0),
                "target_signals": int(row.get("target_signals") or 0),
                "gap": int(row.get("gap") or 0),
                "avg_score": _float_or_none(row.get("avg_score")),
                "digest_count": int(row.get("digest_count") or 0),
            },
        })
    for row in source_quality:
        domain_subject = str(row.get("source_name") or row.get("source_id"))
        updates.append({
            "memory_key": f"source:{row['source_id']}",
            "memory_type": "source",
            "subject": domain_subject,
            "score": float(row.get("quality_score") or 0),
            "facts": {
                "articles_found": int(row.get("articles_found") or 0),
                "relevant_count": int(row.get("relevant_count") or 0),
                "noise_count": int(row.get("noise_count") or 0),
                "duplicate_count": int(row.get("duplicate_count") or 0),
                "avg_score": _float_or_none(row.get("avg_score")),
            },
        })
    for row in candidates:
        domain = repository.normalize_domain(str(row.get("url") or ""))
        if not domain:
            continue
        updates.append({
            "memory_key": f"domain:{domain}",
            "memory_type": "domain",
            "subject": domain,
            "status": _candidate_memory_status(row),
            "score": _candidate_memory_score(row),
            "facts": {
                "candidate_id": int(row["id"]),
                "status": row.get("status"),
                "recommended_action": row.get("recommended_action"),
                "tested_articles": int(row.get("tested_articles") or 0),
                "relevant_articles": int(row.get("relevant_articles") or 0),
                "avg_score": _float_or_none(row.get("avg_score")),
                "operator_decision": _candidate_operator_decision(row),
            },
        })
    return updates


def _topic_reason(row: dict, has_existing_candidate: bool, feedback: dict[str, int]) -> str:
    prefix = "Тема уже исследовалась, но дефицит сохраняется" if has_existing_candidate else "По теме еще нет кандидатов"
    reason = (
        f"{prefix}: signals={int(row.get('signals') or 0)}, "
        f"target={int(row.get('target_signals') or 0)}, gap={int(row.get('gap') or 0)}, "
        f"avg_score={row.get('avg_score') or '-'}."
    )
    if feedback:
        reason += (
            f" Обратная связь: approved={feedback.get('approved', 0)}, "
            f"rejected={feedback.get('rejected', 0)}, paused={feedback.get('paused', 0)}."
        )
    return reason


def _query_hints_by_topic(query_memory: list[dict]) -> dict[str, list[str]]:
    hints: dict[str, list[str]] = {}
    for row in query_memory:
        facts = row.get("facts_json") or {}
        topic = str(facts.get("topic") or "").strip().lower()
        query = str(row.get("subject") or "").strip()
        if not topic or not query:
            continue
        topic_hints = hints.setdefault(topic, [])
        if query.lower() in {item.lower() for item in topic_hints}:
            continue
        topic_hints.append(query)
        if len(topic_hints) > 3:
            del topic_hints[3:]
    return hints


def _candidate_memory_score(row: dict) -> float:
    tested = max(int(row.get("tested_articles") or 0), 1)
    relevant_rate = int(row.get("relevant_articles") or 0) / tested
    avg_score = float(row.get("avg_score") or 0) / 100
    noise_rate = int(row.get("noise_count") or 0) / tested
    duplicate_rate = int(row.get("duplicate_count") or 0) / tested
    score = relevant_rate * 55 + avg_score * 35 - noise_rate * 10 - duplicate_rate * 10
    status = str(row.get("status") or "")
    recommended = str(row.get("recommended_action") or "")
    if status == "approved":
        score += 25
    elif status == "rejected":
        score -= 45
    elif status == "paused":
        score -= 12
    elif status == "needs_human_review" and recommended == "add":
        score += 8
    elif recommended == "reject":
        score -= 25
    elif recommended == "test_more":
        score -= 5
    return round(max(0.0, min(100.0, score)), 2)


def _candidate_memory_status(row: dict) -> str:
    status = str(row.get("status") or "")
    if status == "rejected":
        return "rejected"
    if status == "paused":
        return "muted"
    return "active"


def _candidate_operator_decision(row: dict) -> str:
    status = str(row.get("status") or "")
    recommended = str(row.get("recommended_action") or "")
    if status == "approved":
        return "approved"
    if status == "rejected" or recommended == "reject":
        return "rejected"
    if status == "paused":
        return "paused"
    if recommended == "test_more":
        return "test_more"
    if status == "needs_human_review":
        return "needs_human_review"
    return "none"


def _candidate_feedback_by_topic(candidates: list[dict]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for row in candidates:
        topic = str(row.get("topic") or "").strip().lower()
        if not topic:
            continue
        bucket = result.setdefault(topic, {"approved": 0, "rejected": 0, "paused": 0, "test_more": 0})
        status = str(row.get("status") or "")
        recommended = str(row.get("recommended_action") or "")
        if status == "approved":
            bucket["approved"] += 1
        elif status == "rejected" or recommended == "reject":
            bucket["rejected"] += 1
        elif status == "paused":
            bucket["paused"] += 1
        elif recommended == "test_more":
            bucket["test_more"] += 1
    return result


def _topic_feedback_boost(feedback: dict[str, int]) -> float:
    return (
        feedback.get("approved", 0) * 4
        + feedback.get("test_more", 0) * 1.5
        - feedback.get("rejected", 0) * 6
        - feedback.get("paused", 0) * 2
    )


def _learning_summary(candidates: list[dict]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "candidates": len(candidates),
        "approved": 0,
        "rejected": 0,
        "paused": 0,
        "needs_human_review": 0,
        "test_more": 0,
        "approval_rate": 0.0,
        "rejection_rate": 0.0,
    }
    decided = 0
    for row in candidates:
        status = str(row.get("status") or "")
        recommended = str(row.get("recommended_action") or "")
        if status in summary:
            summary[status] += 1
        if recommended == "test_more":
            summary["test_more"] += 1
        if status in {"approved", "rejected"}:
            decided += 1
    if decided:
        summary["approval_rate"] = round(summary["approved"] / decided, 3)
        summary["rejection_rate"] = round(summary["rejected"] / decided, 3)
    return summary


def _apply_action_policy(action: dict[str, Any]) -> dict[str, Any]:
    action_type = str(action.get("action_type") or "")
    if action_type == "discover_sources":
        decision = "auto"
        reason = (
            "Можно запускать автоматически: действие только ищет и проверяет кандидатов, "
            "но не включает новый источник в основной каталог."
        )
        operator_label = "Показать кандидатов по теме"
        operator_url = _operator_url("source-candidates", topic=action.get("topic"))
    elif action_type == "review_source_candidate":
        decision = "human_review"
        reason = "Нужно решение человека: добавление или отклонение источника влияет на боевой каталог."
        operator_label = "Открыть кандидата"
        operator_url = _operator_url(
            "source-candidates",
            candidate_id=action.get("candidate_id"),
            status=action.get("status") or "needs_human_review",
            topic=action.get("topic"),
        )
    elif action_type == "recheck_source":
        decision = "human_review"
        reason = "Нужно решение человека: изменение режима существующего источника может повлиять на поток сигналов."
        operator_label = "Открыть источники"
        operator_url = _operator_url("sources", source_id=action.get("source_id"))
    elif action_type == "tune_source_frequency":
        decision = "human_review"
        reason = "Нужно решение человека: частота обработки источника влияет на нагрузку и качество потока."
        operator_label = "Открыть источник"
        operator_url = _operator_url(
            "sources",
            source_id=action.get("source_id"),
            update_frequency=_frequency_value(action.get("direction")),
        )
    else:
        decision = "blocked"
        reason = "Неизвестный тип действия не выполняется автоматически."
        operator_label = "Нет действия"
        operator_url = None

    return {
        **action,
        "policy_decision": decision,
        "policy_reason": reason,
        "requires_human_approval": decision != "auto",
        "operator_label": operator_label,
        "operator_url": operator_url,
    }


def _policy_summary(actions: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"auto": 0, "human_review": 0, "blocked": 0}
    for action in actions:
        decision = str(action.get("policy_decision") or "blocked")
        if decision not in summary:
            decision = "blocked"
        summary[decision] += 1
    return summary


def _operator_url(screen: str, **params: Any) -> str:
    from urllib.parse import urlencode

    query = {"screen": screen}
    for key, value in params.items():
        if value is None or value == "":
            continue
        query[key] = str(value)
    return f"/?{urlencode(query)}"


def _frequency_value(direction: Any) -> str | None:
    if direction == "increase":
        return "ежечасно"
    if direction == "decrease":
        return "еженедельно"
    return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)
