"""Read-only readiness report for the source-discovery agent."""

from __future__ import annotations

from typing import Any

from oiltech_digest import config
from oiltech_digest.db import repository


def source_discovery_readiness() -> dict[str, Any]:
    search = _search_status()
    external_ai = _external_ai_status()
    scheduler = _scheduler_status()
    data = _data_status()
    jobs = _job_status()
    budget = _budget_status()

    checks = [search, external_ai, scheduler, data, jobs, budget]
    issues = [issue for item in checks for issue in item.get("issues", [])]
    recommendations = [rec for item in checks for rec in item.get("recommendations", [])]
    hard_blockers = [issue for issue in issues if issue.get("severity") == "blocker"]

    return {
        "ok": not hard_blockers,
        "status": "ready" if not hard_blockers and not issues else "degraded" if not hard_blockers else "blocked",
        "checks": {
            "search": search,
            "external_ai": external_ai,
            "scheduler": scheduler,
            "data": data,
            "jobs": jobs,
            "budget": budget,
        },
        "issues": issues,
        "recommendations": recommendations,
    }


def _search_status() -> dict[str, Any]:
    provider = config.SOURCE_DISCOVERY_SEARCH_PROVIDER
    issues = []
    recommendations = []
    configured = provider not in {"", "none", "disabled"}
    if not configured:
        issues.append({
            "severity": "warning",
            "code": "search_provider_disabled",
            "message": "Внешний поиск источников выключен; агент сможет работать только по seed-url и ручным кандидатам.",
        })
        recommendations.append("Для автономного поиска включите SOURCE_DISCOVERY_SEARCH_PROVIDER=brave или serpapi.")
    elif provider == "brave" and not config.BRAVE_SEARCH_API_KEY:
        issues.append({
            "severity": "blocker",
            "code": "brave_key_missing",
            "message": "Выбран Brave Search, но BRAVE_SEARCH_API_KEY пустой.",
        })
        recommendations.append("Заполните BRAVE_SEARCH_API_KEY на core-сервере.")
    elif provider == "serpapi" and not config.SERPAPI_API_KEY:
        issues.append({
            "severity": "blocker",
            "code": "serpapi_key_missing",
            "message": "Выбран SerpAPI, но SERPAPI_API_KEY пустой.",
        })
        recommendations.append("Заполните SERPAPI_API_KEY на core-сервере.")
    elif provider not in {"brave", "serpapi"}:
        issues.append({
            "severity": "blocker",
            "code": "unsupported_search_provider",
            "message": f"Неподдержанный SOURCE_DISCOVERY_SEARCH_PROVIDER={provider}.",
        })
        recommendations.append("Используйте provider brave, serpapi или none.")
    return {
        "ok": not any(item["severity"] == "blocker" for item in issues),
        "provider": provider or "none",
        "configured": configured,
        "timeout_seconds": config.SOURCE_DISCOVERY_SEARCH_TIMEOUT,
        "issues": issues,
        "recommendations": recommendations,
    }


def _external_ai_status() -> dict[str, Any]:
    issues = []
    recommendations = []
    if not config.EXTERNAL_WORKERS_ENABLED:
        issues.append({
            "severity": "warning",
            "code": "external_workers_disabled",
            "message": "Внешний контур выключен; AI-оценка кандидатов будет выполняться только там, где доступен OpenAI.",
        })
        recommendations.append("Для РФ-core включите EXTERNAL_WORKERS_ENABLED=1 и внешний worker с capability=openai.")
    if config.AI_EXECUTION_REGION == "external" and not config.EXTERNAL_WORKER_TOKEN_HASH:
        issues.append({
            "severity": "blocker",
            "code": "external_worker_token_hash_missing",
            "message": "AI вынесен наружу, но EXTERNAL_WORKER_TOKEN_HASH пустой.",
        })
        recommendations.append("Пропишите EXTERNAL_WORKER_TOKEN_HASH на core и EXTERNAL_WORKER_TOKEN на внешнем worker.")
    return {
        "ok": not any(item["severity"] == "blocker" for item in issues),
        "external_workers_enabled": config.EXTERNAL_WORKERS_ENABLED,
        "ai_execution_region": config.AI_EXECUTION_REGION,
        "worker_queues": config.EXTERNAL_WORKER_QUEUES,
        "worker_capabilities": config.EXTERNAL_WORKER_CAPABILITIES,
        "issues": issues,
        "recommendations": recommendations,
    }


