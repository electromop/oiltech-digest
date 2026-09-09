from oiltech_digest.source_discovery import learning


def test_apply_candidate_learning_approve_boosts_domain_topic_query_strategy(monkeypatch):
    memory_updates = []
    actions = []

    monkeypatch.setattr(
        learning.repository,
        "get_source_candidate",
        lambda candidate_id: {
            "id": candidate_id,
            "url": "https://example.com/news",
            "topic": "роботизация бурения",
            "status": "approved",
            "recommended_action": "add",
            "tested_articles": 5,
            "relevant_articles": 4,
            "avg_score": 72,
            "duplicate_count": 0,
            "noise_count": 1,
        },
    )
    monkeypatch.setattr(learning.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(
        learning.repository,
        "list_agent_actions",
        lambda **kwargs: [{
            "output_json": {
                "candidate_id": 42,
                "discovery_query": "robotic drilling newsroom",
                "query_strategy": "newsroom",
                "topic": "роботизация бурения",
            }
        }],
    )
    monkeypatch.setattr(learning.repository, "upsert_agent_memory", lambda **kwargs: memory_updates.append(kwargs) or len(memory_updates))
    monkeypatch.setattr(learning.repository, "record_agent_action", lambda *args, **kwargs: actions.append((args, kwargs)) or 1)

    result = learning.apply_candidate_learning(42, event_type="approved", status="approved", recommended_action="add", source_id=99)

    by_type = {item["memory_type"]: item for item in memory_updates}
    assert by_type["domain"]["subject"] == "example.com"
    assert by_type["domain"]["score"] == 30
    assert by_type["topic"]["score"] == 18
    assert by_type["query"]["subject"] == "robotic drilling newsroom"
    assert by_type["strategy"]["subject"] == "newsroom"
    assert by_type["topic_query_domain"]["subject"] == "роботизация бурения | robotic drilling newsroom | example.com"
    assert by_type["topic_query_domain"]["score"] == 30
    assert by_type["topic_query_domain"]["facts"]["quality_funnel"]["score_50_plus"] == 0
    assert result["decision_kind"] == "approved"
    assert result["quality_funnel"]["relevant"] == 4
    assert actions[0][0][1] == "source_candidate_learning"


def test_apply_candidate_learning_duplicate_reject_does_not_penalize_topic(monkeypatch):
    memory_updates = []

    monkeypatch.setattr(
        learning.repository,
        "get_source_candidate",
        lambda candidate_id: {
            "id": candidate_id,
            "url": "https://duplicate.example.com/news",
            "topic": "роботизация бурения",
            "status": "rejected",
            "recommended_action": "reject",
            "review_comment": "Дубликат существующего источника",
        },
    )
    monkeypatch.setattr(learning.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(
        learning.repository,
        "list_agent_actions",
        lambda **kwargs: [{
            "output_json": {
                "candidate_id": 42,
                "discovery_query": "robotic drilling newsroom",
                "query_strategy": "newsroom",
            }
        }],
    )
    monkeypatch.setattr(learning.repository, "upsert_agent_memory", lambda **kwargs: memory_updates.append(kwargs) or len(memory_updates))
    monkeypatch.setattr(learning.repository, "record_agent_action", lambda *args, **kwargs: 1)

    result = learning.apply_candidate_learning(
        42,
        event_type="operator_update",
        status="rejected",
        recommended_action="reject",
        review_comment="Дубликат существующего источника",
    )

    assert result["decision_kind"] == "duplicate"
    assert {item["memory_type"] for item in memory_updates} == {"domain", "query", "strategy", "topic_query_domain"}
    assert all(item["memory_type"] != "topic" for item in memory_updates)
    assert next(item for item in memory_updates if item["memory_type"] == "domain")["score"] == -12
    assert next(item for item in memory_updates if item["memory_type"] == "topic_query_domain")["score"] == -12


def test_apply_candidate_learning_accumulates_existing_score(monkeypatch):
    memory_updates = []

    monkeypatch.setattr(
        learning.repository,
        "get_source_candidate",
        lambda candidate_id: {
            "id": candidate_id,
            "url": "https://example.com/news",
            "topic": "бурение",
            "status": "needs_human_review",
            "recommended_action": "test_more",
        },
    )
    monkeypatch.setattr(
        learning.repository,
        "list_agent_memory",
        lambda **kwargs: [{
            "memory_key": "domain:example.com",
            "score": 10,
            "facts_json": {"events": [{"delta": 10}]},
        }],
    )
    monkeypatch.setattr(learning.repository, "list_agent_actions", lambda **kwargs: [])
    monkeypatch.setattr(learning.repository, "upsert_agent_memory", lambda **kwargs: memory_updates.append(kwargs) or 1)
    monkeypatch.setattr(learning.repository, "record_agent_action", lambda *args, **kwargs: 1)

    learning.apply_candidate_learning(
        42,
        event_type="evaluated",
        recommended_action="test_more",
        metrics={"tested_articles": 3, "relevant_articles": 1, "avg_score": 60},
    )

    assert memory_updates[0]["memory_key"] == "domain:example.com"
    assert memory_updates[0]["score"] == 22
    assert len(memory_updates[0]["facts"]["events"]) == 2


def test_apply_candidate_learning_uses_real_article_quality_funnel(monkeypatch):
    memory_updates = []

    monkeypatch.setattr(
        learning.repository,
        "get_source_candidate",
        lambda candidate_id: {
            "id": candidate_id,
            "url": "https://example.com/news",
            "topic": "бурение",
            "status": "needs_human_review",
            "recommended_action": "test_more",
        },
    )
    monkeypatch.setattr(learning.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(
        learning.repository,
        "list_agent_actions",
        lambda **kwargs: [{
            "output_json": {
                "candidate_id": 42,
                "discovery_query": "managed pressure drilling",
                "query_strategy": "search",
            }
        }],
    )
    monkeypatch.setattr(learning.repository, "upsert_agent_memory", lambda **kwargs: memory_updates.append(kwargs) or len(memory_updates))
    monkeypatch.setattr(learning.repository, "record_agent_action", lambda *args, **kwargs: 1)

    result = learning.apply_candidate_learning(
        42,
        event_type="evaluated",
        recommended_action="test_more",
        metrics={
            "tested_articles": 6,
            "parsed_articles": 5,
            "kept_by_prefilter": 4,
            "processed_articles": 4,
            "relevant_articles": 3,
            "scored_articles": 4,
            "high_score_articles": 2,
            "avg_score": 68,
            "noise_count": 1,
            "duplicate_count": 0,
        },
    )

    assert result["decision_kind"] == "sandbox_positive"
    assert result["quality_funnel"] == {
        "found": 6,
        "parsed": 5,
        "prefilter_kept": 4,
        "processed": 4,
        "relevant": 3,
        "scored": 4,
        "score_50_plus": 2,
        "noise": 1,
        "duplicates": 0,
    }
    combo = next(item for item in memory_updates if item["memory_type"] == "topic_query_domain")
    assert combo["facts"]["metrics"]["high_score_rate"] == 0.3333
    assert combo["score"] == 22
