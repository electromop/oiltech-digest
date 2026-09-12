"""Export signal-discovery snapshots for evals and future model training."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from oiltech_digest.config import EXPORTS_DIR
from oiltech_digest.db import repository

SIGNAL_FEEDBACK_VERDICTS = ("approved", "reject", "duplicate", "needs_context")
SIGNAL_CONTEXT_VERSION = 1


def export_signal_training_jsonl(
    path: str | Path | None = None,
    *,
    limit: int = 1000,
    with_feedback_only: bool = True,
    verdict: str | None = None,
) -> dict[str, Any]:
    if verdict and verdict not in SIGNAL_FEEDBACK_VERDICTS:
        raise ValueError(f"unknown signal feedback verdict: {verdict}")
    output_path = Path(path) if path else EXPORTS_DIR / "signal_training_examples.jsonl"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows = repository.list_signal_training_examples(
        limit=limit,
        with_feedback_only=with_feedback_only,
        verdict=verdict,
    )
    with output_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(_training_record(row), ensure_ascii=False, default=_json_default))
            fh.write("\n")

    verdict_counts: dict[str, int] = {}
    pipeline_counts: dict[str, int] = {}
    for row in rows:
        feedback_verdict = row.get("feedback_verdict") or "none"
        pipeline_verdict = row.get("pipeline_verdict") or "unknown"
        verdict_counts[feedback_verdict] = verdict_counts.get(feedback_verdict, 0) + 1
        pipeline_counts[pipeline_verdict] = pipeline_counts.get(pipeline_verdict, 0) + 1
    return {
        "path": str(output_path),
        "examples": len(rows),
        "with_feedback_only": with_feedback_only,
        "verdict": verdict,
        "feedback_verdict_counts": verdict_counts,
        "pipeline_verdict_counts": pipeline_counts,
    }


def export_signal_context_bundle(
    path: str | Path | None = None,
    *,
    include_rejected: bool = False,
) -> dict[str, Any]:
    output_path = Path(path) if path else EXPORTS_DIR / "signal_context_bundle.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    bundle = _build_signal_context_bundle(include_rejected=include_rejected)
    output_path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    counts = _bundle_counts(bundle)
    return {
        "path": str(output_path),
        "include_rejected": include_rejected,
        **counts,
    }


def import_signal_context_bundle(
    path: str | Path,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    bundle_path = Path(path)
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    if int(bundle.get("version") or 0) != SIGNAL_CONTEXT_VERSION:
        raise ValueError(f"unsupported signal context bundle version: {bundle.get('version')}")
    counts = _bundle_counts(bundle)
    if dry_run:
        return {"path": str(bundle_path), "dry_run": True, **counts}

    signal_id_by_old_id: dict[int, int] = {}
    signal_id_by_key: dict[str, int] = {}
    feedback_id_by_old_id: dict[int, int] = {}

    for topic in bundle.get("signal_radar_topics") or []:
        _upsert_signal_radar_topic(topic)

    for signal in bundle.get("signals") or []:
        signal_id = repository.upsert_signal(_signal_import_payload(signal))
        old_id = signal.get("id")
        if old_id is not None:
            signal_id_by_old_id[int(old_id)] = signal_id
        signal_key = signal.get("signal_key")
        if signal_key:
            signal_id_by_key[str(signal_key)] = signal_id

    for evidence in bundle.get("signal_evidence") or []:
        signal_id = _mapped_signal_id(evidence, signal_id_by_old_id, signal_id_by_key)
        if signal_id is None:
            continue
        repository.upsert_signal_evidence(signal_id, _evidence_import_payload(evidence))

    for memory in bundle.get("signal_agent_memory") or []:
        repository.upsert_signal_agent_memory(
            memory_key=str(memory.get("memory_key") or "").strip(),
            memory_type=str(memory.get("memory_type") or "").strip(),
            subject=str(memory.get("subject") or "").strip(),
            status=str(memory.get("status") or "active").strip() or "active",
            score=float(memory.get("score") or 0),
            facts=memory.get("facts_json") if isinstance(memory.get("facts_json"), dict) else {},
        )

    for feedback in bundle.get("signal_feedback_events") or []:
        event_id = _insert_signal_feedback_event_once(
            feedback,
            signal_id_by_old_id=signal_id_by_old_id,
            signal_id_by_key=signal_id_by_key,
        )
        old_id = feedback.get("id")
        if old_id is not None and event_id is not None:
            feedback_id_by_old_id[int(old_id)] = event_id

    for old_signal_id, signal_id in signal_id_by_old_id.items():
        repository.refresh_signal_evidence_count(signal_id)

    return {
        "path": str(bundle_path),
        "dry_run": False,
        **counts,
        "imported_signals": len(signal_id_by_key),
        "imported_feedback_events": len(feedback_id_by_old_id),
    }


def _build_signal_context_bundle(*, include_rejected: bool) -> dict[str, Any]:
    with repository.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT *
            FROM signal_radar_topics
            ORDER BY sort_order, name
            """
        )
        topics = list(cur.fetchall())
        cur.execute(
            """
            SELECT *
            FROM signals
            WHERE (%s OR maturity <> 'reject')
            ORDER BY score DESC, last_seen_at DESC, id DESC
            """,
            (include_rejected,),
        )
        signals = list(cur.fetchall())
        cur.execute(
            """
            SELECT se.*, s.signal_key
            FROM signal_evidence se
            JOIN signals s ON s.id = se.signal_id
            WHERE (%s OR s.maturity <> 'reject')
            ORDER BY s.score DESC, se.strength DESC, se.published_at DESC NULLS LAST, se.id DESC
            """,
            (include_rejected,),
        )
        evidence = list(cur.fetchall())
        cur.execute(
            """
            SELECT *
            FROM signal_agent_memory
            ORDER BY memory_type, score DESC, updated_at DESC
            """
        )
        memories = list(cur.fetchall())
        cur.execute(
            """
            WITH exported_signals AS (
              SELECT id, signal_key
              FROM signals
              WHERE (%s OR maturity <> 'reject')
            ),
            exported_urls AS (
              SELECT se.source_url
              FROM signal_evidence se
              JOIN exported_signals s ON s.id = se.signal_id
            )
            SELECT sfe.*,
                   s.signal_key,
                   duplicate_signal.signal_key AS duplicate_of_signal_key
            FROM signal_feedback_events sfe
            LEFT JOIN signals s ON s.id = sfe.signal_id
            LEFT JOIN signals duplicate_signal ON duplicate_signal.id = sfe.duplicate_of_signal_id
            WHERE sfe.signal_id IN (SELECT id FROM exported_signals)
               OR sfe.source_url IN (SELECT source_url FROM exported_urls)
            ORDER BY sfe.created_at DESC, sfe.id DESC
            """,
            (include_rejected,),
        )
        feedback = list(cur.fetchall())
        cur.execute(
            """
            SELECT ste.*, s.signal_key
            FROM signal_training_examples ste
            LEFT JOIN signals s ON s.id = ste.signal_id
            WHERE ste.signal_id IN (
              SELECT id FROM signals WHERE (%s OR maturity <> 'reject')
            )
            ORDER BY ste.created_at DESC, ste.id DESC
            """,
            (include_rejected,),
        )
        training_examples = list(cur.fetchall())
        cur.execute(
            """
            SELECT sgr.*
            FROM signal_generation_runs sgr
            WHERE sgr.id IN (
              SELECT generation_run_id
              FROM signal_training_examples
              WHERE generation_run_id IS NOT NULL
            )
            ORDER BY sgr.created_at DESC, sgr.id DESC
            """
        )
        generation_runs = list(cur.fetchall())
    return {
        "version": SIGNAL_CONTEXT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "include_rejected": include_rejected,
        "signal_radar_topics": topics,
        "signals": signals,
        "signal_evidence": evidence,
        "signal_agent_memory": memories,
        "signal_feedback_events": feedback,
        "signal_generation_runs": generation_runs,
        "signal_training_examples": training_examples,
    }


