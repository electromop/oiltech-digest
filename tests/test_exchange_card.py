"""Карточка в пакете для потребителя такая же, как на экране витрины (#198, спека #197).

Одна функция чтения на экран и пакет (showcase_cards): заголовок и суть со всеми применёнными правками, в том числе
поданными после закрытия месяца (PDF закрытого месяца берёт только правки до закрытия), атрибуты «Карточки», «почему
важно» с автором из отметки «кто поставил», гипотеза человека или агента, публикации свои и склеенных дублей в порядке
экрана. Баллов, «Решения платформы» и причины отказа «Отбора» в пакете нет."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from oiltech_digest import config, exchange_card, exchange_packages
from oiltech_digest.db import connection
from tests.digest_user_path_support import SEPT, add_signal, msk, set_card
from tests.digest_user_path_support import issue  # noqa: F401: фикстура выпуска
from tests.exchange_consumer import CARD_FIELDS
from tests.exchange_support import exchange  # noqa: F401: фикстура обмена
from tests.exchange_support import freeze


def _correct(person, signal_id: int, title: str, thesis: str, *, at: datetime) -> None:
    """Правка заголовка и сути администратором с формы ОС, поданная в момент at."""
    with connection.get_connection() as conn:
        before = conn.execute("SELECT COALESCE(max(id), 0) FROM signal_feedback_events").fetchone()[0]
    answer = person.post("/api/signals/feedback", json={
        "signal_id": signal_id, "corrected_title": title, "corrected_thesis": thesis})
    assert answer.status_code == 200, answer.text
    with connection.get_connection() as conn:
        conn.execute("UPDATE signal_feedback_events SET created_at = %s WHERE id > %s", (at, before))
        conn.commit()


def _screen_card(person, signal_id: int) -> dict:
    """Карточка, как её получает экран технологического радара (/api/signals)."""
    answer = person.get("/api/signals", params={"limit": 200, "evidence_limit": 20})
    assert answer.status_code == 200, answer.text
    [card] = [row for row in answer.json() if row["id"] == signal_id]
    return card


def test_package_card_is_the_showcase_card_with_corrections_after_the_close(exchange, monkeypatch, hypothesis_on):
    sep = exchange.s["sep"]
    duplicate = add_signal("dup", first_seen=msk(2026, 9, 13, 9, 0))
    with connection.get_connection() as conn:
        conn.execute("UPDATE signals SET merged_into_signal_id = %s WHERE id = %s", (sep, duplicate))
        conn.execute("UPDATE signal_evidence SET strength = 0.9, title_ru = 'Публикация дубля' WHERE signal_id = %s",
                     (duplicate,))
        conn.commit()
    set_card(sep, why_interesting="Сокращает простои при бурении.", hypothesis="Оценить датчики в колонне.",
             hypothesis_author="admin@example.test", event_type="испытание", region="Европа",
             technology_class="датчики", technology_maturity="испытание", oilfield_relevance="direct",
             event_date=date(2026, 9, 10), event_date_precision="day",
             participants_json=[{"name": "Пример Технологии", "country": "Россия"}, {"name": ""}],
             card_set_by={"why_interesting": "agent", "hypothesis": "human"}, reject_reason="old_event")
    exchange.analyst.mark_signal(sep)
    freeze(monkeypatch, exchange.after_close)
    _correct(exchange.admin, sep, "Правка после закрытия", "Суть после закрытия.", at=exchange.after_close)

    exchange_packages.build_release(SEPT, by="test")
    [package] = exchange.take()
    [card] = package["cards"]

    screen = _screen_card(exchange.admin, sep)
    assert {name: value for name, value in card.items() if name != "updated_at"} == exchange_card.card(screen)
    assert (card["title"], card["summary"]) == ("Правка после закрытия", "Суть после закрытия.")
    # PDF закрытого сентября правку после закрытия не берёт.
    [item] = exchange.analyst.issue_items(SEPT)
    assert (item["title"], item["summary"]) == ("Сигнал sep", "Суть сигнала sep.")
    assert card["why_important"] == {"text": "Сокращает простои при бурении.", "set_by": "agent"}
    assert card["hypothesis"] == "Оценить датчики в колонне."
    assert (card["event_type"], card["region"], card["technology_class"], card["maturity"]) == (
        "испытание", "Европа", "датчики", "испытание")
    assert (card["oilfield_relevance"], card["companies"], card["event_date"]) == (
        "прямая", ["Пример Технологии"], "2026-09-10")
    # Публикации свои и склеенного дубля в порядке экрана: у дубля ссылка сильнее, она первая.
    assert [one["url"] for one in card["publications"]] == [one["source_url"] for one in screen["evidence"]] == [
        "https://radar.example.org/dup", "https://radar.example.org/sep"]
    assert card["publications"][0]["title"] == "Публикация дубля"
    assert card["publications"][1]["date"] == "2026-09-11"
    # Ни баллов, ни «Решения платформы», ни причины отказа «Отбора»: только поля формата.
    assert set(card) == CARD_FIELDS
    assert datetime.fromisoformat(card["updated_at"]).tzinfo is not None


def _row(**fields) -> dict:
    return {"id": 7, "title": "Title", "title_ru": "Заголовок", "summary": "Суть.", "theme": "Бурение",
            "card_set_by": {}, "evidence": [], **fields}


def test_why_important_author_hypothesis_and_dates_follow_who_set_them():
    radar = exchange_card.card(_row(why_interesting="Сильнее соседей по пачке.",
                                    card_set_by={"why_interesting": "radar_judge"}))
    assert radar["why_important"] == {"text": "Сильнее соседей по пачке.", "set_by": "radar"}
    legacy = exchange_card.card(_row(why_interesting="Без отметки, до карточки.", hypothesis="Чужая гипотеза"))
    assert legacy["why_important"]["set_by"] == "radar" and legacy["hypothesis"] is None
    human = exchange_card.card(_row(why_interesting="  ", hypothesis="Изучить.", card_set_by={"hypothesis": "agent"}))
    assert human["why_important"] is None and human["hypothesis"] == "Изучить."
    # Дата события (решение владельца 19.11): пусто только у даты, которую понять нельзя совсем; известны месяц или год:
    # уходит первый день периода, как он хранится (в формате нет точности).
    for precision in ("day", "month", "year", None):
        card = exchange_card.card(_row(event_date=date(2026, 9, 1), event_date_precision=precision))
        assert card["event_date"] == "2026-09-01", precision
    unknown = exchange_card.card(_row(event_date=None, event_date_source="не определено",
                                      card_set_by={"event_date": "agent"}))
    assert unknown["event_date"] is None


def test_switched_off_hypothesis_section_sends_no_hypothesis_and_the_screen_shows_none(exchange, monkeypatch):
    """Раздел «Гипотеза для Компании» выключен (по умолчанию, решение владельца 22.11): ни в пакете, ни на экране
    витрины гипотезы нет, хотя её поставил человек; в базе она остаётся."""
    assert config.ISSUE_HYPOTHESIS_ENABLED is False
    sep = exchange.s["sep"]
    set_card(sep, hypothesis="Оценить датчики в колонне.", hypothesis_author="admin@example.test",
             card_set_by={"hypothesis": "human"})
    exchange.analyst.mark_signal(sep)
    freeze(monkeypatch, exchange.after_close)

    exchange_packages.build_release(SEPT, by="test")
    [package] = exchange.take()
    [card] = package["cards"]
    assert card["hypothesis"] is None
    screen = _screen_card(exchange.admin, sep)
    assert (screen["hypothesis"], screen["hypothesis_author"], screen["hypothesis_set_at"]) == (None, None, None)
    assert "hypothesis" not in screen["card_set_by"]


def test_blank_is_null_undefined_stays_and_only_web_links_go():
    card = exchange_card.card(_row(
        title_ru="", summary="", thesis="Тезис.", region="не определено", oilfield_relevance="transferable",
        participants_json={"not": "a list"}, evidence=[
            {"source_url": "https://example.org/a", "title": "A", "title_ru": None, "publisher": "",
             "published_at": datetime(2026, 9, 30, 22, 30, tzinfo=timezone.utc)},
            {"source_url": "file:///etc/passwd", "title": "B"},
            {"source_url": "javascript:alert(1)", "title": "C"},
        ]))
    assert (card["title"], card["summary"], card["region"]) == ("Title", "Тезис.", "не определено")
    assert card["oilfield_relevance"] == "переносимая" and card["companies"] == []
    # 30.09 22:30 UTC: уже 1 октября по Москве.
    assert card["publications"] == [{"title": "A", "source": None, "url": "https://example.org/a",
                                     "date": "2026-10-01"}]


def test_signal_without_a_title_cannot_go_and_fingerprint_ignores_order_of_keys():
    with pytest.raises(exchange_card.CardError, match="у сигнала №7 нет заголовка"):
        exchange_card.card(_row(title=" ", title_ru=None))
    assert exchange_card.fingerprint({"a": 1, "b": "ж"}) == exchange_card.fingerprint({"b": "ж", "a": 1})
    with pytest.raises(ValueError):
        exchange_card.stamped({}, datetime(2026, 10, 6, 9, 0))
