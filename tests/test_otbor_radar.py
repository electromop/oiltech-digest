"""Радар через «Отбор» (#131, спека #128), шов 1 — контур целиком: задача радара → воркер NL с подставными поиском GPT
и страницами → ядро → шаг «Отбора» → воркер → ядро → «Суть и перевод» → API витрин и разделов отклонённого
(tests/otbor_radar_support.py). Поиск только находит: решает «Отбор».
"""

from pathlib import Path

from oiltech_digest import signal_discovery
from oiltech_digest.db import repository, signal_card_store
from oiltech_digest.signal_research import LIST_PAGE, NO_LINK_OR_TITLE, PAGE_NOT_FETCHED, UNCONFIRMED_LINK
from tests.otbor_radar_support import DRAFT_SUMMARY, DRAFT_WHY, PAGE_TEXT, event, page, run_radar, summarize
from tests.otbor_support import answer, decide, reject, showcase
from tests.otbor_support import contour  # noqa: F401 — фикстура
from tests.signal_card_support import admin, api_get, user  # noqa: F401 — фикстуры

CONTRACT_URL = "https://adnoc.example/news/hail-ghasha-drilling"
CONTRACT_TITLE = "ADNOC Drilling awards SLB a contract to drill 40 wells for Hail and Ghasha"
CONTRACT_RU = "ADNOC Drilling заключила с SLB контракт на бурение 40 скважин"


def _sections(admin):
    return {card["title"]: card["section_reason"] for card in api_get("/api/signals", admin, section="all")}


def test_business_finding_the_old_search_dropped_reaches_otbor_and_gets_a_russian_title_and_summary(
        user, admin, contour, monkeypatch):
    # Вход «Отбора» из поиска — только поиск GPT: Brave не подключается и в режиме «оба».
    monkeypatch.setattr(signal_discovery.app_config, "SIGNAL_SEARCH_MODE", "both")
    # Прежний поиск отбросил бы его трижды: «не технология» по своей оценке, технологии не назвал, дата вне периода.
    search, old_chain = run_radar(monkeypatch, [event(CONTRACT_TITLE, CONTRACT_URL, event_kind="not_technology",
                                                      technology="", event_date="2024-03-01")],
                                  {CONTRACT_URL: page(CONTRACT_TITLE)})

    assert old_chain == []
    assert len(search.calls) == 1
    model = decide(monkeypatch, {CONTRACT_TITLE: answer()})

    [call] = model.calls
    # Вход «Отбора» у находки — страница издателя целиком (до 6 000 знаков, а не обрывок в 1 500), её адрес и дата
    # поступления; ни даты события, ни черновых данных модели поиска (тип, компания, суть, «почему важно»).
    assert len(PAGE_TEXT) > 1500 and PAGE_TEXT in call["input"]
    header = call["input"].split("Текст публикации:", 1)[0]
    assert "Источник: adnoc.example" in header and "Дата публикации: не указана" in header
    assert "Ссылки находки: нет" in header
    for draft in (DRAFT_SUMMARY, DRAFT_WHY, "not_technology", "2024-03-01"):
        assert draft not in call["input"]
    # Решение «Отбора» — у сигнала радара, витрину выбирает категория: бизнес-находка в бизнес-ленте.
    summary_model = summarize(monkeypatch, {CONTRACT_TITLE: (CONTRACT_RU, "Суть от «Суть и перевод»")})
    card = showcase(user, "business")[CONTRACT_TITLE]
    assert (card["origin"], card["otbor_decision"], card["signal_category"], card["title_ru"], card["summary"]) == (
        "radar", "accept", "business", CONTRACT_RU, "Суть от «Суть и перевод»")
    assert [step["stage"] for step in card["decision_log"]][:2] == ["поиск", "«Отбор»"]
    # «Суть и перевод» получает категорию, связь и применение, но не решение и не обоснование «Отбора».
    [summary_call] = [one for one in summary_model.calls if one["schema"] == "article_summary"]
    assert "category: business" in summary_call["input"] and "oilfield_relevance: direct" in summary_call["input"]
    assert "accept" not in summary_call["input"] and "Программа бурения оператора" not in summary_call["input"]
    assert showcase(user, "technology") == {}


def test_technical_drops_of_the_search_lie_in_search_sections_and_never_reach_otbor(user, admin, contour, monkeypatch):
    invented, blocked = "https://invented.example/news/1", "https://paywall.example/news/2"
    listing, broken = "https://tgs.example/press-releases", "slb.com/news"
    _, old_chain = run_radar(
        monkeypatch,
        [event("Invented drilling record in Oman", invented), event("Paywalled rig contract in Kuwait", blocked),
         event("TGS multi-client survey offshore Brazil", listing), event("Broken link rig news", broken)],
        {listing: page("Press Releases")},
        cited=[blocked, listing])

    model = decide(monkeypatch, {})

    assert old_chain == [] and model.calls == []
    assert _sections(admin) == {
        "Invented drilling record in Oman": f"Поиск → Ссылка не подтверждена: {UNCONFIRMED_LINK}",
        "Paywalled rig contract in Kuwait": f"Поиск → Текст страницы не получен: {PAGE_NOT_FETCHED}",
        "Press Releases": f"Поиск → Страница-список: {LIST_PAGE}",
        # Причина раздела в короткой причине не повторяется.
        "Broken link rig news": "Поиск → Нет ссылки или заголовка",
    }
    assert NO_LINK_OR_TITLE == "нет ссылки или заголовка"
    assert showcase(user, "technology") == {} and showcase(user, "business") == {}


