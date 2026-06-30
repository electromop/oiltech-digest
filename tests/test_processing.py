from oiltech_digest.processing import pipeline
from oiltech_digest.processing import digest
from oiltech_digest.processing import external_ai
from oiltech_digest.processing.openai_client import AIResponse, OfflineAIClient, _extract_output_text
from oiltech_digest.processing.seed import DEFAULT_SCORING_CRITERIA, _split_keywords


class _RecordingClient:
    """Фейк AI-клиент: пишет порядок вызовов по имени схемы + переданные model/effort/вход."""

    model = "fake"

    def __init__(self, relevant: bool = True) -> None:
        self.calls: list[dict] = []
        self.relevant = relevant

    def complete_json(self, instructions, user_input, schema, max_output_tokens=900,
                      model=None, reasoning_effort=None):
        name = schema["name"]
        self.calls.append({"name": name, "model": model, "reasoning": reasoning_effort, "input": user_input})
        if name == "article_relevance":
            return AIResponse(data={"relevant": self.relevant, "reason": "x"}, model=model or "fake")
        if name == "article_summary":
            return AIResponse(data={"summary": "s"}, model="fake")
        if name == "article_tag":
            return AIResponse(data={"tag_id": 10, "confidence": 0.5, "rationale": "r"}, model="fake")
        if name == "article_score":
            return AIResponse(
                data={"total_score": 50, "score_label": "Средняя", "explanation": "e", "items": []},
                model="fake",
            )
        return AIResponse(data={}, model="fake")


def _external_payload(article_extra: dict | None = None) -> dict:
    article = {
        "id": 1,
        "title": "Война: удары по городу, есть жертвы",
        "url": "https://example.com/a",
        "language": "ru",
        "raw_text": "Военная сводка без отношения к нефтегазу.",
        "source_name": "Интерфакс ТЭК",
        "source_category": "Новости",
    }
    article.update(article_extra or {})
    return {
        "articles": [article],
        "tags": [{"id": 10, "name": "Бурение", "parent_name": "Технологии", "name_en": "Drilling",
                  "keywords_json": [], "keywords_en_json": []}],
        "criteria": [{"id": 20, "name": "Значимость", "weight": 100, "description": "",
                      "keywords_json": [], "keywords_en_json": []}],
    }


def test_external_ai_irrelevant_skips_summary_tag_score(monkeypatch):
    """Гейт релевантности первым: нерелевантную статью НЕ суммируем/тегируем/скорим."""
    client = _RecordingClient(relevant=False)
    monkeypatch.setattr(external_ai, "make_client", lambda offline: client)

    result = external_ai.process_payload(_external_payload())

    assert [c["name"] for c in client.calls] == ["article_relevance"]
    assert result["stats"]["rejected"] == 1
    assert result["stats"]["summary"] == 0
    item = result["articles"][0]
    assert item["relevance"]["relevant"] is False
    assert "summary" not in item and "scoring" not in item


def test_external_ai_relevance_runs_first_and_ignores_summary(monkeypatch):
    """Гейт идёт ПЕРВЫМ и судит по сырому тексту — AI-суть не попадает ему на вход."""
    client = _RecordingClient(relevant=True)
    monkeypatch.setattr(external_ai, "make_client", lambda offline: client)

    result = external_ai.process_payload(_external_payload({"summary": "ПОДКРУЧЕННАЯ-СУТЬ-НЕФТЕГАЗ"}))

    names = [c["name"] for c in client.calls]
    assert names[0] == "article_relevance"
    assert names == ["article_relevance", "article_summary", "article_tag", "article_score"]
    rel_input = next(c["input"] for c in client.calls if c["name"] == "article_relevance")
    assert "ПОДКРУЧЕННАЯ-СУТЬ-НЕФТЕГАЗ" not in rel_input
    assert "summary:" not in rel_input
    assert result["stats"]["relevant"] == 1


def test_relevance_article_uses_relevance_model_and_reasoning(monkeypatch):
    """Гейт зовётся с отдельной (более сильной) моделью и повышенным reasoning."""
    monkeypatch.setattr(pipeline.config, "OPENAI_RELEVANCE_MODEL", "strong-model")
    monkeypatch.setattr(pipeline.config, "OPENAI_RELEVANCE_REASONING", "high")
    client = _RecordingClient(relevant=True)

    pipeline.relevance_article({"title": "t", "raw_text": "x", "summary": "S"}, client)

    call = client.calls[-1]
    assert call["name"] == "article_relevance"
    assert call["model"] == "strong-model"
    assert call["reasoning"] == "high"
    assert "S" not in call["input"]  # суть не в промпте гейта


def test_reasoning_effort_across_gpt5_generations():
    from oiltech_digest.processing.openai_client import _reasoning_effort

    # Исходный GPT-5: minimal валиден, none → minimal.
    assert _reasoning_effort("gpt-5-mini", "minimal") == "minimal"
    assert _reasoning_effort("gpt-5-mini", "none") == "minimal"
    assert _reasoning_effort("gpt-5-nano", "") == "minimal"
    # 5.1+ (включая 5.4/5.5 с датой): minimal невалиден → none; пустое → none.
    assert _reasoning_effort("gpt-5.5-2026-04-23", "minimal") == "none"
    assert _reasoning_effort("gpt-5.5", "") == "none"
    assert _reasoning_effort("gpt-5.4-mini", "") == "none"
    assert _reasoning_effort("gpt-5.1-codex", "minimal") == "none"
    # Явные low/medium/high проходят как есть на любом поколении.
    assert _reasoning_effort("gpt-5.5", "medium") == "medium"
    assert _reasoning_effort("gpt-5-mini", "low") == "low"


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
        lambda article_id, summary, model=None, title_ru=None: state.update(
            {"article_id": article_id, "summary": summary, "summary_model": model, "title_ru": title_ru}
        ),
    )
    monkeypatch.setattr(
        pipeline.repository,
        "set_article_title_ru",
        lambda article_id, title_ru: state.update({"title_ru": title_ru}),
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
        "translated": 1,
        "scored": 1,
        "errors": 0,
    }
    assert state["summary"].startswith(article["title"])
    assert state["relevant"] is True
    assert state["title_ru"]  # иностранный заголовок переведён отдельной стадией
    assert state["tag_id"] == 10
    assert state["total_score"] >= 65
    assert state["score_label"] in {"Выше средней", "Высокая"}
    # Релевантность идёт ПЕРВОЙ — гейт до суммаризации (фикс «мусор в выборке» 2026-06).
    # Перевод заголовка — отдельная стадия после сути.
    assert [run["stage"] for run in state["runs"]] == ["relevance", "summary", "translation", "tagging", "scoring"]
    assert all(run["provider"] == "offline" and run["status"] == "ok" for run in state["runs"])

    class PublishedAt:
        def date(self):
            return self

        def isoformat(self):
            return "2026-06-07"

    monkeypatch.setattr(
        digest.repository,
        "digest_candidates",
        lambda month=None, limit=20, min_score=60, user_id=None, **kwargs: [
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
