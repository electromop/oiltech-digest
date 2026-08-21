from oiltech_digest.source_discovery import agent
from oiltech_digest.ingestion.source_diagnostics import ProbeResult


def test_generate_search_queries_offline_uses_topic():
    queries = agent.generate_search_queries("роботизация бурения", offline=True, limit=3)

    assert len(queries) == 3
    assert all("роботизация бурения" in query for query in queries)


def test_generate_search_queries_reuses_successful_query_memory(monkeypatch):
    def fake_memory(**kwargs):
        if kwargs.get("status") == "muted":
            return []
        return [
            {
                "subject": "robotic drilling automation newsroom",
                "facts_json": {"topic": "роботизация бурения"},
            },
            {
                "subject": "unrelated query",
                "facts_json": {"topic": "другая тема"},
            },
        ]

    monkeypatch.setattr(agent.repository, "list_agent_memory", fake_memory)

    queries = agent.generate_search_queries("роботизация бурения", offline=True, limit=3)

    assert queries[0] == "robotic drilling automation newsroom"
    assert len(queries) == 3


def test_generate_search_queries_skips_muted_queries(monkeypatch):
    muted_query = "роботизация бурения нефтегаз новости"

    def fake_memory(**kwargs):
        if kwargs.get("status") == "muted":
            return [{"subject": muted_query, "facts_json": {"topic": "роботизация бурения"}}]
        return []

    monkeypatch.setattr(agent.repository, "list_agent_memory", fake_memory)

    queries = agent.generate_search_queries("роботизация бурения", offline=True, limit=3)

    assert muted_query not in queries
    assert len(queries) == 3


def test_generate_search_queries_uses_strategy_specific_templates(monkeypatch):
    monkeypatch.setattr(agent.repository, "list_agent_memory", lambda **kwargs: [])

    queries = agent.generate_search_queries("роботизация бурения", offline=True, limit=2, strategy="technical")

    assert queries[0] == "роботизация бурения technology case study oilfield"
    assert queries[1] == "роботизация бурения technical paper upstream"


def test_score_source_candidate_rewards_relevance():
    strong = agent.score_source_candidate({
        "tested_articles": 10,
        "relevant_articles": 8,
        "avg_score": 75,
        "duplicate_count": 1,
        "noise_count": 1,
    })
    weak = agent.score_source_candidate({
        "tested_articles": 10,
        "relevant_articles": 1,
        "avg_score": 20,
        "duplicate_count": 3,
        "noise_count": 5,
    })

    assert strong["quality_score"] > weak["quality_score"]


def test_recommend_source_action_requires_human_review_without_test_articles():
    rec = agent.recommend_source_action({"tested_articles": 0}, offline=True)

    assert rec["recommended_action"] == "human_review"
    assert "провер" in rec["reason"].lower()


