from oiltech_digest.source_discovery import loop


def _stub_loop_memory(monkeypatch, updates=None):
    monkeypatch.setattr(loop.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(loop.repository, "upsert_agent_memory", lambda **kwargs: (updates.append(kwargs) if updates is not None else None) or 1)
    monkeypatch.setattr(loop.repository, "source_discovery_daily_usage", lambda: {"loop_runs": 1, "candidates_created": 0, "candidate_evaluations": 0})


def test_agent_loop_runs_auto_discovery_iterations(monkeypatch):
    actions = []
    finished = []
    plans = []
    memory = []

    _stub_loop_memory(monkeypatch, memory)
    monkeypatch.setattr(loop.repository, "create_agent_run", lambda *args, **kwargs: 77)
    monkeypatch.setattr(loop.repository, "finish_agent_run", lambda *args, **kwargs: finished.append((args, kwargs)))
    monkeypatch.setattr(loop.repository, "record_agent_action", lambda *args, **kwargs: actions.append((args, kwargs)) or 1)
    monkeypatch.setattr(loop, "evaluate_source_candidate", lambda *args, **kwargs: {"metrics": {"relevant_articles": 0, "avg_score": None}})

    def fake_plan(config):
        plans.append(config)
        return {
            "policy": {"auto": 1, "human_review": 0, "blocked": 0},
            "learning": {"approved": 0},
            "actions": [
                {
                    "action_type": "discover_sources",
                    "policy_decision": "auto",
                    "topic": "бурение",
                    "priority": 90,
                    "limit": 3,
                }
            ],
        }

    monkeypatch.setattr(loop, "build_plan", fake_plan)
    monkeypatch.setattr(
        loop,
        "discover_sources",
        lambda config: {
            "task_id": 10,
            "queries": ["q1"],
            "search": {"status": "ok"},
            "candidates": [{"id": 1}, {"id": 2}],
        },
    )

    result = loop.run_agent_loop(loop.AgentLoopConfig(max_iterations=2, candidate_limit=3, offline=True))

    assert result["run_id"] == 77
    assert len(result["iterations"]) == 2
    assert result["total_candidates"] == 4
    assert result["terminal_reason"] == "max_iterations_reached"
    assert len(actions) == 2
    assert finished[-1][1]["status"] == "ok"
    assert all(plan.run_id == 77 for plan in plans)
    assert memory[0]["memory_type"] == "strategy"


def test_agent_loop_continues_with_next_strategy_after_empty_iteration(monkeypatch):
    calls = []
    memory = []
    _stub_loop_memory(monkeypatch, memory)
    monkeypatch.setattr(loop.repository, "create_agent_run", lambda *args, **kwargs: 77)
    monkeypatch.setattr(loop.repository, "finish_agent_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(loop.repository, "record_agent_action", lambda *args, **kwargs: 1)
    monkeypatch.setattr(
        loop,
        "build_plan",
        lambda config: {
            "policy": {"auto": 1, "human_review": 0, "blocked": 0},
            "learning": {},
            "actions": [
                {
                    "action_type": "discover_sources",
                    "policy_decision": "auto",
                    "topic": "бурение",
                    "priority": 90,
                    "limit": 3,
                }
            ],
        },
    )

    def fake_discover(config):
        calls.append(config)
        candidates = [] if config.query_strategy == "balanced" else [{"id": 1}]
        return {
            "task_id": 10,
            "queries": ["q1"],
            "search": {"status": "ok"},
            "candidates": candidates,
        }

    monkeypatch.setattr(loop, "discover_sources", fake_discover)
    monkeypatch.setattr(loop, "evaluate_source_candidate", lambda *args, **kwargs: {"metrics": {"relevant_articles": 1, "avg_score": 70}})

    result = loop.run_agent_loop(loop.AgentLoopConfig(max_iterations=2))

    assert [call.query_strategy for call in calls] == ["balanced", "newsroom"]
    assert result["total_candidates"] == 1
    assert result["empty_iterations"] == 0
    assert result["terminal_reason"] == "max_iterations_reached"
    assert result["iterations"][0]["observations"][0]["query_strategy"] == "balanced"
    assert result["iterations"][1]["observations"][0]["query_strategy"] == "newsroom"
    assert memory[0]["status"] == "muted"
    assert memory[1]["status"] == "active"


def test_agent_loop_evaluates_candidates_and_persists_strategy_quality(monkeypatch):
    memory = []
    _stub_loop_memory(monkeypatch, memory)
    monkeypatch.setattr(loop.repository, "create_agent_run", lambda *args, **kwargs: 77)
    monkeypatch.setattr(loop.repository, "finish_agent_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(loop.repository, "record_agent_action", lambda *args, **kwargs: 1)
    monkeypatch.setattr(
        loop,
        "build_plan",
        lambda config: {
            "policy": {"auto": 1, "human_review": 0, "blocked": 0},
            "learning": {},
            "actions": [
                {
                    "action_type": "discover_sources",
                    "policy_decision": "auto",
                    "topic": "бурение",
                    "priority": 90,
                    "limit": 3,
                }
            ],
        },
    )
    monkeypatch.setattr(
        loop,
        "discover_sources",
        lambda config: {
            "task_id": 10,
            "queries": ["q1"],
            "search": {"status": "ok"},
            "candidates": [{"id": 11}, {"id": 12}],
        },
    )
    evaluated = []
    monkeypatch.setattr(
        loop,
        "evaluate_source_candidate",
        lambda candidate_id, **kwargs: evaluated.append((candidate_id, kwargs)) or {
            "metrics": {"tested_articles": 5, "relevant_articles": 2, "avg_score": 80},
        },
    )

    result = loop.run_agent_loop(loop.AgentLoopConfig(max_iterations=1, article_limit=7, offline=True))

    observation = result["iterations"][0]["observations"][0]
    assert [item[0] for item in evaluated] == [11, 12]
    assert evaluated[0][1]["article_limit"] == 7
    assert observation["evaluated_count"] == 2
    assert observation["relevant_articles"] == 4
    assert observation["avg_score"] == 80
    assert memory[0]["memory_type"] == "strategy"
    assert memory[0]["status"] == "active"
    assert memory[0]["facts"]["strategy"] == "balanced"


def test_agent_loop_delegates_evaluation_to_external_worker(monkeypatch):
    memory = []
    jobs = []
    _stub_loop_memory(monkeypatch, memory)
    monkeypatch.setattr(loop.config, "EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr(loop.config, "AI_EXECUTION_REGION", "external")
    monkeypatch.setattr(loop.repository, "create_agent_run", lambda *args, **kwargs: 77)
    monkeypatch.setattr(loop.repository, "finish_agent_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(loop.repository, "record_agent_action", lambda *args, **kwargs: 1)
    monkeypatch.setattr(
        loop.repository,
        "create_background_job",
        lambda kind, payload, **kwargs: jobs.append({"kind": kind, "payload": payload, **kwargs}) or {"id": 901},
    )
    monkeypatch.setattr(
        loop,
        "build_plan",
        lambda config: {
            "policy": {"auto": 1, "human_review": 0, "blocked": 0},
            "learning": {},
            "actions": [
                {
                    "action_type": "discover_sources",
                    "policy_decision": "auto",
                    "topic": "бурение",
                    "priority": 90,
                    "limit": 3,
                }
            ],
        },
    )
    monkeypatch.setattr(
        loop,
        "discover_sources",
        lambda config: {
            "task_id": 10,
            "queries": ["q1"],
            "search": {"status": "ok"},
            "candidates": [{"id": 11}],
        },
    )
    called = []
    monkeypatch.setattr(loop, "evaluate_source_candidate", lambda *args, **kwargs: called.append(args))

    result = loop.run_agent_loop(loop.AgentLoopConfig(max_iterations=1, article_limit=7, offline=False))

    observation = result["iterations"][0]["observations"][0]
    assert called == []
    assert jobs == [
        {
            "kind": "source_candidate_evaluate",
            "payload": {"candidate_id": 11, "article_limit": 7, "offline": False, "collect": True, "process": True},
            "queue_name": "external-ai",
            "execution_region": "external",
            "capability": "openai",
            "max_attempts": 1,
            "agent_run_id": 77,
        }
    ]
    assert observation["evaluated_count"] == 0
    assert observation["evaluation_jobs"] == 1
    assert memory[0]["facts"]["evaluation_jobs"] == 1


def test_agent_loop_stops_before_planning_when_daily_candidate_budget_is_reached(monkeypatch):
    actions = []
    _stub_loop_memory(monkeypatch)
    monkeypatch.setattr(loop.repository, "create_agent_run", lambda *args, **kwargs: 77)
    monkeypatch.setattr(loop.repository, "finish_agent_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(loop.repository, "record_agent_action", lambda *args, **kwargs: actions.append((args, kwargs)) or 1)
    monkeypatch.setattr(
        loop.repository,
        "source_discovery_daily_usage",
        lambda: {"loop_runs": 1, "candidates_created": 5, "candidate_evaluations": 0},
    )
    called = []
    monkeypatch.setattr(loop, "build_plan", lambda config: called.append(config) or {"actions": []})

    result = loop.run_agent_loop(loop.AgentLoopConfig(max_iterations=2, max_daily_candidates=5))

    assert result["terminal_reason"] == "daily_candidate_budget_reached"
    assert result["iterations"] == []
    assert called == []
    assert actions[0][0][1] == "source_discovery_loop_budget_stop"


def test_agent_loop_stops_when_only_human_review_actions(monkeypatch):
    _stub_loop_memory(monkeypatch)
    monkeypatch.setattr(loop.repository, "create_agent_run", lambda *args, **kwargs: 77)
    monkeypatch.setattr(loop.repository, "finish_agent_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(loop.repository, "record_agent_action", lambda *args, **kwargs: 1)
    monkeypatch.setattr(
        loop,
        "build_plan",
        lambda config: {
            "policy": {"auto": 0, "human_review": 1, "blocked": 0},
            "learning": {},
            "actions": [
                {
                    "action_type": "review_source_candidate",
                    "policy_decision": "human_review",
                    "candidate_id": 42,
                    "priority": 80,
                }
            ],
        },
    )
    called = []
    monkeypatch.setattr(loop, "discover_sources", lambda config: called.append(config))

    result = loop.run_agent_loop(loop.AgentLoopConfig(max_iterations=3))

    assert result["terminal_reason"] == "no_auto_actions"
    assert result["total_candidates"] == 0
    assert called == []
