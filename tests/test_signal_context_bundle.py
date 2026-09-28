import json

from oiltech_digest import signal_training
from oiltech_digest.db import repository


def test_signal_context_bundle_exports_runtime_context(isolated_db, tmp_path):
    repository.seed_signal_radar_topics([
        {
            "name": "Бурение",
            "description": "Автоматизация бурения",
            "query_seeds": ["closed-loop drilling"],
            "industry_scope": ["oilfield services"],
        }
    ])
    signal_id = repository.upsert_signal({
        "signal_key": "context-signal",
        "title": "Closed-loop drilling",
        "title_ru": "Управление бурением с замкнутым контуром",
        "theme": "Бурение",
        "summary": "Оператор внедрил систему.",
        "thesis": "Оператор внедрил систему.",
        "transferability": "Можно применять на кустах.",
        "maturity": "watch",
        "confidence": 0.8,
        "score": 74,
        "why_now": "Появился пилот.",
        "why_not_noise": "Есть объект внедрения.",
        "companies": ["Example Oil"],
        "industries": ["oil and gas"],
        "evidence_count": 1,
    })
    repository.upsert_signal_evidence(signal_id, {
        "source_url": "https://example.com/closed-loop",
        "title": "Closed-loop drilling",
        "publisher": "Example",
        "strength": 80,
    })
    repository.upsert_signal_agent_memory(
        memory_key="signal:query:closed-loop",
        memory_type="signal_query_hint",
        subject="closed-loop drilling",
        score=90,
        facts={"query": "closed-loop drilling oilfield deployment"},
    )
    repository.record_signal_feedback_event(
        None,
        "comment_added",
        signal_id=signal_id,
        verdict="approved",
        reason="Хороший промышленный сигнал",
    )
    path = tmp_path / "bundle.json"

    result = signal_training.export_signal_context_bundle(path)

    bundle = json.loads(path.read_text(encoding="utf-8"))
    assert result["signals"] == 1
    assert bundle["version"] == signal_training.SIGNAL_CONTEXT_VERSION
    assert bundle["signals"][0]["signal_key"] == "context-signal"
    assert bundle["signal_evidence"][0]["signal_key"] == "context-signal"
    assert bundle["signal_agent_memory"][0]["memory_type"] == "signal_query_hint"
    assert bundle["signal_feedback_events"][0]["signal_key"] == "context-signal"


def test_signal_context_bundle_import_dry_run_counts(tmp_path):
    path = tmp_path / "bundle.json"
    path.write_text(
        json.dumps(
            {
                "version": signal_training.SIGNAL_CONTEXT_VERSION,
                "signal_radar_topics": [{"name": "Бурение"}],
                "signals": [{"signal_key": "s1"}],
                "signal_evidence": [{"source_url": "https://example.com/a"}],
                "signal_agent_memory": [{"memory_key": "m1"}],
                "signal_feedback_events": [{"id": 1}],
                "signal_generation_runs": [],
                "signal_training_examples": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = signal_training.import_signal_context_bundle(path, dry_run=True)

    assert result["dry_run"] is True
    assert result["topics"] == 1
    assert result["signals"] == 1
    assert result["evidence"] == 1
    assert result["memories"] == 1
    assert result["feedback_events"] == 1


def test_signal_context_bundle_import_restores_runtime_context(isolated_db, tmp_path):
    path = tmp_path / "bundle.json"
    path.write_text(
        json.dumps(
            {
                "version": signal_training.SIGNAL_CONTEXT_VERSION,
                "signal_radar_topics": [
                    {
                        "name": "Бурение",
                        "description": "Автоматизация бурения",
                        "query_seeds_json": ["closed-loop drilling"],
                        "industry_scope_json": ["oilfield services"],
                        "enabled": True,
                        "sort_order": 1,
                    }
                ],
                "signals": [
                    {
                        "id": 100,
                        "signal_key": "imported-context-signal",
                        "title": "Closed-loop drilling",
                        "title_ru": "Управление бурением с замкнутым контуром",
                        "theme": "Бурение",
                        "summary": "Оператор внедрил систему.",
                        "thesis": "Оператор внедрил систему.",
                        "transferability": "Можно применять на кустах.",
                        "maturity": "watch",
                        "confidence": 0.8,
                        "score": 74,
                        "why_now": "Появился пилот.",
                        "why_not_noise": "Есть объект внедрения.",
                        "companies_json": ["Example Oil"],
                        "industries_json": ["oil and gas"],
                        "evidence_count": 1,
                    }
                ],
                "signal_evidence": [
                    {
                        "signal_id": 100,
                        "signal_key": "imported-context-signal",
                        "source_url": "https://example.com/imported-context",
                        "title": "Closed-loop drilling",
                        "publisher": "Example",
                        "strength": 80,
                    }
                ],
                "signal_agent_memory": [
                    {
                        "memory_key": "signal:query:imported-context",
                        "memory_type": "signal_query_hint",
                        "subject": "closed-loop drilling",
                        "status": "active",
                        "score": 90,
                        "facts_json": {"query": "closed-loop drilling oilfield deployment"},
                    }
                ],
                "signal_feedback_events": [
                    {
                        "id": 200,
                        "signal_id": 100,
                        "signal_key": "imported-context-signal",
                        "event_type": "comment_added",
                        "verdict": "approved",
                        "reason": "Хороший промышленный сигнал",
                    }
                ],
                "signal_generation_runs": [],
                "signal_training_examples": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = signal_training.import_signal_context_bundle(path, dry_run=False)

    assert result["dry_run"] is False
    assert result["imported_signals"] == 1
    signals = repository.list_signals(limit=10)
    assert signals[0]["signal_key"] == "imported-context-signal"
    evidence = repository.list_signal_evidence(int(signals[0]["id"]), limit=10)
    assert evidence[0]["source_url"] == "https://example.com/imported-context"
    memories = repository.list_signal_agent_memory(memory_type="signal_query_hint", limit=10)
    assert memories[0]["memory_key"] == "signal:query:imported-context"
    with repository.get_connection() as conn:
        feedback_count = conn.execute("SELECT COUNT(*) FROM signal_feedback_events").fetchone()[0]
    assert feedback_count == 1
