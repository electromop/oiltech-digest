"""«Режим ChatGPT» радара: находки темы — один вызов модели со встроенным поиском OpenAI."""

from __future__ import annotations

from datetime import date

import pytest

from oiltech_digest import config, signal_discovery, signal_research
from oiltech_digest.processing.openai_client import AIClientError, AIResponse

TOPIC = "Бурение, направленное бурение, буровые растворы и буровое оборудование"


@pytest.fixture(autouse=True)
def _no_retry_pause(monkeypatch):
    monkeypatch.setattr(signal_research, "RESEARCH_RETRY_PAUSE_SECONDS", 0)


def _event(**overrides):
    event = {
        "title": "SLB wins Aramco integrated well construction contracts",
        "company": "SLB",
        "event_date": "2026-09-24",
        "summary": "SLB получила четыре контракта Aramco на строительство скважин с автоматизацией бурения.",
        "why_important": "Масштабирование автоматизированного бурения у крупнейшего оператора.",
        "source_url": "https://www.slb.com/newsroom/press-release/2026/pr-2026-0924-slb-aramco?utm_source=openai",
        "publisher": "SLB",
        "source_kind": "primary",
    }
    event.update(overrides)
    return event


class _Research:
    def __init__(self, events, cited=None, error=None):
        self.events = events
        self.cited = cited if cited is not None else [e["source_url"] for e in events]
        self.error = error
        self.calls = []

    def research_json(self, instructions, user_input, schema, **kwargs):
        self.calls.append({"input": user_input, **kwargs})
        if self.error:
            raise self.error
        return AIResponse(data={"events": self.events, "_cited_urls": self.cited, "_web_search_calls": 7},
                          model="gpt-5-test", input_tokens=17000, output_tokens=1300)


def _research(client, **kwargs):
    return signal_research.research_topic(TOPIC, period_end=date(2026, 9, 30), days=30,
                                          client_factory=lambda: client, **kwargs)


def test_events_become_radar_evidence_with_primary_source_strength():
    client = _Research([_event()])

    result = _research(client)

    assert result["status"] == "ok" and result["events"] == 1 and result["web_search_calls"] == 7
    item = result["evidence"][0]
    # utm_source=openai — не часть адреса события.
    assert item["source_url"] == "https://www.slb.com/newsroom/press-release/2026/pr-2026-0924-slb-aramco"
    assert item["published_at"] == "2026-09-24"
    assert item["strength"] == 0.9
    assert item["raw_payload"]["evidence_source"] == "openai_web_search"
    assert item["raw_payload"]["cited_by_search"] is True
    assert "Период: с 2026-08-31 по 2026-09-30" in client.calls[0]["input"]
    assert client.calls[0]["model"] == config.SIGNAL_RESEARCH_MODEL


@pytest.mark.parametrize("event, reason", [
    (_event(event_date="2025-01-15"), "вне периода"),
    (_event(source_url="slb.com/news"), "нет ссылки"),
    (_event(title=""), "нет ссылки или заголовка"),
])
def test_bad_events_are_dropped_with_a_reason(event, reason):
    result = _research(_Research([event]))

    assert result["evidence"] == []
    assert reason in result["dropped"][0]["reason"]


def test_event_without_date_is_kept_for_the_judge():
    result = _research(_Research([_event(event_date="")]))

    assert result["evidence"][0]["published_at"] is None


def test_model_failure_is_a_status_not_a_crash():
    result = _research(_Research([], error=AIClientError("OpenAI API error 400: tool not supported")))

    assert result["status"] == "error"
    assert "tool not supported" in result["error"]
    assert result["evidence"] == []


