"""AI quality assessment for source candidates."""

from __future__ import annotations

import json
from typing import Any

from oiltech_digest.processing.pipeline import make_client
from oiltech_digest.source_discovery.agent import score_source_candidate
from oiltech_digest.source_discovery.prompts import SOURCE_QUALITY_INSTRUCTIONS, SOURCE_QUALITY_SCHEMA


def assess_source_quality(
    candidate: dict[str, Any],
    metrics: dict[str, Any],
    evidence: list[dict[str, Any]],
    *,
    offline: bool = True,
) -> dict[str, Any]:
    """Explain source quality from sandbox evidence.

    The function is DB-free so both core and external workers can use it. In
    offline mode it returns deterministic rules; online mode asks the configured
    AI client for a structured editorial assessment.
    """
    score = score_source_candidate(metrics)
    compact_evidence = _compact_evidence(evidence)
    if offline:
        return _offline_quality(candidate, score, compact_evidence)

    client = make_client(False)
    response = client.complete_json(
        SOURCE_QUALITY_INSTRUCTIONS,
        json.dumps(
            {
                "candidate": {
                    "id": candidate.get("id"),
                    "name": candidate.get("name"),
                    "url": candidate.get("url"),
                    "domain": candidate.get("normalized_domain"),
                    "topic": candidate.get("topic"),
                    "candidate_type": candidate.get("candidate_type"),
                },
                "metrics": metrics,
                "score": score,
                "article_evidence": compact_evidence,
            },
            ensure_ascii=False,
        ),
        SOURCE_QUALITY_SCHEMA,
        max_output_tokens=900,
    )
    data = response.data
    return {
        "source": "ai",
        "model": response.model,
        "quality_label": _quality_label(data.get("quality_label"), score),
        "usefulness_score": _score_value(data.get("usefulness_score"), score["quality_score"]),
        "topic_fit": _text(data.get("topic_fit")),
        "article_pattern": _text(data.get("article_pattern")),
        "useful_summary": _text(data.get("useful_summary")),
        "strengths": _strings(data.get("strengths"))[:4],
        "risks": _strings(data.get("risks"))[:4],
        "next_checks": _strings(data.get("next_checks"))[:4],
        "confidence": _confidence(data.get("confidence"), default=_rule_confidence(score)),
    }


def quality_comment(quality: dict[str, Any] | None) -> str:
    if not quality:
        return ""
    summary = _text(quality.get("useful_summary"))
    label = _text(quality.get("quality_label"))
    score = quality.get("usefulness_score")
    confidence = quality.get("confidence")
    bits = []
    if label:
        bits.append(f"качество: {label}")
    if score is not None:
        bits.append(f"полезность {round(float(score))}/100")
    if confidence is not None:
        bits.append(f"уверенность {float(confidence):.2f}")
    prefix = "AI-оценка источника"
    suffix = f" ({'; '.join(bits)})" if bits else ""
    return f"{prefix}{suffix}: {summary}".strip()


def _offline_quality(candidate: dict[str, Any], score: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    quality = float(score.get("quality_score") or 0)
    tested = int(score.get("tested_articles") or 0)
    relevant = int(score.get("relevant_articles") or 0)
    high = int(score.get("high_score_articles") or 0)
    if tested == 0:
        label = "сомнительный"
    elif quality >= 65 and relevant >= 3:
        label = "сильный"
    elif quality >= 40 and relevant > 0:
        label = "перспективный"
    elif quality <= 20:
        label = "шумный"
    else:
        label = "сомнительный"
    risks = []
    if tested < 5:
        risks.append("Мало проверенных материалов для уверенного решения.")
    if relevant == 0:
        risks.append("Пробная выборка не дала релевантных материалов.")
    if int(score.get("noise_count") or 0) > relevant:
        risks.append("Шума больше, чем релевантных материалов.")
    strengths = []
    if relevant:
        strengths.append(f"Есть релевантные материалы: {relevant}.")
    if high:
        strengths.append(f"Есть сильные сигналы со score 50+: {high}.")
    if evidence:
        strengths.append("Материалы удалось распарсить в песочнице.")
    topic = _text(candidate.get("topic")) or "заданной теме"
    return {
        "source": "rules",
        "model": "offline-rules",
        "quality_label": label,
        "usefulness_score": round(max(0.0, min(100.0, quality)), 2),
        "topic_fit": f"Оценка по теме: {topic}.",
        "article_pattern": _article_pattern(evidence),
        "useful_summary": _offline_summary(label, tested, relevant, quality),
        "strengths": strengths[:4],
        "risks": risks[:4],
        "next_checks": _next_checks(label, tested, relevant),
        "confidence": _rule_confidence(score),
    }


def _compact_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in evidence[:6]:
        result.append({
            "title": _text(item.get("title") or item.get("title_ru"))[:220],
            "url": _text(item.get("url"))[:300],
            "summary": _text(item.get("summary"))[:500],
            "relevant": item.get("relevant"),
            "relevance_reason": _text(item.get("relevance_reason"))[:260],
            "tag": item.get("tag_name") or item.get("tag_id"),
            "score": item.get("total_score"),
            "score_label": item.get("score_label"),
            "text_chars": item.get("text_chars"),
            "processing_status": item.get("processing_status"),
        })
    return result


def _quality_label(value: Any, score: dict[str, Any]) -> str:
    text = _text(value).lower()
    if text in {"сильный", "перспективный", "сомнительный", "шумный"}:
        return text
    return _offline_quality({}, score, [])["quality_label"]


def _score_value(value: Any, default: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default or 0)
    return round(max(0.0, min(100.0, parsed)), 2)


def _confidence(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return round(max(0.0, min(1.0, parsed)), 2)


def _rule_confidence(score: dict[str, Any]) -> float:
    tested = int(score.get("tested_articles") or 0)
    quality = float(score.get("quality_score") or 0)
    if tested == 0:
        return 0.25
    if tested < 5:
        return 0.45
    if quality >= 55 or quality <= 20:
        return 0.72
    return 0.55


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item) for item in value if _text(item)]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _article_pattern(evidence: list[dict[str, Any]]) -> str:
    if not evidence:
        return "Паттерн материалов пока не виден: нет примеров из песочницы."
    ok = sum(1 for item in evidence if item.get("processing_status") == "ok" or item.get("relevant") is True)
    rejected = sum(1 for item in evidence if item.get("processing_status") == "rejected" or item.get("relevant") is False)
    return f"В выборке {len(evidence)} материалов: полезных {ok}, отклоненных {rejected}."


def _offline_summary(label: str, tested: int, relevant: int, quality: float) -> str:
    if tested == 0:
        return "Источник еще не проверен на материалах, решение принимать рано."
    return f"Источник выглядит как {label}: проверено {tested}, релевантных {relevant}, расчетная полезность {quality:.1f}/100."


def _next_checks(label: str, tested: int, relevant: int) -> list[str]:
    checks = []
    if tested < 5:
        checks.append("Проверить еще несколько материалов из этого источника.")
    if relevant > 0:
        checks.append("Посмотреть регулярность публикаций за последние 30-90 дней.")
    if label in {"сомнительный", "шумный"}:
        checks.append("Проверить, не является ли URL разовой статьей вместо раздела источника.")
    if not checks:
        checks.append("Проверить регулярность и устойчивость парсинга перед добавлением.")
    return checks
