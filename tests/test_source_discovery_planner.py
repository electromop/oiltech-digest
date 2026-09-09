from datetime import datetime, timezone

from oiltech_digest.source_discovery import planner


def test_build_plan_prioritizes_topic_gaps_and_persists_memory(monkeypatch):
    memory_updates = []
    actions = []

    monkeypatch.setattr(
        planner.repository,
        "compute_topic_gap_rows",
        lambda period_from, period_to, target_per_topic, limit: [
            {
                "topic": "Роботизация бурения",
                "signals": 1,
                "target_signals": 10,
                "gap": 9,
                "avg_score": 40,
                "digest_count": 0,
                "priority": 90,
            }
        ],
    )
    monkeypatch.setattr(planner.repository, "compute_source_quality_rows", lambda period_from, period_to: [])
    monkeypatch.setattr(planner.repository, "list_source_candidates", lambda limit=200: [])
    monkeypatch.setattr(
        planner.repository,
        "list_agent_memory",
        lambda **kwargs: [
            {
                "subject": "robotic drilling automation newsroom",
                "facts_json": {"topic": "Роботизация бурения"},
                "score": 90,
            }
        ] if kwargs.get("memory_type") == "query" else [],
    )
    monkeypatch.setattr(
        planner.repository,
        "upsert_agent_memory",
        lambda **kwargs: memory_updates.append(kwargs) or 1,
    )
    monkeypatch.setattr(
        planner.repository,
        "record_agent_action",
        lambda task_id, action_type, **kwargs: actions.append({"action_type": action_type, **kwargs}) or 1,
    )

    plan = planner.build_plan(planner.PlannerConfig(topic_limit=1, candidate_limit=7, max_actions=3))

    assert plan["actions"][0]["action_type"] == "discover_sources"
    assert plan["actions"][0]["topic"] == "Роботизация бурения"
    assert plan["actions"][0]["limit"] == 7
    assert plan["actions"][0]["priority"] == 100
    assert plan["actions"][0]["query_hints"] == ["robotic drilling automation newsroom"]
    assert plan["actions"][0]["policy_decision"] == "auto"
    assert plan["actions"][0]["requires_human_approval"] is False
    assert plan["actions"][0]["operator_label"] == "Показать кандидатов по теме"
    assert "screen=source-candidates" in plan["actions"][0]["operator_url"]
    assert "%D0%A0%D0%BE%D0%B1%D0%BE%D1%82%D0%B8%D0%B7%D0%B0%D1%86%D0%B8%D1%8F" in plan["actions"][0]["operator_url"]
    assert plan["policy"] == {"auto": 1, "human_review": 0, "blocked": 0}
    assert plan["inputs"]["query_memory_count"] == 1
    assert memory_updates[0]["memory_key"] == "topic:роботизация бурения"
    assert actions[0]["action_type"] == "source_discovery_plan_built"


def test_build_plan_skips_rejected_topic_memory(monkeypatch):
    monkeypatch.setattr(
        planner.repository,
        "compute_topic_gap_rows",
        lambda period_from, period_to, target_per_topic, limit: [
            {
                "topic": "Роботизация бурения",
                "signals": 0,
                "target_signals": 10,
                "gap": 10,
                "avg_score": None,
                "digest_count": 0,
                "priority": 95,
            }
        ],
    )
    monkeypatch.setattr(planner.repository, "compute_source_quality_rows", lambda period_from, period_to: [])
    monkeypatch.setattr(planner.repository, "list_source_candidates", lambda limit=200: [])

    def fake_memory(**kwargs):
        if kwargs.get("memory_type") == "topic" and kwargs.get("status") == "rejected":
            return [{"subject": "Роботизация бурения", "score": 0, "facts_json": {"manual": True}}]
        return []

    monkeypatch.setattr(planner.repository, "list_agent_memory", fake_memory)
    monkeypatch.setattr(planner.repository, "upsert_agent_memory", lambda **kwargs: 1)
    monkeypatch.setattr(planner.repository, "record_agent_action", lambda *args, **kwargs: 1)

    plan = planner.build_plan(planner.PlannerConfig(topic_limit=1, max_actions=3))

    assert not [action for action in plan["actions"] if action.get("action_type") == "discover_sources"]


