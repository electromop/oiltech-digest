"""Агент «Карточка сигнала» (#96, спека #93), шов 1 — применение ответа и API списка сигналов на
изолированной базе. Ядро проверяет ответ модели кодом и пишет карточку; путь «задача → воркер →
применение → карточка в API» — целиком, модель подставная (tests/signal_card_support.py).
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from oiltech_digest import cli, external_worker, signal_card, signal_card_pack
from oiltech_digest.db import connection, repository
from tests.signal_card_support import (CARD_KEYS, DRILLING, MODEL, FakeModel, answer, api_get, business, feed_signal,
                                       fill, radar_finding, run_worker, worker)
from tests.signal_card_support import admin, contour, user  # noqa: F401 — фикстуры


def test_accepted_article_gets_a_card_through_the_worker(user, contour, monkeypatch):
    text = "SLB подписала контракт с Saudi Aramco 24 сентября 2026 года на бурение 40 скважин."
    feed_signal("Контракт SLB", text)
    model = FakeModel({"Контракт SLB": answer()})
    monkeypatch.setattr(signal_card, "make_client", lambda: model)

    cli.main(["enqueue-signal-cards"])
    jobs = run_worker("external-ai")

    assert [job["kind"] for job in jobs] == ["signal_card"]
    card = business(user)["Контракт SLB"]
    assert card["event_date"] == "2026-09-24"
    assert card["event_date_source"] == "из текста"
    assert card["participants_json"] == [{"name": "SLB", "quote": "SLB подписала контракт", "verified": True}]
    assert (card["event_type"], card["region"], card["technology_class"], card["technology_maturity"]) == (
        "контракт", "Ближний Восток", "неприменимо", "неприменимо")
    assert card["oilfield_relevance"] == "direct"
    assert card["oilfield_application"] == "Бурение скважин на шельфе"
    assert card["why_interesting"] == "Крупный контракт на буровые услуги"
    assert card["card_set_by"] == {key: "agent" for key in CARD_KEYS}
    recipe = card["card_recipe"]
    assert recipe["model"] == f"{MODEL}-2026-03-17"
    assert recipe["dictionaries_version"] == signal_card_pack.load_dictionaries()["version"]
    assert recipe["prompt_version"] == signal_card.PROMPT_VERSION
    assert recipe["filled_at"]


def test_value_outside_the_dictionaries_becomes_undefined(user, contour, monkeypatch):
    feed_signal("Форум", "На форуме в Тюмени обсуждали бурение.")
    feed_signal("Контракт SLB", "SLB подписала контракт с Saudi Aramco.")
    fill(monkeypatch, {
        "Форум": answer(event_type="форум", region="Марс", technology_class="блокчейн", maturity="TRL 7",
                        oilfield_relevance="косвенная", event_date_source="из головы"),
        # Регистр — не чужое значение; фраза длиннее 400 знаков — обрезана, как у судьи радара.
        "Контракт SLB": answer(event_type="Контракт", region="ближний восток", why_important="Важно. " * 200),
    })

    cards = business(user)
    forum = cards["Форум"]
    assert (forum["event_type"], forum["region"], forum["technology_class"], forum["technology_maturity"]) == (
        "не определено", "не определено", "не определено", "не определено")
    assert forum["oilfield_relevance"] is None
    assert forum["oilfield_application"] is None  # применение — только при прямой или переносимой связи
    assert (forum["event_date"], forum["event_date_source"]) == (None, "не определено")
    assert forum["card_set_by"]["oilfield_relevance"] == "agent"
    assert (cards["Контракт SLB"]["event_type"], cards["Контракт SLB"]["region"]) == ("контракт", "Ближний Восток")
    assert len(cards["Контракт SLB"]["why_interesting"]) == 400


def test_participant_needs_a_quote_from_the_text_publisher_is_dropped_aliases_collapse(user, contour, monkeypatch):
    text = ("Schlumberger и Saudi Aramco подписали\nконтракт на бурение 40 скважин. Об этом сообщает "
            "Нефтегаз со ссылкой на пресс-службу. Соглашение с Роснефтью подписано отдельно.")
    feed_signal("Контракт SLB", text, source="Нефтегаз")
    fill(monkeypatch, {"Контракт SLB": answer(companies=[
        {"name": "Schlumberger", "quote": "Schlumberger и Saudi Aramco подписали контракт"},
        {"name": "SLB", "quote": "Schlumberger и Saudi Aramco"},  # та же компания под другим именем
        {"name": "Aramco", "quote": "Saudi Aramco подписали контракт на бурение"},
        {"name": "Halliburton", "quote": "Halliburton выиграла тендер"},  # цитаты в тексте нет
        {"name": "Нефтегаз", "quote": "Об этом сообщает Нефтегаз"},  # издатель публикации
        {"name": "Halliburton", "quote": "подписали контракт на бурение"},  # цитата есть, компании в ней нет
        {"name": "Роснефть", "quote": "Соглашение с Роснефтью подписано"},  # названа в другом падеже
    ])})

    participants = business(user)["Контракт SLB"]["participants_json"]
    assert participants == [
        {"name": "SLB", "quote": "Schlumberger и Saudi Aramco подписали контракт", "verified": True},
        {"name": "Saudi Aramco", "quote": "Saudi Aramco подписали контракт на бурение", "verified": True},
        {"name": "Роснефть", "quote": "Соглашение с Роснефтью подписано", "verified": True},
    ]


def test_company_that_publishes_its_own_news_stays_a_participant(user, contour, monkeypatch):
    # Пресс-релиз компании: издатель — она сама и главный участник. Не участник — издатель-СМИ.
    feed_signal("Пресс-релиз Halliburton", "Halliburton и ADNOC подписали контракт на бурение.",
                source="Halliburton", source_type="Company")
    fill(monkeypatch, {"Пресс-релиз Halliburton": answer(companies=[
        {"name": "Halliburton", "quote": "Halliburton и ADNOC подписали контракт"},
        {"name": "ADNOC", "quote": "ADNOC подписали контракт на бурение"},
    ])})

    participants = business(user)["Пресс-релиз Halliburton"]["participants_json"]
    assert [item["name"] for item in participants] == ["Halliburton", "ADNOC"]


def test_event_date_after_publication_or_before_2000_is_undefined(user, contour, monkeypatch):
    for title in ("После публикации", "Через день", "До 2000 года", "Не дата", "Даты в тексте нет"):
        feed_signal(title, "Компания запустила новый буровой станок.",
                    published=datetime(2026, 9, 25, 10, tzinfo=timezone.utc))
    fill(monkeypatch, {
        # Дата публикации — день по UTC; сутки допуска — на часовой пояс события, не больше.
        "После публикации": answer(event_date="2026-09-27"),
        "Через день": answer(event_date="2026-09-26"),
        "До 2000 года": answer(event_date="1999-12-31"),
        "Не дата": answer(event_date="24.09.2026"),
        "Даты в тексте нет": answer(event_date="", event_date_source="по дате публикации"),
    })

    cards = business(user)
    for title in ("После публикации", "До 2000 года", "Не дата"):
        assert (cards[title]["event_date"], cards[title]["event_date_source"]) == (None, "не определено"), title
    assert (cards["Через день"]["event_date"], cards["Через день"]["event_date_source"]) == ("2026-09-26", "из текста")
    assert (cards["Даты в тексте нет"]["event_date"], cards["Даты в тексте нет"]["event_date_source"]) == (
        "2026-09-25", "по дате публикации")


def test_agent_does_not_overwrite_values_set_by_a_human(user, contour, monkeypatch):
    signal_id = None
    feed_signal("Контракт SLB", "SLB подписала контракт с Saudi Aramco 24 сентября 2026 года.")
    # Правку человека внесёт сервис ОС; здесь — её след в карточке.
    with repository.get_connection() as conn:
        signal_id = conn.execute("SELECT id FROM signals WHERE title = 'Контракт SLB'").fetchone()[0]
        conn.execute("UPDATE signals SET event_type = 'сделка (M&A)', participants_json = '[]', "
                     "card_set_by = '{\"event_type\": \"human\", \"participants\": \"human\"}' WHERE id = %s",
                     (signal_id,))
        conn.commit()

    fill(monkeypatch, {"Контракт SLB": answer()})

    card = business(user)["Контракт SLB"]
    assert card["id"] == signal_id
    assert (card["event_type"], card["participants_json"]) == ("сделка (M&A)", [])
    assert card["card_set_by"] == {**{key: "agent" for key in CARD_KEYS}, "event_type": "human", "participants": "human"}
    assert card["region"] == "Ближний Восток"
    assert card["card_recipe"]["prompt_version"] == signal_card.PROMPT_VERSION


def test_radar_finding_gets_only_empty_fields_and_keeps_the_judge_values(user, contour, monkeypatch):
    text = "Equinor и SLB испытали робота-бурильщика на платформе в Северном море."
    radar_finding("robot", "Робот-бурильщик на платформе", text, event_date="2026-09-01",
                  oilfield_relevance="transferable", oilfield_application="Роботизация буровой площадки")
    fill(monkeypatch, {"Робот-бурильщик на платформе": answer(
        event_date="2026-09-10", oilfield_relevance="прямая", oilfield_application="Бурение",
        why_important="Первый промышленный пилот", event_type="пилот", region="Европа",
        technology_class="робототехника", maturity="пилот/ОПИ",
        companies=[{"name": "Equinor", "quote": "Equinor и SLB испытали робота-бурильщика"}])})

    card = api_get("/api/signals", user)[0]
    assert (card["event_date"], card["oilfield_relevance"], card["oilfield_application"]) == (
        "2026-09-01", "transferable", "Роботизация буровой площадки")
    assert card["event_date_source"] is None  # откуда дата у судьи — неизвестно
    assert card["why_interesting"] == "Первый промышленный пилот"  # у судьи было пусто
    assert (card["event_type"], card["region"], card["technology_class"], card["technology_maturity"]) == (
        "пилот", "Европа", "робототехника", "пилот/ОПИ")
    assert [item["name"] for item in card["participants_json"]] == ["Equinor"]
    judge = ("event_date", "oilfield_relevance", "oilfield_application")
    assert card["card_set_by"] == {key: ("radar_judge" if key in judge else "agent") for key in CARD_KEYS}


# Атрибуты карточки и время правки — то, что агент меняет у находки радара по замыслу.
_CARD_FIELDS = {"event_date", "event_date_source", "participants_json", "event_type", "region", "technology_class",
                "technology_maturity", "oilfield_relevance", "oilfield_application", "why_interesting",
                "card_set_by", "card_recipe", "updated_at", "user_status_updated_at"}


def _radar_views(user, admin):
    """Всё, что радар показывает: список, сводка, «Отсеянные и скрытые», итоги недели — без атрибутов карточки."""
    def strip(rows):
        return [{key: value for key, value in row.items() if key not in _CARD_FIELDS} for row in rows]
    weekly = api_get("/api/signals/weekly", admin)
    weekly["top"] = strip(weekly.get("top") or [])
    return {
        "list": strip(api_get("/api/signals", user)),
        "summary": api_get("/api/signals/summary", user),
        "hidden": strip(api_get("/api/signals", admin, hidden=True)),
        "hidden_summary": api_get("/api/signals/summary", admin, hidden=True),
        "weekly": weekly,
    }


def test_agent_values_do_not_change_what_the_radar_shows(user, admin, contour, monkeypatch):
    # Карточка до 02.10: связь с нефтесервисом и дату события судья тогда не ставил.
    radar_finding("early", "Ранняя находка", "Робот обслуживает буровую.")
    radar_finding("fresh", "Свежая находка", "Датчик утечек на трубопроводе.", event_date="2026-09-01",
                  oilfield_relevance="direct")
    radar_finding("filtered", "Отсеянная находка", "Обзор рынка роботов.", filter_stage="judge",
                  filter_reason="нет технологии")
    before = _radar_views(user, admin)
    assert len(before["list"]) == 2 and len(before["hidden"]) == 1

    # Агент считает их «не про нефтесервис» со старым событием: решение судьи так спрятало бы карточку.
    unrelated = answer(oilfield_relevance="нет связи", event_date="2025-01-10", oilfield_application="")
    fill(monkeypatch, {title: unrelated for title in ("Ранняя находка", "Свежая находка", "Отсеянная находка")})

    assert _radar_views(user, admin) == before
    early = next(card for card in api_get("/api/signals", user) if card["title"] == "Ранняя находка")
    assert (early["oilfield_relevance"], early["event_date"]) == ("none", "2025-01-10")


def test_agent_spend_is_accounted_under_its_name(user, admin, contour, monkeypatch):
    feed_signal("Контракт SLB", "SLB подписала контракт с Saudi Aramco.")
    feed_signal("Пилот робота", "Equinor испытывает робота на платформе.")
    fill(monkeypatch, {"Контракт SLB": answer(), "Пилот робота": answer(companies=[])})

    report = {row["stage"]: row for row in api_get("/api/reports/ai-cost", admin)}
    assert report["signal_card"]["runs"] == 2
    # gpt-5.4-nano: 3 000 входных × $0,20 + 600 выходных × $1,25 за 1 млн = $0,00135 на сигнал.
    assert float(report["signal_card"]["cost_usd"]) == pytest.approx(0.0027)
    monthly = {row["area"]: (row["model"], row["runs"]) for row in api_get("/api/stats/monthly", admin, months=1)["ai_cost"]}
    # Своя строка — не в расходе ленты (её здесь дала подготовка статей: модель «m»).
    assert monthly == {"signal_card": (f"{MODEL}-2026-03-17", 2), "feed": ("m", 8)}


def _issue_and_run(answers, monkeypatch, between):
    """Выдать задачу воркеру, между выдачей и итогом сделать `between`, довести задачу до итога."""
    monkeypatch.setattr(signal_card, "make_client", lambda: FakeModel(answers))
    cli.main(["enqueue-signal-cards"])
    client = worker("external-ai")
    job = client.claim()
    between()
    external_worker._run_job(client, job, lambda done=None: None)
    return job


def test_card_is_not_written_when_dictionaries_changed_after_the_job_was_issued(user, admin, contour, monkeypatch):
    feed_signal("Контракт SLB", "SLB подписала контракт с Saudi Aramco.")
    approved = {**signal_card_pack.load_dictionaries(), "version": "2026-11-01.1"}

    job = _issue_and_run({"Контракт SLB": answer()}, monkeypatch,
                         lambda: monkeypatch.setattr(signal_card, "load_dictionaries", lambda: approved))

    assert business(user)["Контракт SLB"]["card_recipe"] is None
    [error] = api_get(f"/api/jobs/{job['id']}", admin)["result"]["applied"]["errors"]
    assert "справочники" in error["error"]


def test_card_is_not_written_when_the_main_publication_changed_after_the_job_was_issued(user, admin, contour,
                                                                                       monkeypatch):
    signal_id = radar_finding("robot", "Робот-бурильщик", "Equinor и SLB испытали робота-бурильщика.")

    def stronger_publication():
        repository.upsert_signal_evidence(signal_id, {
            "source_url": "https://radar.example/robot-press", "title": "Робот-бурильщик: пресс-релиз",
            "extracted_fact": "Пресс-релиз Equinor о роботе.", "strength": 5,
            "raw_payload": {"evidence_source": "brave", "full_text_fetched": "true"},
        })

    job = _issue_and_run({"Робот-бурильщик": answer()}, monkeypatch, stronger_publication)

    assert api_get("/api/signals", user)[0]["card_recipe"] is None
    [error] = api_get(f"/api/jobs/{job['id']}", admin)["result"]["applied"]["errors"]
    assert (error["signal_id"], "публикация" in error["error"]) == (signal_id, True)


def test_judge_finding_again_takes_back_the_field_the_agent_filled(user, contour, monkeypatch):
    text = "Equinor и SLB испытали робота-бурильщика на платформе."
    radar_finding("robot", "Робот-бурильщик", text)  # дату события судья не назвал
    fill(monkeypatch, {"Робот-бурильщик": answer(event_date="2026-09-10", companies=[])})

    radar_finding("robot", "Робот-бурильщик", text, event_date="2026-09-05")  # повторная находка

    card = api_get("/api/signals", user)[0]
    assert (card["event_date"], card["event_date_source"]) == ("2026-09-05", None)
    assert card["card_set_by"]["event_date"] == "radar_judge"


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "signals" / "signal-card-before-deploy.sql"
NEW_COLUMNS = {"event_type", "region", "technology_class", "technology_maturity", "participants_json",
               "event_date_source", "card_set_by", "card_recipe"}


def _columns(table):
    with connection.get_connection() as conn:
        return {row[0] for row in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = %s", (table,))}


def test_before_deploy_sql_adds_the_card_to_the_old_schema_and_repeats_safely(isolated_db):
    with connection.get_connection() as conn:
        for column in NEW_COLUMNS:
            conn.execute(f"ALTER TABLE signals DROP COLUMN {column}")
        conn.execute("ALTER TABLE ai_processing_runs DROP COLUMN signal_id")
        conn.execute("INSERT INTO signals (signal_key, title, theme, event_date, oilfield_relevance) "
                     "VALUES ('radar', 'Находка', 'Бурение', '2026-09-01', 'direct')")
        conn.execute("INSERT INTO signals (signal_key, title, theme, origin) VALUES ('feed:1', 'Статья', 'Бурение', 'feed')")
        conn.commit()

    for _ in range(2):  # повтор безопасен
        with connection.get_connection() as conn:
            conn.commit()  # SET search_path открыл транзакцию — у скрипта своя
            conn.autocommit = True
            conn.execute(SCRIPT.read_text(encoding="utf-8"))

    assert NEW_COLUMNS <= _columns("signals") and "signal_id" in _columns("ai_processing_runs")
    with connection.get_connection() as conn:
        rows = conn.execute("SELECT signal_key, card_set_by FROM signals ORDER BY signal_key").fetchall()
    # Значения судьи у прежней находки — его; у сигнала ленты карточки ещё нет.
    assert rows == [("feed:1", None), ("radar", {"event_date": "radar_judge", "oilfield_relevance": "radar_judge"})]


def test_judge_finding_again_does_not_overwrite_a_human_correction(user, contour, monkeypatch):
    text = "Equinor и SLB испытали робота-бурильщика на платформе."
    signal_id = radar_finding("robot", "Робот-бурильщик", text, event_date="2026-09-01")
    # Правку человека внесёт сервис ОС; здесь — её след в карточке.
    with connection.get_connection() as conn:
        conn.execute("UPDATE signals SET event_date = '2026-08-30', card_set_by = card_set_by || "
                     "'{\"event_date\": \"human\"}' WHERE id = %s", (signal_id,))
        conn.commit()

    radar_finding("robot", "Робот-бурильщик", text, event_date="2026-09-05", oilfield_relevance="direct")

    card = api_get("/api/signals", user)[0]
    assert (card["event_date"], card["card_set_by"]["event_date"]) == ("2026-08-30", "human")
    assert (card["oilfield_relevance"], card["card_set_by"]["oilfield_relevance"]) == ("direct", "radar_judge")


def test_chatgpt_finding_link_date_is_not_a_publication_date(user, contour, monkeypatch):
    # У находки «режима ChatGPT» дата ссылки — дата события по версии модели (и после докачки страницы
    # без даты): модель не должна получить её как дату публикации, а «по дате публикации» — опереться на неё.
    signal_id = repository.upsert_signal({"signal_key": "chatgpt", "title": "Робот на буровой", "theme": DRILLING,
                                          "maturity": "shortlist", "score": 70, "signal_category": "technology"})
    repository.upsert_signal_evidence(signal_id, {
        "source_url": "https://radar.example/chatgpt", "title": "Робот на буровой",
        "published_at": "2026-09-20T00:00:00+00:00", "extracted_fact": "Equinor испытала робота на буровой.",
        "raw_payload": {"evidence_source": "openai_web_search", "full_text_fetched": "true"},
    })
    model = fill(monkeypatch, {"Робот на буровой": answer(event_date="", event_date_source="по дате публикации")})

    assert "Дата публикации: не указана" in model.calls[0]["input"]
    card = api_get("/api/signals", user)[0]
    assert (card["event_date"], card["event_date_source"]) == (None, "не определено")
