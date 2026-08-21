from oiltech_digest.db import repository


def test_normalize_domain_handles_common_url_shapes():
    assert repository.normalize_domain("https://www.slb.com/newsroom") == "slb.com"
    assert repository.normalize_domain("http://user@example.com:8080/news") == "example.com"
    assert repository.normalize_domain("example.org/news") == "example.org"


def test_source_quality_score_rewards_relevance_and_penalizes_noise():
    strong = repository._source_quality_score({
        "articles_found": 10,
        "relevant_count": 8,
        "avg_score": 75,
        "digest_count": 3,
        "duplicate_count": 1,
        "noise_count": 1,
    })
    weak = repository._source_quality_score({
        "articles_found": 10,
        "relevant_count": 2,
        "avg_score": 30,
        "digest_count": 0,
        "duplicate_count": 3,
        "noise_count": 4,
    })

    assert strong > weak
    assert 0 <= weak <= 100
    assert 0 <= strong <= 100


def test_approve_source_candidate_creates_disabled_request_source(monkeypatch):
    executed = []

    monkeypatch.setattr(
        repository,
        "get_source_candidate",
        lambda candidate_id: {
            "id": candidate_id,
            "url": "https://example.com/news",
            "name": "Example",
            "candidate_type": "newsroom",
            "topic": "роботизация",
            "approved_source_id": None,
        },
    )

    class Cursor:
        def fetchone(self):
            return [77]

    class Conn:
        def execute(self, sql, params=None):
            executed.append((sql, params))
            return Cursor()

        def commit(self):
            executed.append(("commit", None))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(repository, "get_connection", lambda: Conn())

    source_id = repository.approve_source_candidate(42)

    assert source_id == 77
    insert_params = executed[0][1]
    assert insert_params[0] == "Example"
    assert insert_params[3] is None
    assert insert_params[4] is False
    assert insert_params[5] == "request"
    assert insert_params[6] == "https://example.com/news"
    assert executed[1][1] == (77, 42)


def test_approve_source_candidate_uses_rss_url_for_rss_candidate(monkeypatch):
    executed = []

    monkeypatch.setattr(
        repository,
        "get_source_candidate",
        lambda candidate_id: {
            "id": candidate_id,
            "url": "https://example.com/feed.xml",
            "name": "Example feed",
            "candidate_type": "rss",
            "topic": None,
            "approved_source_id": None,
        },
    )

    class Cursor:
        def fetchone(self):
            return [78]

    class Conn:
        def execute(self, sql, params=None):
            executed.append((sql, params))
            return Cursor()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(repository, "get_connection", lambda: Conn())

    source_id = repository.approve_source_candidate(42, enabled=True)

    assert source_id == 78
    insert_params = executed[0][1]
    assert insert_params[3] == "https://example.com/feed.xml"
    assert insert_params[4] is True
    assert insert_params[5] == "rss"
    assert insert_params[6] is None


def test_approve_source_candidate_returns_existing_source(monkeypatch):
    monkeypatch.setattr(
        repository,
        "get_source_candidate",
        lambda candidate_id: {"id": candidate_id, "approved_source_id": 99},
    )

    assert repository.approve_source_candidate(42) == 99


def test_source_candidate_quality_report_groups_by_topic_and_domain(isolated_db):
    repository.upsert_source_candidate({
        "url": "https://good.example.com/news",
        "topic": "бурение",
        "status": "approved",
        "tested_articles": 5,
        "relevant_articles": 4,
        "avg_score": 80,
        "noise_count": 1,
        "recommended_action": "add",
    })
    repository.upsert_source_candidate({
        "url": "https://bad.example.com/news",
        "topic": "бурение",
        "status": "rejected",
        "tested_articles": 5,
        "relevant_articles": 1,
        "avg_score": 20,
        "noise_count": 4,
        "recommended_action": "reject",
    })

    topic_rows = repository.source_candidate_quality_report(group_by="topic", limit=10)
    domain_rows = repository.source_candidate_quality_report(group_by="domain", limit=10)

    drilling = next(row for row in topic_rows if row["subject"] == "бурение")
    assert drilling["candidates"] == 2
    assert drilling["approved"] == 1
    assert drilling["rejected"] == 1
    assert float(drilling["approval_rate"]) == 0.5
    assert float(drilling["relevance_rate"]) == 0.5
    assert {row["subject"] for row in domain_rows} == {"good.example.com", "bad.example.com"}


def test_source_candidate_triage_report_prioritizes_actionable_candidates(isolated_db):
    add_id = repository.upsert_source_candidate({
        "url": "https://ready.example.com/news",
        "topic": "бурение",
        "status": "needs_human_review",
        "tested_articles": 5,
        "relevant_articles": 4,
        "avg_score": 82,
        "noise_count": 0,
        "recommended_action": "add",
    })
    test_more_id = repository.upsert_source_candidate({
        "url": "https://maybe.example.com/news",
        "topic": "бурение",
        "status": "test_parsing",
        "tested_articles": 2,
        "relevant_articles": 1,
        "avg_score": 50,
        "noise_count": 1,
        "recommended_action": "test_more",
    })
    repository.upsert_source_candidate({
        "url": "https://done.example.com/news",
        "topic": "бурение",
        "status": "approved",
        "recommended_action": "add",
    })

    rows = repository.source_candidate_triage_report(limit=10)

    assert [row["id"] for row in rows] == [add_id, test_more_id]
    assert rows[0]["triage_priority"] > rows[1]["triage_priority"]
    assert "добав" in rows[0]["triage_reason"].lower()


