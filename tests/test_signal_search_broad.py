"""Широкий поиск радара (#138, спека #128), шов 1: задача радара → воркер NL с подставным поиском GPT и страницами →
ядро (tests/otbor_radar_support.py). При «Отборе» поиск ищет все материалы нефтесервиса и нефтегаза, а не только
технологии; ссылка, которая уже есть в статьях сбора, новой находкой не становится; издания, которые парсер собирает
целиком, поиск получает как «уже есть, ищи дальше»; модель поиска — явная настройка ядра, едет в задаче.
"""

from datetime import datetime, timedelta, timezone

import pytest

from oiltech_digest import background_jobs, cli, config
from oiltech_digest.db import repository
from oiltech_digest.ingestion.normalize import url_key
from tests.otbor_radar_support import event, page, run_radar
from tests.otbor_support import answer, decide
from tests.otbor_support import contour  # noqa: F401 — фикстура
from tests.signal_card_support import DRILLING, radar_finding, run_worker, tag, user, worker  # noqa: F401 — фикстуры

MARKET = "Рынок, бизнес-модели, сервисные модели, партнёрства и M&A"
DEAL_URL = "https://adnoc.example/news/adnoc-buys-stake"
DEAL_TITLE = "ADNOC buys a stake in a drilling contractor for 1.2 billion dollars"


def collected(url, *, source="Upstream Online", site="https://www.upstream.example", source_type="News",
              days_ago=1):
    """Статья сбора с этим адресом: ключ тождества по адресу — как его пишет сбор (normalize.url_key)."""
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    with repository.get_connection() as conn:
        row = conn.execute("SELECT id FROM sources WHERE name = %s", (source,)).fetchone()
        if row is None:
            row = conn.execute("INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
                               "VALUES (%s, %s, %s, TRUE, 'rss') RETURNING id", (source, source_type, site)).fetchone()
        conn.execute("INSERT INTO articles (source_id, title, url, url_key, published_at, collected_at, raw_text, "
                     "language) VALUES (%s, %s, %s, %s, %s, %s, 'Текст статьи сбора', 'en')",
                     (row[0], f"Статья {url}", url, url_key(url), when, when))
        conn.commit()


def findings():
    """Заголовки находок, которые ждут «Отбора»."""
    with repository.get_connection() as conn:
        return sorted(row[0] for row in conn.execute("SELECT title FROM signals WHERE selector = 'otbor'"))


def radar_result():
    """Итог последнего прогона радара, как его записало ядро."""
    with repository.get_connection() as conn:
        return conn.execute("SELECT result_json FROM background_jobs WHERE kind = 'signal_discovery' "
                            "ORDER BY id DESC LIMIT 1").fetchone()[0]["applied"]


def test_broad_search_asks_for_every_oil_and_gas_material_and_a_deal_reaches_otbor(user, contour, monkeypatch):
    search, old_chain = run_radar(monkeypatch, [event(DEAL_TITLE, DEAL_URL, event_kind="deal")],
                                  {DEAL_URL: page(DEAL_TITLE)})

    [call] = search.calls
    instructions = call["instructions"].lower()
    # Технологии, контракты, сделки, программы бурения, регулирование; требования «только технологии» нет.
    for kind in ("технолог", "контракт", "сделк", "программ", "регулирован"):
        assert kind in instructions
    assert "не бизнес-новости" not in instructions and "not_technology" not in instructions
    assert call["schema"] == "radar_topic_research_broad"
    assert old_chain == []
    model = decide(monkeypatch, {DEAL_TITLE: answer()})
    assert len(model.calls) == 1


def test_market_topic_is_searched_when_otbor_decides(user, contour, monkeypatch):
    tag(MARKET)

    search, _ = run_radar(monkeypatch, [event(DEAL_TITLE, DEAL_URL)], {DEAL_URL: page(DEAL_TITLE)})

    # Тема рынка возвращается в число тем поиска: «Отбор» сам решает, что брать.
    assert sorted(call["input"].splitlines()[0] for call in search.calls) == [f"Тема: {DRILLING}", f"Тема: {MARKET}"]