def test_unverified_link_is_dropped_but_cited_or_fetched_ones_stay():
    fetched = {"source_url": "https://a.example/1", "title": "a", "summary_ru": "текст страницы",
               "raw_payload": {"evidence_source": "openai_web_search", "full_text_fetched": True,
                               "cited_by_search": False, "research_summary": "сводка модели"}}
    cited = {"source_url": "https://b.example/2", "title": "b",
             "raw_payload": {"evidence_source": "openai_web_search", "full_text_fetched": False, "cited_by_search": True}}
    invented = {"source_url": "https://c.example/3", "title": "c",
                "raw_payload": {"evidence_source": "openai_web_search", "full_text_fetched": False, "cited_by_search": False}}
    brave = {"source_url": "https://d.example/4", "title": "d", "raw_payload": {"evidence_source": "web_search"}}

    kept, dropped = signal_research.verify_research_evidence([fetched, cited, invented, brave])

    assert [item["source_url"] for item in kept] == ["https://a.example/1", "https://b.example/2", "https://d.example/4"]
    # Судье — и текст страницы (extracted_fact), и сводка модели.
    assert kept[0]["summary_ru"] == "сводка модели"
    assert dropped[0]["url"] == "https://c.example/3"


def test_each_research_event_is_its_own_cluster():
    first = {"source_url": "https://slb.com/a", "title": "SLB autonomous drilling contract",
             "extracted_fact": "SLB autonomous drilling", "raw_payload": {"evidence_source": "openai_web_search"}}
    second = {**first, "source_url": "https://slb.com/b"}

    clusters = signal_discovery._cluster_evidence([first, second], TOPIC)

    assert len(clusters) == 2


def test_run_in_research_mode_uses_openai_search_not_brave(monkeypatch):
    client = _Research([_event()])
    monkeypatch.setattr(signal_research, "make_client", lambda offline: client)
    monkeypatch.setattr(signal_discovery, "_search_web_evidence",
                        lambda *args, **kwargs: pytest.fail("Brave не должен вызываться в режиме openai_web"))
    monkeypatch.setattr(signal_discovery, "_fetch_full_text",
                        lambda url, fallback_title="", **kwargs: {"ok": False, "error": "403", "title": "", "raw_text": ""})
    run_config = signal_discovery.SignalDiscoveryConfig(offline=True, dry_run=True, web_only=True, search_mode="openai_web")

    run = signal_discovery.run_discovery(run_config, {"topics": [{"name": TOPIC}], "tags": []})

    topic = run["topics"][0]
    assert topic["web_search"]["provider"] == "openai_web_search"
    assert topic["web_search"]["research"]["events"] == 1
    # Страница не открылась, но адрес из найденного — находка осталась и дошла до судьи.
    assert topic["candidates"][0]["signal"]["evidence"][0]["source_url"].startswith("https://www.slb.com/newsroom/")


def test_search_mode_travels_from_core_to_worker(monkeypatch):
    monkeypatch.setattr(config, "SIGNAL_SEARCH_MODE", "openai_web")

    assert signal_discovery.config_from_payload({}).search_mode == "openai_web"
    assert signal_discovery.config_from_payload({"search_mode": "both"}).search_mode == "both"
    assert signal_discovery.config_from_payload({"search_mode": "gpt"}).search_mode == "brave"


def test_research_answer_in_several_messages_or_with_trailing_text_is_parsed():
    # Сравнительный прогон 30.09: две темы из трёх упали «non-JSON output» при нормальных событиях.
    from oiltech_digest.processing.openai_client import _research_payload

    raw = {"output": [
        {"type": "web_search_call"},
        {"type": "message", "content": [{"type": "output_text", "text": '{"events": [{"title": "a"}]}'}]},
        {"type": "message", "content": [{"type": "output_text",
                                         "text": '{"events": [{"title": "a"}, {"title": "b"}]}\n\nИсточники: [slb.com](x)'}]},
    ]}

    assert _research_payload(raw) == {"events": [{"title": "a"}, {"title": "b"}]}


def test_research_answer_without_any_json_is_an_error():
    from oiltech_digest.processing.openai_client import _research_payload

    raw = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "Не нашёл событий."}]}]}

    with pytest.raises(AIClientError, match="non-JSON"):
        _research_payload(raw)