def test_build_plan_explains_memory_combos(monkeypatch):
    monkeypatch.setattr(
        planner.repository,
        "compute_topic_gap_rows",
        lambda period_from, period_to, target_per_topic, limit: [
            {
                "topic": "Роботизация бурения",
                "signals": 1,
                "target_signals": 10,
                "gap": 9,
                "avg_score": 40,
                "digest_count": 0,
                "priority": 80,
            }
        ],
    )
    monkeypatch.setattr(planner.repository, "compute_source_quality_rows", lambda period_from, period_to: [])
    monkeypatch.setattr(planner.repository, "list_source_candidates", lambda limit=200: [])

    def fake_memory(**kwargs):
        memory_type = kwargs.get("memory_type")
        if memory_type == "topic_query_domain":
            return [
                {
                    "subject": "x",
                    "status": "active",
                    "score": 42,
                    "facts_json": {
                        "topic": "Роботизация бурения",
                        "query": "autonomous drilling source",
                        "domain": "useful.example.com",
                    },
                },
                {
                    "subject": "y",
                    "status": "muted",
                    "score": -15,
                    "facts_json": {
                        "topic": "Роботизация бурения",
                        "query": "bad drilling source",
                        "domain": "bad.example.com",
                    },
                },
            ]
        if memory_type == "query":
            return [{
                "subject": "robotic drilling automation newsroom",
                "score": 10,
                "facts_json": {"topic": "Роботизация бурения"},
            }]
        return []

    monkeypatch.setattr(planner.repository, "list_agent_memory", fake_memory)
    monkeypatch.setattr(planner.repository, "upsert_agent_memory", lambda **kwargs: 1)
    monkeypatch.setattr(planner.repository, "record_agent_action", lambda *args, **kwargs: 1)

    plan = planner.build_plan(planner.PlannerConfig(topic_limit=1, max_actions=1))
    action = plan["actions"][0]

    assert action["query_hints"][:2] == ["autonomous drilling source", "robotic drilling automation newsroom"]
    assert action["memory_explanation"]["promoted_combos"][0]["domain"] == "useful.example.com"
    assert action["memory_explanation"]["muted_combos"][0]["domain"] == "bad.example.com"
    assert plan["inputs"]["combo_memory_count"] == 2


def test_build_plan_uses_loop_reflection_hints(monkeypatch):
    monkeypatch.setattr(
        planner.repository,
        "compute_topic_gap_rows",
        lambda period_from, period_to, target_per_topic, limit: [
            {
                "topic": "Роботизация бурения",
                "signals": 1,
                "target_signals": 10,
                "gap": 9,
                "avg_score": 40,
                "digest_count": 0,
                "priority": 70,
            }
        ],
    )
    monkeypatch.setattr(planner.repository, "compute_source_quality_rows", lambda period_from, period_to: [])
    monkeypatch.setattr(planner.repository, "list_source_candidates", lambda limit=200: [])

    def fake_memory(**kwargs):
        if kwargs.get("memory_type") == "reflection":
            return [
                {
                    "subject": "Найти источники",
                    "score": 80,
                    "facts_json": {
                        "next_hints": [
                            {
                                "kind": "change_strategy",
                                "topic": "Роботизация бурения",
                                "strategy": "technical",
                                "reason": "В прошлом запуске тема не дала кандидатов.",
                            }
                        ]
                    },
                }
            ]
        return []

    monkeypatch.setattr(planner.repository, "list_agent_memory", fake_memory)
    monkeypatch.setattr(planner.repository, "upsert_agent_memory", lambda **kwargs: 1)
    monkeypatch.setattr(planner.repository, "record_agent_action", lambda *args, **kwargs: 1)

    plan = planner.build_plan(planner.PlannerConfig(topic_limit=1, max_actions=1))
    action = plan["actions"][0]

    assert action["query_hints"][0] == "Роботизация бурения технология исследование внедрение"
    assert "Вывод прошлого запуска" in action["reason"]
    assert action["memory_explanation"]["reflection"]["priority_boost"] == 4.0
    assert plan["inputs"]["reflection_memory_count"] == 1