def test_link_already_collected_is_not_a_new_finding_and_its_page_is_not_fetched(user, contour, monkeypatch):
    collected("https://www.upstream.example/news/drilling-contract")
    # Тот же материал в другом написании адреса: без www, со слэшем и меткой поиска OpenAI.
    known = "https://upstream.example/news/drilling-contract/?utm_source=openai"

    search, _ = run_radar(monkeypatch, [event("Upstream: rig contract in Kuwait", known),
                                        event(DEAL_TITLE, DEAL_URL)],
                          {known: page("Upstream: rig contract in Kuwait"), DEAL_URL: page(DEAL_TITLE)})

    assert findings() == [DEAL_TITLE]
    assert search.fetched == [DEAL_URL]
    [topic] = radar_result()["topics"]
    assert topic["research"]["already_collected"] == 1
    assert [item["url"] for item in topic["already_collected"]] == [
        "https://upstream.example/news/drilling-contract/"]
    assert len(topic["signal_ids"]) == 1


def test_link_the_parser_collected_while_the_search_ran_is_not_a_new_finding_either(user, contour, monkeypatch):
    late = "https://rigzone.example/news/frac-fleet"

    def parser_collects(url):
        if url == late:
            collected(url, source="Rigzone", site="https://www.rigzone.example", days_ago=0)

    run_radar(monkeypatch, [event("Halliburton completes its first electric frac fleet job", late),
                            event(DEAL_TITLE, DEAL_URL)],
              {late: page("Halliburton completes its first electric frac fleet job"), DEAL_URL: page(DEAL_TITLE)},
              on_fetch=parser_collects)

    assert findings() == [DEAL_TITLE]
    [topic] = radar_result()["topics"]
    assert [item["url"] for item in topic["already_collected"]] == [late]


def test_search_is_told_which_outlets_the_parser_collects_whole(user, contour, monkeypatch):
    monkeypatch.setattr(config, "SIGNAL_RESEARCH_COVERED_MIN_ARTICLES", 2)
    for number in (1, 2):
        collected(f"https://www.worldoil.example/news/{number}", source="World Oil", site="https://www.worldoil.example")
        collected(f"https://t.me/energytoday/{number}", source="Energy Today", site="https://t.me/energytoday",
                  source_type="Telegram")
    collected("https://www.bakerhughes.example/news/1", source="Baker Hughes", site="https://www.bakerhughes.example")
    # Статья до периода поиска охват издания не подтверждает.
    collected("https://www.offshore.example/news/old-1", source="Offshore", site="https://www.offshore.example",
              days_ago=40)
    collected("https://www.offshore.example/news/old-2", source="Offshore", site="https://www.offshore.example",
              days_ago=40)

    search, _ = run_radar(monkeypatch, [event(DEAL_TITLE, DEAL_URL)], {DEAL_URL: page(DEAL_TITLE)})

    prompt = search.calls[0]["input"]
    assert "worldoil.example" in prompt
    # Пара статей — издание собирается не целиком; Telegram-канал не сайт, который ищет поиск.
    for site in ("bakerhughes.example", "t.me", "offshore.example"):
        assert site not in prompt


def test_search_prompt_under_otbor_carries_no_expert_feedback_only_what_is_already_found(user, contour, monkeypatch):
    # Память ОС экспертов: одобренный пример, отказ с причиной, правило заказчика, доверенный сайт.
    memory = (("signal_verdict", "approved", {"signal_title": "Одобренный пример SLB", "topic": DRILLING,
                                              "reason": "Причина одобрения экспертом"}),
              ("signal_verdict", "wrong_block", {"signal_title": "Отклонённый контракт",
                                                 "reason": "Причина отказа экспертом"}),
              ("signal_quality_rule", "Правило эксперта про заголовки", {}),
              ("signal_source_preference", "trusted.example", {}))
    for number, (kind, subject, facts) in enumerate(memory):
        repository.upsert_signal_agent_memory(memory_key=f"{kind}:{number}", memory_type=kind, subject=subject,
                                              score=50, facts=facts)
    radar_finding("known-card", "Уже найденная находка о бурении", "Текст страницы")

    search, _ = run_radar(monkeypatch, [event(DEAL_TITLE, DEAL_URL)], {DEAL_URL: page(DEAL_TITLE)})

    # Граница данных О-7 (решение владельца 03.11): поиск только находит, из прошлого ему нужен список уже найденного.
    prompt = search.calls[0]["input"]
    assert "Уже найденная находка о бурении" in prompt
    for text in ("Одобренный пример SLB", "Причина одобрения экспертом", "Отклонённый контракт", "Причина отказа экспертом",
                 "Правило эксперта", "trusted.example", "заказчик"):
        assert text not in prompt