class _Sequence:
    """Клиент, отвечающий по очереди: ответ — список событий или исключение."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def research_json(self, instructions, user_input, schema, **kwargs):
        self.prompts.append(user_input)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return AIResponse(data={"events": answer, "_cited_urls": [e["source_url"] for e in answer],
                                "_web_search_calls": 5}, model="gpt-5-test", input_tokens=100, output_tokens=10)


def test_empty_answer_is_retried_with_a_hint_and_usage_is_summed():
    client = _Sequence([], [_event()])

    result = _research(client)

    assert result["events"] == 1
    assert result["attempts"] == 2
    assert result["retries"] == ["пустой список событий"]
    assert "Предыдущая попытка не нашла" in client.prompts[1]
    assert (result["web_search_calls"], result["input_tokens"]) == (10, 200)


def test_transient_failure_is_retried():
    client = _Sequence(AIClientError("OpenAI returned non-JSON output: ..."), [_event()])

    result = _research(client)

    assert result["status"] == "ok" and result["events"] == 1


def test_two_empty_answers_are_ok_with_no_events():
    result = _research(_Sequence([], []))

    assert result["status"] == "ok" and result["events"] == 0 and result["attempts"] == 2


def test_two_failures_are_an_error():
    result = _research(_Sequence(AIClientError("503"), AIClientError("503")))

    assert result["status"] == "error" and result["evidence"] == []


def test_research_client_collects_search_sources_as_grounding(monkeypatch):
    # При строгом JSON сносок нет (30.09: 0 из 18) — подтверждение ссылки даёт список
    # источников, которые поиск реально вернул (include web_search_call.action.sources).
    from oiltech_digest.processing import openai_client

    sent = {}

    class Response:
        status_code = 200

        def json(self):
            return {"model": "gpt-5", "usage": {"input_tokens": 1, "output_tokens": 1}, "output": [
                {"type": "web_search_call", "action": {"type": "search", "sources": [
                    {"type": "url", "url": "https://investors.bakerhughes.com/news/bp-award"}]}},
                {"type": "message", "content": [{"type": "output_text", "text": '{"events": []}'}]},
            ]}

    monkeypatch.setattr(openai_client.requests, "post", lambda url, **kwargs: sent.update(kwargs["json"]) or Response())
    client = openai_client.OpenAIResponsesClient(api_key="test")

    response = client.research_json("i", "u", signal_research.RESEARCH_SCHEMA, model="gpt-5",
                                     reasoning_effort="low", max_output_tokens=100, timeout=5)

    assert sent["include"] == ["web_search_call.action.sources"]
    assert response.data["_cited_urls"] == ["https://investors.bakerhughes.com/news/bp-award"]
    assert response.data["_web_search_calls"] == 1


def test_heartbeat_before_each_attempt_keeps_the_job_alive_on_the_worker():
    beats = []
    signal_research.research_topic(TOPIC, period_end=date(2026, 9, 30), days=30,
                                   client_factory=lambda: _Sequence([], [_event()]),
                                   heartbeat=lambda: beats.append(1))

    assert len(beats) == 2


# --- Обратная связь заказчика — во вход поиска ------------------------------------------------


def _memory(**types):
    base = {name: [] for name in ("signal_verdict", "signal_quality_rule", "signal_source_preference",
                                  "signal_glossary", "signal_query_hint", "signal_title_correction", "signal_duplicate")}
    base.update(types)
    return base


def _verdict(subject, title, reason="", topic=""):
    return {"memory_type": "signal_verdict", "subject": subject, "score": 0,
            "facts_json": {"signal_title": title, "reason": reason, "topic": topic}}


def test_feedback_block_carries_verdicts_rules_sources_and_known_cards():
    from oiltech_digest import signal_feedback

    memory = _memory(
        signal_verdict=[
            _verdict("approved", "Роботизированная буровая на Ямале", "первое внедрение в РФ", topic=TOPIC),
            _verdict("wrong_block", "SLB купила стартап", "сделка без технологии"),
            _verdict("too_generic", "Обзор рынка бурения 2026", "нет события"),
            _verdict("merge_duplicate", "Повтор карточки 12"),
        ],
        signal_quality_rule=[{"subject": "Не приносить вебинары", "facts_json": {}},
                             {"subject": "Для сигнала 'X' использовать исправленную суть: Y", "facts_json": {}}],
        signal_source_preference=[{"subject": "jpt.spe.org", "facts_json": {}}],
    )

    with signal_feedback.use_memory_snapshot(memory):
        block = signal_feedback.research_feedback_block(TOPIC, known_titles=["SLB: 4 контракта Aramco"])

    assert "одобрил" in block and "- Роботизированная буровая на Ямале — первое внедрение в РФ" in block
    assert "- SLB купила стартап — это бизнес-сигнал, а не технология. сделка без технологии" in block
    assert "Обзор рынка бурения 2026" in block
    # Дубль — не урок для поиска; правка сути конкретной карточки — тоже.
    assert "Повтор карточки 12" not in block and "исправленную суть" not in block
    assert "- Не приносить вебинары" in block
    assert "в первую очередь: jpt.spe.org" in block
    assert "не повторяй, ищи новое:\n- SLB: 4 контракта Aramco" in block


def test_feedback_block_is_empty_without_memory():
    from oiltech_digest import signal_feedback

    with signal_feedback.use_memory_snapshot(_memory()):
        assert signal_feedback.research_feedback_block(TOPIC) == ""


def test_research_prompt_gets_feedback_and_cards_already_on_the_radar(monkeypatch):
    client = _Research([_event()])
    monkeypatch.setattr(signal_research, "make_client", lambda offline: client)
    monkeypatch.setattr(signal_discovery, "_fetch_full_text",
                        lambda url, fallback_title="", **kwargs: {"ok": False, "error": "403", "title": "", "raw_text": ""})
    run_config = signal_discovery.SignalDiscoveryConfig(offline=True, dry_run=True, web_only=True, search_mode="openai_web")
    snapshot = {
        "topics": [{"name": TOPIC}], "tags": [],
        "memory": _memory(signal_verdict=[_verdict("reject", "Вебинар по бурению", "нет события")]),
        "existing_signals": [
            {"id": 1, "title_ru": "Nabors встроила MPD в SmartROS", "theme": TOPIC, "fresh": True},
            {"id": 2, "title_ru": "Старая карточка", "theme": TOPIC, "fresh": False},
            {"id": 3, "title_ru": "Чужая тема", "theme": "HSE", "fresh": True},
        ],
    }

    signal_discovery.run_discovery(run_config, snapshot)

    prompt = client.calls[0]["input"]
    assert "Вебинар по бурению — нет события" in prompt
    assert "- Nabors встроила MPD в SmartROS" in prompt
    assert "Старая карточка" not in prompt and "Чужая тема" not in prompt


# --- Только технологические сигналы: бизнес отсекается до судьи (02.10) ------------------------


@pytest.mark.parametrize("event", [
    _event(event_kind="other", technology="", title="ProPetro получила контракт на генерацию 230 МВт"),
    _event(event_kind="deployment", technology="  ", title="Внедрение без названной технологии"),
    _event(event_kind="merger", technology="ИИ", title="Неизвестный тип события"),
])
def test_business_or_unnamed_technology_events_never_reach_the_judge(event):
    result = _research(_Research([event]))

    assert result["evidence"] == []
    assert result["dropped"][0]["reason"] == "не технологическое событие (нет технологии)"


def test_technology_contract_is_kept_and_names_the_technology():
    event = _event(event_kind="technology_contract", technology="Автоматизированное строительство скважин SLB")

    item = _research(_Research([event]))["evidence"][0]

    assert "Технология: Автоматизированное строительство скважин SLB." in item["extracted_fact"]
    assert (item["raw_payload"]["event_kind"], item["raw_payload"]["technology"]) == (
        "technology_contract", "Автоматизированное строительство скважин SLB")


def test_research_asks_for_technology_and_event_kind_and_excludes_business():
    item = signal_research.RESEARCH_SCHEMA["schema"]["properties"]["events"]["items"]
    assert {"technology", "event_kind"} <= set(item["required"])
    assert item["properties"]["event_kind"]["enum"][-1] == "other"
    text = signal_research.RESEARCH_INSTRUCTIONS
    assert "Нужны ТЕХНОЛОГИЧЕСКИЕ сигналы, не бизнес-новости" in text
    assert "FID" in text and "слияния и поглощения" in text
