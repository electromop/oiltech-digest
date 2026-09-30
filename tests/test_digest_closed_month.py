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

from psycopg.rows import dict_row

from oiltech_digest import feed_window
from oiltech_digest.db import connection, repository
from tests.digest_user_path_support import SEPT, THEME, Person, add_signal, msk, utc
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


# ---------------------------------------------------------------------------
#  2. Повторная находка карточки радаром
# ---------------------------------------------------------------------------

def _refind(key: str, text: str, *, link: str, score: float = 81) -> int:
    """Прогон радара снова нашёл карточку (signal_discovery._store_candidate): тот же ключ, новый
    текст модели и тема, новая ссылка, пересчёт числа ссылок."""
    signal_id = repository.upsert_signal({
        "signal_key": key, "title": f"{text} (en)", "title_ru": text, "theme": "Цифровизация",
        "summary": f"Суть: {text}.", "thesis": f"Тезис: {text}.", "maturity": "watch", "score": score,
    })
    repository.upsert_signal_evidence(signal_id, {
        "source_url": link, "title": text, "publisher": "rigzone.example", "strength": 0.5,
    })
    repository.refresh_signal_evidence_count(signal_id)
    return signal_id


def _stored(signal_id: int) -> dict:
    with connection.get_connection() as conn:
        return conn.cursor(row_factory=dict_row).execute(
            "SELECT title, title_ru, summary, thesis, theme, score, evidence_count, last_seen_at "
            "FROM signals WHERE id = %s",
            (signal_id,),
        ).fetchone()


def _text(row: dict) -> tuple:
    return row["title"], row["title_ru"], row["summary"], row["thesis"], row["theme"]


def test_refind_after_the_close_adds_the_link_but_keeps_the_card_text(issue, monkeypatch):
    """Ежедневный радар 10.10 снова нашёл сентябрьскую карточку с другим текстом и темой. Сентябрь
    закрыт: заголовок, суть, тезис и тема — прежние, их показывают и выпуск, и радар. Ссылка,
    число ссылок, балл и время последней находки обновляются, как раньше."""
    analyst, card = issue["analyst"], issue["s"]["sep"]
    analyst.mark_signal(card)
    _freeze(monkeypatch, msk(2026, 10, 10, 0, 20))

    assert _refind("sep", "Новый текст модели", link="https://radar.example.org/sep-2") == card

    stored = _stored(card)
    assert _text(stored) == ("Сигнал sep", "Сигнал sep", "Суть сигнала sep.", None, THEME)
    assert (float(stored["score"]), stored["evidence_count"]) == (81, 2)
    assert stored["last_seen_at"] > msk(2026, 9, 12, 0, 20)
    shown = _radar_card(analyst, card)
    assert {link["source_url"] for link in shown["evidence"]} == {
        "https://radar.example.org/sep", "https://radar.example.org/sep-2",
    }
    [item] = analyst.issue_items(SEPT)
    assert (item["title"], item["summary"], item["category"]) == ("Сигнал sep", "Суть сигнала sep.", THEME)


def test_refind_in_an_open_month_rewrites_the_card_text_as_before(issue, monkeypatch):
    """Открытый месяц — как раньше: текст карточки — последний ответ модели. 1–4 октября сентябрь
    ещё открыт, как и октябрь."""
    _freeze(monkeypatch, msk(2026, 10, 4, 0, 20))
    october = add_signal("oct", first_seen=msk(2026, 10, 2, 0, 15))

    for key, card in (("sep", issue["s"]["sep"]), ("oct", october)):
        assert _refind(key, f"Новый текст {key}", link=f"https://radar.example.org/{key}-2") == card
        stored = _stored(card)
        assert _text(stored) == (
            f"Новый текст {key} (en)", f"Новый текст {key}", f"Суть: Новый текст {key}.",
            f"Тезис: Новый текст {key}.", "Цифровизация",
        )
        assert stored["evidence_count"] == 2


# ---------------------------------------------------------------------------
#  3. Ссылка и дата карточки радара в выпуске
# ---------------------------------------------------------------------------

def _new_link(signal_id: int, url: str, *, strength: float, published: datetime | None, found: datetime) -> None:
    """Повторная находка в момент `found` принесла карточке ещё одну ссылку (upsert_signal_evidence)
    и сдвинула время последней находки. Время записи ставим сами: часы базы не заморожены."""
    repository.upsert_signal_evidence(signal_id, {
        "source_url": url, "title": "Та же новость в другом издании", "publisher": "worldoil.example",
        "published_at": published, "strength": strength,
    })
    with connection.get_connection() as conn:
        conn.execute("UPDATE signal_evidence SET created_at = %s WHERE source_url = %s", (found, url))
        conn.execute("UPDATE signals SET last_seen_at = %s WHERE id = %s", (found, signal_id))
        conn.commit()


def _link(item: dict) -> tuple:
    """Что выпуск показывает о ссылке карточки: «Читать далее», издатель, дата."""
    return item["url"], item["source"], item["published_at"]


def test_closed_issue_keeps_the_link_and_date_the_card_had_at_the_close(issue, monkeypatch):
    """«Читать далее», издатель и дата карточки в выпуске — от её лучшей (самой сильной) ссылки. 10.10
    радар принёс сентябрьским карточкам ссылки сильнее прежних: экран радара показывает все, а
    сентябрьский выпуск — ссылку, лучшую на момент закрытия. Дата карточки, у ссылки которой даты
    нет, — день поступления на радар («Поступил» на экране). До этой правки выпуск брал время
    последней находки, и каждый прогон радара сдвигал дату, в том числе в закрытом выпуске."""
    analyst, dated = issue["analyst"], issue["s"]["sep"]
    undated = add_signal("undated", first_seen=msk(2026, 9, 20, 12, 0))
    analyst.mark_signal(dated)
    analyst.mark_signal(undated)
    at_close = {
        dated: ("https://radar.example.org/sep", "radar.example.org", "2026-09-11"),
        undated: ("https://radar.example.org/undated", "radar.example.org", "2026-09-20"),
    }
    assert {item["signal_id"]: _link(item) for item in analyst.issue_items(SEPT)} == at_close

    _freeze(monkeypatch, msk(2026, 10, 10, 0, 20))
    _new_link(dated, "https://worldoil.example/sep", strength=0.95, published=utc(2026, 10, 9, 9),
              found=msk(2026, 10, 10, 0, 20))
    _new_link(undated, "https://worldoil.example/undated", strength=0.95, published=None,
              found=msk(2026, 10, 10, 0, 20))

    assert {item["signal_id"]: _link(item) for item in analyst.issue_items(SEPT)} == at_close
    assert {link["source_url"] for link in _radar_card(analyst, dated)["evidence"]} == {
        "https://radar.example.org/sep", "https://worldoil.example/sep",
    }


def test_open_month_issue_takes_the_best_link_found_so_far(issue, monkeypatch):
    """Открытый месяц — как раньше: ссылка карточки в выпуске — самая сильная из найденных. 1–4
    октября сентябрь ещё открыт."""
    analyst, card = issue["analyst"], issue["s"]["sep"]
    analyst.mark_signal(card)
    _freeze(monkeypatch, msk(2026, 10, 4, 0, 20))
    _new_link(card, "https://worldoil.example/sep", strength=0.95, published=utc(2026, 10, 3, 9),
              found=msk(2026, 10, 4, 0, 20))

    [item] = analyst.issue_items(SEPT)
    assert _link(item) == ("https://worldoil.example/sep", "worldoil.example", "2026-10-03")