def _bundle_counts(bundle: dict[str, Any]) -> dict[str, int]:
    return {
        "topics": len(bundle.get("signal_radar_topics") or []),
        "signals": len(bundle.get("signals") or []),
        "evidence": len(bundle.get("signal_evidence") or []),
        "memories": len(bundle.get("signal_agent_memory") or []),
        "feedback_events": len(bundle.get("signal_feedback_events") or []),
        "generation_runs": len(bundle.get("signal_generation_runs") or []),
        "training_examples": len(bundle.get("signal_training_examples") or []),
    }


def _upsert_signal_radar_topic(topic: dict[str, Any]) -> None:
    with repository.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO signal_radar_topics (
              name, description, query_seeds_json, industry_scope_json, enabled, sort_order
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (name) DO UPDATE SET
              description = EXCLUDED.description,
              query_seeds_json = EXCLUDED.query_seeds_json,
              industry_scope_json = EXCLUDED.industry_scope_json,
              enabled = EXCLUDED.enabled,
              sort_order = EXCLUDED.sort_order,
              updated_at = now()
            """,
            (
                topic["name"],
                topic.get("description"),
                Json(_jsonable(topic.get("query_seeds_json") or [])),
                Json(_jsonable(topic.get("industry_scope_json") or [])),
                bool(topic.get("enabled", True)),
                int(topic.get("sort_order") or 0),
            ),
        )
        conn.commit()


def _signal_import_payload(signal: dict[str, Any]) -> dict[str, Any]:
    return {
        "signal_key": signal["signal_key"],
        "title": signal["title"],
        "title_ru": signal.get("title_ru"),
        "theme": signal["theme"],
        "summary": signal.get("summary"),
        "thesis": signal.get("thesis"),
        "transferability": signal.get("transferability"),
        "maturity": signal.get("maturity") or "watch",
        "confidence": signal.get("confidence") or 0,
        "score": signal.get("score") or 0,
        "why_now": signal.get("why_now"),
        "why_not_noise": signal.get("why_not_noise"),
        "companies": signal.get("companies_json") or [],
        "industries": signal.get("industries_json") or [],
        "evidence_count": signal.get("evidence_count") or 0,
    }


def _evidence_import_payload(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "article_id": None,
        "source_url": evidence["source_url"],
        "title": evidence["title"],
        "title_ru": evidence.get("title_ru"),
        "publisher": evidence.get("publisher"),
        "published_at": evidence.get("published_at"),
        "evidence_type": evidence.get("evidence_type") or "article",
        "extracted_fact": evidence.get("extracted_fact"),
        "summary_ru": evidence.get("summary_ru"),
        "strength": evidence.get("strength") or 0,
        "raw_payload": evidence.get("raw_payload_json") or {},
    }


def _mapped_signal_id(
    row: dict[str, Any],
    signal_id_by_old_id: dict[int, int],
    signal_id_by_key: dict[str, int],
) -> int | None:
    signal_key = row.get("signal_key")
    if signal_key and str(signal_key) in signal_id_by_key:
        return signal_id_by_key[str(signal_key)]
    old_id = row.get("signal_id")
    if old_id is not None:
        return signal_id_by_old_id.get(int(old_id))
    return None


def _insert_signal_feedback_event_once(
    feedback: dict[str, Any],
    *,
    signal_id_by_old_id: dict[int, int],
    signal_id_by_key: dict[str, int],
) -> int | None:
    signal_id = _mapped_signal_id(feedback, signal_id_by_old_id, signal_id_by_key)
    source_url = feedback.get("source_url")
    if signal_id is None and not source_url:
        return None
    duplicate_signal_key = feedback.get("duplicate_of_signal_key")
    duplicate_of_signal_id = (
        signal_id_by_key.get(str(duplicate_signal_key))
        if duplicate_signal_key
        else signal_id_by_old_id.get(int(feedback["duplicate_of_signal_id"]))
        if feedback.get("duplicate_of_signal_id") is not None
        else None
    )
    with repository.get_connection() as conn:
        cur = conn.execute(
            """
            SELECT id
            FROM signal_feedback_events
            WHERE COALESCE(signal_id, 0) = COALESCE(%s, 0)
              AND COALESCE(source_url, '') = COALESCE(%s, '')
              AND event_type = %s
              AND COALESCE(verdict, '') = COALESCE(%s, '')
              AND COALESCE(reason, '') = COALESCE(%s, '')
              AND COALESCE(comment, '') = COALESCE(%s, '')
              AND COALESCE(corrected_title, '') = COALESCE(%s, '')
              AND COALESCE(corrected_thesis, '') = COALESCE(%s, '')
            LIMIT 1
            """,
            (
                signal_id,
                source_url,
                feedback.get("event_type") or "comment_added",
                feedback.get("verdict"),
                feedback.get("reason"),
                feedback.get("comment"),
                feedback.get("corrected_title"),
                feedback.get("corrected_thesis"),
            ),
        )
        existing = cur.fetchone()
        if existing:
            return int(existing[0])
        cur = conn.execute(
            """
            INSERT INTO signal_feedback_events (
              article_id, signal_id, signal_evidence_id, source_url, signal_title,
              user_id, event_type, old_value, new_value, comment,
              verdict, reason, corrected_title, corrected_thesis, duplicate_of_signal_id
            )
            VALUES (NULL, %s, NULL, %s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                signal_id,
                source_url,
                feedback.get("signal_title"),
                feedback.get("event_type") or "comment_added",
                feedback.get("old_value"),
                feedback.get("new_value"),
                feedback.get("comment"),
                feedback.get("verdict"),
                feedback.get("reason"),
                feedback.get("corrected_title"),
                feedback.get("corrected_thesis"),
                duplicate_of_signal_id,
            ),
        )
        event_id = int(cur.fetchone()[0])
        conn.commit()
        return event_id


