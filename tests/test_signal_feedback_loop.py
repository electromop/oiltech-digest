"""Обратная связь: вердикт «оценка завышена» принимается и не учит поиск, итоги недели."""

from __future__ import annotations

import pytest

from oiltech_digest import cli, signal_feedback
from oiltech_digest.db import repository

DRILLING = "Бурение, направленное бурение, буровые растворы и буровое оборудование"


# --- 5б. «Оценка завышена» -------------------------------------------------------------------


@pytest.mark.parametrize("value", ["overrated", "завышено", "Оценка завышена"])
def test_overrated_verdict_is_accepted(value):
    assert signal_feedback._normalize_verdict(value) == "overrated"


def test_overrated_is_a_weak_plus_not_a_search_hint():
    assert signal_feedback._verdict_score("overrated") == 30
    assert "overrated" not in signal_feedback.QUERY_HINT_VERDICTS
    assert "overrated" not in signal_feedback.NEGATIVE_VERDICTS


def _verdict(signal_id, verdict):
    with repository.get_connection() as conn:
        conn.execute("INSERT INTO signal_feedback_events (signal_id, event_type, verdict) VALUES (%s, 'verdict', %s)",
                     (signal_id, verdict))
        conn.commit()


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
