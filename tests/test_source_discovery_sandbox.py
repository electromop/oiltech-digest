from oiltech_digest.ingestion.source_diagnostics import ProbeResult
from oiltech_digest.source_discovery import sandbox


def test_collect_candidate_articles_writes_sandbox_rows(monkeypatch):
    listing = b"""
    <html><body>
      <a href="/news/robotic-drilling">Robotic drilling system improves oilfield operations</a>
    </body></html>
    """
    article = b"""
    <html><head><title>Robotic drilling system improves oilfield operations</title></head>
    <body><article>
      <p>The company deployed a robotic drilling system for oilfield well construction.</p>
      <p>The technology improves uptime, safety and operational control for upstream teams.</p>
    </article></body></html>
    """
    rows = []

    def fake_probe(url, timeout=20):
        if url == "https://example.com/news":
            return ProbeResult(url=url, status=200, bytes=len(listing)), listing
        return ProbeResult(url=url, status=200, bytes=len(article)), article

    monkeypatch.setattr(sandbox, "probe_url", fake_probe)
    monkeypatch.setattr(
        sandbox.repository,
        "upsert_source_candidate_article",
        lambda candidate_id, rec: rows.append({"candidate_id": candidate_id, **rec}) or 101,
    )

    result = sandbox.collect_candidate_articles(
        {"id": 42, "url": "https://example.com/news", "name": "Example"},
        article_limit=3,
    )

    assert result["inserted_or_updated"] == 1
    assert rows[0]["candidate_id"] == 42
    assert rows[0]["url"] == "https://example.com/news/robotic-drilling"
    assert rows[0]["prefilter_keep"] is True


def test_process_candidate_articles_saves_ai_result(monkeypatch):
    saved = []
    article = {
        "id": 101,
        "candidate_id": 42,
        "title": "Robotic drilling system improves oilfield operations",
        "url": "https://example.com/news/robotic-drilling",
        "raw_text": "The company deployed a robotic drilling system for oilfield well construction.",
        "language": "en",
        "source_name": "Example",
        "source_category": "роботизация бурения",
    }
    tags = [{"id": 7, "name": "Бурение", "parent_id": None, "keywords_json": ["бурение"], "keywords_en_json": ["drilling"]}]
    criteria = [{"id": 9, "name": "Технологическая значимость", "weight": 100, "keywords_json": [], "keywords_en_json": ["drilling"]}]

    monkeypatch.setattr(sandbox.repository, "list_enabled_tags", lambda: tags)
    monkeypatch.setattr(sandbox.repository, "list_enabled_scoring_criteria", lambda: criteria)
    monkeypatch.setattr(
        sandbox.repository,
        "list_source_candidate_articles",
        lambda candidate_id, limit=5, only_unprocessed=True: [article],
    )
    monkeypatch.setattr(
        sandbox.repository,
        "update_source_candidate_article_result",
        lambda article_id, payload: saved.append({"article_id": article_id, **payload}),
    )

    stats = sandbox.process_candidate_articles(42, limit=5, offline=True)

    assert stats == {"processed": 1, "relevant": 1, "rejected": 0, "errors": 0}
    assert saved[0]["article_id"] == 101
    assert saved[0]["relevant"] is True
    assert saved[0]["processing_status"] == "ok"
    assert saved[0]["total_score"] == 33.33


