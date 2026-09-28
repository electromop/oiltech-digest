import json

from oiltech_digest import signal_training


def test_export_signal_training_jsonl_applies_feedback_corrections(tmp_path, monkeypatch):
    rows = [
        {
            "id": 10,
            "generation_run_id": 3,
            "signal_id": 42,
            "feedback_event_id": 9,
            "topic": "Бурение",
            "signal_key": "closed-loop-drilling",
            "pipeline_verdict": "accepted",
            "input_json": {"topic": "Бурение", "evidence": [{"source_url": "https://example.com/a"}]},
            "raw_output_json": {"score": 0.73},
            "normalized_output_json": {"title": "Old title", "score": 73, "maturity": "watch"},
            "feedback_event_type": "comment_added",
            "feedback_verdict": "approved",
            "feedback_reason": "Нужный сигнал",
            "feedback_corrected_title": "Внедрение управления бурением с замкнутым контуром",
            "feedback_corrected_thesis": "Оператор внедрил управление бурением с замкнутым контуром на промысле.",
            "feedback_duplicate_of_signal_id": None,
            "feedback_comment": "",
            "signal_title": "Old title",
            "signal_title_ru": "Old title",
            "signal_theme": "Бурение",
            "signal_score": 73,
            "signal_maturity": "watch",
        }
    ]
    captured = {}

    def fake_list_signal_training_examples(**kwargs):
        captured.update(kwargs)
        return rows

    monkeypatch.setattr(signal_training.repository, "list_signal_training_examples", fake_list_signal_training_examples)
    path = tmp_path / "signals.jsonl"

    result = signal_training.export_signal_training_jsonl(path, limit=5, with_feedback_only=True)

    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert result["examples"] == 1
    assert captured == {"limit": 5, "with_feedback_only": True, "verdict": None}
    assert record["input"]["evidence"][0]["source_url"] == "https://example.com/a"
    assert record["expected_output"]["title"] == "Внедрение управления бурением с замкнутым контуром"
    assert record["expected_output"]["thesis"] == "Оператор внедрил управление бурением с замкнутым контуром на промысле."
    assert record["expected_output"]["pipeline_verdict"] == "accepted"
    assert record["feedback"]["verdict"] == "approved"


def test_export_signal_training_jsonl_marks_reject_and_duplicate(tmp_path, monkeypatch):
    rows = [
        {
            "id": 11,
            "signal_id": 50,
            "feedback_event_id": 12,
            "topic": "Цифровизация",
            "signal_key": "noise",
            "pipeline_verdict": "accepted",
            "input_json": {},
            "raw_output_json": {},
            "normalized_output_json": {"maturity": "watch"},
            "feedback_verdict": "reject",
        },
        {
            "id": 12,
            "signal_id": 51,
            "feedback_event_id": 13,
            "topic": "Цифровизация",
            "signal_key": "same-fact",
            "pipeline_verdict": "accepted",
            "input_json": {},
            "raw_output_json": {},
            "normalized_output_json": {"maturity": "watch"},
            "feedback_verdict": "duplicate",
            "feedback_duplicate_of_signal_id": 50,
        },
    ]
    monkeypatch.setattr(signal_training.repository, "list_signal_training_examples", lambda **_: rows)
    path = tmp_path / "signals.jsonl"

    signal_training.export_signal_training_jsonl(path)

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert records[0]["expected_output"]["maturity"] == "reject"
    assert records[0]["expected_output"]["pipeline_verdict"] == "rejected"
    assert records[1]["expected_output"]["pipeline_verdict"] == "duplicate"
    assert records[1]["expected_output"]["duplicate_of_signal_id"] == 50
