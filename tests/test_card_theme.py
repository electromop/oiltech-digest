"""Основной вызов «Карточки» в конвейере «Отбора» (#132, спека #128), шов 1: статья сбора → «Отбор» → суть → вызов
«Карточки» на подставной модели через настоящий клиент воркера NL → ядро → API витрин (tests/card_pipeline_support.py).
Тема — по содержанию, одна из 13; связь с нефтесервисом ставит только «Отбор»; дата события хранится с точностью; дата
из текста старше 180 дней возвращает публикацию в «Отбор» один раз.
"""

from datetime import datetime, timezone

from oiltech_digest import cli
from oiltech_digest.db import marks, repository
from tests.card_pipeline_support import (DRILLING, MARKET, THEMES, accepted, answer, card_answer, fill_cards,
                                         pipeline)  # noqa: F401 — фикстура pipeline
from tests.otbor_support import JUSTIFICATION, reject, showcase
from tests.otbor_support import contour  # noqa: F401 — фикстура
from tests.signal_card_support import admin, api_get, feed_signal, run_worker, user  # noqa: F401 — фикстуры

CONTRACT = "Контракт Weatherford в Омане"
CONTRACT_TEXT = ("Weatherford получила контракт на бурение 274 скважин в Омане. Контракт подписан 24 сентября "
                 "2026 года, бурение начнётся в 2027 году.")


def test_card_gets_the_summary_and_category_but_nothing_of_otbor_and_sets_the_theme(user, pipeline, monkeypatch):
    accepted(monkeypatch, {CONTRACT: answer(industry="oilfield_services")}, {CONTRACT: CONTRACT_TEXT})
    summary = showcase(user)[CONTRACT]["summary"]
    assert summary

    model = fill_cards(monkeypatch, {CONTRACT: card_answer(theme=MARKET)})

    [call] = model.calls
    header = call["input"].split("Текст публикации:", 1)[0]
    # На входе суть и категория; решения, причины и обоснования «Отбора», его отрасли и связи — нет.
    assert f"Суть: {summary}" in header and "Категория: бизнес" in header
    assert JUSTIFICATION not in call["input"] and "accept" not in call["input"]
    for word in ("Отрасль", "Связь", "нефтесервис:"):
        assert word not in header
    # Три темы-кандидата по ключам (совпадений больше у бурения) и имена всех 13 тематик в инструкции.
    assert f"Темы-кандидаты по ключам: {DRILLING}; {MARKET}" in header
    assert all(name in call["instructions"] for name in THEMES)
    # Связи с нефтесервисом и применения в инструкции и схеме больше нет: их ставит «Отбор».
    properties = call["schema"]["schema"]["properties"]
    assert "theme" in properties and set(THEMES) <= set(properties["theme"]["enum"])
    assert not {"oilfield_relevance", "oilfield_application"} & set(properties)
    assert "oilfield_relevance" not in call["instructions"] and "связь с нефтесервисом" not in call["instructions"].lower()
    card = showcase(user)[CONTRACT]
    assert (card["theme"], card["card_set_by"]["theme"]) == (MARKET, "agent")
    # Связь и применение — по-прежнему от «Отбора».
    assert (card["oilfield_relevance"], card["card_set_by"]["oilfield_relevance"]) == ("direct", "otbor")


def test_theme_is_set_only_after_otbor_and_only_from_the_list(user, pipeline, monkeypatch):
    accepted(monkeypatch, {"Цифровой двойник бурения": answer(category="technology")},
             {"Цифровой двойник бурения": "Цифровой двойник ведёт бурение скважин на шельфе."})
    # Сигнал прежней цепочки (гейт ленты): его тему ставил тег статьи, «Карточка» её не меняет.
    feed_signal("Контракт до «Отбора»", "SLB подписала контракт на бурение.")
    before = showcase(user)["Контракт до «Отбора»"]["theme"]

    fill_cards(monkeypatch, {"Цифровой двойник бурения": card_answer(theme="Космос"),
                             "Контракт до «Отбора»": card_answer(theme=MARKET)})

    twin = showcase(user, "technology")["Цифровой двойник бурения"]
    # Тема не из списка — не тема: остаётся прежняя, поле не помечено как заполненное агентом.
    assert twin["theme"] != "Космос" and "theme" not in twin["card_set_by"]
    legacy = showcase(user)["Контракт до «Отбора»"]
    assert legacy["theme"] == before and "theme" not in (legacy["card_set_by"] or {})


