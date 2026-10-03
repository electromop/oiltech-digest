"""Обратная связь работает: ревью видит вердикты, «завышено» калибрует баллы, итоги недели."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from oiltech_digest import cli, signal_discovery, signal_feedback
from oiltech_digest.db import repository
from oiltech_digest.processing.openai_client import AIResponse

DRILLING = "Бурение, направленное бурение, буровые растворы и буровое оборудование"
SNAPSHOT_ITEMS = [{"id": 101, "name": "Технологическая новизна", "weight": 50, "text_hash": "h1"},
                  {"id": 102, "name": "Ценность для нефтесервиса", "weight": 50, "text_hash": "h2"}]


def _memory(**types):
    base = {name: [] for name in signal_feedback.FEEDBACK_MEMORY_TYPES}
    base.update(types)
    return base


# --- 5а. Ревью пачки видит вердикты заказчика ------------------------------------------------


def test_batch_review_gets_customer_feedback(monkeypatch):
    sent = {}

    class Client:
        def complete_json(self, instructions, user_input, schema, **kwargs):
            sent.update(instructions=instructions, payload=user_input)
            return AIResponse(data={"decisions": []}, model="m")

    monkeypatch.setattr(signal_discovery, "make_client", lambda offline: Client())
    memory = _memory(signal_verdict=[{"memory_type": "signal_verdict", "subject": "wrong_block", "score": -80,
                                      "facts_json": {"signal_title": "SLB купила стартап", "reason": "сделка"}}])
    candidates = [{"signal": {"signal_key": f"k{i}", "title": f"t{i}", "score": 50}, "rejected": False} for i in range(2)]

    with signal_feedback.use_memory_snapshot(memory):
        signal_discovery._batch_review_candidates(candidates, DRILLING, offline=False)

    assert "SLB купила стартап" in sent["payload"]
    assert "customer_feedback" in sent["instructions"]


# --- 5б. «Оценка завышена» и калибровка баллов -----------------------------------------------


@pytest.mark.parametrize("value", ["overrated", "завышено", "Оценка завышена"])
def test_overrated_verdict_is_accepted(value):
    assert signal_feedback._normalize_verdict(value) == "overrated"


def test_overrated_is_a_weak_plus_not_a_search_hint():
    assert signal_feedback._verdict_score("overrated") == 30
    assert "overrated" not in signal_feedback.QUERY_HINT_VERDICTS
    assert "overrated" not in signal_feedback.NEGATIVE_VERDICTS


def _scored(key, novelty, value):
    signal_id = repository.upsert_signal({
        "signal_key": key, "title": key, "theme": DRILLING, "score": 60, "score_profile": "tech_radar",
        "score_items": [{"criterion_id": 101, "final_score": novelty}, {"criterion_id": 102, "final_score": value}],
        "criteria_snapshot": SNAPSHOT_ITEMS,
    })
    return signal_id


def _verdict(signal_id, verdict):
    with repository.get_connection() as conn:
        conn.execute("INSERT INTO signal_feedback_events (signal_id, event_type, verdict) VALUES (%s, 'verdict', %s)",
                     (signal_id, verdict))
        conn.commit()


def test_calibration_compares_criterion_scores_by_the_latest_verdict(isolated_db):
    for index in range(3):
        _verdict(_scored(f"over{index}", 80, 40), "overrated")
    good = _scored("good", 78, 85)
    _verdict(good, "reject")
    _verdict(good, "approved")  # последний вердикт решает

    rows = {row["name"]: row for row in repository.radar_score_calibration()}

    assert rows["Технологическая новизна"]["too_high_n"] == 3
    assert rows["Технологическая новизна"]["too_high_avg"] == 80.0
    assert rows["Технологическая новизна"]["fair_avg"] == 78.0
    assert rows["Ценность для нефтесервиса"]["too_high_avg"] == 40.0


def test_calibration_lines_only_where_the_judge_does_not_tell_apart():
    rows = [
        {"name": "Технологическая новизна", "too_high_n": 4, "too_high_avg": 80.0, "fair_n": 2, "fair_avg": 78.0},
        {"name": "Зрелость", "too_high_n": 4, "too_high_avg": 55.0, "fair_n": 3, "fair_avg": 85.0},  # различает
        {"name": "Свежесть", "too_high_n": 2, "too_high_avg": 90.0, "fair_n": 0, "fair_avg": None},  # мало вердиктов
        {"name": "Переносимость", "too_high_n": 5, "too_high_avg": 40.0, "fair_n": 0, "fair_avg": None},  # и так низко
    ]

    lines = signal_discovery._calibration_lines(rows)

    assert lines == ["- Технологическая новизна: у карточек, которые заказчик отклонил или счёл завышенными (4), "
                     "ты ставил в среднем 80; у одобренных — 78. Ставь по этому критерию строже."]


def test_calibration_reaches_the_judge_prompt():
    snapshot = {"radar_themes": [DRILLING], "radar_criteria": [{"id": 101, "name": "Технологическая новизна",
                                                                  "description": "новизна", "weight": 100}],
                "score_calibration": [{"name": "Технологическая новизна", "too_high_n": 3, "too_high_avg": 75.0,
                                       "fair_n": 0, "fair_avg": None}]}
    with signal_discovery.use_discovery_snapshot(snapshot):
        prompt = signal_discovery._judge_prompt([{"source_url": "https://a", "title": "t"}], DRILLING)

    assert "score_calibration (по вердиктам заказчика):" in prompt
    assert "ты ставил в среднем 75. Ставь по этому критерию строже." in prompt


def test_snapshot_carries_calibration(isolated_db, monkeypatch):
    monkeypatch.setattr(repository, "radar_score_calibration", lambda: [{"name": "x", "too_high_n": 1}])

    snapshot = signal_discovery.build_discovery_snapshot(signal_discovery.SignalDiscoveryConfig(web_only=True))

    assert snapshot["score_calibration"] == [{"name": "x", "too_high_n": 1}]


# --- 5в. Итоги недели ------------------------------------------------------------------------


def _card(key, **fields):
    signal_id = repository.upsert_signal({"signal_key": key, "title": key, "title_ru": key, "theme": DRILLING,
                                          "score": fields.pop("score", 60), **fields})
    repository.upsert_signal_evidence(signal_id, {"source_url": f"https://example.com/{key}", "title": key})
    return signal_id


def test_weekly_summary_counts_radar_filtered_reasons_review_and_feedback(isolated_db):
    best = _card("best", signal_category="technology", score=79)
    reviewed = _card("reviewed", signal_category="technology", score=60)
    _card("deal", signal_category="business")
    _card("deal2", signal_category="business")
    _card("noise", filter_stage="judge", filter_reason="обзор")
    old = _card("old", signal_category="technology")
    with repository.get_connection() as conn:
        conn.execute("UPDATE signals SET first_seen_at = now() - interval '30 days' WHERE id = %s", (old,))
        conn.commit()
    _verdict(reviewed, "approved")

    report = repository.radar_weekly_summary(days=7)

    assert (report["on_radar"], report["filtered"]) == (2, 3)
    assert report["filtered_reasons"] == [{"reason": "бизнес-сигнал", "count": 2},
                                          {"reason": "отсеяно (судья)", "count": 1}]
    assert [row["id"] for row in report["top"]] == [best, reviewed]
    # Ждут разбора — карточки радара без вердикта: best и вне недели old.
    assert report["awaiting_review"] == 2
    assert (report["feedback"], report["feedback_verdicts"]) == (1, [{"verdict": "approved", "count": 1}])


def test_weekly_api_and_cli(isolated_db, monkeypatch, capsys):
    from fastapi.testclient import TestClient

    from oiltech_digest import api

    _card("best", signal_category="technology", score=79)
    client = TestClient(api.app)
    try:
        api.app.dependency_overrides[api.require_user] = lambda: {"id": 2, "email": "u@example.com", "role": "user"}
        assert client.get("/api/signals/weekly").json()["on_radar"] == 1
    finally:
        api.app.dependency_overrides.pop(api.require_user, None)

    cli.main(["radar-weekly-report"])

    out = capsys.readouterr().out
    assert "На радаре новых: 1" in out and "#" in out and "best" in out