def test_search_model_reasoning_and_cap_are_core_settings_carried_in_the_task(user, contour, monkeypatch):
    monkeypatch.setattr(config, "SIGNAL_RESEARCH_MODEL", "gpt-6.1-sol")
    monkeypatch.setattr(config, "SIGNAL_RESEARCH_REASONING", "medium")
    monkeypatch.setattr(config, "SIGNAL_RESEARCH_EVENTS_PER_TOPIC", 20)

    def nl_env():
        # Свои строки поиска в .env NL воркер не читает: модель, уровень и потолок — решение ядра.
        monkeypatch.setattr(config, "SIGNAL_RESEARCH_MODEL", "gpt-5")
        monkeypatch.setattr(config, "SIGNAL_RESEARCH_REASONING", "minimal")
        monkeypatch.setattr(config, "SIGNAL_RESEARCH_EVENTS_PER_TOPIC", 3)

    search, _ = run_radar(monkeypatch, [event(DEAL_TITLE, DEAL_URL)], {DEAL_URL: page(DEAL_TITLE)},
                          before_worker=nl_env)

    [call] = search.calls
    assert (call["model"], call["reasoning_effort"]) == ("gpt-6.1-sol", "medium")
    assert "Сколько событий: до 20" in call["input"]


@pytest.mark.parametrize("model, reasoning, problem", [
    ("", "low", "SIGNAL_RESEARCH_MODEL не задан"),
    ("gpt-5", "low", "снимается 11.12.2026"),
    ("gpt-5-mini", "low", "снимается 11.12.2026"),
    ("gpt-5-2025-08-07", "low", "снимается 11.12.2026"),
    ("gpt-6.1-sol", "", "SIGNAL_RESEARCH_REASONING не задан"),
])
def test_radar_run_needs_an_explicit_search_model_outside_the_retired_gpt_5(user, contour, monkeypatch, model,
                                                                             reasoning, problem):
    monkeypatch.setattr(config, "SIGNAL_RESEARCH_MODEL", model)
    monkeypatch.setattr(config, "SIGNAL_RESEARCH_REASONING", reasoning)

    with pytest.raises(SystemExit, match=problem):
        cli.main(["enqueue-daily-signal-discovery", "--force"])

    with repository.get_connection() as conn:
        assert conn.execute("SELECT count(*) FROM background_jobs WHERE kind = 'signal_discovery'").fetchone()[0] == 0


def test_newer_gpt_5_generations_are_not_retired(user, contour, monkeypatch):
    monkeypatch.setattr(config, "SIGNAL_RESEARCH_MODEL", "gpt-5.5")

    assert background_jobs.enqueue_daily_signal_discovery(force=True)["enqueued"]


def test_brave_run_of_the_old_chain_needs_no_search_model(user, contour, monkeypatch):
    # Прежняя цепочка на поиске Brave модели поиска GPT не зовёт.
    monkeypatch.setattr(config, "OTBOR_ENABLED", False)
    monkeypatch.setattr(config, "SIGNAL_SEARCH_MODE", "brave")
    monkeypatch.setattr(config, "SIGNAL_RESEARCH_MODEL", "")

    assert background_jobs.enqueue_daily_signal_discovery(force=True)["enqueued"]


def test_worker_build_before_the_broad_search_gets_no_radar_run(user, contour, monkeypatch):
    tag(DRILLING)
    background_jobs.enqueue_daily_signal_discovery(force=True)

    # Сборка NL контракта 8 искала бы прежним промптом «только технологии» и моделью своего .env: задачу она не
    # получает, задача падает с причиной.
    assert worker("external-agents", contract_number=8).claim() is None
    with repository.get_connection() as conn:
        [(error,)] = conn.execute("SELECT error_message FROM background_jobs WHERE status = 'failed'").fetchall()
    assert "нужен контракт 9" in error
    run_worker("external-agents")
    assert findings() == []
