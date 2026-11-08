"""Поиск радара («режим ChatGPT»): находки темы — один вызов модели со встроенным поиском OpenAI. Поиск только
находит: по смыслу не отсеивает ничего, остаются технические проверки (#131)."""

from __future__ import annotations

from datetime import date, timedelta
import re

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


def _run_config():
    """Задача прогона радара: модель и уровень поиска едут в ней (#138), воркер своих не читает."""
    return signal_discovery.SignalDiscoveryConfig(offline=True, dry_run=True, research_model="gpt-6.1-sol",
                                                  research_reasoning="low")


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


@pytest.mark.parametrize("event", [_event(source_url="slb.com/news"), _event(title="")])
def test_material_without_a_link_or_a_title_is_dropped_with_a_reason(event):
    result = _research(_Research([event]))

    assert result["evidence"] == []
    assert result["dropped"][0]["reason"] == signal_research.NO_LINK_OR_TITLE


@pytest.mark.parametrize("event", [
    # Дата модели вне периода и её нет вовсе: дата — черновые данные, старое событие решает «Отбор» по тексту.
    _event(event_date="2025-01-15"),
    _event(event_date=""),
    # «Не технология» по оценке поиска и технология не названа: категорию ставит «Отбор».
    _event(event_kind="not_technology", technology="", title="ProPetro получила контракт на генерацию 230 МВт"),
    _event(material_kind="deal", technology="", title="SLB покупает долю в разработчике ПО для бурения"),
])
def test_search_drops_nothing_by_meaning_every_material_goes_on(event):
    result = _research(_Research([event]))

    assert result["dropped"] == [] and len(result["evidence"]) == 1
    assert result["evidence"][0]["published_at"] == (event["event_date"] or None)


def test_model_failure_is_a_status_not_a_crash():
    result = _research(_Research([], error=AIClientError("OpenAI API error 400: tool not supported")))

    assert result["status"] == "error"
    assert "tool not supported" in result["error"]
    assert result["evidence"] == []


def test_only_a_finding_with_its_publisher_page_goes_on():
    """Во вход «Отбора» идёт текст издателя: ссылка без страницы — отсев, даже если адрес из найденного поиском;
    выдуманная ссылка и страница-список — тоже. Сводка модели остаётся в summary_ru рядом с текстом страницы."""
    title = "SLB wins Aramco integrated well construction contracts"
    fetched = {"source_url": "https://a.example/1", "title": title, "summary_ru": "текст страницы",
               "raw_payload": {"evidence_source": "openai_web_search", "full_text_fetched": True,
                               "cited_by_search": False, "research_summary": "сводка модели"}}
    cited = {"source_url": "https://b.example/2", "title": title,
             "raw_payload": {"evidence_source": "openai_web_search", "full_text_fetched": False, "cited_by_search": True}}
    invented = {"source_url": "https://c.example/3", "title": title,
                "raw_payload": {"evidence_source": "openai_web_search", "full_text_fetched": False, "cited_by_search": False}}
    listing = {"source_url": "https://d.example/news", "title": "Press Releases",
               "raw_payload": {"evidence_source": "openai_web_search", "full_text_fetched": True}}

    kept, dropped = signal_research.verify_research_evidence([fetched, cited, invented, listing])

    assert [item["source_url"] for item in kept] == ["https://a.example/1"]
    assert kept[0]["summary_ru"] == "сводка модели"
    assert [(item["url"], item["reason"]) for item in dropped] == [
        ("https://b.example/2", signal_research.PAGE_NOT_FETCHED),
        ("https://c.example/3", signal_research.UNCONFIRMED_LINK),
        ("https://d.example/news", signal_research.LIST_PAGE),
    ]


def test_each_research_event_is_its_own_cluster():
    first = {"source_url": "https://slb.com/a", "title": "SLB autonomous drilling contract",
             "extracted_fact": "SLB autonomous drilling", "raw_payload": {"evidence_source": "openai_web_search"}}
    second = {**first, "source_url": "https://slb.com/b"}

    clusters = signal_discovery._cluster_evidence([first, second])

    assert len(clusters) == 2


