"""Iterative source-discovery agent loop.

The loop is intentionally conservative: it can run safe discovery actions, but
it never approves a source or changes an existing source without an operator.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import time
from typing import Any

from oiltech_digest import config
from oiltech_digest.db import repository
from oiltech_digest.source_discovery.agent import DiscoveryConfig, discover_sources
from oiltech_digest.source_discovery.planner import PlannerConfig, build_plan
from oiltech_digest.source_discovery.sandbox import evaluate_source_candidate

QUERY_STRATEGIES = ("balanced", "newsroom", "technical", "company")


@dataclass(frozen=True)
class AgentLoopConfig:
    goal: str = "Найти новые полезные источники сигналов"
    days: int = 30
    target_per_topic: int = 10
    topic_limit: int = 5
    candidate_limit: int = 10
    max_actions: int = 5
    max_iterations: int = 3
    offline: bool = True
    fetch_inspection: bool = False
    dry_run: bool = False
    auto_evaluate: bool = True
    article_limit: int = 5
    persist_memory: bool = True
    run_id: int | None = None
    max_daily_loop_runs: int = 4
    max_daily_candidates: int = 100
    max_daily_evaluations: int = 100


def run_agent_loop(config: AgentLoopConfig | None = None) -> dict[str, Any]:
    config = config or AgentLoopConfig()
    started = time.monotonic()
    run_id = config.run_id or repository.create_agent_run(
        "source_discovery_loop",
        trigger="cli" if config.run_id is None else "nested",
        payload={
            "goal": config.goal,
            "days": config.days,
            "target_per_topic": config.target_per_topic,
            "topic_limit": config.topic_limit,
            "candidate_limit": config.candidate_limit,
            "max_actions": config.max_actions,
            "max_iterations": config.max_iterations,
            "offline": config.offline,
            "dry_run": config.dry_run,
            "auto_evaluate": config.auto_evaluate,
            "article_limit": config.article_limit,
            "max_daily_loop_runs": config.max_daily_loop_runs,
            "max_daily_candidates": config.max_daily_candidates,
            "max_daily_evaluations": config.max_daily_evaluations,
        },
    )
    iterations: list[dict[str, Any]] = []
    total_candidates = 0
    terminal_reason = "max_iterations_reached"
    empty_iterations = 0

    try:
        initial_budget = _budget_state(config)
        if initial_budget["blocked"]:
            terminal_reason = initial_budget["reason"]
            result = {
                "run_id": run_id,
                "goal": config.goal,
                "iterations": [],
                "total_candidates": 0,
                "empty_iterations": 0,
                "terminal_reason": terminal_reason,
                "budget": initial_budget,
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
            repository.record_agent_action(
                None,
                "source_discovery_loop_budget_stop",
                run_id=run_id,
                input_payload={"goal": config.goal},
                output_payload=result,
            )
            repository.finish_agent_run(run_id, status="ok", result=result)
            return result

        for iteration in range(1, max(1, config.max_iterations) + 1):
            iteration_budget = _budget_state(config, candidates_in_run=total_candidates)
            if iteration_budget["blocked"]:
                terminal_reason = iteration_budget["reason"]
                break
            plan = build_plan(PlannerConfig(
                days=config.days,
                target_per_topic=config.target_per_topic,
                topic_limit=config.topic_limit,
                candidate_limit=config.candidate_limit,
                max_actions=config.max_actions,
                persist_memory=config.persist_memory,
                record_action=True,
                run_id=run_id,
            ))
            auto_actions = [
                action for action in plan.get("actions") or []
                if action.get("policy_decision") == "auto" and action.get("action_type") == "discover_sources"
            ]
            if not auto_actions:
                terminal_reason = "no_auto_actions"
                iterations.append(_iteration_result(iteration, plan, []))
                repository.record_agent_action(
                    None,
                    "source_discovery_loop_iteration",
                    run_id=run_id,
                    input_payload={"iteration": iteration, "goal": config.goal},
                    output_payload=iterations[-1],
                )
                break

            observations = []
            for action in auto_actions[: config.max_actions]:
                action_budget = _budget_state(config, candidates_in_run=total_candidates)
                if action_budget["blocked"]:
                    terminal_reason = action_budget["reason"]
                    break
                query_strategy = _strategy_for_action(str(action["topic"]), iteration)
                discovery = discover_sources(DiscoveryConfig(
                    topic=str(action["topic"]),
                    limit=int(action.get("limit") or config.candidate_limit),
                    offline=config.offline,
                    dry_run=config.dry_run,
                    fetch_inspection=config.fetch_inspection,
                    test_parse=False,
                    run_id=run_id,
                    query_strategy=query_strategy,
                ))
                candidate_count = len(discovery.get("candidates") or [])
                evaluations = _evaluate_discovered_candidates(discovery, config, run_id=run_id)
                evaluated = [item for item in evaluations if item.get("ok") and item.get("metrics")]
                relevant_articles = sum(int((item.get("metrics") or {}).get("relevant_articles") or 0) for item in evaluated)
                avg_score = _avg_score([item.get("metrics") for item in evaluated])
                total_candidates += candidate_count
                observation = {
                    "action_type": action["action_type"],
                    "topic": action.get("topic"),
                    "priority": action.get("priority"),
                    "query_strategy": query_strategy,
                    "search_status": (discovery.get("search") or {}).get("status"),
                    "query_count": len(discovery.get("queries") or []),
                    "candidate_count": candidate_count,
                    "evaluated_count": len(evaluated),
                    "evaluation_jobs": sum(1 for item in evaluations if item.get("queued")),
                    "evaluation_errors": sum(1 for item in evaluations if not item.get("ok") and not item.get("queued")),
                    "relevant_articles": relevant_articles,
                    "avg_score": avg_score,
                    "task_id": discovery.get("task_id"),
                }
                observations.append(observation)
                if config.persist_memory:
                    _persist_strategy_memory(observation)

            iterations.append(_iteration_result(iteration, plan, observations))
            repository.record_agent_action(
                None,
                "source_discovery_loop_iteration",
                run_id=run_id,
                input_payload={"iteration": iteration, "goal": config.goal},
                output_payload=iterations[-1],
            )

            if any(item["candidate_count"] for item in observations):
                empty_iterations = 0
                terminal_reason = "max_iterations_reached"
            else:
                empty_iterations += 1
                terminal_reason = "no_candidates_found"

        result = {
            "run_id": run_id,
            "goal": config.goal,
            "iterations": iterations,
            "total_candidates": total_candidates,
            "empty_iterations": empty_iterations,
            "terminal_reason": terminal_reason,
            "budget": _budget_state(config, candidates_in_run=total_candidates),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        repository.finish_agent_run(run_id, status="ok", result=result)
        return result
    except Exception as exc:
        repository.finish_agent_run(run_id, status="failed", result={"iterations": iterations}, error_message=str(exc)[:1000])
        raise


def _iteration_result(iteration: int, plan: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "iteration": iteration,
        "policy": plan.get("policy"),
        "learning": plan.get("learning"),
        "action_count": len(plan.get("actions") or []),
        "auto_action_count": sum(1 for action in plan.get("actions") or [] if action.get("policy_decision") == "auto"),
        "human_review_count": sum(1 for action in plan.get("actions") or [] if action.get("policy_decision") == "human_review"),
        "observations": observations,
    }


def _budget_state(config_value: AgentLoopConfig, *, candidates_in_run: int = 0) -> dict[str, Any]:
    try:
        usage = repository.source_discovery_daily_usage()
    except Exception as exc:  # noqa: BLE001 - budget read failure should stop autonomous work conservatively
        return {
            "blocked": True,
            "reason": "budget_usage_unavailable",
            "error": str(exc)[:1000],
        }
    limits = {
        "loop_runs": max(0, int(config_value.max_daily_loop_runs)),
        "candidates_created": max(0, int(config_value.max_daily_candidates)),
        "candidate_evaluations": max(0, int(config_value.max_daily_evaluations)),
    }
    projected_candidates = int(usage.get("candidates_created") or 0) + int(candidates_in_run)
    checks = {
        "loop_runs": int(usage.get("loop_runs") or 0),
        "candidates_created": projected_candidates,
        "candidate_evaluations": int(usage.get("candidate_evaluations") or 0),
    }
    if limits["loop_runs"] and checks["loop_runs"] > limits["loop_runs"]:
        reason = "daily_loop_budget_reached"
    elif limits["candidates_created"] and checks["candidates_created"] >= limits["candidates_created"]:
        reason = "daily_candidate_budget_reached"
    elif limits["candidate_evaluations"] and checks["candidate_evaluations"] >= limits["candidate_evaluations"]:
        reason = "daily_evaluation_budget_reached"
    else:
        reason = ""
    return {
        "blocked": bool(reason),
        "reason": reason,
        "usage": usage,
        "projected": checks,
        "limits": limits,
    }


def _strategy_for_iteration(iteration: int) -> str:
    return QUERY_STRATEGIES[(max(1, iteration) - 1) % len(QUERY_STRATEGIES)]


def _strategy_for_action(topic: str, iteration: int) -> str:
    topic_key = topic.strip().lower()
    try:
        rows = repository.list_agent_memory(memory_type="strategy", status=None, limit=500)
    except Exception:  # noqa: BLE001 - memory must not break the agent loop
        rows = []
    active = []
    muted = set()
    for row in rows:
        facts = row.get("facts_json") or {}
        if str(facts.get("topic") or "").strip().lower() != topic_key:
            continue
        strategy = str(facts.get("strategy") or row.get("subject") or "").strip().lower()
        if strategy not in QUERY_STRATEGIES:
            continue
        if row.get("status") == "muted":
            muted.add(strategy)
            continue
        if row.get("status") == "active":
            active.append((float(row.get("score") or 0), strategy))
    if active:
        return sorted(active, reverse=True)[0][1]
    fallback = [strategy for strategy in QUERY_STRATEGIES if strategy not in muted] or list(QUERY_STRATEGIES)
    return fallback[(max(1, iteration) - 1) % len(fallback)]


def _evaluate_discovered_candidates(discovery: dict[str, Any], config: AgentLoopConfig, *, run_id: int | None = None) -> list[dict[str, Any]]:
    if config.dry_run or not config.auto_evaluate:
        return []
    results = []
    for candidate in discovery.get("candidates") or []:
        candidate_id = candidate.get("id")
        if not candidate_id:
            continue
        try:
            if _should_delegate_evaluation(config):
                job = repository.create_background_job(
                    "source_candidate_evaluate",
                    {
                        "candidate_id": int(candidate_id),
                        "article_limit": config.article_limit,
                        "offline": False,
                        "collect": True,
                        "process": True,
                    },
                    queue_name="external-ai",
                    execution_region="external",
                    capability="openai",
                    max_attempts=1,
                    agent_run_id=run_id,
                )
                results.append({
                    "ok": True,
                    "queued": "external-ai",
                    "candidate_id": int(candidate_id),
                    "job_id": int(job["id"]),
                })
                continue
            evaluation = evaluate_source_candidate(
                int(candidate_id),
                article_limit=config.article_limit,
                offline=config.offline,
                collect=True,
                process=True,
            )
            results.append({"ok": True, "candidate_id": int(candidate_id), **evaluation})
        except Exception as exc:  # noqa: BLE001 - one bad candidate must not stop the loop
            results.append({"ok": False, "candidate_id": int(candidate_id), "error": str(exc)[:1000]})
    return results


def _should_delegate_evaluation(config_value: AgentLoopConfig) -> bool:
    return (
        not config_value.offline
        and config.EXTERNAL_WORKERS_ENABLED
        and config.AI_EXECUTION_REGION == "external"
    )


def _persist_strategy_memory(observation: dict[str, Any]) -> None:
    topic = str(observation.get("topic") or "").strip()
    strategy = str(observation.get("query_strategy") or "").strip().lower()
    if not topic or strategy not in QUERY_STRATEGIES:
        return
    candidates = int(observation.get("candidate_count") or 0)
    evaluated = int(observation.get("evaluated_count") or 0)
    evaluation_jobs = int(observation.get("evaluation_jobs") or 0)
    relevant = int(observation.get("relevant_articles") or 0)
    avg_score = observation.get("avg_score")
    errors = int(observation.get("evaluation_errors") or 0)
    search_status = str(observation.get("search_status") or "")
    empty_success = candidates == 0 and search_status in {"ok", "empty"}
    if candidates == 0 and search_status not in {"ok", "empty"}:
        return
    score = 0.0 if empty_success else min(
        100.0,
        candidates * 12 + evaluated * 4 + relevant * 12 + (float(avg_score or 0) * 0.25) - errors * 8,
    )
    digest = hashlib.sha1(f"{topic.lower()}::{strategy}".encode("utf-8")).hexdigest()[:16]
    try:
        repository.upsert_agent_memory(
            memory_key=f"strategy:{digest}",
            memory_type="strategy",
            subject=strategy,
            status="muted" if empty_success else "active",
            score=round(max(0.0, score), 2),
            facts={
                "topic": topic,
                "strategy": strategy,
                "search_status": search_status,
                "candidate_count": candidates,
                "evaluated_count": evaluated,
                "evaluation_jobs": evaluation_jobs,
                "evaluation_errors": errors,
                "relevant_articles": relevant,
                "avg_score": avg_score,
            },
        )
    except Exception:  # noqa: BLE001 - learning write must not break the loop
        return


def _avg_score(metrics: list[dict[str, Any] | None]) -> float | None:
    values = [float(item["avg_score"]) for item in metrics if item and item.get("avg_score") is not None]
    return round(sum(values) / len(values), 2) if values else None
