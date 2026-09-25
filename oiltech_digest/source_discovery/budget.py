"""Суточный бюджет агента источников — одна проверка для цикла и разовых запусков.

Раньше лимиты смотрел только цикл (`loop.run_agent_loop`): разовый поиск из кнопки
«Поставить в очередь» и шаги плана (`discover_source_candidates`) создавали кандидатов
и ставили платную оценку сверх суточного лимита.
"""

from __future__ import annotations

import os
from typing import Any

from oiltech_digest.db import repository

DEFAULT_LIMITS = {
    "loop_runs": ("SOURCE_DISCOVERY_MAX_DAILY_LOOP_RUNS", 4),
    "candidates_created": ("SOURCE_DISCOVERY_MAX_DAILY_CANDIDATES", 100),
    "candidate_evaluations": ("SOURCE_DISCOVERY_MAX_DAILY_EVALUATIONS", 100),
}


def default_limits() -> dict[str, int]:
    """Лимиты из окружения — те же, что показывает экран готовности."""
    limits = {}
    for key, (env_name, default) in DEFAULT_LIMITS.items():
        try:
            limits[key] = max(0, int(os.environ.get(env_name, str(default))))
        except ValueError:
            limits[key] = default
    return limits


def limits_from_payload(payload: dict[str, Any]) -> dict[str, int]:
    """Лимиты задачи: явные `max_daily_*` из payload, остальное — из окружения."""
    limits = default_limits()
    for key, field in (
        ("loop_runs", "max_daily_loop_runs"),
        ("candidates_created", "max_daily_candidates"),
        ("candidate_evaluations", "max_daily_evaluations"),
    ):
        if payload.get(field) is not None:
            limits[key] = max(0, int(payload[field]))
    return limits


def check(
    limits: dict[str, int],
    *,
    candidates_in_run: int = 0,
    evaluations_in_run: int = 0,
    count_loop_runs: bool = True,
) -> dict[str, Any]:
    """Исчерпан ли суточный бюджет. Лимит 0 — без ограничения.

    `count_loop_runs=False` — для разовых запусков: они не цикл, и число циклов за
    день их не останавливает. Текущий цикл уже записан в `agent_runs` к моменту
    проверки, поэтому циклы сравниваются строго (`>`), остальное — `>=`.
    """
    try:
        usage = repository.source_discovery_daily_usage()
    except Exception as exc:  # noqa: BLE001 - budget read failure should stop autonomous work conservatively
        return {
            "blocked": True,
            "reason": "budget_usage_unavailable",
            "error": str(exc)[:1000],
        }
    checks = {
        "loop_runs": int(usage.get("loop_runs") or 0),
        "candidates_created": int(usage.get("candidates_created") or 0) + int(candidates_in_run),
        "candidate_evaluations": int(usage.get("candidate_evaluations") or 0) + int(evaluations_in_run),
    }
    if count_loop_runs and limits["loop_runs"] and checks["loop_runs"] > limits["loop_runs"]:
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