def _training_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "generation_run_id": row.get("generation_run_id"),
        "signal_id": row.get("signal_id"),
        "feedback_event_id": row.get("feedback_event_id"),
        "topic": row.get("topic"),
        "signal_key": row.get("signal_key"),
        "pipeline_verdict": row.get("pipeline_verdict"),
        "input": _json_object(row.get("input_json")),
        "raw_model_output": _json_object(row.get("raw_output_json")),
        "model_output": _json_object(row.get("normalized_output_json")),
        "expected_output": _expected_output(row),
        "feedback": _feedback_payload(row),
        "signal": {
            "title": row.get("signal_title"),
            "title_ru": row.get("signal_title_ru"),
            "theme": row.get("signal_theme"),
            "score": _json_default(row.get("signal_score")),
            "maturity": row.get("signal_maturity"),
        },
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _expected_output(row: dict[str, Any]) -> dict[str, Any]:
    expected = dict(_json_object(row.get("normalized_output_json")))
    feedback_verdict = row.get("feedback_verdict")
    corrected_title = _clean(row.get("feedback_corrected_title"))
    corrected_thesis = _clean(row.get("feedback_corrected_thesis"))
    duplicate_of = row.get("feedback_duplicate_of_signal_id")

    if feedback_verdict:
        expected["feedback_verdict"] = feedback_verdict
    if feedback_verdict == "reject":
        expected["maturity"] = "reject"
        expected["pipeline_verdict"] = "rejected"
    elif feedback_verdict == "approved":
        expected["pipeline_verdict"] = "accepted"
        if expected.get("maturity") == "reject":
            expected["maturity"] = "watch"
    elif feedback_verdict == "duplicate":
        expected["pipeline_verdict"] = "duplicate"
        expected["duplicate_of_signal_id"] = duplicate_of
    elif feedback_verdict == "needs_context":
        expected["pipeline_verdict"] = "needs_context"

    if corrected_title:
        expected["title"] = corrected_title
        expected["title_ru"] = corrected_title
    if corrected_thesis:
        expected["thesis"] = corrected_thesis
        expected.setdefault("summary", corrected_thesis)
    return expected


def _feedback_payload(row: dict[str, Any]) -> dict[str, Any] | None:
    if row.get("feedback_event_id") is None:
        return None
    return {
        "event_id": row.get("feedback_event_id"),
        "event_type": row.get("feedback_event_type"),
        "verdict": row.get("feedback_verdict"),
        "reason": row.get("feedback_reason"),
        "comment": row.get("feedback_comment"),
        "corrected_title": row.get("feedback_corrected_title"),
        "corrected_thesis": row.get("feedback_corrected_thesis"),
        "duplicate_of_signal_id": row.get("feedback_duplicate_of_signal_id"),
        "created_at": row.get("feedback_created_at"),
    }


def _json_object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return _json_default(value)