def _scheduler_status() -> dict[str, Any]:
    enabled = _env_enabled("SOURCE_DISCOVERY_ENABLED")
    planner_enabled = _env_enabled("SOURCE_DISCOVERY_PLANNER_ENABLED")
    mode = _env_str("SOURCE_DISCOVERY_MODE", "plan").lower()
    issues = []
    recommendations = []
    if not enabled:
        issues.append({
            "severity": "warning",
            "code": "scheduler_discovery_disabled",
            "message": "Автопостановка source-discovery задач выключена.",
        })
        recommendations.append("Включите SOURCE_DISCOVERY_ENABLED=1 в scheduler-контейнере.")
    if enabled and mode not in {"plan", "loop", "topics"}:
        issues.append({
            "severity": "blocker",
            "code": "scheduler_discovery_mode_unknown",
            "message": f"Неподдержанный SOURCE_DISCOVERY_MODE={mode}.",
        })
        recommendations.append("Используйте SOURCE_DISCOVERY_MODE=loop, plan или topics.")
    if enabled and mode == "plan" and not planner_enabled:
        issues.append({
            "severity": "warning",
            "code": "scheduler_planner_disabled",
            "message": "Scheduler включён, но планировщик агента выключен; будут работать только явно заданные темы.",
        })
        recommendations.append("Для автономного режима включите SOURCE_DISCOVERY_PLANNER_ENABLED=1.")
    if enabled and mode == "loop":
        recommendations.append("Для production-автопилота держите jobs-worker включённым на очереди default и внешний worker на external-ai.")
    return {
        "ok": not any(item["severity"] == "blocker" for item in issues),
        "source_discovery_enabled": enabled,
        "mode": mode,
        "planner_enabled": planner_enabled,
        "topic_limit": _env_int("SOURCE_DISCOVERY_TOPIC_LIMIT", 3),
        "max_actions": _env_int("SOURCE_DISCOVERY_MAX_ACTIONS", 5),
        "max_iterations": _env_int("SOURCE_DISCOVERY_MAX_ITERATIONS", 3),
        "issues": issues,
        "recommendations": recommendations,
    }


def _data_status() -> dict[str, Any]:
    triage = repository.source_candidate_triage_report(limit=20)
    active_queries = repository.query_memory_report(status="active", limit=20)
    muted_queries = repository.query_memory_report(status="muted", limit=20)
    issues = []
    recommendations = []
    if not triage:
        issues.append({
            "severity": "info",
            "code": "candidate_triage_empty",
            "message": "Очередь решений по кандидатам пустая.",
        })
        recommendations.append("Запустите agent-plan или discover-sources по приоритетной теме.")
    if not active_queries:
        issues.append({
            "severity": "info",
            "code": "query_memory_empty",
            "message": "Память удачных поисковых формулировок пока пустая.",
        })
        recommendations.append("После первых успешных поисков агент начнёт повторно использовать лучшие формулировки.")
    return {
        "ok": True,
        "candidate_triage_count": len(triage),
        "active_query_memory_count": len(active_queries),
        "muted_query_memory_count": len(muted_queries),
        "issues": issues,
        "recommendations": recommendations,
    }


def _job_status() -> dict[str, Any]:
    counts = repository.background_job_status_counts(capability="source-discovery")
    failed = int(counts.get("failed") or 0)
    queued = int(counts.get("queued") or 0)
    running = int(counts.get("running") or 0)
    issues = []
    recommendations = []
    if failed:
        issues.append({
            "severity": "warning",
            "code": "source_discovery_jobs_failed",
            "message": f"Есть failed source-discovery задачи: {failed}.",
        })
        recommendations.append("Посмотрите jobs/source-discovery логи и перезапустите упавшие задачи после исправления причины.")
    return {
        "ok": failed == 0,
        "counts": counts,
        "queued": queued,
        "running": running,
        "issues": issues,
        "recommendations": recommendations,
    }


def _budget_status() -> dict[str, Any]:
    try:
        usage = repository.source_discovery_daily_usage()
    except Exception as exc:  # noqa: BLE001 - readiness should explain DB/report issues
        return {
            "ok": False,
            "usage": {},
            "limits": {},
            "issues": [{
                "severity": "warning",
                "code": "source_discovery_budget_unavailable",
                "message": f"Не удалось прочитать суточный бюджет агента: {str(exc)[:200]}",
            }],
            "recommendations": ["Проверьте миграции таблиц agent_runs, agent_actions и background_jobs."],
        }
    limits = {
        "loop_runs": _env_int("SOURCE_DISCOVERY_MAX_DAILY_LOOP_RUNS", 4),
        "candidates_created": _env_int("SOURCE_DISCOVERY_MAX_DAILY_CANDIDATES", 100),
        "candidate_evaluations": _env_int("SOURCE_DISCOVERY_MAX_DAILY_EVALUATIONS", 100),
    }
    issues = []
    for key, limit in limits.items():
        if limit and int(usage.get(key) or 0) >= limit:
            issues.append({
                "severity": "info",
                "code": f"{key}_budget_reached",
                "message": f"Суточный лимит {key} исчерпан: {usage.get(key)}/{limit}.",
            })
    return {
        "ok": True,
        "usage": usage,
        "limits": limits,
        "issues": issues,
        "recommendations": ["Увеличивайте лимиты только после проверки качества кандидатов и стоимости external-ai."],
    }


def _env_enabled(name: str) -> bool:
    import os

    return os.environ.get(name, "0").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    import os

    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    import os

    return os.environ.get(name, default).strip() or default
