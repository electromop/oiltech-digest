"""Combined source-candidate verdict from quality, regularity and metrics."""

from __future__ import annotations

from typing import Any

from oiltech_digest.source_discovery.agent import score_source_candidate


def assess_source_health(
    metrics: dict[str, Any],
    recommendation: dict[str, Any],
    source_quality: dict[str, Any] | None = None,
    source_regularity: dict[str, Any] | None = None,
    inspection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one operator-facing verdict for a candidate source."""
    score = score_source_candidate(metrics)
    quality = source_quality or {}
    regularity = source_regularity or {}
    inspect = inspection or {}

    quality_score = _num(quality.get("usefulness_score"), score["quality_score"])
    regularity_score = _regularity_score(regularity)
    availability_score = _availability_score(inspect)
    sample_penalty = 15 if score["tested_articles"] < 5 else 0
    health_score = round(max(0.0, min(100.0, quality_score * 0.55 + regularity_score * 0.3 + availability_score * 0.15 - sample_penalty)), 2)

    factors = _factors(score, quality, regularity, inspect)
    risks = _risks(score, quality, regularity, inspect)
    base_action = str(recommendation.get("recommended_action") or "human_review")
    action = _recommended_action(score, regularity, health_score, base_action)
    verdict = _verdict(action, regularity, health_score, score)

    return {
        "source": "rules",
        "health_score": health_score,
        "verdict": verdict,
        "recommended_action": action,
        "reason": _reason(verdict, factors, risks),
        "factors": factors[:6],
        "risks": risks[:6],
        "confidence": _confidence(score, regularity, quality),
        "components": {
            "quality_score": round(quality_score, 2),
            "regularity_score": round(regularity_score, 2),
            "availability_score": round(availability_score, 2),
            "sample_penalty": sample_penalty,
        },
    }


def health_comment(health: dict[str, Any] | None) -> str:
    if not health:
        return ""
    verdict = _verdict_label(str(health.get("verdict") or "needs_review"))
    score = health.get("health_score")
    action = _action_label(str(health.get("recommended_action") or "human_review"))
    reason = str(health.get("reason") or "").strip()
    score_part = f"{round(float(score))}/100" if score is not None else "нет оценки"
    return f"Итоговая оценка источника: {verdict}, здоровье {score_part}, действие: {action}. {reason}".strip()


def _recommended_action(score: dict[str, Any], regularity: dict[str, Any], health_score: float, base_action: str) -> str:
    regularity_label = str(regularity.get("regularity_label") or "")
    if score["tested_articles"] >= 3 and score["relevant_articles"] == 0:
        return "reject"
    if regularity_label in {"archive", "stale"} and score["relevant_articles"] < 3:
        return "reject"
    if health_score <= 25:
        return "reject"
    if health_score >= 70 and score["tested_articles"] >= 5 and score["relevant_articles"] >= 3:
        return "add"
    if health_score >= 45 or regularity_label in {"unknown", "active_but_sparse"}:
        return "test_more" if base_action != "reject" else "human_review"
    return base_action if base_action in {"add", "test_more", "reject", "human_review"} else "human_review"


def _verdict(action: str, regularity: dict[str, Any], health_score: float, score: dict[str, Any]) -> str:
    regularity_label = str(regularity.get("regularity_label") or "")
    if regularity_label == "archive":
        return "archive"
    if regularity_label == "stale":
        return "stale"
    if score["duplicate_count"] > 0:
        return "duplicate_risk"
    if action == "add":
        return "strong_source"
    if action == "test_more":
        return "promising_needs_more_data"
    if action == "reject":
        return "weak_source"
    if health_score >= 50:
        return "needs_review"
    return "risky_needs_review"


def _factors(score: dict[str, Any], quality: dict[str, Any], regularity: dict[str, Any], inspection: dict[str, Any]) -> list[str]:
    factors = []
    if score["tested_articles"]:
        factors.append(f"проверено материалов: {score['tested_articles']}")
    if score["relevant_articles"]:
        factors.append(f"релевантных материалов: {score['relevant_articles']}")
    if score["high_score_articles"]:
        factors.append(f"сильных сигналов 50+: {score['high_score_articles']}")
    if quality.get("quality_label"):
        factors.append(f"качество: {quality['quality_label']}")
    if regularity.get("regularity_label"):
        factors.append(f"регулярность: {regularity['regularity_label']}")
    probe = inspection.get("probe") if isinstance(inspection.get("probe"), dict) else {}
    if probe.get("status"):
        factors.append(f"доступность HTTP {probe['status']}")
    return factors


def _risks(score: dict[str, Any], quality: dict[str, Any], regularity: dict[str, Any], inspection: dict[str, Any]) -> list[str]:
    risks = []
    if score["tested_articles"] < 5:
        risks.append("малая выборка")
    if score["noise_count"] > score["relevant_articles"]:
        risks.append("шума больше, чем релевантных материалов")
    if regularity.get("archive_suspected"):
        risks.append("похож на архив")
    if regularity.get("regularity_label") in {"stale", "unknown"}:
        risks.append("нет подтвержденного свежего потока")
    if quality.get("quality_label") in {"шумный", "сомнительный"}:
        risks.append(f"качество: {quality['quality_label']}")
    if inspection.get("quality_gate_reason"):
        risks.append(str(inspection["quality_gate_reason"]))
    return risks


def _reason(verdict: str, factors: list[str], risks: list[str]) -> str:
    parts = []
    if factors:
        parts.append("Плюсы: " + "; ".join(factors[:3]) + ".")
    if risks:
        parts.append("Риски: " + "; ".join(risks[:3]) + ".")
    if not parts:
        parts.append(f"Вердикт сформирован по совокупной оценке: {verdict}.")
    return " ".join(parts)


def _regularity_score(regularity: dict[str, Any]) -> float:
    label = str(regularity.get("regularity_label") or "")
    if label == "regular":
        return 90.0
    if label == "active_but_sparse":
        return 60.0
    if label == "unknown":
        return 35.0
    if label == "stale":
        return 15.0
    if label == "archive":
        return 5.0
    return 30.0


def _availability_score(inspection: dict[str, Any]) -> float:
    probe = inspection.get("probe") if isinstance(inspection.get("probe"), dict) else {}
    status = probe.get("status")
    if isinstance(status, int):
        if 200 <= status < 300:
            return 90.0
        if 300 <= status < 400:
            return 70.0
        if status in {401, 403, 429}:
            return 20.0
        if status >= 500:
            return 25.0
        return 35.0
    if inspection.get("quality_gate_reason"):
        return 10.0
    return 55.0


def _confidence(score: dict[str, Any], regularity: dict[str, Any], quality: dict[str, Any]) -> float:
    tested = int(score.get("tested_articles") or 0)
    dated = int(regularity.get("dated_articles") or 0)
    quality_confidence = _num(quality.get("confidence"), 0.45)
    sample = min(0.35, tested * 0.05) + min(0.2, dated * 0.04)
    return round(max(0.2, min(0.9, quality_confidence * 0.45 + sample + 0.2)), 2)


def _num(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _verdict_label(verdict: str) -> str:
    labels = {
        "strong_source": "сильный источник",
        "promising_needs_more_data": "перспективный, нужна выборка",
        "weak_source": "слабый источник",
        "archive": "архив, не поток",
        "stale": "не обновляется",
        "duplicate_risk": "риск дубля",
        "needs_review": "нужно решение",
        "risky_needs_review": "рискованный кандидат",
    }
    return labels.get(verdict, verdict)


def _action_label(action: str) -> str:
    labels = {
        "add": "добавить",
        "test_more": "проверить еще",
        "reject": "не добавлять",
        "human_review": "показать человеку",
    }
    return labels.get(action, action)