def test_discover_sources_dry_run_does_not_write(monkeypatch):
    calls = []

    monkeypatch.setattr(agent, "get_topic_gaps", lambda limit=10: [{"topic": "t", "signals": 1, "target_signals": 10, "gap": 9}])
    monkeypatch.setattr(agent, "search_web", lambda queries, limit=20: {"status": "not_configured", "reason": "x", "queries": queries, "limit": limit, "results": []})
    monkeypatch.setattr(agent.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(agent.repository, "upsert_source_candidate", lambda rec: calls.append(rec) or 1)

    result = agent.discover_sources(agent.DiscoveryConfig(
        topic="роботизация бурения",
        seed_urls=("https://www.slb.com/newsroom",),
        offline=True,
        dry_run=True,
    ))

    assert result["dry_run"] is True
    assert result["candidates"][0]["url"] == "https://www.slb.com/newsroom"
    assert calls == []


def test_search_web_none_provider_is_explicit_noop(monkeypatch):
    monkeypatch.setattr(agent.app_config, "SOURCE_DISCOVERY_SEARCH_PROVIDER", "none")

    result = agent.search_web(["robotic drilling newsroom"], limit=5)

    assert result["status"] == "not_configured"
    assert result["provider"] == "none"
    assert result["results"] == []


def test_search_web_brave_maps_results(monkeypatch):
    monkeypatch.setattr(agent.app_config, "SOURCE_DISCOVERY_SEARCH_PROVIDER", "brave")
    monkeypatch.setattr(agent.app_config, "BRAVE_SEARCH_API_KEY", "token")

    class Response:
        status_code = 200
        text = ""

        def json(self):
            return {
                "web": {
                    "results": [
                        {"url": "https://example.com/news", "title": "News", "description": "Desc"},
                        {"url": "https://example.com/news", "title": "Duplicate", "description": "Desc"},
                    ]
                }
            }

    monkeypatch.setattr(agent.requests, "get", lambda *a, **k: Response())

    result = agent.search_web(["q"], limit=10)

    assert result["status"] == "ok"
    assert result["provider"] == "brave"
    assert result["results"] == [
        {
            "query": "q",
            "url": "https://example.com/news",
            "title": "News",
            "snippet": "Desc",
            "provider": "brave",
        }
    ]


def test_discover_sources_uses_search_results_as_candidates(monkeypatch):
    monkeypatch.setattr(agent.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(agent, "get_topic_gaps", lambda limit=10: [])
    monkeypatch.setattr(
        agent,
        "search_web",
        lambda queries, limit=20: {
            "status": "ok",
            "reason": "",
            "queries": queries,
            "limit": limit,
            "results": [{"url": "https://example.com/news", "query": "q"}],
        },
    )

    result = agent.discover_sources(agent.DiscoveryConfig(
        topic="роботизация бурения",
        offline=True,
        dry_run=True,
    ))

    assert result["candidates"][0]["url"] == "https://example.com/news"
    assert "Search result" in result["candidates"][0]["discovery_reason"]


def test_discover_sources_persists_query_memory(monkeypatch):
    memory = []
    actions = []
    monkeypatch.setattr(agent, "get_topic_gaps", lambda limit=10: [])
    monkeypatch.setattr(
        agent,
        "search_web",
        lambda queries, limit=20: {
            "status": "ok",
            "queries": queries,
            "limit": limit,
            "results": [{"url": "https://example.com/news", "query": "robotic drilling automation"}],
        },
    )
    monkeypatch.setattr(agent.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(agent.repository, "create_agent_task", lambda *args, **kwargs: 11)
    monkeypatch.setattr(agent.repository, "upsert_source_candidate", lambda rec: 22)
    monkeypatch.setattr(agent.repository, "record_agent_action", lambda *args, **kwargs: actions.append((args, kwargs)) or 1)
    monkeypatch.setattr(agent.repository, "upsert_agent_memory", lambda **kwargs: memory.append(kwargs) or 1)

    result = agent.discover_sources(agent.DiscoveryConfig(
        topic="роботизация бурения",
        offline=True,
        dry_run=False,
    ))

    assert result["candidates"][0]["id"] == 22
    assert memory[0]["memory_type"] == "query"
    assert memory[0]["subject"] == "robotic drilling automation"
    assert memory[0]["facts"]["topic"] == "роботизация бурения"
    assert memory[0]["facts"]["empty_result"] is False
    assert actions[-1][0][1] == "discover_sources_finished"


def test_discover_sources_mutes_empty_queries_after_successful_search(monkeypatch):
    memory = []
    monkeypatch.setattr(agent, "get_topic_gaps", lambda limit=10: [])
    monkeypatch.setattr(
        agent,
        "search_web",
        lambda queries, limit=20: {
            "status": "empty",
            "queries": queries,
            "limit": limit,
            "results": [],
        },
    )
    monkeypatch.setattr(agent.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(agent.repository, "create_agent_task", lambda *args, **kwargs: 11)
    monkeypatch.setattr(agent.repository, "record_agent_action", lambda *args, **kwargs: 1)
    monkeypatch.setattr(agent.repository, "upsert_agent_memory", lambda **kwargs: memory.append(kwargs) or 1)

    agent.discover_sources(agent.DiscoveryConfig(
        topic="роботизация бурения",
        offline=True,
        dry_run=False,
    ))

    assert memory
    assert {item["status"] for item in memory} == {"muted"}
    assert all(item["score"] == 0 for item in memory)
    assert all(item["facts"]["empty_result"] is True for item in memory)


def test_discover_sources_does_not_mute_queries_when_search_is_not_configured(monkeypatch):
    memory = []
    monkeypatch.setattr(agent, "get_topic_gaps", lambda limit=10: [])
    monkeypatch.setattr(
        agent,
        "search_web",
        lambda queries, limit=20: {
            "status": "not_configured",
            "queries": queries,
            "limit": limit,
            "results": [],
        },
    )
    monkeypatch.setattr(agent.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(agent.repository, "create_agent_task", lambda *args, **kwargs: 11)
    monkeypatch.setattr(agent.repository, "record_agent_action", lambda *args, **kwargs: 1)
    monkeypatch.setattr(agent.repository, "upsert_agent_memory", lambda **kwargs: memory.append(kwargs) or 1)

    agent.discover_sources(agent.DiscoveryConfig(
        topic="роботизация бурения",
        offline=True,
        dry_run=False,
    ))

    assert memory == []


def test_candidate_urls_skip_rejected_search_domains_but_keep_seed_urls():
    result = agent._candidate_urls(
        ("https://bad.example.com/news",),
        [
            {"url": "https://bad.example.com/press", "query": "q"},
            {"url": "https://good.example.com/news", "query": "q"},
        ],
        10,
        rejected_domains={"bad.example.com"},
    )

    assert [item["url"] for item in result] == [
        "https://bad.example.com/news",
        "https://good.example.com/news",
    ]


def test_test_parse_source_scores_useful_candidate(monkeypatch):
    listing = b"""
    <html><body>
      <a href="/news/drilling-robot">Robotic drilling system improves oilfield operations</a>
    </body></html>
    """
    article = b"""
    <html><head><meta property="og:title" content="Robotic drilling system improves oilfield operations"></head>
    <body><article>
      <p>The company deployed a robotic drilling system for oilfield well construction.</p>
      <p>The technology improves uptime, safety and operational control for upstream teams.</p>
    </article></body></html>
    """

    def fake_probe(url, timeout=20):
        if url == "https://example.com/news":
            return ProbeResult(url=url, status=200, bytes=len(listing)), listing
        return ProbeResult(url=url, status=200, bytes=len(article)), article

    monkeypatch.setattr(agent, "probe_url", fake_probe)

    result = agent.test_parse_source("https://example.com/news", article_limit=3)

    assert result["verdict"] == "ok"
    assert result["metrics"]["tested_articles"] == 1
    assert result["metrics"]["relevant_articles"] == 1
    assert result["metrics"]["avg_score"] is not None


def test_test_source_candidate_updates_assessment(monkeypatch):
    updates = []
    actions = []

    monkeypatch.setattr(
        agent.repository,
        "get_source_candidate",
        lambda candidate_id: {"id": candidate_id, "url": "https://example.com/news"},
    )
    monkeypatch.setattr(
        agent,
        "test_parse_source",
        lambda url, article_limit=5: {
            "url": url,
            "verdict": "ok",
            "metrics": {
                "tested_articles": 6,
                "relevant_articles": 5,
                "avg_score": 75,
                "duplicate_count": 0,
                "noise_count": 1,
            },
            "candidates": [],
        },
    )
    monkeypatch.setattr(
        agent.repository,
        "update_source_candidate_assessment",
        lambda candidate_id, **kwargs: updates.append({"candidate_id": candidate_id, **kwargs}),
    )
    monkeypatch.setattr(
        agent.repository,
        "record_agent_action",
        lambda task_id, action_type, **kwargs: actions.append({"task_id": task_id, "action_type": action_type, **kwargs}),
    )

    result = agent.test_source_candidate(42, article_limit=6, offline=True, dry_run=False)

    assert result["recommended_action"] == "add"
    assert result["next_status"] == "needs_human_review"
    assert updates == [{
        "candidate_id": 42,
        "status": "needs_human_review",
        "tested_articles": 6,
        "relevant_articles": 5,
        "avg_score": 75,
        "duplicate_count": 0,
        "noise_count": 1,
        "recommended_action": "add",
        "review_comment": "Источник дал достаточно релевантных материалов и выглядит полезным.",
    }]
    assert actions[0]["action_type"] == "test_source_candidate"


def test_test_source_candidate_dry_run_does_not_update(monkeypatch):
    updates = []

    monkeypatch.setattr(
        agent.repository,
        "get_source_candidate",
        lambda candidate_id: {"id": candidate_id, "url": "https://example.com/news"},
    )
    monkeypatch.setattr(
        agent,
        "test_parse_source",
        lambda url, article_limit=5: {
            "url": url,
            "verdict": "no_useful_articles",
            "metrics": {
                "tested_articles": 5,
                "relevant_articles": 0,
                "avg_score": None,
                "duplicate_count": 0,
                "noise_count": 5,
            },
            "candidates": [],
        },
    )
    monkeypatch.setattr(
        agent.repository,
        "update_source_candidate_assessment",
        lambda candidate_id, **kwargs: updates.append(kwargs),
    )

    result = agent.test_source_candidate(42, offline=True, dry_run=True)

    assert result["recommended_action"] == "reject"
    assert result["next_status"] == "rejected"
    assert updates == []
