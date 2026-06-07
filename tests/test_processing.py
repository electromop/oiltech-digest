from oiltech_digest.processing import pipeline
from oiltech_digest.processing import digest
from oiltech_digest.processing.openai_client import AIResponse, OfflineAIClient, _extract_output_text
from oiltech_digest.processing.seed import DEFAULT_SCORING_CRITERIA, _split_keywords


def test_extract_output_text_from_responses_shape():
    raw = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": '{"summary":"ok"}'}],
            }
        ]
    }
    assert _extract_output_text(raw) == '{"summary":"ok"}'


def test_ai_response_cost_uses_configured_rates():
    response = AIResponse(data={}, model="m", input_tokens=1_000_000, output_tokens=1_000_000)
    assert response.cost_usd > 0


def test_split_keywords_semicolon_and_newline():
    assert _split_keywords("ГРП; бурение\nцементирование") == ["ГРП", "бурение", "цементирование"]


def test_default_scoring_weights_equal_100():
    assert sum(item["weight"] for item in DEFAULT_SCORING_CRITERIA) == 100


def test_keyword_tag_selects_best_tag():
    article = {
        "title": "Electric frac fleet reduces diesel consumption",
        "raw_text": "New electric frac technology improves hydraulic fracturing operations.",
    }
    tags = [
        {"id": 1, "keywords_json": [], "keywords_en_json": ["seismic"]},
        {"id": 2, "keywords_json": ["ГРП"], "keywords_en_json": ["electric frac", "hydraulic fracturing"]},
    ]
    assert pipeline.keyword_tag(article, tags)["tag_id"] == 2


def test_offline_summary_is_deterministic():
    client = OfflineAIClient()
    response = client.complete_json(
        "x",
        "title: Test title\ntext: First sentence. Second sentence. Third.",
        {"name": "article_summary"},
    )
    assert "Test title" in response.data["summary"]


def test_offline_pipeline_outputs_digest_ready_content(monkeypatch):
    article = {
        "id": 501,
        "title": "Electric frac fleet reduces diesel consumption",
        "source_name": "World Oil",
        "url": "https://example.com/electric-frac",
        "language": "en",
        "published_at": None,
        "raw_text": (
            "New electric frac technology improves hydraulic fracturing operations. "
            "The fleet reduces diesel consumption and lowers emissions for oilfield service crews."
        ),
        "text_truncated": False,
    }
    tags = [
        {
            "id": 10,
            "name": "ГРП",
            "parent_name": "Технологии",
            "keywords_json": [],
            "keywords_en_json": ["electric frac", "hydraulic fracturing"],
        }
    ]
    criteria = [
        {
            "id": 20,
            "name": "Технологическая значимость",
            "weight": 100,
            "keywords_json": [],
            "keywords_en_json": ["electric frac", "hydraulic fracturing", "oilfield service"],
        }
    ]
    state = {"runs": []}

    monkeypatch.setattr(pipeline.repository, "list_enabled_tags", lambda: tags)
    monkeypatch.setattr(pipeline.repository, "list_enabled_scoring_criteria", lambda: criteria)
    monkeypatch.setattr(
        pipeline.repository,
        "upsert_article_card",
        lambda article_id, summary, model=None: state.update(
            {"article_id": article_id, "summary": summary, "summary_model": model}
        ),
    )
    monkeypatch.setattr(
        pipeline.repository,
        "set_article_relevance",
        lambda article_id, relevant, reason, model=None: state.update(
            {"relevant": relevant, "relevance_reason": reason, "relevance_model": model}
        ),
    )
    monkeypatch.setattr(
        pipeline.repository,
        "upsert_article_tag",
        lambda article_id, tag_id, confidence, rationale, model=None: state.update(
            {"tag_id": tag_id, "tag_confidence": confidence, "tag_rationale": rationale, "tag_model": model}
        ),
    )
    monkeypatch.setattr(
        pipeline.repository,
        "replace_article_score",
        lambda article_id, total_score, score_label, explanation, items, model=None: state.update(
            {
                "score_article_id": article_id,
                "total_score": total_score,
                "score_label": score_label,
                "score_explanation": explanation,
                "score_items": items,
                "score_model": model,
            }
        ),
    )
    monkeypatch.setattr(pipeline.repository, "insert_ai_run", lambda rec: state["runs"].append(rec))

    stats = pipeline.process_pipeline_articles([article], OfflineAIClient(), fetch_full=False)

    assert stats == {
        "processed": 1,
        "fulltext": 0,
        "summary": 1,
        "relevant": 1,
        "rejected": 0,
        "tagged": 1,
        "scored": 1,
        "errors": 0,
    }
    assert state["summary"].startswith(article["title"])
    assert state["relevant"] is True
    assert state["tag_id"] == 10
    assert state["total_score"] >= 65
    assert state["score_label"] in {"Выше средней", "Высокая"}
    assert [run["stage"] for run in state["runs"]] == ["summary", "relevance", "tagging", "scoring"]
    assert all(run["provider"] == "offline" and run["status"] == "ok" for run in state["runs"])

    class PublishedAt:
        def date(self):
            return self

        def isoformat(self):
            return "2026-06-07"

    monkeypatch.setattr(
        digest.repository,
        "digest_candidates",
        lambda month=None, limit=20, min_score=60: [
            {
                "id": article["id"],
                "title": article["title"],
                "source_name": article["source_name"],
                "url": article["url"],
                "published_at": PublishedAt(),
                "tag_name": "ГРП",
                "parent_tag_name": "Технологии",
                "total_score": state["total_score"],
                "score_label": state["score_label"],
                "summary": state["summary"],
                "image_url": "",
            }
        ],
    )

    content = digest.build_digest_content("2026-06", min_score=65)

    assert content["news"][0]["article_id"] == 501
    assert content["news"][0]["category"] == "Технологии / ГРП"
    assert content["news"][0]["score"] == state["total_score"]
    assert content["news"][0]["summary"]
    assert not content["news"][0]["summary"].startswith(article["title"] + ":")
