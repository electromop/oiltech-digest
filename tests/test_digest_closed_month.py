"""Выпуск закрытого месяца — только просмотр, в том числе его текст (решение владельца, задача 30.09).

Окно ленты (feed_window): текущий месяц, а 1–4 числа по Москве ещё и прошлый; с 5-го 00:00 МСК
прошлый месяц закрыт. Отметки «в дайджест» в закрытом месяце уже запрещены (статьи — 23.09,
карточки радара — #85). Здесь — текст закрытого выпуска: его не меняют ни отзывы коллег, поданные
после закрытия, ни повторная находка карточки радаром.

Выпуск строится при каждом открытии заново из живых таблиц, снимка нет: заморозка — правило
чтения и записи, а не копия. Часы окна замораживаются подменой feed_window._now; время события
в базе — её now(), поэтому время отзыва тест ставит сам.
"""

from __future__ import annotations

from datetime import datetime

from oiltech_digest import feed_window
from oiltech_digest.db import connection
from tests.digest_user_path_support import SEPT, Person, msk
from tests.digest_user_path_support import issue  # noqa: F401 — фикстура выпуска


def _freeze(monkeypatch, moment: datetime) -> None:
    monkeypatch.setattr(feed_window, "_now", lambda: moment)


def _correct(person: Person, signal_id: int, title: str, thesis: str, *, at: datetime) -> None:
    """Правка заголовка и сути в обратной связи радара, поданная в момент `at`."""
    with connection.get_connection() as conn:
        before = conn.execute("SELECT COALESCE(max(id), 0) FROM signal_feedback_events").fetchone()[0]
    response = person.post("/api/signals/feedback", json={
        "signal_id": signal_id, "corrected_title": title, "corrected_thesis": thesis,
    })
    assert response.status_code == 200, response.text
    with connection.get_connection() as conn:
        conn.execute("UPDATE signal_feedback_events SET created_at = %s WHERE id > %s", (at, before))
        conn.commit()


def _radar_card(person: Person, signal_id: int) -> dict:
    response = person.get("/api/signals", params={"limit": 200})
    assert response.status_code == 200, response.text
    [card] = [row for row in response.json() if row["id"] == signal_id]
    return card


# ---------------------------------------------------------------------------
#  1. Правки из обратной связи радара
# ---------------------------------------------------------------------------

def test_correction_after_the_close_stays_on_the_radar_but_not_in_the_closed_issue(issue, monkeypatch):
    """Карточка выбрана 03.10 (сентябрь ещё открыт), правка заголовка и сути — 10.10, после закрытия
    сентября (05.10 00:00 МСК). Отзыв принят: экран радара показывает правку, агент учится на ней.
    Сентябрьский выпуск — прежний: его текст не меняет ничто после закрытия."""
    analyst, colleague, card = issue["analyst"], issue["colleague"], issue["s"]["sep"]
    _freeze(monkeypatch, msk(2026, 10, 3, 12, 0))
    analyst.mark_signal(card)

    _freeze(monkeypatch, msk(2026, 10, 10, 12, 0))
    _correct(colleague, card, "Правка после закрытия", "Суть после закрытия.", at=msk(2026, 10, 10, 12, 0))

    [item] = analyst.issue_items(SEPT)
    assert (item["title"], item["summary"]) == ("Сигнал sep", "Суть сигнала sep.")
    # «Все месяцы» — та же карточка тем же текстом: её месяц закрыт.
    [item] = analyst.issue_items("")
    assert (item["title"], item["summary"]) == ("Сигнал sep", "Суть сигнала sep.")

    shown = _radar_card(analyst, card)
    assert (shown["title_ru"], shown["summary"]) == ("Правка после закрытия", "Суть после закрытия.")


def test_closed_issue_keeps_the_last_correction_made_before_the_close(issue, monkeypatch):
    """Правки до закрытия — часть выпуска, как и раньше: последняя из них. 05.10 00:00 МСК — это
    04.10 21:00 UTC; правка в 23:59 МСК 04.10 ещё успевает, правка в 00:00 МСК 05.10 — уже нет."""
    analyst, colleague, card = issue["analyst"], issue["colleague"], issue["s"]["sep"]
    analyst.mark_signal(card)
    _correct(analyst, card, "Первая правка", "Первая суть.", at=msk(2026, 9, 29, 12, 0))
    _correct(colleague, card, "Правка в последнюю минуту", "Суть в последнюю минуту.", at=msk(2026, 10, 4, 23, 59))

    # До закрытия сентябрьский выпуск ещё правится — в нём последняя правка.
    _freeze(monkeypatch, msk(2026, 10, 4, 23, 59, 59))
    [item] = analyst.issue_items(SEPT)
    assert (item["title"], item["summary"]) == ("Правка в последнюю минуту", "Суть в последнюю минуту.")

    _freeze(monkeypatch, msk(2026, 10, 5, 0, 0))
    _correct(colleague, card, "Правка в момент закрытия", "Суть в момент закрытия.", at=msk(2026, 10, 5, 0, 0))
    [item] = analyst.issue_items(SEPT)
    assert (item["title"], item["summary"]) == ("Правка в последнюю минуту", "Суть в последнюю минуту.")
    assert _radar_card(analyst, card)["title_ru"] == "Правка в момент закрытия"
