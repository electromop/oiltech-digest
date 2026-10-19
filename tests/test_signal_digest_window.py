"""Отметка «в дайджест» у карточки технологического радара — только в открытом месяце.

Решение владельца 29.09 (вопрос из хендоффа 29.09: «карточка радара, отмеченная после 05.10, —
пускать ли в закрытый сентябрь» — нет). Как статус статьи из архива ленты
(PATCH /api/articles/{id} → 409): у карточки закрытого месяца «В дайджест» и «Убрать» не
работают ни из интерфейса, ни прямым запросом. Окно — то же, что у ленты
(feed_window.current): текущий месяц, а 1–4 числа по Москве ещё и прошлый, пока собирается
его выпуск. Месяц карточки — месяц её поступления на радар по Москве, тем же выражением, по
которому сборщик кладёт её в выпуск (#84): feed_window.signal_month_sql.

Прочие статусы, комментарий и отзыв о карточке окно не трогает. Часы окна замораживаются
подменой feed_window._now.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from oiltech_digest import api, feed_window
from oiltech_digest.db import connection, repository
from tests.digest_user_path_support import Person, add_signal, msk, utc


def _freeze(monkeypatch, moment: datetime) -> None:
    monkeypatch.setattr(feed_window, "_now", lambda: moment)


@pytest.fixture()
def analyst(isolated_db):
    person = Person(int(repository.create_user("analyst@example.test", "long-enough-password", "user")["id"]))
    yield person
    api.app.dependency_overrides.clear()


def _select(person: Person, signal_id: int, selected: bool = True):
    """Кнопка «В дайджест» (selected=True) или «Убрать» на карточке радара."""
    return person.patch(f"/api/signals/{signal_id}", json={"selected_for_digest": selected})


def _status(person: Person, signal_id: int) -> str | None:
    with connection.get_connection() as conn:
        row = conn.execute(
            "SELECT status FROM signal_marks WHERE signal_id = %s",
            (signal_id,),
        ).fetchone()
    return row[0] if row else None


def test_card_of_the_current_month_is_marked_and_unmarked(analyst, monkeypatch):
    _freeze(monkeypatch, msk(2026, 9, 23, 12, 0))
    card = add_signal("sep", first_seen=msk(2026, 9, 10, 0, 20))

    assert _select(analyst, card).status_code == 200
    assert _status(analyst, card) == "digest"
    assert _select(analyst, card, selected=False).status_code == 200
    assert _status(analyst, card) == "watch"


@pytest.mark.parametrize("now", [msk(2026, 10, 1, 0, 0), msk(2026, 10, 4, 23, 59, 59)])
def test_previous_month_card_is_marked_and_unmarked_on_days_1_to_4(analyst, monkeypatch, now):
    """Ради зазора и сделано окно: 1–4 числа выпуск за прошлый месяц ещё собирается."""
    _freeze(monkeypatch, now)
    card = add_signal("sep", first_seen=msk(2026, 9, 20, 0, 15))

    assert _select(analyst, card).status_code == 200
    assert analyst.issue("2026-09") == [("signal", card)]
    assert _select(analyst, card, selected=False).status_code == 200
    assert analyst.issue("2026-09") == []


# 05.10 00:00 по Москве — это 04.10 21:00 UTC: окно сменяется по московским суткам.
@pytest.mark.parametrize("now", [msk(2026, 10, 5, 0, 0), utc(2026, 10, 4, 21, 0)])
def test_from_the_5th_the_previous_month_card_is_refused(analyst, monkeypatch, now):
    _freeze(monkeypatch, now)
    card = add_signal("sep", first_seen=msk(2026, 9, 20, 0, 15))
    # Для всех ролей одинаково, как у статей: исключения для админа нет.
    admin = Person(int(repository.create_user("admin@example.test", "long-enough-password", "admin")["id"]), "admin")

    for person, body in (
        (analyst, {"selected_for_digest": True}),
        (analyst, {"status": "digest"}),
        (admin, {"selected_for_digest": True}),
    ):
        refused = person.patch(f"/api/signals/{card}", json=body)
        assert refused.status_code == 409, (person.role, body)
        assert "архиву за сентябрь 2026" in refused.json()["detail"]
    with connection.get_connection() as conn:
        states = conn.execute("SELECT count(*) FROM signal_marks WHERE signal_id = %s", (card,)).fetchone()[0]
        events = conn.execute("SELECT count(*) FROM signal_feedback_events WHERE signal_id = %s", (card,)).fetchone()[0]
    assert (states, events) == (0, 0), "отказ не оставляет следов: ни отметки, ни события «в дайджест» для обучения"

    # Карточка текущего месяца в это же время отмечается как обычно.
    october = add_signal("oct", first_seen=msk(2026, 10, 2, 0, 15))
    assert _select(analyst, october).status_code == 200


def test_unmarking_in_a_closed_month_is_refused_and_the_card_stays_in_its_issue(analyst, monkeypatch):
    card = add_signal("aug", first_seen=msk(2026, 8, 20, 0, 15))
    _freeze(monkeypatch, msk(2026, 9, 3, 12, 0))  # зазор: август ещё открыт
    assert _select(analyst, card).status_code == 200

    _freeze(monkeypatch, msk(2026, 9, 5, 12, 0))
    # «Убрать» и любой другой статус вывели бы карточку из закрытого августовского выпуска;
    # повторное «В дайджест» — та же отметка в закрытом месяце.
    for body in ({"selected_for_digest": False}, {"status": "watch"}, {"status": "noise"}, {"selected_for_digest": True}):
        refused = analyst.patch(f"/api/signals/{card}", json=body)
        assert refused.status_code == 409, body
        assert "архиву за август 2026" in refused.json()["detail"]
    assert _status(analyst, card) == "digest"
    assert analyst.issue("2026-08") == [("signal", card)]


def test_other_statuses_comment_and_feedback_stay_open_in_a_closed_month(analyst, monkeypatch):
    """Окно — про выпуск: в закрытом месяце нельзя поставить или снять «в дайджест», а разбирать
    карточку (статус мимо выпуска, комментарий, отзыв с «Дублем») можно, как и раньше."""
    chosen = add_signal("chosen", first_seen=msk(2026, 8, 12, 0, 15))
    other = add_signal("other", first_seen=msk(2026, 8, 20, 0, 15))
    _freeze(monkeypatch, msk(2026, 9, 2, 12, 0))
    assert _select(analyst, chosen).status_code == 200
    _freeze(monkeypatch, msk(2026, 9, 23, 12, 0))

    for status in ("noise", "duplicate", "archive", "watch"):
        response = analyst.patch(f"/api/signals/{other}", json={"status": status})
        assert response.status_code == 200, (status, response.text)
        assert _status(analyst, other) == status
    commented = analyst.patch(f"/api/signals/{chosen}", json={"analyst_comment": "Берём в августовский выпуск"})
    assert commented.status_code == 200, commented.text
    assert _status(analyst, chosen) == "digest"
    feedback = analyst.post("/api/signals/feedback", json={
        "signal_id": other, "verdict": "merge_duplicate", "duplicate_of_signal_id": chosen,
    })
    assert feedback.status_code == 200, feedback.text


def test_card_month_is_its_moscow_arrival_day(analyst, monkeypatch):
    """Прогон радара 01.09 в 00:30 МСК — по UTC ещё 31.08. Экран пишет «Поступил: 01.09.2026»,
    сборщик кладёт карточку в сентябрьский выпуск — значит, и отмечать её можно весь сентябрь.
    Карточка 31.08 в 23:30 МСК — августовская, и с 05.09 она закрыта."""
    first_of_september = add_signal("sep-1", first_seen=utc(2026, 8, 31, 21, 30))
    last_of_august = add_signal("aug-31", first_seen=utc(2026, 8, 31, 20, 30))
    _freeze(monkeypatch, msk(2026, 9, 10, 12, 0))

    assert _select(analyst, first_of_september).status_code == 200
    assert analyst.issue("2026-09") == [("signal", first_of_september)]
    refused = _select(analyst, last_of_august)
    assert refused.status_code == 409
    assert "архиву за август 2026" in refused.json()["detail"]


def test_radar_list_tells_the_screen_which_cards_are_closed(analyst, monkeypatch):
    """Экран гасит кнопку по признаку сервера: месяц карточки — тем же выражением, что у
    выпуска, закрыт ли он — тем же окном, что у отказа. Своей формулы у экрана нет."""
    aug = add_signal("aug", first_seen=msk(2026, 8, 20, 0, 15))
    sep = add_signal("sep", first_seen=utc(2026, 8, 31, 21, 30))  # 01.09 по Москве
    oct_ = add_signal("oct", first_seen=msk(2026, 10, 2, 0, 15))

    def listed() -> dict[int, tuple[str, bool]]:
        response = analyst.get("/api/signals", params={"limit": 50})
        assert response.status_code == 200, response.text
        return {row["id"]: (row["digest_month"], row["digest_locked"]) for row in response.json()}

    _freeze(monkeypatch, msk(2026, 10, 4, 23, 59, 59))
    assert listed() == {aug: ("2026-08", True), sep: ("2026-09", False), oct_: ("2026-10", False)}
    _freeze(monkeypatch, msk(2026, 10, 5, 0, 0))
    assert listed() == {aug: ("2026-08", True), sep: ("2026-09", True), oct_: ("2026-10", False)}


def test_unknown_card_is_still_404_and_unknown_status_400(analyst, monkeypatch):
    _freeze(monkeypatch, msk(2026, 9, 23, 12, 0))
    card = add_signal("aug", first_seen=msk(2026, 8, 20, 0, 15))
    assert _select(analyst, 999999).status_code == 404
    assert analyst.patch(f"/api/signals/{card}", json={"status": "делете"}).status_code == 400


def test_radar_mark_is_shared_by_everyone(analyst, monkeypatch):
    """Отметка карточки общая (#111): кнопка, число «В дайджесте», выпуск и отказ в закрытом месяце
    — одни и те же у всех учёток; снять отметку коллеги может любой."""
    from oiltech_digest import signal_feedback

    learned = []
    monkeypatch.setattr(signal_feedback, "learn_from_digest_selection",
                        lambda signal_id, **kwargs: learned.append(kwargs["selected"]) or {})
    colleague = Person(int(repository.create_user("colleague@example.test", "long-enough-password", "user")["id"]))
    _freeze(monkeypatch, msk(2026, 9, 3, 12, 0))  # зазор: август ещё открыт
    card = add_signal("aug", first_seen=msk(2026, 8, 20, 0, 15))
    sep = add_signal("sep", first_seen=msk(2026, 9, 2, 0, 15))

    assert _select(analyst, card).status_code == 200
    by_colleague = {row["id"]: row for row in colleague.get("/api/signals").json()}
    assert by_colleague[card]["selected_for_digest"] is True
    assert colleague.get("/api/signals/summary").json()["in_digest"] == 1
    assert colleague.issue("2026-08") == [("signal", card)]
    # Повторное «В дайджест» коллегой отметку не меняет — второго примера для обучения нет.
    assert _select(colleague, card).status_code == 200
    assert learned == [True]

    # Коллега снимает отметку аналитика — карточка уходит из выпуска у всех, пример гаснет.
    assert _select(analyst, sep).status_code == 200
    assert analyst.issue("2026-09") == [("signal", sep)]
    assert _select(colleague, sep, selected=False).status_code == 200
    assert analyst.issue("2026-09") == []
    assert {row["id"]: row for row in analyst.get("/api/signals").json()}[sep]["selected_for_digest"] is False
    assert learned == [True, True, False]

    _freeze(monkeypatch, msk(2026, 9, 5, 12, 0))
    refused = colleague.patch(f"/api/signals/{card}", json={"selected_for_digest": False})
    assert refused.status_code == 409, "отметка аналитика в закрытом месяце закрыта и для коллеги"
    assert _status(analyst, card) == "digest"