def test_build_plan_surfaces_candidates_for_human_review(monkeypatch):
    monkeypatch.setattr(planner.repository, "compute_topic_gap_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(planner.repository, "compute_source_quality_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        planner.repository,
        "list_source_candidates",
        lambda limit=200: [
            {
                "id": 42,
                "url": "https://example.com/news",
                "topic": "бурение",
                "status": "needs_human_review",
                "recommended_action": "add",
                "tested_articles": 5,
                "relevant_articles": 4,
                "avg_score": 75,
                "noise_count": 1,
                "duplicate_count": 0,
            }
        ],
    )
    monkeypatch.setattr(planner.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(planner.repository, "upsert_agent_memory", lambda **kwargs: 1)
    monkeypatch.setattr(planner.repository, "record_agent_action", lambda *args, **kwargs: 1)

    plan = planner.build_plan(planner.PlannerConfig(max_actions=3))

    assert plan["actions"][0]["action_type"] == "review_source_candidate"
    assert plan["actions"][0]["candidate_id"] == 42
    assert plan["actions"][0]["priority"] == 80
    assert plan["actions"][0]["policy_decision"] == "human_review"
    assert plan["actions"][0]["requires_human_approval"] is True
    assert plan["actions"][0]["operator_label"] == "Открыть кандидата"
    assert "candidate_id=42" in plan["actions"][0]["operator_url"]
    assert "status=needs_human_review" in plan["actions"][0]["operator_url"]
    assert plan["policy"] == {"auto": 0, "human_review": 1, "blocked": 0}


def test_build_plan_uses_candidate_triage_order(monkeypatch):
    monkeypatch.setattr(planner.repository, "compute_topic_gap_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(planner.repository, "compute_source_quality_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(planner.repository, "list_source_candidates", lambda limit=200: [{"id": 1}, {"id": 2}])
    monkeypatch.setattr(
        planner.repository,
        "source_candidate_triage_report",
        lambda limit=50: [
            {
                "id": 2,
                "url": "https://ready.example.com/news",
                "topic": "бурение",
                "status": "needs_human_review",
                "recommended_action": "add",
                "tested_articles": 5,
                "relevant_articles": 4,
                "avg_score": 80,
                "triage_priority": 98,
                "triage_reason": "Можно добавлять после проверки человеком",
            },
            {
                "id": 1,
                "url": "https://maybe.example.com/news",
                "topic": "бурение",
                "status": "test_parsing",
                "recommended_action": "test_more",
                "tested_articles": 2,
                "relevant_articles": 1,
                "avg_score": 50,
                "triage_priority": 64,
                "triage_reason": "Нужна дополнительная песочница",
            },
        ],
    )
    monkeypatch.setattr(planner.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(planner.repository, "upsert_agent_memory", lambda **kwargs: 1)
    monkeypatch.setattr(planner.repository, "record_agent_action", lambda *args, **kwargs: 1)

    plan = planner.build_plan(planner.PlannerConfig(max_actions=2))

    assert [item["candidate_id"] for item in plan["actions"]] == [2, 1]
    assert plan["actions"][0]["priority"] == 98
    assert "Можно добавлять" in plan["actions"][0]["reason"]
    assert "status=needs_human_review" in plan["actions"][0]["operator_url"]
    assert "status=test_parsing" in plan["actions"][1]["operator_url"]


def test_build_plan_recommends_source_frequency_tuning(monkeypatch):
    monkeypatch.setattr(planner.repository, "compute_topic_gap_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        planner.repository,
        "compute_source_quality_rows",
        lambda *args, **kwargs: [
            {
                "source_id": 11,
                "source_name": "Strong Source",
                "articles_found": 12,
                "relevant_count": 8,
                "digest_count": 3,
                "noise_count": 0,
                "duplicate_count": 0,
                "quality_score": 72,
            },
            {
                "source_id": 12,
                "source_name": "Noisy Source",
                "articles_found": 12,
                "relevant_count": 1,
                "digest_count": 0,
                "noise_count": 8,
                "duplicate_count": 0,
                "quality_score": 10,
            },
        ],
    )
    monkeypatch.setattr(planner.repository, "list_source_candidates", lambda limit=200: [])
    monkeypatch.setattr(planner.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(planner.repository, "upsert_agent_memory", lambda **kwargs: 1)
    monkeypatch.setattr(planner.repository, "record_agent_action", lambda *args, **kwargs: 1)

    plan = planner.build_plan(planner.PlannerConfig(max_actions=5))
    frequency_actions = [item for item in plan["actions"] if item["action_type"] == "tune_source_frequency"]

    assert {item["source_name"] for item in frequency_actions} == {"Strong Source", "Noisy Source"}
    assert {item["recommended_frequency"] for item in frequency_actions} == {"чаще", "реже"}
    assert all(item["policy_decision"] == "human_review" for item in frequency_actions)
    assert all(item["operator_url"].startswith("/?screen=sources") for item in frequency_actions)
    assert any("update_frequency=%D0%B5%D0%B6%D0%B5%D1%87%D0%B0%D1%81%D0%BD%D0%BE" in item["operator_url"] for item in frequency_actions)
    assert any("update_frequency=%D0%B5%D0%B6%D0%B5%D0%BD%D0%B5%D0%B4%D0%B5%D0%BB%D1%8C%D0%BD%D0%BE" in item["operator_url"] for item in frequency_actions)


def test_build_plan_adds_existing_source_audit_actions_and_source_memory(monkeypatch):
    memory_updates = []
    monkeypatch.setattr(planner.repository, "compute_topic_gap_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        planner.repository,
        "compute_source_quality_rows",
        lambda *args, **kwargs: [
            {
                "source_id": 21,
                "source_name": "Silent Source",
                "enabled": True,
                "parse_strategy": "request",
                "network_region": "auto",
                "network_profile": "direct",
                "last_ru_probe_status": None,
                "last_external_probe_status": None,
                "external_required_reason": None,
                "articles_found": 0,
                "articles_processed": 0,
                "relevant_count": 0,
                "digest_count": 0,
                "noise_count": 0,
                "duplicate_count": 0,
                "avg_score": None,
                "quality_score": 0,
            },
            {
                "source_id": 22,
                "source_name": "Blocked Source",
                "enabled": True,
                "parse_strategy": "request",
                "network_region": "auto",
                "network_profile": "direct",
                "last_ru_probe_status": "403",
                "last_external_probe_status": None,
                "external_required_reason": "Источник не открывается из RU",
                "articles_found": 8,
                "articles_processed": 0,
                "relevant_count": 0,
                "digest_count": 0,
                "noise_count": 0,
                "duplicate_count": 0,
                "avg_score": None,
                "quality_score": 5,
            },
        ],
    )
    monkeypatch.setattr(planner.repository, "list_source_candidates", lambda limit=200: [])
    monkeypatch.setattr(planner.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(planner.repository, "upsert_agent_memory", lambda **kwargs: memory_updates.append(kwargs) or 1)
    monkeypatch.setattr(planner.repository, "record_agent_action", lambda *args, **kwargs: 1)

    plan = planner.build_plan(planner.PlannerConfig(max_actions=5))
    audit_actions = [item for item in plan["actions"] if item["action_type"] == "audit_existing_source"]

    assert {item["source_name"] for item in audit_actions} == {"Silent Source", "Blocked Source"}
    assert all(item["policy_decision"] == "human_review" for item in audit_actions)
    assert any(item["audit_recommendation"] == "diagnose_or_pause" for item in audit_actions)
    assert any(item["audit_recommendation"] == "move_to_external_region" for item in audit_actions)
    assert any(item["audit_recommendation_label"] == "Перенести в external-worker" for item in audit_actions)
    blocked_action = next(item for item in audit_actions if item["source_name"] == "Blocked Source")
    assert blocked_action["audit_problem_type"] == "needs_external"
    assert blocked_action["audit_severity"] == "high"
    assert blocked_action["audit_confidence"] == "high"
    assert blocked_action["audit_decision_log"]["triggered_rules"][0]["rule"] == "low_quality"
    assert any(rule["rule"] == "needs_external" for rule in blocked_action["audit_decision_log"]["triggered_rules"])
    assert any(rule["rule"] == "parser_suspect" for rule in blocked_action["audit_decision_log"]["suppressed_rules"])
    source_memory = [item for item in memory_updates if item["memory_type"] == "source"]
    assert {item["memory_key"] for item in source_memory} == {"source:21", "source:22"}
    assert all(item["status"] == "muted" for item in source_memory)
    assert any(item["facts"]["recommendation"] == "move_to_external_region" for item in source_memory)
    assert any(item["facts"]["recommendation_label"] == "Перенести в external-worker" for item in source_memory)
    assert any(item["facts"]["confidence"] == "high" for item in source_memory)


def test_source_memory_recommends_increasing_frequency_for_stable_useful_source(monkeypatch):
    memory_updates = []
    monkeypatch.setattr(planner.repository, "compute_topic_gap_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        planner.repository,
        "compute_source_quality_rows",
        lambda *args, **kwargs: [{
            "source_id": 31,
            "source_name": "Strong Source",
            "enabled": True,
            "parse_strategy": "rss",
            "network_region": "auto",
            "network_profile": "direct",
            "articles_found": 12,
            "articles_processed": 12,
            "relevant_count": 8,
            "digest_count": 3,
            "noise_count": 0,
            "duplicate_count": 0,
            "avg_score": 78,
            "quality_score": 72,
        }],
    )
    monkeypatch.setattr(planner.repository, "list_source_candidates", lambda limit=200: [])
    monkeypatch.setattr(planner.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(planner.repository, "upsert_agent_memory", lambda **kwargs: memory_updates.append(kwargs) or 1)
    monkeypatch.setattr(planner.repository, "record_agent_action", lambda *args, **kwargs: 1)

    planner.build_plan(planner.PlannerConfig(max_actions=5))

    source_memory = next(item for item in memory_updates if item["memory_type"] == "source")
    assert source_memory["status"] == "active"
    assert source_memory["facts"]["problem_type"] == "stable"
    assert source_memory["facts"]["confidence"] == "high"
    assert source_memory["facts"]["recommendation"] == "increase_frequency"
    assert source_memory["facts"]["recommendation_label"] == "Проверять чаще"


def test_enqueue_plan_actions_creates_discovery_jobs(monkeypatch):
    jobs = []
    monkeypatch.setattr(
        planner.repository,
        "create_background_job",
        lambda kind, payload, **kwargs: jobs.append({"kind": kind, "payload": payload, **kwargs}) or {"id": 55},
    )

    result = planner.enqueue_plan_actions(
        {
            "actions": [
                {
                    "action_type": "discover_sources",
                    "topic": "бурение",
                    "priority": 90,
                    "limit": 8,
                    "reason": "gap",
                    "policy_decision": "auto",
                },
                {
                    "action_type": "review_source_candidate",
                    "candidate_id": 42,
                    "priority": 80,
                    "reason": "ready",
                    "policy_decision": "human_review",
                },
            ]
        },
        offline=True,
        evaluate=False,
        run_id=777,
    )

    assert result == {
        "queued": 1,
        "jobs": [{"job_id": 55, "kind": "discover_source_candidates", "topic": "бурение", "priority": 90}],
    }
    assert jobs[0]["kind"] == "discover_source_candidates"
    assert jobs[0]["payload"]["topics"] == ["бурение"]
    assert jobs[0]["payload"]["agent_run_id"] == 777
    assert jobs[0]["payload"]["auto_evaluate"] is False
    assert jobs[0]["queue_name"] == "default"
    assert jobs[0]["agent_run_id"] == 777


def test_enqueue_plan_actions_skips_actions_without_auto_policy(monkeypatch):
    jobs = []
    monkeypatch.setattr(
        planner.repository,
        "create_background_job",
        lambda kind, payload, **kwargs: jobs.append({"kind": kind, "payload": payload, **kwargs}) or {"id": 55},
    )

    result = planner.enqueue_plan_actions(
        {
            "actions": [
                {
                    "action_type": "discover_sources",
                    "topic": "бурение",
                    "priority": 90,
                    "limit": 8,
                    "reason": "gap",
                    "policy_decision": "human_review",
                },
                {"action_type": "discover_sources", "topic": "геология", "priority": 80, "limit": 8, "reason": "gap"},
            ]
        }
    )

    assert result == {"queued": 0, "jobs": []}
    assert jobs == []


def test_memory_score_for_bad_candidate_is_lower_than_good_candidate():
    good = planner._candidate_memory_score({
        "tested_articles": 5,
        "relevant_articles": 4,
        "avg_score": 80,
        "noise_count": 0,
        "duplicate_count": 0,
    })
    bad = planner._candidate_memory_score({
        "tested_articles": 5,
        "relevant_articles": 1,
        "avg_score": 20,
        "noise_count": 3,
        "duplicate_count": 1,
    })

    assert good > bad


def test_candidate_operator_decision_changes_domain_memory_score():
    approved = planner._candidate_memory_score({
        "status": "approved",
        "recommended_action": "add",
        "tested_articles": 5,
        "relevant_articles": 4,
        "avg_score": 70,
        "noise_count": 0,
        "duplicate_count": 0,
    })
    rejected = planner._candidate_memory_score({
        "status": "rejected",
        "recommended_action": "reject",
        "tested_articles": 5,
        "relevant_articles": 4,
        "avg_score": 70,
        "noise_count": 0,
        "duplicate_count": 0,
    })

    assert approved > rejected
    assert planner._candidate_memory_status({"status": "rejected"}) == "rejected"
    assert planner._candidate_memory_status({"status": "paused"}) == "muted"


def test_build_plan_uses_operator_feedback_in_topic_priority(monkeypatch):
    memory_updates = []
    monkeypatch.setattr(
        planner.repository,
        "compute_topic_gap_rows",
        lambda *args, **kwargs: [
            {
                "topic": "Роботизация",
                "signals": 2,
                "target_signals": 10,
                "gap": 8,
                "avg_score": 50,
                "digest_count": 0,
                "priority": 60,
            }
        ],
    )
    monkeypatch.setattr(planner.repository, "compute_source_quality_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        planner.repository,
        "list_source_candidates",
        lambda limit=200: [
            {
                "id": 1,
                "url": "https://bad.example.com/news",
                "topic": "Роботизация",
                "status": "rejected",
                "recommended_action": "reject",
                "tested_articles": 5,
                "relevant_articles": 1,
                "avg_score": 20,
                "noise_count": 4,
                "duplicate_count": 0,
            },
            {
                "id": 2,
                "url": "https://good.example.com/news",
                "topic": "Роботизация",
                "status": "approved",
                "recommended_action": "add",
                "tested_articles": 5,
                "relevant_articles": 4,
                "avg_score": 80,
                "noise_count": 0,
                "duplicate_count": 0,
            },
        ],
    )
    monkeypatch.setattr(planner.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(planner.repository, "upsert_agent_memory", lambda **kwargs: memory_updates.append(kwargs) or 1)
    monkeypatch.setattr(planner.repository, "record_agent_action", lambda *args, **kwargs: 1)

    plan = planner.build_plan(planner.PlannerConfig(topic_limit=1, max_actions=1))

    assert plan["learning"]["approved"] == 1
    assert plan["learning"]["rejected"] == 1
    assert plan["learning"]["approval_rate"] == 0.5
    assert plan["actions"][0]["priority"] == 58
    assert "Обратная связь" in plan["actions"][0]["reason"]
    domain_updates = {item["memory_key"]: item for item in memory_updates if item["memory_type"] == "domain"}
    assert domain_updates["domain:bad.example.com"]["status"] == "rejected"
    assert domain_updates["domain:good.example.com"]["status"] == "active"


def test_planner_period_is_timezone_aware(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        planner.repository,
        "compute_topic_gap_rows",
        lambda period_from, period_to, *args, **kwargs: captured.update({"from": period_from, "to": period_to}) or [],
    )
    monkeypatch.setattr(planner.repository, "compute_source_quality_rows", lambda period_from, period_to: [])
    monkeypatch.setattr(planner.repository, "list_source_candidates", lambda limit=200: [])
    monkeypatch.setattr(planner.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(planner.repository, "record_agent_action", lambda *args, **kwargs: 1)

    planner.build_plan(planner.PlannerConfig(persist_memory=False))

    assert isinstance(captured["from"], datetime)
    assert captured["from"].tzinfo == timezone.utc
    assert captured["to"].tzinfo == timezone.utc
