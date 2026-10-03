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


def test_filtered_and_hidden_cards_are_open_to_every_user(monkeypatch):
    # Решение 02.10: Виктор разбирает всё, что радар не пустил, — не только админ.
    from fastapi.testclient import TestClient

    from oiltech_digest import api

    seen = {}
    monkeypatch.setattr(api.repository, "list_signals", lambda **kwargs: seen.update(kwargs) or [])
    monkeypatch.setattr(api.repository, "signal_radar_summary",
                        lambda **kwargs: {"total": 1, "hidden": 3, "matching": 1, "themes": []})
    client = TestClient(api.app)
    try:
        api.app.dependency_overrides[api.require_user] = lambda: {"id": 2, "email": "u@example.com", "role": "user"}
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


# --- Отсеянное — карточкой «Отсеяно», чтобы заказчик просмотрел всё (02.10) ---------------------


def _apply_run(topics):
    config_ = signal_discovery.SignalDiscoveryConfig(offline=False, dry_run=False, persist_training_examples=False)
    return signal_discovery.apply_discovery(config_, {"topics": topics})


def _rejected(key, title, **fields):
    signal = signal_discovery._normalize_signal_payload(_judge_answer(title_ru=title, maturity="reject"), DRILLING)
    signal.update({"signal_key": key, "evidence": [{"source_url": f"https://example.com/{key}", "title": title}],
                   "why_not_noise": "обзор без события", **fields})
    return {"signal": signal, "rejected": True}


def _filtered():
    return {int(row["id"]): row["hidden_reason"] for row in repository.list_signals(limit=50, hidden=True)}


def test_judge_and_batch_rejects_are_kept_as_filtered_cards(isolated_db):
    _apply_run([{"topic": DRILLING, "candidates": [
        _rejected("judge-no", "Обзор рынка бурения"),
        _rejected("review-no", "Реклама вендора", batch_review_reason="реклама без факта применения"),
    ]}])

    owners = repository.signal_key_owners(["judge-no", "review-no"])
    reasons = _filtered()
    assert reasons[owners["judge-no"]["id"]] == "отсеяно (судья): обзор без события"
    assert reasons[owners["review-no"]["id"]] == "отсеяно (ревью пачки): реклама без факта применения"
    assert _visible_ids() == set()


def test_reject_does_not_overwrite_a_card_already_on_the_radar(isolated_db):
    visible = _store("same-key", signal_category="technology")

    _apply_run([{"topic": DRILLING, "candidates": [_rejected("same-key", "Тот же материал")]}])

    assert visible in _visible_ids()


def test_search_drops_and_judge_failures_are_kept_with_reason(isolated_db):
    _apply_run([{"topic": DRILLING, "candidates": [], "filtered_findings": [
        {"stage": "search", "title": "ProPetro: контракт на 230 МВт", "reason": "не технологическое событие (нет технологии)",
         "url": "https://example.com/propetro", "summary": "контракт", "event_date": "2026-09-20"},
        {"stage": "search", "title": "Событие с битой ссылкой", "reason": "нет ссылки или заголовка", "url": ""},
        {"stage": "judge_error", "title": "Кластер без ответа", "reason": "ReadTimeout", "url": "https://example.com/t"},
    ]}])

    reasons = sorted(_filtered().values())
    assert reasons == [
        "отсеяно (поиск): не технологическое событие (нет технологии)",
        "отсеяно (поиск): нет ссылки или заголовка",  # без ссылки — тоже видна в разборе
        "отсеяно (судья не ответил): ReadTimeout",
    ]


def test_filtered_card_does_not_hold_its_link_and_stays_out_of_dedup(isolated_db):
    _apply_run([{"topic": DRILLING, "candidates": [], "filtered_findings": [
        {"stage": "search", "title": "Рано отсеяно", "reason": "дата события вне периода", "url": "https://example.com/x"},
    ]}])
    assert repository.visible_evidence_owners(["https://example.com/x"]) == {}
    assert repository.list_signals_for_dedup() == []

    # Найдено снова и принято судьёй — ссылка переходит к принятой карточке.
    accepted = repository.upsert_signal({"signal_key": "accepted", "title": "Принято", "theme": DRILLING,
                                         "signal_category": "technology"})
    repository.upsert_signal_evidence(accepted, {"source_url": "https://example.com/x", "title": "x"})

    assert set(repository.visible_evidence_owners(["https://example.com/x"])) == {"https://example.com/x"}
    assert accepted in _visible_ids()


def test_refiltered_finding_updates_the_same_card(isolated_db):
    finding = {"stage": "search", "title": "Повтор", "reason": "дата вне периода", "url": "https://example.com/r"}
    _apply_run([{"topic": DRILLING, "candidates": [], "filtered_findings": [finding]}])
    _apply_run([{"topic": DRILLING, "candidates": [], "filtered_findings": [finding]}])

    assert len(_filtered()) == 1


