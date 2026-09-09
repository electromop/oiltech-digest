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
    combo_memory = repository.list_agent_memory(memory_type="topic_query_domain", status=None, limit=500)
    reflection_memory = repository.list_agent_memory(memory_type="reflection", status="active", limit=5)

    reflection_hints = _reflection_hints_by_topic(reflection_memory)
    actions = _topic_discovery_actions(topic_gaps, candidates, topic_memory, query_memory, combo_memory, config, rejected_topic_memory, reflection_hints)
    actions += _candidate_review_actions(candidate_triage, config)
    actions += _source_audit_actions(source_quality, config)
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
            "combo_memory_count": len(combo_memory),
            "reflection_memory_count": len(reflection_memory),
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
    if not candidates:
        return []
    candidate_ids = {int(row["id"]) for row in candidates if row.get("id") is not None}
    try:
        rows = repository.source_candidate_triage_report(limit=limit)
    except Exception:  # noqa: BLE001 - fallback keeps planning available during partial migrations
        return candidates
    if not candidate_ids:
        return rows
    filtered = [row for row in rows if int(row.get("id") or 0) in candidate_ids]
    return filtered or candidates[:limit]


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
    combo_memory: list[dict],
    config: PlannerConfig,
    rejected_topic_memory: list[dict] | None = None,
    reflection_hints: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    seen_topics = {str(row.get("topic") or "").strip().lower() for row in candidates if row.get("topic")}
    memory_by_subject = {str(row.get("subject") or "").strip().lower(): row for row in topic_memory}
    rejected_topics = {str(row.get("subject") or "").strip().lower() for row in rejected_topic_memory or [] if row.get("subject")}
    query_hints_by_topic = _query_hints_by_topic(query_memory)
    combo_hints_by_topic = _combo_hints_by_topic(combo_memory)
    reflection_hints = reflection_hints or {}
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
        reflection = reflection_hints.get(topic_key, {})
        reflection_boost = float(reflection.get("priority_boost") or 0)
        priority = min(100.0, max(0.0, float(row.get("priority") or 0) + memory_boost + feedback_boost + reflection_boost))
        if topic_key not in seen_topics:
            priority = min(100.0, priority + 10)
        combo_hints = combo_hints_by_topic.get(topic_key, {"promoted": [], "muted": []})
        query_hints = _merge_hint_lists(
            [item["query"] for item in combo_hints["promoted"]],
            [str(item) for item in reflection.get("query_hints") or []],
            query_hints_by_topic.get(topic_key, []),
            limit=4,
        )
        actions.append({
            "action_type": "discover_sources",
            "topic": topic,
            "priority": round(priority, 2),
            "limit": config.candidate_limit,
            "query_hints": query_hints,
            "memory_explanation": _topic_memory_explanation(query_hints, combo_hints, feedback, reflection),
            "reason": _topic_reason(row, topic_key in seen_topics, feedback, combo_hints, reflection),
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


def _source_audit_actions(source_quality: list[dict], config: PlannerConfig) -> list[dict[str, Any]]:
    actions = []
    for row in source_quality:
        if not row.get("enabled", True):
            continue
        verdict = _source_audit_verdict(row)
        if not verdict:
            continue
        actions.append({
            "action_type": "audit_existing_source",
            "source_id": int(row["source_id"]),
            "source_name": row.get("source_name"),
            "priority": verdict["priority"],
            "audit_status": verdict["status"],
            "audit_problem_type": verdict["problem_type"],
            "audit_severity": verdict["severity"],
            "audit_confidence": verdict["confidence"],
            "audit_recommendation": verdict["recommendation"],
            "audit_recommendation_label": _source_recommendation_label(verdict["recommendation"]),
            "audit_reasons": verdict["reasons"],
            "audit_decision_log": verdict["decision_log"],
            "reason": verdict["reason"],
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


def _source_audit_verdict(row: dict) -> dict[str, Any] | None:
    found = int(row.get("articles_found") or 0)
    processed = int(row.get("articles_processed") or 0)
    relevant = int(row.get("relevant_count") or 0)
    digest = int(row.get("digest_count") or 0)
    noise = int(row.get("noise_count") or 0)
    duplicates = int(row.get("duplicate_count") or 0)
    quality = float(row.get("quality_score") or 0)
    avg_score = _float_or_none(row.get("avg_score"))
    parse_strategy = str(row.get("parse_strategy") or "")
    network_region = str(row.get("network_region") or "auto")
    network_profile = str(row.get("network_profile") or "direct")
    ru_probe = str(row.get("last_ru_probe_status") or "").lower()
    external_probe = str(row.get("last_external_probe_status") or "").lower()
    external_reason = str(row.get("external_required_reason") or "").strip()

    reasons: list[str] = []
    triggered_rules: list[dict[str, Any]] = []
    suppressed_rules: list[dict[str, Any]] = []
    recommendation = "review"
    status = "needs_attention"
    problem_type = "needs_attention"
    priority = 45.0

    if found == 0:
        message = "За период источник не дал ни одного сигнала."
        reasons.append(message)
        triggered_rules.append(_audit_rule("stale", message, "medium"))
        status = "stale"
        problem_type = "stale"
        recommendation = "diagnose_or_pause"
        priority = max(priority, 62.0)

    if found >= 5 and quality <= 20:
        message = f"Низкое качество: quality={quality}."
        reasons.append(message)
        triggered_rules.append(_audit_rule("low_quality", message, "medium"))
        status = "low_quality"
        problem_type = "low_quality"
        recommendation = "review_frequency_or_pause"
        priority = max(priority, 58.0)

    if found >= 5 and noise >= max(3, found // 3):
        message = f"Много шума: noise={noise}/{found}."
        reasons.append(message)
        triggered_rules.append(_audit_rule("noisy", message, "high"))
        status = "noisy"
        problem_type = "noisy"
        recommendation = "decrease_frequency_or_pause"
        priority = max(priority, 64.0)

    if found >= 5 and duplicates >= max(3, found // 3):
        message = f"Много дублей: duplicates={duplicates}/{found}."
        reasons.append(message)
        triggered_rules.append(_audit_rule("duplicating", message, "medium"))
        status = "duplicating"
        problem_type = "duplicating"
        recommendation = "dedupe_or_decrease_frequency"
        priority = max(priority, 57.0)

    if found >= 5 and avg_score is not None and avg_score < 35:
        message = f"Низкий средний score: avg_score={avg_score}."
        reasons.append(message)
        triggered_rules.append(_audit_rule("low_score", message, "medium"))
        status = "low_score"
        problem_type = "low_score"
        recommendation = "review_scoring_or_pause"
        priority = max(priority, 55.0)

    if ru_probe in {"blocked", "failed", "timeout", "403", "451"} or external_reason:
        message = external_reason or f"RU-доступ проблемный: {ru_probe}."
        reasons.append(message)
        triggered_rules.append(_audit_rule("needs_external", message, "high"))
        status = "needs_external"
        problem_type = "needs_external"
        recommendation = "move_to_external_region"
        priority = max(priority, 72.0)

    if network_region == "external" and external_probe in {"blocked", "failed", "timeout", "403", "451"}:
        message = f"External-доступ проблемный: {external_probe}."
        reasons.append(message)
        triggered_rules.append(_audit_rule("broken", message, "critical"))
        status = "broken"
        problem_type = "broken"
        recommendation = "diagnose_network"
        priority = max(priority, 78.0)

    parser_message = "Статьи находятся, но не проходят обработку; возможно нужен Playwright или другой парсер."
    if found >= 5 and processed == 0 and parse_strategy not in {"playwright", "telegram"} and status not in {"needs_external", "broken"}:
        reasons.append(parser_message)
        triggered_rules.append(_audit_rule("parser_suspect", parser_message, "high"))
        status = "parser_suspect"
        problem_type = "parser_suspect"
        recommendation = "try_playwright_parser"
        priority = max(priority, 66.0)
    elif found >= 5 and processed == 0 and parse_strategy not in {"playwright", "telegram"}:
        suppressed_rules.append(_audit_rule("parser_suspect", parser_message, "high", suppressed_by=status))

    if not reasons:
        return None

    if digest >= 3 and relevant >= max(3, found // 3) and quality >= 55:
        message = "Несмотря на проблемы, источник даёт полезные материалы; не отключать автоматически."
        reasons.append(message)
        suppressed_rules.append(_audit_rule("do_not_pause_useful_source", message, "medium"))
        priority = min(priority, 60.0)
        if recommendation in {"decrease_frequency_or_pause", "review_frequency_or_pause", "diagnose_or_pause"}:
            recommendation = "review"

    confidence = _source_audit_confidence(found=found, processed=processed, triggered_rules=triggered_rules, suppressed_rules=suppressed_rules)
    severity = _source_audit_severity(triggered_rules, priority)

    return {
        "status": status,
        "problem_type": problem_type,
        "severity": severity,
        "confidence": confidence,
        "recommendation": recommendation,
        "recommendation_label": _source_recommendation_label(recommendation),
        "priority": round(min(100.0, priority), 2),
        "reasons": reasons,
        "reason": " ".join(reasons),
        "decision_log": {
            "metrics": {
                "articles_found": found,
                "articles_processed": processed,
                "relevant_count": relevant,
                "digest_count": digest,
                "noise_count": noise,
                "duplicate_count": duplicates,
                "quality_score": quality,
                "avg_score": avg_score,
                "parse_strategy": parse_strategy,
                "network_region": network_region,
                "network_profile": network_profile,
                "last_ru_probe_status": ru_probe or None,
                "last_external_probe_status": external_probe or None,
            },
            "triggered_rules": triggered_rules,
            "suppressed_rules": suppressed_rules,
        },
    }


def _audit_rule(rule: str, reason: str, severity: str, *, suppressed_by: str | None = None) -> dict[str, Any]:
    payload = {"rule": rule, "reason": reason, "severity": severity}
    if suppressed_by:
        payload["suppressed_by"] = suppressed_by
    return payload


def _source_audit_confidence(
    *,
    found: int,
    processed: int,
    triggered_rules: list[dict[str, Any]],
    suppressed_rules: list[dict[str, Any]],
) -> str:
    if any(rule.get("rule") in {"needs_external", "broken"} for rule in triggered_rules):
        return "high"
    if found == 0:
        return "medium"
    if found < 5 or processed < 3:
        return "low"
    if suppressed_rules:
        return "medium"
    return "high"


def _source_audit_severity(triggered_rules: list[dict[str, Any]], priority: float) -> str:
    severities = [str(rule.get("severity") or "low") for rule in triggered_rules]
    if "critical" in severities or priority >= 78:
        return "critical"
    if "high" in severities or priority >= 64:
        return "high"
    if "medium" in severities or priority >= 55:
        return "medium"
    return "low"


def _source_recommendation_for_stable(row: dict) -> str:
    found = int(row.get("articles_found") or 0)
    relevant = int(row.get("relevant_count") or 0)
    digest = int(row.get("digest_count") or 0)
    quality = float(row.get("quality_score") or 0)
    if found >= 8 and quality >= 65 and relevant >= max(4, found // 3):
        return "increase_frequency"
    if digest >= 3 and quality >= 55:
        return "keep"
    return "keep"


def _source_recommendation_label(recommendation: str) -> str:
    labels = {
        "increase_frequency": "Проверять чаще",
        "review_frequency_or_pause": "Проверять реже или поставить на паузу",
        "decrease_frequency_or_pause": "Проверять реже или поставить на паузу",
        "dedupe_or_decrease_frequency": "Проверять реже и проверить дубли",
        "review_scoring_or_pause": "Проверить scoring или поставить на паузу",
        "try_playwright_parser": "Сменить стратегию парсинга",
        "diagnose_or_pause": "Отправить на диагностику или поставить на паузу",
        "diagnose_network": "Отправить на сетевую диагностику",
        "move_to_external_region": "Перенести в external-worker",
        "keep": "Оставить как есть",
        "review": "Проверить вручную",
    }
    return labels.get(recommendation, recommendation)


def _source_memory_score(row: dict, verdict: dict[str, Any] | None) -> float:
    quality = float(row.get("quality_score") or 0)
    found = int(row.get("articles_found") or 0)
    relevant = int(row.get("relevant_count") or 0)
    digest = int(row.get("digest_count") or 0)
    if verdict:
        return round(max(-100.0, min(100.0, quality - float(verdict.get("priority") or 0))), 2)
    stable_bonus = 10 if found and relevant else 0
    digest_bonus = min(20, digest * 3)
    return round(max(-100.0, min(100.0, quality + stable_bonus + digest_bonus)), 2)


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
        verdict = _source_audit_verdict(row)
        recommendation = verdict["recommendation"] if verdict else _source_recommendation_for_stable(row)
        updates.append({
            "memory_key": f"source:{row['source_id']}",
            "memory_type": "source",
            "subject": domain_subject,
            "status": "muted" if verdict else "active",
            "score": _source_memory_score(row, verdict),
            "facts": {
                "source_id": int(row["source_id"]),
                "source_name": row.get("source_name"),
                "status": verdict["status"] if verdict else "stable",
                "problem_type": verdict["problem_type"] if verdict else "stable",
                "severity": verdict["severity"] if verdict else "low",
                "confidence": verdict["confidence"] if verdict else "high",
                "recommendation": recommendation,
                "recommendation_label": _source_recommendation_label(recommendation),
                "reasons": verdict["reasons"] if verdict else [],
                "decision_log": verdict["decision_log"] if verdict else {"triggered_rules": [], "suppressed_rules": []},
                "articles_found": int(row.get("articles_found") or 0),
                "articles_processed": int(row.get("articles_processed") or 0),
                "relevant_count": int(row.get("relevant_count") or 0),
                "digest_count": int(row.get("digest_count") or 0),
                "noise_count": int(row.get("noise_count") or 0),
                "duplicate_count": int(row.get("duplicate_count") or 0),
                "avg_score": _float_or_none(row.get("avg_score")),
                "quality_score": _float_or_none(row.get("quality_score")),
                "parse_strategy": row.get("parse_strategy"),
                "network_region": row.get("network_region"),
                "network_profile": row.get("network_profile"),
                "last_ru_probe_status": row.get("last_ru_probe_status"),
                "last_external_probe_status": row.get("last_external_probe_status"),
                "external_required_reason": row.get("external_required_reason"),
                "last_seen_published_at": str(row.get("last_seen_published_at") or "") or None,
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


def _topic_reason(
    row: dict,
    has_existing_candidate: bool,
    feedback: dict[str, int],
    combo_hints: dict[str, list[dict]] | None = None,
    reflection: dict[str, Any] | None = None,
) -> str:
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
    combo_hints = combo_hints or {"promoted": [], "muted": []}
    if combo_hints.get("promoted"):
        reason += f" Память усилила {len(combo_hints['promoted'])} связк."
    if combo_hints.get("muted"):
        reason += f" Память исключит {len(combo_hints['muted'])} плохих связк."
    reflection = reflection or {}
    if reflection.get("next_hint"):
        reason += f" Вывод прошлого запуска: {reflection['next_hint']}."
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


def _combo_hints_by_topic(combo_memory: list[dict]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    result: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for row in combo_memory:
        facts = row.get("facts_json") or {}
        topic = str(facts.get("topic") or "").strip().lower()
        query = str(facts.get("query") or "").strip()
        domain = str(facts.get("domain") or "").strip().lower()
        if not topic or not query or not domain:
            continue
        score = float(row.get("score") or 0)
        status = str(row.get("status") or "")
        item = {
            "query": query,
            "domain": domain,
            "score": round(score, 2),
            "status": status,
            "reason": facts.get("last_reason"),
        }
        bucket = result.setdefault(topic, {"promoted": [], "muted": []})
        if status == "active" and score > 0:
            bucket["promoted"].append(item)
        elif status in {"muted", "rejected"} or score < 0:
            bucket["muted"].append(item)
    for bucket in result.values():
        bucket["promoted"].sort(key=lambda item: float(item.get("score") or 0), reverse=True)
        bucket["muted"].sort(key=lambda item: float(item.get("score") or 0))
        del bucket["promoted"][3:]
        del bucket["muted"][3:]
    return result


def _reflection_hints_by_topic(reflection_memory: list[dict]) -> dict[str, dict[str, Any]]:
    hints: dict[str, dict[str, Any]] = {}
    for row in reflection_memory:
        facts = row.get("facts_json") or {}
        for item in facts.get("next_hints") or []:
            if not isinstance(item, dict):
                continue
            topic = str(item.get("topic") or "").strip()
            if not topic:
                continue
            topic_key = topic.lower()
            kind = str(item.get("kind") or "")
            strategy = str(item.get("strategy") or "").strip()
            reason = str(item.get("reason") or "").strip()
            current = hints.setdefault(topic_key, {
                "topic": topic,
                "query_hints": [],
                "priority_boost": 0.0,
                "next_hint": "",
            })
            if kind == "promote_topic":
                current["priority_boost"] = max(float(current.get("priority_boost") or 0), 8.0)
            elif kind == "change_strategy":
                current["priority_boost"] = max(float(current.get("priority_boost") or 0), 4.0)
            if strategy:
                current["query_hints"].append(_strategy_hint_query(topic, strategy))
            if reason and not current.get("next_hint"):
                current["next_hint"] = reason
    for value in hints.values():
        value["query_hints"] = _merge_hint_lists(value.get("query_hints") or [], limit=3)
    return hints


def _strategy_hint_query(topic: str, strategy: str) -> str:
    if strategy == "newsroom":
        return f"{topic} пресс-релиз новости"
    if strategy == "technical":
        return f"{topic} технология исследование внедрение"
    if strategy == "company":
        return f"{topic} нефтегаз компания запуск"
    return f"{topic} новости"


def _topic_memory_explanation(
    query_hints: list[str],
    combo_hints: dict[str, list[dict[str, Any]]],
    feedback: dict[str, int],
    reflection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "query_hints": query_hints,
        "promoted_combos": combo_hints.get("promoted", []),
        "muted_combos": combo_hints.get("muted", []),
        "feedback": feedback,
        "reflection": reflection or {},
    }


def _merge_hint_lists(*groups: list[str], limit: int) -> list[str]:
    result = []
    seen = set()
    for group in groups:
        for item in group:
            value = str(item or "").strip()
            key = value.lower()
            if not value or key in seen:
                continue
            seen.add(key)
            result.append(value)
            if len(result) >= limit:
                return result
    return result


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
    elif action_type == "audit_existing_source":
        decision = "human_review"
        reason = "Нужно решение человека: аудит может привести к паузе, смене региона или смене парсера источника."
        operator_label = "Открыть источник"
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
