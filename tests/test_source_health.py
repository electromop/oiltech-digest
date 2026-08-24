from oiltech_digest.source_discovery.source_health import assess_source_health, health_comment


def test_source_health_promotes_strong_regular_source():
    health = assess_source_health(
        {
            "tested_articles": 6,
            "processed_articles": 6,
            "relevant_articles": 5,
            "high_score_articles": 4,
            "avg_score": 76,
            "duplicate_count": 0,
            "noise_count": 1,
        },
        {"recommended_action": "test_more", "reason": "base"},
        {"quality_label": "сильный", "usefulness_score": 82, "confidence": 0.8},
        {"regularity_label": "regular", "dated_articles": 6, "archive_suspected": False},
    )

    assert health["recommended_action"] == "add"
    assert health["verdict"] == "strong_source"
    assert health["health_score"] >= 70
    assert "Итоговая оценка источника" in health_comment(health)


def test_source_health_rejects_stale_weak_source():
    health = assess_source_health(
        {
            "tested_articles": 5,
            "processed_articles": 5,
            "relevant_articles": 1,
            "high_score_articles": 0,
            "avg_score": 18,
            "duplicate_count": 0,
            "noise_count": 4,
        },
        {"recommended_action": "human_review", "reason": "base"},
        {"quality_label": "шумный", "usefulness_score": 12, "confidence": 0.7},
        {"regularity_label": "stale", "dated_articles": 5, "archive_suspected": False},
    )

    assert health["recommended_action"] == "reject"
    assert health["verdict"] == "stale"
    assert "нет подтвержденного свежего потока" in health["risks"]


def test_source_health_keeps_unknown_dates_on_more_testing():
    health = assess_source_health(
        {
            "tested_articles": 2,
            "processed_articles": 2,
            "relevant_articles": 1,
            "high_score_articles": 1,
            "avg_score": 58,
            "duplicate_count": 0,
            "noise_count": 0,
        },
        {"recommended_action": "test_more", "reason": "base"},
        {"quality_label": "перспективный", "usefulness_score": 52, "confidence": 0.45},
        {"regularity_label": "unknown", "dated_articles": 0, "archive_suspected": False},
    )

    assert health["recommended_action"] == "test_more"
    assert health["verdict"] == "promising_needs_more_data"
    assert "малая выборка" in health["risks"]