def test_run_searches_with_gpt_and_a_finding_without_its_page_is_a_search_drop(monkeypatch):
    client = _Research([_recent_event()])
    monkeypatch.setattr(signal_research, "make_client", lambda offline: client)
    monkeypatch.setattr(signal_discovery, "_fetch_full_text",
                        lambda url, fallback_title="", **kwargs: {"ok": False, "error": "403", "title": "", "raw_text": ""})
    run_config = _run_config()

    run = signal_discovery.run_discovery(run_config, {"topics": [{"name": TOPIC}], "tags": []})

    topic = run["topics"][0]
    assert topic["web_search"]["provider"] == "openai_web_search"
    assert topic["web_search"]["research"]["events"] == 1
    # Страница не открылась, хотя адрес из найденного: «Отбору» нечего дать — находка уходит в технический отсев.
    assert topic["candidates"] == []
    [dropped] = topic["filtered_findings"]
    assert (dropped["stage"], dropped["reason"]) == ("search", signal_research.PAGE_NOT_FETCHED)
    assert dropped["url"].startswith("https://www.slb.com/newsroom/")


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

    response = client.research_json("i", "u", signal_research.RESEARCH_SCHEMA_BROAD, model="gpt-5",
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


# --- Что поиск знает о прошлом: только уже найденное по теме (граница данных О-7) -------------------


def _memory():
    """Пустая память ОС в снимке: воркер NL в базу за ней не ходит."""
    return {name: [] for name in ("signal_verdict", "signal_quality_rule", "signal_source_preference",
                                  "signal_glossary", "signal_query_hint", "signal_title_correction", "signal_duplicate")}


def _known_cards_prompt(monkeypatch, existing_signals):
    client = _Research([_event()])
    monkeypatch.setattr(signal_research, "make_client", lambda offline: client)
    monkeypatch.setattr(signal_discovery, "_fetch_full_text",
                        lambda url, fallback_title="", **kwargs: {"ok": False, "error": "403", "title": "", "raw_text": ""})
    monkeypatch.setattr(config, "SIGNAL_RESEARCH_DAYS", 30)
    run_config = _run_config()
    snapshot = {"topics": [{"name": TOPIC}], "tags": [], "memory": _memory(), "existing_signals": existing_signals}

    signal_discovery.run_discovery(run_config, snapshot)

    return client.calls[0]["input"]


def _days_ago(days):
    return (date.today() - timedelta(days=days)).isoformat()


def _recent_event(**overrides):
    """Событие внутри периода поиска, который run_discovery отсчитывает от сегодня: зашитая дата из него со временем
    выпадает (28.10 так выпало 24.09, и находка не доходила до судьи)."""
    return _event(**{"event_date": _days_ago(3), **overrides})


def test_research_prompt_knows_topic_cards_seen_within_the_search_period(monkeypatch):
    """Разовые прогоны идут реже, чем раз в 3 дня, — свежих (fresh) нет, и поиск не знал ничего.

    Прод 14.10 и 17.10: на старте свежих карточек — 0 из 243 и 0 из 264, список «уже на радаре»
    пуст во всех темах; 15 и 20 находок — уже известные карточки (17.10 — 20 из 41). Среди них
    WesCan (впервые 03.10) и Expro Velonix (впервые 08.09, снова — 14.10): обе видел радар за
    период поиска, а в промпт они не попадали."""
    prompt = _known_cards_prompt(monkeypatch, [
        {"id": 176, "title_ru": "WesCan пробурила МЛТ‑скважину с 7 латералями в Rex/Mannville", "theme": TOPIC,
         "fresh": False, "first_seen_day": _days_ago(15), "last_seen_day": _days_ago(15)},
        {"id": 193, "title_ru": "Expro Velonix обеспечил первые ILI‑прогоны на водородном трубопроводе", "theme": TOPIC,
         "fresh": False, "first_seen_day": _days_ago(39), "last_seen_day": _days_ago(3)},
        {"id": 2, "title_ru": "Событие прошлого квартала", "theme": TOPIC, "fresh": False,
         "first_seen_day": _days_ago(80), "last_seen_day": _days_ago(45)},
        {"id": 3, "title_ru": "Чужая тема", "theme": "HSE", "fresh": False,
         "first_seen_day": _days_ago(5), "last_seen_day": _days_ago(5)},
    ])

    known = prompt.split("Уже есть на радаре по этой теме — не повторяй, ищи новое:\n", 1)[1]
    # Свежие первыми: Velonix радар видел 3 дня назад, WesCan — 15.
    assert known.startswith("- Expro Velonix обеспечил первые ILI‑прогоны на водородном трубопроводе\n"
                            "- WesCan пробурила МЛТ‑скважину с 7 латералями в Rex/Mannville")
    assert "Событие прошлого квартала" not in prompt and "Чужая тема" not in prompt


def test_research_prompt_caps_known_cards_per_topic_keeping_the_latest(monkeypatch):
    from oiltech_digest import signal_feedback

    cap = signal_feedback.RESEARCH_KNOWN_TITLES
    prompt = _known_cards_prompt(monkeypatch, [
        {"id": index, "title_ru": f"Карточка №{index:02d}", "theme": TOPIC, "fresh": False,
         "first_seen_day": _days_ago(index // 2), "last_seen_day": _days_ago(index // 2)}
        for index in range(cap + 5)
    ])

    assert f"- Карточка №{cap - 1:02d}" in prompt and f"- Карточка №{cap:02d}" not in prompt
    assert prompt.count("- Карточка №") == cap


def test_core_snapshot_lets_the_search_know_a_card_refound_within_the_period(isolated_db, monkeypatch):
    """Ядро отдаёт день последней находки: карточку, впервые найденную 40 дней назад и снова —
    5 дней назад, поиск темы знает (17.10 так повторились Expro Velonix и Aganova — впервые в сентябре)."""
    from oiltech_digest.db import repository

    card = repository.upsert_signal({"signal_key": "velonix", "title": "Expro Velonix first ILI runs",
                                     "title_ru": "Expro Velonix: первые ILI‑прогоны на водородном трубопроводе",
                                     "theme": TOPIC, "maturity": "watch", "score": 60, "companies": ["Expro"]})
    with repository.get_connection() as conn:
        conn.execute("UPDATE signals SET first_seen_at = now() - interval '40 days', "
                     "last_seen_at = now() - interval '5 days' WHERE id = %s", (card,))
        conn.commit()
    rows = repository.list_signals_for_dedup()
    assert rows[0]["fresh"] is False and rows[0]["first_seen_day"] < rows[0]["last_seen_day"]

    prompt = _known_cards_prompt(monkeypatch, rows)

    assert "- Expro Velonix: первые ILI‑прогоны на водородном трубопроводе" in prompt


def test_named_technology_stays_in_the_finding_as_draft_data():
    event = _event(material_kind="contract", technology="Автоматизированное строительство скважин SLB")

    item = _research(_Research([event]))["evidence"][0]

    assert "Технология: Автоматизированное строительство скважин SLB." in item["extracted_fact"]
    assert (item["raw_payload"]["material_kind"], item["raw_payload"]["technology"]) == (
        "contract", "Автоматизированное строительство скважин SLB")


def test_research_asks_for_a_russian_title_by_the_title_rules():
    """#150: правила заголовка «Сути и перевода» (prompts.TRANSLATE_INSTRUCTIONS) — строкой и в промпте поиска: его
    заголовок остаётся у находки, если у страницы нет своего, и виден в отсеянном поиском. Предел длины один."""
    from oiltech_digest.processing.prompts import TRANSLATE_INSTRUCTIONS

    limit = re.search(r"Не длиннее (\d+) знаков", TRANSLATE_INSTRUCTIONS).group(1)
    text = " ".join(signal_research.RESEARCH_INSTRUCTIONS_BROAD.split())

    assert f"- title: по-русски, по правилам заголовка (кто что сделал, до {limit} знаков, без тире и скобок)" in text


# --- Ревью #89 (02.10): итог задачи показывает, чем искали и почему поиск не ответил -------------


def test_research_failure_reaches_the_job_result_with_its_text():
    web = {"status": "error", "provider": "openai_web_search",
           "research": {"status": "error", "events": 0, "feedback_chars": 812, "web_search_calls": 0, "attempts": 2,
                        "error": "AIClientError: OpenAI HTTP 403: tool web_search is not supported", "dropped": []}}

    assert signal_discovery._search_error_summary(web) == "AIClientError: OpenAI HTTP 403: tool web_search is not supported"
    assert signal_discovery._research_summary(web["research"]) == {
        "status": "error", "events": 0, "feedback_chars": 812, "web_search_calls": 0, "attempts": 2,
        "unverified": 0, "dropped": 0, "error": "AIClientError: OpenAI HTTP 403: tool web_search is not supported",
    }


def test_research_summary_counts_without_events():
    research = {"status": "ok", "events": 7, "evidence_count": 5, "feedback_chars": 900, "web_search_calls": 23,
                "attempts": 1, "unverified": [{"url": "x"}, {"url": "y"}], "dropped": [{"title": "t", "reason": "r"}]}

    assert signal_discovery._research_summary(research) == {
        "status": "ok", "events": 7, "evidence_count": 5, "feedback_chars": 900, "web_search_calls": 23,
        "attempts": 1, "unverified": 2, "dropped": 1,
    }
    assert signal_discovery._research_summary(None) is None