def test_event_date_keeps_its_precision_without_a_substituted_first_day(user, pipeline, monkeypatch):
    texts = {"Сентябрьский контракт": "Контракт на бурение подписан в сентябре 2026 года.",
             "Контракт года": "В 2026 году подписан контракт на бурение.",
             "Контракт дня": "Контракт на бурение подписан 24 сентября 2026 года."}
    accepted(monkeypatch, {title: answer() for title in texts}, texts)

    fill_cards(monkeypatch, {"Сентябрьский контракт": card_answer(event_date="2026-09"),
                             "Контракт года": card_answer(event_date="2026"),
                             "Контракт дня": card_answer(event_date="2026-09-24")})

    cards = showcase(user)
    assert {title: (cards[title]["event_date"], cards[title]["event_date_precision"]) for title in texts} == {
        "Сентябрьский контракт": ("2026-09-01", "month"), "Контракт года": ("2026-01-01", "year"),
        "Контракт дня": ("2026-09-24", "day")}
    assert {cards[title]["event_date_source"] for title in texts} == {"из текста"}


OLD = "Контракт Halliburton в Кувейте"
OLD_TEXT = "Halliburton подписала контракт на бурение 10 марта 2026 года, сообщила компания в октябре."


def test_old_event_date_from_the_text_returns_the_publication_to_otbor_once(user, admin, pipeline, monkeypatch):
    ids, otbor_model = accepted(monkeypatch, {OLD: [answer(), answer()]}, {OLD: OLD_TEXT})

    fill_cards(monkeypatch, {OLD: card_answer(event_date="2026-03-10", theme=MARKET)})

    # Публикация 20.10, событие 10.03: 224 дня. «Отбор» получил её снова — с этим фактом, и снова принял: «спорное».
    first, again = otbor_model.calls
    assert "Факт «Карточки»" not in first["input"]
    assert "Факт «Карточки»: дата события в тексте 10.03.2026, событию 224 дн. к дате публикации 20.10.2026" in (
        again["input"])
    card = showcase(user)[OLD]
    assert card["otbor_decision"] == "accept" and card["otbor_returns"] == 1
    assert "«Карточка» нашла в тексте дату события старше 180 дней" in card["otbor_disputed"]
    steps = [(step["stage"], step["text"]) for step in card["decision_log"]]
    assert any(stage == "«Карточка»" and "возврат в «Отбор»" in text for stage, text in steps)
    # Второго возврата нет: повторное заполнение карточки по номеру ничего не возвращает.
    cli.main(["enqueue-signal-cards", "--ids", str(card["id"])])
    run_worker("external-ai")
    assert len(otbor_model.calls) == 2
    assert ids[OLD] == card["origin_article_id"]


def test_otbor_may_reject_the_returned_publication_as_an_old_event(user, admin, pipeline, monkeypatch):
    accepted(monkeypatch, {OLD: [answer(), reject("old_event", oilfield_relevance="direct",
                                                  justification="Событие марта")]}, {OLD: OLD_TEXT})

    fill_cards(monkeypatch, {OLD: card_answer(event_date="2026-03-10")})

    assert OLD not in showcase(user)
    rejected = {card["title"]: card["section_reason"] for card in api_get("/api/signals", admin, section="all")}
    assert rejected[OLD].startswith("«Отбор» → Старое событие")


def test_no_return_without_an_event_date_in_the_text_or_when_its_month_may_be_recent(user, pipeline, monkeypatch):
    texts = {"Апрельский контракт": "Контракт на бурение подписан в апреле 2026 года.",
             "Дата не названа": "Компания подписала контракт на бурение.",
             "Старая публикация": "Оператор заключил контракт на бурение скважин."}
    ids, otbor_model = accepted(monkeypatch, {title: answer() for title in texts}, texts)
    with repository.get_connection() as conn:
        conn.execute("UPDATE articles SET published_at = %s WHERE id = %s",
                     (datetime(2025, 12, 1, tzinfo=timezone.utc), ids["Старая публикация"]))
        conn.commit()
    decisions = len(otbor_model.calls)

    fill_cards(monkeypatch, {
        # Апрель 2026: последний день периода 30.04 — 173 дня к 20.10; первое число месяца не подставляется.
        "Апрельский контракт": card_answer(event_date="2026-04"),
        "Дата не названа": card_answer(event_date="", event_date_source="не определено"),
        # Дата публикации вместо даты события не подставляется ни для «Свежести», ни для возврата.
        "Старая публикация": card_answer(event_date="", event_date_source="по дате публикации"),
    })

    assert len(otbor_model.calls) == decisions
    cards = showcase(user)
    assert {title: cards[title]["otbor_returns"] for title in texts} == {title: 0 for title in texts}
    assert all(cards[title]["otbor_decision"] == "accept" for title in texts)


def test_digest_choice_is_never_returned_to_otbor(user, pipeline, monkeypatch):
    ids, _ = accepted(monkeypatch, {OLD: answer()}, {OLD: OLD_TEXT})
    marks.set_article_mark(ids[OLD], status="digest")

    fill_cards(monkeypatch, {OLD: card_answer(event_date="2026-03-10")})

    card = showcase(user)[OLD]
    assert (card["otbor_decision"], card["otbor_returns"]) == ("accept", 0)
