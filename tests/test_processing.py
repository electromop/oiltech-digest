from oiltech_digest.processing import pipeline
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