def test_run_collects_search_drops_and_judge_failures(monkeypatch):
    from oiltech_digest import signal_research
    from tests.test_signal_research import _Research, _event

    monkeypatch.setattr(signal_discovery, "JUDGE_RETRY_PAUSE_SECONDS", 0)
    monkeypatch.setattr(signal_research, "make_client", lambda offline: _Research([
        _event(title="Бизнес", event_kind="other", technology="", source_url="https://example.com/biz"),
        _event(title="Технология А", event_kind="deployment", technology="MPD", source_url="https://example.com/a"),
        _event(title="Технология Б", event_kind="field_test", technology="ГРП", source_url="https://example.com/b"),
    ]))
    monkeypatch.setattr(signal_discovery, "_fetch_full_text",
                        lambda url, fallback_title="", **kwargs: {"ok": False, "error": "403", "title": "", "raw_text": ""})

    def judge(cluster, topic, offline=True):
        if cluster[0]["source_url"].endswith("/a"):
            raise AIClientError("OpenAI API error 503: busy")
        return {"title": "Б", "theme": DRILLING, "score": 60, "maturity": "watch"}, {}

    monkeypatch.setattr(signal_discovery, "judge_signal_snapshot", judge)
    run_config = signal_discovery.SignalDiscoveryConfig(offline=True, dry_run=True, web_only=True, search_mode="openai_web")

    run = signal_discovery.run_discovery(run_config, {"topics": [{"name": DRILLING}], "tags": []})

    findings = run["topics"][0]["filtered_findings"]
    assert [(item["stage"], item["title"], item["url"]) for item in findings] == [
        ("search", "Бизнес", "https://example.com/biz"),
        ("judge_error", "Технология А", "https://example.com/a"),
    ]
    assert findings[1]["reason"].startswith("AIClientError: OpenAI API error 503")


# --- Причина и ход решения (03.10) ------------------------------------------------------------


def test_judge_is_asked_for_reasoning_steps_and_verdict_reason():
    schema = signal_discovery._judge_schema(THEMES, CRITERIA)["schema"]

    assert {"reasoning_steps", "verdict_reason"} <= set(schema["required"])
    assert schema["properties"]["reasoning_steps"] == {"type": "array", "items": {"type": "string"}}
    assert "reasoning_steps — ход решения" in signal_discovery.SIGNAL_JUDGE_INSTRUCTIONS


def test_reasoning_steps_are_kept_trimmed_and_capped():
    signal = signal_discovery._normalize_signal_payload(
        _judge_answer(reasoning_steps=["Событие: SLB внедрила MPD", "", *[f"шаг {i}" for i in range(10)]],
                      verdict_reason="Внедрение с цифрами"),
        DRILLING,
    )

    assert signal["reasoning_steps"][0] == "Событие: SLB внедрила MPD"
    assert len(signal["reasoning_steps"]) == 8
    assert signal["verdict_reason"] == "Внедрение с цифрами"


def test_decision_log_tells_why_a_card_was_accepted():
    candidate = {
        "signal": {"reasoning_steps": ["Событие: Nabors встроила MPD", "Технология: да"],
                   "verdict_reason": "внедрение на действующих буровых", "why_interesting": "первое применение"},
        "raw_output": {"theme_choice": {"theme": DRILLING, "reason": "judge"}},
        "rejected": False,
    }

    assert signal_discovery._decision_log(candidate) == [
        {"stage": "судья", "text": "Событие: Nabors встроила MPD"},
        {"stage": "судья", "text": "Технология: да"},
        {"stage": "судья", "text": "Итог: внедрение на действующих буровых"},
        {"stage": "ревью пачки", "text": "Оставлен: первое применение"},
        {"stage": "тематика", "text": f"{DRILLING} — выбор судьи"},
    ]


def test_decision_log_tells_why_a_card_was_filtered_and_by_whom():
    candidate = {
        "signal": {"reasoning_steps": ["Событие: вебинар"], "verdict_reason": "нет события",
                   "batch_review_reason": "реклама вендора без факта применения"},
        "raw_output": {"theme_choice": {"theme": DIGITAL, "reason": "theme_check",
                                        "theme_check": {"reason": "общая платформа"}}},
        "rejected": True,
    }

    log = signal_discovery._decision_log(candidate)

    assert {"stage": "тематика", "text": f"Второе мнение: {DIGITAL} — общая платформа"} in log
    assert log[-1] == {"stage": "отсев", "text": "Отсеяно (ревью пачки): реклама вендора без факта применения"}


def test_stored_cards_carry_reason_and_decision_log(isolated_db):
    accepted = _rejected("ok", "Принятая")
    accepted["rejected"] = False
    accepted["signal"].update(maturity="watch", reasoning_steps=["шаг"], verdict_reason="внедрение с цифрами")
    filtered = _rejected("no", "Отсеянная")
    filtered["signal"].update(reasoning_steps=["Событие: обзор"], verdict_reason="обзор рынка без события")

    _apply_run([{"topic": DRILLING, "candidates": [accepted, filtered], "filtered_findings": [
        {"stage": "search", "title": "Контракт ProPetro", "reason": "не технологическое событие",
         "url": "https://example.com/p", "publisher": "ProPetro", "event_date": "2026-09-20", "technology": ""},
    ]}])

    with repository.get_connection() as conn:
        rows = {key: (reason, log, filter_reason) for key, reason, log, filter_reason in conn.execute(
            "SELECT signal_key, verdict_reason, decision_log, filter_reason FROM signals")}
    assert rows["ok"][0] == "внедрение с цифрами" and rows["ok"][1][0] == {"stage": "судья", "text": "шаг"}
    # У отсеянного судьёй причина отсева — его итог, а не общий why_not_noise.
    assert rows["no"][2] == "обзор рынка без события"
    search = next(value for key, value in rows.items() if key.startswith("filtered:"))
    assert search[1] == [
        {"stage": "поиск", "text": "Найдено: Контракт ProPetro (ProPetro, 2026-09-20)"},
        {"stage": "отсев", "text": "Отсеяно (поиск): не технологическое событие"},
    ]


def test_decision_log_survives_a_refind_by_an_old_worker(isolated_db):
    signal_id = _store("card", decision_log=[{"stage": "судья", "text": "шаг"}], verdict_reason="причина")
    repository.upsert_signal({"signal_key": "card", "title": "card", "theme": DRILLING, "score": 50})

    with repository.get_connection() as conn:
        row = conn.execute("SELECT verdict_reason, decision_log FROM signals WHERE id = %s", (signal_id,)).fetchone()
    assert row == ("причина", [{"stage": "судья", "text": "шаг"}])
