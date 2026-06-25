from oiltech_digest.processing import external_ai


def test_external_ai_process_payload_offline_returns_structured_result():
    payload = {
        "offline": True,
        "articles": [
            {
                "id": 1,
                "title": "Directional drilling automation",
                "url": "https://example.com/a",
                "language": "en",
                "raw_text": "Automation improves drilling efficiency and well construction quality.",
                "source_name": "Example",
                "source_category": "Drilling",
            }
        ],
        "tags": [
            {
                "id": 10,
                "name": "Бурение",
                "parent_name": "Технологии",
                "name_en": "Drilling",
                "keywords_json": ["бурение"],
                "keywords_en_json": ["drilling", "well construction"],
            }
        ],
        "criteria": [
            {
                "id": 20,
                "name": "Технологическая значимость",
                "weight": 100,
                "description": "Technology impact",
                "keywords_json": ["технология"],
                "keywords_en_json": ["automation", "drilling"],
            }
        ],
    }

    result = external_ai.process_payload(payload)

    assert result["external_ai"] is True
    assert result["stats"]["processed"] == 1
    assert result["stats"]["summary"] == 1
    assert result["stats"]["tagged"] == 1
    assert result["stats"]["scored"] == 1
    article = result["articles"][0]
    assert article["article_id"] == 1
    assert article["summary"]["summary"]
    assert article["relevance"]["relevant"] is True
    assert article["tagging"]["tag_id"] == 10
    assert article["scoring"]["items"][0]["criterion_id"] == 20


def test_external_ai_process_payload_heartbeats_per_article():
    article = {
        "id": 1,
        "title": "Directional drilling automation",
        "url": "https://example.com/a",
        "language": "en",
        "raw_text": "Automation improves drilling efficiency and well construction quality.",
        "source_name": "Example",
        "source_category": "Drilling",
    }
    payload = {
        "offline": True,
        "articles": [dict(article, id=1), dict(article, id=2), dict(article, id=3)],
        "tags": [{"id": 10, "name": "Бурение", "parent_name": "Технологии", "name_en": "Drilling",
                  "keywords_json": ["бурение"], "keywords_en_json": ["drilling"]}],
        "criteria": [{"id": 20, "name": "Значимость", "weight": 100, "description": "x",
                      "keywords_json": [], "keywords_en_json": ["automation"]}],
    }
    beats = []
    # Колбэк, который один раз бросает — обработка не должна падать (heartbeat защищён).
    def heartbeat():
        beats.append(1)
        if len(beats) == 2:
            raise RuntimeError("transient heartbeat failure")

    result = external_ai.process_payload(payload, heartbeat=heartbeat)

    assert len(beats) == 3              # по разу на каждую из 3 статей
    assert result["stats"]["processed"] == 3   # сбой heartbeat не прервал батч


def test_external_ai_apply_process_result_calls_repository(monkeypatch):
    calls = []

    monkeypatch.setattr(external_ai.repository, "upsert_article_card", lambda *args: calls.append(("summary", args)))
    monkeypatch.setattr(external_ai.repository, "set_article_relevance", lambda *args: calls.append(("relevance", args)))
    monkeypatch.setattr(external_ai.repository, "upsert_article_tag", lambda *args: calls.append(("tagging", args)))
    monkeypatch.setattr(external_ai.repository, "replace_article_score", lambda *args: calls.append(("scoring", args)))
    monkeypatch.setattr(external_ai.repository, "insert_ai_run", lambda rec: calls.append(("run", rec["stage"])))

    stats = external_ai.apply_process_result(
        {
            "external_ai": True,
            "articles": [
                {
                    "article_id": 1,
                    "summary": {"summary": "Short", "model": "offline", "provider": "offline"},
                    "relevance": {"relevant": True, "reason": "ok", "model": "offline", "provider": "offline"},
                    "tagging": {"tag_id": 10, "confidence": 0.5, "rationale": "ok", "model": "offline", "provider": "offline"},
                    "scoring": {
                        "total_score": 50,
                        "score_label": "Средняя",
                        "explanation": "ok",
                        "items": [{"criterion_id": 20, "final_score": 50}],
                        "model": "offline",
                        "provider": "offline",
                    },
                }
            ],
        }
    )

    assert stats == {"articles": 1, "summary": 1, "relevance": 1, "tagging": 1, "scoring": 1, "errors": 0}
    assert [item[0] for item in calls[:4]] == ["summary", "run", "relevance", "run"]
    assert ("run", "scoring") in calls