def test_evaluate_source_candidate_uses_ai_recommendation_with_evidence(monkeypatch):
    updates = []
    actions = []
    recommendations = []
    memory_updates = []
    articles = [
        {
            "title": "Robotic drilling system",
            "url": "https://example.com/news/robotic-drilling",
            "relevant": True,
            "summary": "Запущена роботизированная буровая.",
            "total_score": 82,
            "score_label": "Высокая",
            "processing_status": "ok",
        }
    ]

    monkeypatch.setattr(
        sandbox.repository,
        "get_source_candidate",
        lambda candidate_id: {
            "id": candidate_id,
            "url": "https://example.com/news",
            "name": "Example",
            "topic": "роботизация бурения",
        },
    )
    monkeypatch.setattr(sandbox.repository, "create_agent_task", lambda *args, **kwargs: 77)
    monkeypatch.setattr(sandbox, "collect_candidate_articles", lambda candidate, article_limit=5: {"inserted_or_updated": 1, "errors": 0, "articles": []})
    monkeypatch.setattr(sandbox, "process_candidate_articles", lambda candidate_id, limit=5, offline=True: {"processed": 1, "relevant": 1, "rejected": 0, "errors": 0})
    monkeypatch.setattr(
        sandbox.repository,
        "source_candidate_article_metrics",
        lambda candidate_id: {
            "tested_articles": 1,
            "relevant_articles": 1,
            "avg_score": 82,
            "duplicate_count": 0,
            "noise_count": 0,
        },
    )
    monkeypatch.setattr(
        sandbox.repository,
        "list_source_candidate_articles",
        lambda candidate_id, limit=5, only_unprocessed=False: articles,
    )
    monkeypatch.setattr(
        sandbox,
        "recommend_source_action",
        lambda metrics, offline=True, evidence=None: recommendations.append({
            "metrics": metrics,
            "offline": offline,
            "evidence": evidence,
        }) or {
            "recommended_action": "test_more",
            "reason": "AI просит проверить больше материалов.",
        },
    )
    monkeypatch.setattr(
        sandbox,
        "assess_source_quality",
        lambda candidate, metrics, evidence, offline=True: {
            "source": "ai",
            "model": "test-model",
            "quality_label": "перспективный",
            "usefulness_score": 64,
            "topic_fit": "Подходит теме.",
            "article_pattern": "Есть технические материалы.",
            "useful_summary": "Источник дает материалы по роботизации бурения.",
            "strengths": ["Есть релевантный материал."],
            "risks": ["Малая выборка."],
            "next_checks": ["Проверить регулярность."],
            "confidence": 0.62,
        },
    )
    monkeypatch.setattr(
        sandbox,
        "assess_source_regularity",
        lambda candidate, collected, evidence: {
            "source": "rules",
            "regularity_label": "active_but_sparse",
            "is_regular": True,
            "sample_size": 1,
            "dated_articles": 1,
            "articles_last_30_days": 1,
            "articles_last_90_days": 1,
            "latest_age_days": 3,
            "archive_suspected": False,
            "section_updates": True,
            "reason": "Источник обновляется, но выборка мала.",
            "risks": [],
            "next_checks": [],
            "confidence": 0.62,
        },
    )
    monkeypatch.setattr(
        sandbox,
        "apply_candidate_learning",
        lambda candidate_id, **kwargs: {"candidate_id": candidate_id, "event_type": kwargs["event_type"]},
    )
    monkeypatch.setattr(
        sandbox.repository,
        "upsert_agent_memory",
        lambda **kwargs: memory_updates.append(kwargs) or 501,
    )
    monkeypatch.setattr(
        sandbox.repository,
        "update_source_candidate_assessment",
        lambda candidate_id, **kwargs: updates.append({"candidate_id": candidate_id, **kwargs}),
    )
    monkeypatch.setattr(
        sandbox.repository,
        "record_agent_action",
        lambda task_id, action_type, **kwargs: actions.append({"task_id": task_id, "action_type": action_type, **kwargs}),
    )

    result = sandbox.evaluate_source_candidate(42, article_limit=5, offline=False)

    assert recommendations[0]["offline"] is False
    assert recommendations[0]["evidence"] == articles
    assert updates[0]["recommended_action"] == "test_more"
    assert updates[0]["review_comment"].startswith("Итоговая оценка источника")
    assert "AI просит проверить больше материалов." in updates[0]["review_comment"]
    assert "AI-оценка источника" in updates[0]["review_comment"]
    assert "Регулярность источника" in updates[0]["review_comment"]
    assert result["source_quality"]["quality_label"] == "перспективный"
    assert result["source_regularity"]["regularity_label"] == "active_but_sparse"
    assert result["source_health"]["recommended_action"] == "test_more"
    assert result["quality_memory"] == {"ok": True, "memory_id": 501}
    assert memory_updates[0]["memory_type"] == "source_candidate_quality"
    assert memory_updates[0]["facts"]["source_quality"]["useful_summary"] == "Источник дает материалы по роботизации бурения."
    assert memory_updates[0]["facts"]["source_regularity"]["regularity_label"] == "active_but_sparse"
    assert memory_updates[0]["facts"]["source_health"]["verdict"] == "promising_needs_more_data"
    assert result["review_comment"] == updates[0]["review_comment"]
    assert actions[0]["action_type"] == "evaluate_source_candidate_finished"