def test_agent_memory_upsert_and_list(isolated_db):
    first = repository.upsert_agent_memory(
        memory_key="topic:robotics",
        memory_type="topic",
        subject="robotics",
        score=42,
        facts={"gap": 5},
    )
    second = repository.upsert_agent_memory(
        memory_key="topic:robotics",
        memory_type="topic",
        subject="robotics",
        score=80,
        facts={"gap": 2},
    )

    rows = repository.list_agent_memory(memory_type="topic", limit=10)

    assert first == second
    assert rows[0]["memory_key"] == "topic:robotics"
    assert float(rows[0]["score"]) == 80
    assert rows[0]["facts_json"] == {"gap": 2}


def test_update_agent_memory_status(isolated_db):
    memory_id = repository.upsert_agent_memory(
        memory_key="strategy:test",
        memory_type="strategy",
        subject="balanced",
        score=42,
        facts={"topic": "бурение"},
    )

    assert repository.update_agent_memory_status(memory_id, "muted") is True
    rows = repository.list_agent_memory(memory_type="strategy", status="muted", limit=10)

    assert rows[0]["id"] == memory_id
    assert repository.update_agent_memory_status(999999, "active") is False


def test_source_discovery_daily_usage_counts_today_rows(isolated_db):
    run_id = repository.create_agent_run("source_discovery_loop", trigger="test")
    task_id = repository.create_agent_task("discover_sources", topic="бурение")
    repository.record_agent_action(task_id, "create_source_candidate", run_id=run_id)
    repository.create_background_job(
        "source_candidate_evaluate",
        {"candidate_id": 1},
        queue_name="external-ai",
        execution_region="external",
        capability="openai",
        agent_run_id=run_id,
    )

    usage = repository.source_discovery_daily_usage()

    assert usage["loop_runs"] == 1
    assert usage["candidates_created"] == 1
    assert usage["candidate_evaluations"] == 1


def test_query_memory_report_reads_query_facts(isolated_db):
    repository.upsert_agent_memory(
        memory_key="query:test",
        memory_type="query",
        subject="robotic drilling automation newsroom",
        score=76,
        facts={
            "topic": "бурение",
            "found_candidates": 3,
            "tested_articles": 5,
            "relevant_articles": 4,
            "avg_score": 80,
        },
    )

    rows = repository.query_memory_report(limit=10)

    assert rows[0]["query"] == "robotic drilling automation newsroom"
    assert rows[0]["topic"] == "бурение"
    assert rows[0]["status"] == "active"
    assert rows[0]["found_candidates"] == 3
    assert rows[0]["empty_result"] is False
    assert rows[0]["relevance_rate"] == 0.8


def test_agent_actions_list_includes_task_metadata(isolated_db):
    run_id = repository.create_agent_run(
        "source_discovery_cycle",
        trigger="test",
        payload={"topic_limit": 2},
    )
    task_id = repository.create_agent_task(
        "discover_sources",
        topic="бурение",
        status="running",
    )
    action_id = repository.record_agent_action(
        task_id,
        "discover_sources_finished",
        run_id=run_id,
        input_payload={"topic": "бурение"},
        output_payload={"candidates": 2},
        duration_ms=123,
    )

    rows = repository.list_agent_actions(action_type="discover_sources_finished", run_id=run_id, limit=10)

    assert rows[0]["id"] == action_id
    assert rows[0]["run_id"] == run_id
    assert rows[0]["task_id"] == task_id
    assert rows[0]["task_kind"] == "discover_sources"
    assert rows[0]["task_status"] == "running"
    assert rows[0]["task_topic"] == "бурение"
    assert rows[0]["decision_title"] == "Поиск источников завершён"
    assert "кандидатов" in rows[0]["decision_summary"]


def test_agent_actions_list_adds_learning_summary(isolated_db):
    run_id = repository.create_agent_run("source_discovery_loop", trigger="test")
    repository.record_agent_action(
        None,
        "source_candidate_learning",
        run_id=run_id,
        output_payload={
            "candidate_id": 42,
            "topic": "бурение",
            "domain": "example.com",
            "relevant_articles": 3,
            "score": 88,
            "recommended_action": "add",
        },
    )

    rows = repository.list_agent_actions(action_type="source_candidate_learning", run_id=run_id, limit=10)

    assert rows[0]["decision_title"] == "Агент обучился"
    assert rows[0]["decision_tone"] == "good"
    assert "example.com" in rows[0]["decision_summary"]
    assert rows[0]["output_json"] == {"candidates": 2}


def test_agent_runs_list_counts_actions_and_jobs(isolated_db):
    run_id = repository.create_agent_run("source_discovery_cycle", trigger="scheduler")
    repository.record_agent_action(None, "source_discovery_plan_built", run_id=run_id)
    repository.create_background_job(
        "discover_source_candidates",
        {"topics": ["бурение"]},
        agent_run_id=run_id,
    )
    repository.finish_agent_run(run_id, status="ok", result={"queued": {"queued": 1}})

    rows = repository.list_agent_runs(status="ok", limit=10)

    assert rows[0]["id"] == run_id
    assert rows[0]["status"] == "ok"
    assert rows[0]["trigger"] == "scheduler"
    assert rows[0]["action_count"] == 1
    assert rows[0]["job_count"] == 1
    assert rows[0]["result_json"] == {"queued": {"queued": 1}}


def test_background_job_status_counts_filters_capability(isolated_db):
    repository.create_background_job(
        "discover_source_candidates",
        {"topics": ["бурение"]},
        capability="source-discovery",
    )
    repository.create_background_job("process_articles", {"limit": 10}, capability="openai")

    counts = repository.background_job_status_counts(capability="source-discovery")

    assert counts == {"queued": 1}
