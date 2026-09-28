from oiltech_digest.processing.openai_client import AIResponse
from oiltech_digest.source_discovery import source_quality


def test_assess_source_quality_offline_explains_source():
    result = source_quality.assess_source_quality(
        {"id": 42, "url": "https://example.com/news", "topic": "роботизация бурения"},
        {
            "tested_articles": 5,
            "processed_articles": 5,
            "relevant_articles": 4,
            "high_score_articles": 3,
            "avg_score": 76,
            "duplicate_count": 0,
            "noise_count": 0,
        },
        [{"title": "Robotic drilling", "relevant": True, "total_score": 82, "processing_status": "ok"}],
        offline=True,
    )

    assert result["source"] == "rules"
    assert result["quality_label"] == "сильный"
    assert result["usefulness_score"] > 60
    assert "роботизация бурения" in result["topic_fit"]
    assert result["next_checks"]


def test_assess_source_quality_online_uses_ai_client(monkeypatch):
    calls = []

    class FakeClient:
        def complete_json(self, instructions, user_input, schema, max_output_tokens=900):
            calls.append({
                "instructions": instructions,
                "user_input": user_input,
                "schema": schema,
                "max_output_tokens": max_output_tokens,
            })
            return AIResponse(
                data={
                    "quality_label": "перспективный",
                    "usefulness_score": 61,
                    "topic_fit": "Подходит теме.",
                    "article_pattern": "Похоже на новости.",
                    "useful_summary": "Источник полезен для мониторинга.",
                    "strengths": ["Есть релевантные материалы."],
                    "risks": ["Малая выборка."],
                    "next_checks": ["Проверить регулярность."],
                    "confidence": 0.7,
                },
                model="test-model",
            )

    monkeypatch.setattr(source_quality, "make_client", lambda offline=False: FakeClient())

    result = source_quality.assess_source_quality(
        {"id": 42, "url": "https://example.com/news", "topic": "бурение"},
        {"tested_articles": 1, "processed_articles": 1, "relevant_articles": 1, "avg_score": 70},
        [{"title": "Drilling automation", "summary": "Summary", "total_score": 70}],
        offline=False,
    )

    assert calls[0]["schema"]["name"] == "source_candidate_quality"
    assert result["source"] == "ai"
    assert result["model"] == "test-model"
    assert result["quality_label"] == "перспективный"
    assert result["confidence"] == 0.7
