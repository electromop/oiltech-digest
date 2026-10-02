"""Качество радара — замечания Виктора 29.09.

«75% — это бизнес-сигналы», «оценки завышены», «в одной карточке могут быть разные темы»,
«неправильно классифицирует по тегам», «нашёл очень старый сигнал». Судья в том же вызове
ставит категорию, дату события, флаг смешанных событий, тему из списка и баллы по профилю
tech_radar; итог балла считает код; радар не показывает бизнес, смешанное, старое и архив.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from oiltech_digest import config, scoring_profiles, signal_discovery
from oiltech_digest.db import repository
from oiltech_digest.processing.openai_client import AIClientError, AIResponse

DRILLING = "Бурение, направленное бурение, буровые растворы и буровое оборудование"
DIGITAL = "Автоматизация, цифровизация, промышленный AI и автономные операции"
MARKET = "Рынок, бизнес-модели, сервисные модели, партнёрства и M&A"
THEMES = [DRILLING, DIGITAL, MARKET]
CRITERIA = [
    {"id": 101, "name": "Ценность для нефтесервиса", "description": "задача", "weight": 30,
     "keywords_json": [], "keywords_en_json": [], "profile": "tech_radar"},
    {"id": 102, "name": "Технологическая новизна", "description": "новизна", "weight": 25,
     "keywords_json": [], "keywords_en_json": [], "profile": "tech_radar"},
    {"id": 103, "name": "Зрелость / доказательность", "description": "зрелость", "weight": 20,
     "keywords_json": [], "keywords_en_json": [], "profile": "tech_radar"},
    {"id": 104, "name": "Переносимость", "description": "перенос", "weight": 15,
     "keywords_json": [], "keywords_en_json": [], "profile": "tech_radar"},
    {"id": 105, "name": "Свежесть", "description": "дата события", "weight": 10,
     "keywords_json": [], "keywords_en_json": [], "profile": "tech_radar"},
]


def _judge_answer(**overrides):
    answer = {
        "title": "Autonomous drilling rigs scaled", "title_ru": "Масштабирование автономного бурения",
        "theme": DRILLING, "summary": "Оператор перевёл 120 установок на автономное бурение.",
        "thesis": "Автономия бурения выходит в промышленный масштаб.", "transferability": "Высокая",
        "maturity": "shortlist", "confidence": 0.8, "score": 92, "why_now": "Масштаб", "why_not_noise": "Цифры",
        "companies": ["SLB"], "industries": ["нефтегаз"],
        "signal_category": "technology", "event_date": "2026-09-10",
        "mixed_events": False, "mixed_events_reason": "",
        "criteria_scores": [
            {"criterion_id": 101, "ai_score": 80, "rationale": "бурение"},
            {"criterion_id": 102, "ai_score": 60, "rationale": "известный подход"},
            {"criterion_id": 103, "ai_score": 50, "rationale": "vendor-reported"},
            {"criterion_id": 104, "ai_score": 70, "rationale": "переносимо"},
            {"criterion_id": 105, "ai_score": 90, "rationale": "сентябрь"},
        ],
    }
    answer.update(overrides)
    return answer


class _Judge:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def complete_json(self, instructions, user_input, schema, **kwargs):
        self.calls.append({"input": user_input, "schema": schema})
        return AIResponse(data=dict(self.answer), model="fake-judge")


def _snapshot(**extra):
    return {"topics": [{"name": DRILLING, "tag_id": 2}], "tags": [], "radar_themes": THEMES,
            "radar_criteria": CRITERIA, **extra}


def _run(monkeypatch, answer, snapshot=None, evidence=None):
    judge = _Judge(answer)
    monkeypatch.setattr(signal_discovery, "make_client", lambda offline: judge)
    items = evidence or [{"source_url": "https://slb.com/autonomy", "title": "SLB autonomous drilling",
                          "extracted_fact": "120 rigs"}]
    monkeypatch.setattr(
        signal_discovery,
        "_search_web_evidence",
        lambda topic, config_, **kwargs: {"status": "ok", "queries": ["q"], "results": len(items), "evidence": items},
    )
    monkeypatch.setattr(signal_discovery, "_batch_review_candidates",
                        lambda candidates, topic, offline: {"status": "skipped"})
    monkeypatch.setattr(signal_discovery, "_dedupe_run", lambda *args: {"skipped": "test"})
    run_config = signal_discovery.SignalDiscoveryConfig(offline=False, dry_run=True, web_only=True)
    run = signal_discovery.run_discovery(run_config, snapshot or _snapshot())
    return run, judge


# --- Судья: схема и вход ----------------------------------------------------------------------


def test_judge_schema_offers_themes_categories_and_criteria_of_the_run():
    schema = signal_discovery._judge_schema(THEMES, CRITERIA)["schema"]

    assert schema["properties"]["theme"]["enum"] == THEMES
    assert schema["properties"]["signal_category"]["enum"] == ["technology", "business", "other"]
    item = schema["properties"]["criteria_scores"]["items"]
    assert item["properties"]["criterion_id"]["enum"] == [101, 102, 103, 104, 105]
    # Строгая схема: каждое поле обязано быть в required.
    assert set(schema["properties"]) == set(schema["required"])
    # Базовая схема не испорчена сборкой.
    assert "criteria_scores" not in signal_discovery.SIGNAL_JUDGE_SCHEMA["schema"]["properties"]


def test_judge_schema_without_context_keeps_free_theme_and_no_scores():
    schema = signal_discovery._judge_schema([], [])["schema"]

    assert schema["properties"]["theme"] == {"type": "string"}
    assert "criteria_scores" not in schema["properties"]
    assert "event_date" in schema["required"]


def test_judge_input_lists_themes_and_criteria_without_weights(monkeypatch):
    _, judge = _run(monkeypatch, _judge_answer())

    prompt = judge.calls[0]["input"]
    assert f"- {DIGITAL}" in prompt
    assert "- id 102: Технологическая новизна — новизна" in prompt
    # Вес не показываем: итог считает код, правка весов не должна менять оценки модели.
    assert "30" not in prompt.split("criteria:")[1].split("\n\n")[0]


# --- Разбор ответа ---------------------------------------------------------------------------


def test_normalize_parses_category_date_and_mixed_flag():
    signal = signal_discovery._normalize_signal_payload(
        _judge_answer(signal_category="Business", event_date="2026-03-01T00:00", mixed_events=True,
                      mixed_events_reason="две разные сделки"),
        DRILLING,
    )

    assert signal["signal_category"] == "business"
    assert signal["event_date"] == "2026-03-01"
    assert signal["mixed_events"] is True
    assert signal["mixed_events_reason"] == "две разные сделки"


@pytest.mark.parametrize("value", ["", "не указано", "2026-13-01", (date.today() + timedelta(days=30)).isoformat()])
def test_unknown_or_future_event_date_is_none(value):
    assert signal_discovery._normalize_event_date(value) is None


def test_unknown_category_is_none_and_reason_needs_mixed_flag():
    signal = signal_discovery._normalize_signal_payload(
        _judge_answer(signal_category="tech", mixed_events=False, mixed_events_reason="мусор"), DRILLING,
    )

    assert signal["signal_category"] is None
    assert signal["mixed_events_reason"] == ""


# --- Тема и балл в прогоне -------------------------------------------------------------------


def test_theme_is_the_judges_choice_from_the_list(monkeypatch):
    run, _ = _run(monkeypatch, _judge_answer(theme=DIGITAL))

    candidate = run["topics"][0]["candidates"][0]
    assert candidate["signal"]["theme"] == DIGITAL
    assert candidate["raw_output"]["theme_choice"] == {"search_topic": DRILLING, "theme": DIGITAL, "reason": "judge"}


def test_theme_outside_the_list_falls_back_to_keywords(monkeypatch):
    run, _ = _run(monkeypatch, _judge_answer(theme="Беспилотники"))

    choice = run["topics"][0]["candidates"][0]["raw_output"]["theme_choice"]
    assert choice["judge_theme_rejected"] == "Беспилотники"
    assert choice["reason"] in {"content", "search_topic_confirmed", "no_keywords"}


def test_score_is_the_tech_radar_profile_total_not_the_judges_own(monkeypatch):
    run, _ = _run(monkeypatch, _judge_answer(score=92))

    candidate = run["topics"][0]["candidates"][0]
    signal = candidate["signal"]
    # 80·0,30 + 60·0,25 + 50·0,20 + 70·0,15 + 90·0,10 = 24 + 15 + 10 + 10,5 + 9 = 68,5
    assert signal["score"] == 68.5
    assert signal["score_profile"] == scoring_profiles.TECH_RADAR
    assert [item["criterion_id"] for item in signal["score_items"]] == [101, 102, 103, 104, 105]
    assert [entry["weight"] for entry in signal["criteria_snapshot"]] == [30, 25, 20, 15, 10]
    assert candidate["raw_output"]["judge_score"] == 92
    assert candidate["raw_output"]["profile_score"] == 68.5


def test_without_criteria_in_snapshot_the_judges_score_stays(monkeypatch):
    run, judge = _run(monkeypatch, _judge_answer(score=77), snapshot=_snapshot(radar_criteria=[]))

    signal = run["topics"][0]["candidates"][0]["signal"]
    assert signal["score"] == 77
    assert "score_profile" not in signal
    assert "criteria_scores" not in judge.calls[0]["schema"]["schema"]["properties"]


# --- Снимок и темы поиска --------------------------------------------------------------------


def test_radar_criteria_are_empty_when_weights_do_not_sum_to_100(monkeypatch):
    monkeypatch.setattr(repository, "list_enabled_scoring_criteria",
                        lambda profile: [{**CRITERIA[0], "weight": 30}, {**CRITERIA[1], "weight": 25}])

    assert signal_discovery._radar_criteria() == []


def test_radar_criteria_are_the_tech_radar_profile(monkeypatch):
    asked = []
    monkeypatch.setattr(repository, "list_enabled_scoring_criteria", lambda profile: asked.append(profile) or CRITERIA)

    assert [item["id"] for item in signal_discovery._radar_criteria()] == [101, 102, 103, 104, 105]
    assert asked == [scoring_profiles.TECH_RADAR]


def test_market_topic_is_not_searched_unless_asked_explicitly(monkeypatch):
    monkeypatch.setattr(signal_discovery, "_radar_topics", lambda: [{"name": DRILLING}, {"name": MARKET}])

    assert [row["name"] for row in signal_discovery._selected_topics(None)] == [DRILLING]
    assert [row["name"] for row in signal_discovery._selected_topics("Рынок")] == [MARKET]


# --- Надёжность прогона (хвосты ревью 29.09) --------------------------------------------------


def test_answer_without_text_is_a_transient_error():
    assert signal_discovery._is_transient_ai_error(
        AIClientError("OpenAI response does not contain output text (status=incomplete)")
    )


def test_run_fails_when_the_judge_answered_no_cluster(monkeypatch):
    monkeypatch.setattr(signal_discovery, "JUDGE_RETRY_PAUSE_SECONDS", 0)

    class Down:
        def complete_json(self, *args, **kwargs):
            raise AIClientError("OpenAI API error 503: busy")

    monkeypatch.setattr(signal_discovery, "make_client", lambda offline: Down())
    monkeypatch.setattr(
        signal_discovery, "_search_web_evidence",
        lambda topic, config_, **kwargs: {"status": "ok", "queries": ["q"], "results": 1,
                                          "evidence": [{"source_url": "https://a.example/1", "title": "t"}]},
    )
    run_config = signal_discovery.SignalDiscoveryConfig(offline=False, dry_run=True, web_only=True)

    with pytest.raises(signal_discovery.JudgeUnavailable, match="ни на один из 1"):
        signal_discovery.run_discovery(run_config, _snapshot())


def test_empty_day_without_errors_is_still_ok(monkeypatch):
    monkeypatch.setattr(
        signal_discovery, "_search_web_evidence",
        lambda topic, config_, **kwargs: {"status": "ok", "queries": ["q"], "results": 0, "evidence": []},
    )
    run_config = signal_discovery.SignalDiscoveryConfig(offline=False, dry_run=True, web_only=True)

    run = signal_discovery.run_discovery(run_config, _snapshot())

    assert run["topics"][0]["candidates"] == []


# --- Ядро: запись и видимость (живая база) ---------------------------------------------------


def _store(key, *, title=None, **fields):
    signal = {"signal_key": key, "title": title or key, "title_ru": title or key, "theme": DRILLING,
              "maturity": "watch", "score": 60, **fields}
    signal_id = repository.upsert_signal(signal)
    repository.upsert_signal_evidence(signal_id, {"source_url": f"https://example.com/{key}", "title": key})
    return signal_id


def _visible_ids():
    return {int(row["id"]) for row in repository.list_signals(limit=100)}


def test_radar_hides_business_other_mixed_old_and_archived(isolated_db):
    recent = (date.today() - timedelta(days=10)).isoformat()
    old = (date.today() - timedelta(days=config.SIGNAL_RADAR_MAX_EVENT_AGE_DAYS + 5)).isoformat()
    tech = _store("tech", signal_category="technology", event_date=recent, mixed_events=False)
    legacy = _store("legacy")
    business = _store("business", signal_category="business", event_date=recent)
    other = _store("other", signal_category="other")
    mixed = _store("mixed", signal_category="technology", mixed_events=True, mixed_events_reason="две сделки")
    stale = _store("stale", signal_category="technology", event_date=old)
    archived = _store("archived", signal_category="technology")
    repository.archive_signals([archived], reason="test")

    assert _visible_ids() == {tech, legacy}
    summary = repository.signal_radar_summary()
    assert summary["total"] == 2
    hidden = {business, other, mixed, stale, archived}
    assert hidden.isdisjoint(_visible_ids())


def test_judge_fields_are_stored_and_kept_when_an_old_worker_refinds_the_card(isolated_db):
    signal_id = _store("card", signal_category="technology", event_date="2026-09-10", mixed_events=False,
                       score_profile="tech_radar", score_items=[{"criterion_id": 101, "final_score": 80}],
                       criteria_snapshot=[{"id": 101, "weight": 30}])
    # Старая сборка NL: полей судьи нет — прежние не стираются.
    repository.upsert_signal({"signal_key": "card", "title": "card", "theme": DRILLING, "score": 50})

    with repository.get_connection() as conn:
        row = conn.execute(
            "SELECT signal_category, event_date, mixed_events, score_profile FROM signals WHERE id = %s",
            (signal_id,),
        ).fetchone()
    assert row == ("technology", date(2026, 9, 10), False, None)


def test_archive_selects_early_free_theme_cards_and_is_reversible(isolated_db):
    with repository.get_connection() as conn:
        conn.execute("INSERT INTO tags (name, enabled) VALUES (%s, TRUE)", (DRILLING,))
        conn.commit()
    early = _store("early", title="HSE/цифровые разрешения")
    with repository.get_connection() as conn:
        conn.execute("UPDATE signals SET theme = 'HSE/цифровые разрешения', first_seen_at = '2026-09-13' WHERE id = %s",
                     (early,))
        conn.commit()
    topical = _store("topical")

    rows = repository.archive_signal_candidates(created_before="2026-09-14")

    assert [row["id"] for row in rows] == [early]
    assert rows[0]["reviewed"] is False
    assert repository.archive_signals([early], reason="early-free-theme-2026-09") == 1
    assert _visible_ids() == {topical}
    assert repository.unarchive_signals(reason="early-free-theme-2026-09") == 1
    assert _visible_ids() == {early, topical}


def test_archive_needs_a_selection_rule(isolated_db):
    with pytest.raises(ValueError):
        repository.archive_signal_candidates(free_theme_only=False)


# --- Модели радара отдельно от ленты --------------------------------------------------------


def test_judge_uses_radar_model_reasoning_and_budget(monkeypatch):
    from oiltech_digest.processing import openai_client

    seen = {}

    class Client(openai_client.OpenAIResponsesClient):
        def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None,
                          reasoning_effort=None):
            seen.update(model=model, effort=reasoning_effort, budget=max_output_tokens, timeout=self.timeout)
            return AIResponse(data=_judge_answer(), model=model)

    monkeypatch.setattr(signal_discovery, "make_client", lambda offline: Client(api_key="test"))
    monkeypatch.setattr(config, "SIGNAL_JUDGE_MODEL", "gpt-5")
    monkeypatch.setattr(config, "SIGNAL_JUDGE_REASONING", "medium")
    monkeypatch.setattr(config, "SIGNAL_AI_TIMEOUT_SECONDS", 240.0)

    signal_discovery.judge_signal_snapshot([{"source_url": "https://a.example/1", "title": "t"}], DRILLING, offline=False)

    # Лимиты подбирались под minimal: на medium без запаса ответ приходил без текста.
    assert seen == {"model": "gpt-5", "effort": "medium", "budget": 8600, "timeout": 240.0}


@pytest.mark.parametrize("effort, expected", [("minimal", 900), ("low", 2900), ("medium", 6900), ("", 900)])
def test_output_budget_grows_with_reasoning(effort, expected):
    from oiltech_digest.processing.openai_client import output_budget

    assert output_budget(900, effort) == expected


def test_gpt5_has_its_own_price():
    # Без строки gpt-5 считался бы по ставке nano — в ~25 раз дешевле.
    assert config.price_for_model("gpt-5-2025-08-07") == (1.25, 10.0)
    assert config.price_for_model("gpt-5-mini-2025-08-07") == (0.25, 2.0)


# --- Экран: скрытые карточки для админа ------------------------------------------------------


def test_hidden_view_lists_only_hidden_cards_with_a_reason(isolated_db):
    recent = (date.today() - timedelta(days=5)).isoformat()
    _store("tech", signal_category="technology", event_date=recent)
    business = _store("deal", signal_category="business", event_date=recent)
    mixed = _store("mixed", signal_category="technology", mixed_events=True, mixed_events_reason="две сделки SLB")
    archived = _store("old13", signal_category="technology")
    repository.archive_signals([archived], reason="early")

    rows = {int(row["id"]): row["hidden_reason"] for row in repository.list_signals(limit=50, hidden=True)}

    assert rows == {
        business: "бизнес-сигнал, не технология",
        mixed: "ссылки о разных событиях: две сделки SLB",
        archived: "в архиве",
    }
    assert all(row["hidden_reason"] is None for row in repository.list_signals(limit=50))
    summary = repository.signal_radar_summary(hidden=True)
    assert (summary["total"], summary["hidden"], summary["matching"]) == (1, 3, 3)


def test_hidden_radar_cards_are_for_admin_only(monkeypatch):
    from fastapi.testclient import TestClient

    from oiltech_digest import api

    seen = {}
    monkeypatch.setattr(api.repository, "list_signals", lambda **kwargs: seen.update(kwargs) or [])
    monkeypatch.setattr(api.repository, "signal_radar_summary",
                        lambda **kwargs: {"total": 1, "hidden": 3, "matching": 1, "themes": []})
    client = TestClient(api.app)
    try:
        api.app.dependency_overrides[api.require_user] = lambda: {"id": 2, "email": "u@example.com", "role": "user"}
        assert client.get("/api/signals?hidden=true").status_code == 403
        assert client.get("/api/signals/summary?hidden=true").status_code == 403
        # Обычному пользователю и число скрытых не показываем.
        assert "hidden" not in client.get("/api/signals/summary").json()

        api.app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "a@example.com", "role": "admin"}
        assert client.get("/api/signals?hidden=true").status_code == 200
        assert seen["hidden"] is True
        assert client.get("/api/signals/summary").json()["hidden"] == 3
    finally:
        api.app.dependency_overrides.pop(api.require_user, None)


# --- Релевантность нефтесервису («релевантности мало», Виктор 29.09) --------------------------


def test_judge_schema_asks_for_oilfield_relevance_and_application():
    schema = signal_discovery._judge_schema(THEMES, CRITERIA)["schema"]

    assert schema["properties"]["oilfield_relevance"]["enum"] == ["direct", "transferable", "none"]
    assert {"oilfield_relevance", "oilfield_application"} <= set(schema["required"])
    assert "коммунальная и сетевая энергетика" in signal_discovery.SIGNAL_JUDGE_INSTRUCTIONS


@pytest.mark.parametrize("relevance, application, expected", [
    # Глоссарий делает первую букву заглавной, как во всех текстах карточки.
    ("direct", "мониторинг парафина в промысловых трубопроводах", "Мониторинг парафина в промысловых трубопроводах"),
    ("transferable", "автономная доставка проппанта на кустовые площадки", "Автономная доставка проппанта на кустовые площадки"),
    # Для «нет связи» применения нет, даже если модель что-то написала.
    ("none", "можно было бы где-нибудь", ""),
])
def test_normalize_keeps_application_only_for_relevant(relevance, application, expected):
    signal = signal_discovery._normalize_signal_payload(
        _judge_answer(oilfield_relevance=relevance, oilfield_application=application), DRILLING,
    )

    assert signal["oilfield_relevance"] == relevance
    assert signal["oilfield_application"] == expected


def test_unknown_relevance_is_none_and_card_stays_visible_by_default():
    signal = signal_discovery._normalize_signal_payload(_judge_answer(oilfield_relevance="maybe"), DRILLING)

    assert signal["oilfield_relevance"] is None


def test_radar_hides_cards_without_oilfield_relevance(isolated_db):
    direct = _store("chevron", signal_category="technology", oilfield_relevance="direct",
                    oilfield_application="мониторинг парафина")
    transferable = _store("aurora", signal_category="technology", oilfield_relevance="transferable")
    legacy = _store("old")
    resort = _store("acwa", signal_category="technology", oilfield_relevance="none")

    assert _visible_ids() == {direct, transferable, legacy}
    hidden = {int(row["id"]): row["hidden_reason"] for row in repository.list_signals(limit=20, hidden=True)}
    assert hidden == {resort: "не про нефтесервис"}


def test_relevance_is_kept_when_an_old_worker_refinds_the_card(isolated_db):
    signal_id = _store("card", oilfield_relevance="direct", oilfield_application="ГРП на кустах")
    repository.upsert_signal({"signal_key": "card", "title": "card", "theme": DRILLING, "score": 50})

    with repository.get_connection() as conn:
        row = conn.execute("SELECT oilfield_relevance, oilfield_application FROM signals WHERE id = %s",
                           (signal_id,)).fetchone()
    assert row == ("direct", "ГРП на кустах")


def test_research_prompt_requires_oilfield_relevance():
    from oiltech_digest import signal_research

    text = signal_research.RESEARCH_INSTRUCTIONS
    assert "важны\nНЕФТЕСЕРВИСУ" in text
    assert "грузоперевозки по общим трассам" in text


# --- Ревью #89 (02.10): разобранную карточку решение судьи не прячет, выбранную «в дайджест» — ничто


def _choose_for_digest(signal_id):
    user = repository.create_user(f"editor-{signal_id}@example.test", "long-enough-password", "admin")
    repository.set_user_signal_status(int(user["id"]), signal_id, status="digest")


def test_card_in_digest_stays_on_radar_when_refound_with_a_hiding_verdict(isolated_db):
    chosen = _store("chosen", signal_category="technology")
    _choose_for_digest(chosen)
    # Повторная находка: судья передумал по всем правилам сразу — карточка в выпуске всё равно видна,
    # иначе её не снять с выпуска (отметку снимают только с экрана радара).
    old = (date.today() - timedelta(days=config.SIGNAL_RADAR_MAX_EVENT_AGE_DAYS + 5)).isoformat()
    repository.upsert_signal({"signal_key": "chosen", "title": "chosen", "theme": DRILLING, "score": 60,
                              "signal_category": "business", "oilfield_relevance": "none",
                              "mixed_events": True, "event_date": old})

    assert chosen in _visible_ids()
    assert chosen not in {int(row["id"]) for row in repository.list_signals(limit=50, hidden=True)}
    assert repository.signal_radar_summary()["total"] == 1


def test_archive_never_takes_a_card_in_digest(isolated_db):
    chosen = _store("chosen", signal_category="technology")
    _choose_for_digest(chosen)

    assert repository.archive_signal_candidates(ids=[chosen], free_theme_only=False) == []
    assert repository.archive_signals([chosen], reason="test") == 0
    assert chosen in _visible_ids()


def test_judge_does_not_hide_a_reviewed_card_but_archive_and_age_do(isolated_db):
    old = (date.today() - timedelta(days=config.SIGNAL_RADAR_MAX_EVENT_AGE_DAYS + 5)).isoformat()
    reviewed = _store("reviewed", signal_category="business", oilfield_relevance="none")
    archived = _store("archived", signal_category="business")
    stale = _store("stale", signal_category="business", event_date=old)
    plain = _store("plain", signal_category="business")
    with repository.get_connection() as conn:
        for signal_id in (reviewed, archived, stale):
            conn.execute("INSERT INTO signal_feedback_events (signal_id, event_type, verdict) VALUES (%s, 'verdict', 'approved')",
                         (signal_id,))
        conn.commit()
    repository.archive_signals([archived], reason="test")

    assert _visible_ids() == {reviewed}
    hidden = {int(row["id"]): row["hidden_reason"] for row in repository.list_signals(limit=50, hidden=True)}
    # Причина — только то, что прячет на самом деле: у разобранной решение судьи не в счёт.
    assert hidden == {
        archived: "в архиве",
        stale: f"событие старше {config.SIGNAL_RADAR_MAX_EVENT_AGE_DAYS} дней",
        plain: "бизнес-сигнал, не технология",
    }