def test_mixed_events_is_a_reason_only_for_a_finding_with_several_links(user, admin, contour, monkeypatch):
    first, second = "https://slb.example/news/autonomous-drilling", "https://aramco.example/news/autonomous-drilling"
    single = "https://equinor.example/news/robot-drilling"
    run_radar(monkeypatch,
              [event("SLB starts autonomous drilling for Aramco in 40 wells", first, company="SLB"),
               event("Aramco and SLB begin autonomous drilling in 40 wells", second, company="SLB"),
               event("Equinor tests a drilling robot offshore Norway", single, company="Equinor")],
              {first: page("SLB starts autonomous drilling for Aramco in 40 wells"),
               second: page("Aramco and SLB begin autonomous drilling in 40 wells"),
               single: page("Equinor tests a drilling robot offshore Norway")},
              same_event=True)

    mixed = reject("mixed_events", industry="oilfield_services", oilfield_relevance="direct")
    model = decide(monkeypatch, {"SLB starts autonomous drilling for Aramco in 40 wells": mixed,
                                 "Aramco and SLB begin autonomous drilling in 40 wells": mixed,
                                 "Equinor tests a drilling robot offshore Norway": [mixed, answer(
                                     industry="oil_gas", category="technology")]})

    # «Дедуп» склеил две находки одного события: у главной две ссылки, и «разные события» у неё — решение, а не
    # конфликт. У находки с одной ссылкой та же причина — конфликт шагов и повтор.
    calls = {}
    for call in model.calls:
        calls.setdefault(call["input"].splitlines()[0].split(":", 1)[1].strip(), []).append(call)
    [multi_title] = [title for title in calls if title.endswith("in 40 wells")]
    [multi_call] = calls[multi_title]
    assert "Ссылки находки:\n- " in multi_call["input"] and multi_call["input"].count("\n- ") >= 2
    assert len(calls["Equinor tests a drilling robot offshore Norway"]) == 2
    assert "Прошлый ответ: шаги не сошлись" in calls["Equinor tests a drilling robot offshore Norway"][1]["input"]
    assert _sections(admin)[multi_title].startswith("«Отбор» → Разные события в одной находке")
    assert "Equinor tests a drilling robot offshore Norway" in showcase(user, "technology")


def test_no_cap_on_findings_of_a_topic_every_one_reaches_otbor(user, contour, monkeypatch):
    monkeypatch.setattr(signal_discovery.app_config, "SIGNAL_RESEARCH_EVENTS_PER_TOPIC", 1)
    first, second = "https://slb.example/news/rig", "https://halliburton.example/news/frac"
    run_radar(monkeypatch, [event("SLB opens a rig automation centre in Oman", first),
                            event("Halliburton completes its first electric frac fleet job", second)],
              {first: page("SLB opens a rig automation centre in Oman"),
               second: page("Halliburton completes its first electric frac fleet job")})

    model = decide(monkeypatch, {"SLB opens a rig automation centre in Oman": answer(category="technology"),
                                 "Halliburton completes its first electric frac fleet job": answer(
                                     category="technology")})

    # Потолок числа событий в задании поиска остался, но то, что поиск принёс сверх него, не отрезается.
    assert len(model.calls) == 2


def test_finding_found_again_is_not_decided_twice(user, admin, contour, monkeypatch):
    run_radar(monkeypatch, [event(CONTRACT_TITLE, CONTRACT_URL)], {CONTRACT_URL: page(CONTRACT_TITLE)})
    decide(monkeypatch, {CONTRACT_TITLE: answer()})

    run_radar(monkeypatch, [event(CONTRACT_TITLE, CONTRACT_URL)], {CONTRACT_URL: page(CONTRACT_TITLE)})
    again = decide(monkeypatch, {CONTRACT_TITLE: answer()})

    assert again.calls == []
    assert [card["otbor_decision"] for card in showcase(user, "business").values()] == ["accept"]


def test_finding_waiting_for_otbor_is_shown_nowhere_and_gets_no_card(user, admin, contour, monkeypatch):
    run_radar(monkeypatch, [event(CONTRACT_TITLE, CONTRACT_URL)], {CONTRACT_URL: page(CONTRACT_TITLE)})

    assert showcase(user, "technology") == {} and showcase(user, "business") == {}
    assert _sections(admin) == {}
    assert signal_card_store.new_signal_ids(days=7, limit=10) == []


def test_before_deploy_sql_adds_the_columns_to_the_old_schema_and_repeats_safely(isolated_db):
    script = Path(__file__).resolve().parents[1] / "scripts" / "signals" / "otbor-radar-before-deploy.sql"
    with repository.get_connection() as conn:
        conn.execute("ALTER TABLE signals DROP COLUMN selector")
        conn.execute("ALTER TABLE colleague_materials DROP COLUMN notified_outcome, DROP COLUMN notified_at")
        conn.execute("ALTER TABLE signal_feedback_events DROP COLUMN author")
        conn.commit()

    for _ in range(2):  # повтор безопасен
        with repository.get_connection() as conn:
            conn.commit()  # SET search_path открыл транзакцию — у скрипта своя
            conn.autocommit = True
            conn.execute(script.read_text(encoding="utf-8"))

    with repository.get_connection() as conn:
        columns = {(row[0], row[1]) for row in conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = current_schema()")}
        indexes = {row[0] for row in conn.execute("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")}
    assert {("signals", "selector"), ("colleague_materials", "notified_outcome"), ("colleague_materials", "notified_at"),
            ("signal_feedback_events", "author")} <= columns
    assert "idx_signals_otbor_waiting" in indexes
