"""«Режим ChatGPT» радара: находки темы — один вызов модели со встроенным поиском OpenAI."""

from __future__ import annotations

from datetime import date

import pytest

from oiltech_digest import config, signal_discovery, signal_research
from oiltech_digest.processing.openai_client import AIClientError, AIResponse

TOPIC = "Бурение, направленное бурение, буровые растворы и буровое оборудование"


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
