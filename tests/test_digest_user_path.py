"""Сентябрьский выпуск собирают сами пользователи (решение владельца 29.09): сборка.

Путь «отметить → собрать» обычным пользователем, на настоящей базе и через те же
запросы, что шлёт экран: отметка в ленте и на радаре, конструктор выпуска (месяц,
лимит 500, «Оценка от 0 до 100»), черновик. Каждый тест — один риск пути; тексты тестов
называют риск, а не механику. Выгрузка — test_digest_user_export.py.

Часы окна ленты заморожены на 29.09 12:00 МСК: сентябрь открыт, август — архив.
"""

from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile

import pytest

from oiltech_digest import feed_window
from oiltech_digest.db import connection, repository
from oiltech_digest.scoring_profiles import SCORING_PRESETS
from tests.digest_user_path_support import (DRILLING, ENERGY, MARKET, SEPT, THEME, add_article, add_signal, breakdown,
                                            msk, set_card, utc)
from tests.digest_user_path_support import issue  # noqa: F401 — фикстура выпуска


# ---------------------------------------------------------------------------
#  1. Отметка в ленте и на радаре обычным пользователем
# ---------------------------------------------------------------------------

def test_marks_from_feed_and_radar_reach_the_issue_and_unmarking_takes_them_out(issue):
    analyst, a, s = issue["analyst"], issue["a"], issue["s"]
    assert analyst.issue() == []

    analyst.mark(a["hi"])
    analyst.mark_signal(s["sep"])
    # Смешанный выпуск: статья (88) выше сигнала (75) — порядок по баллу, пока нет черновика.
    assert analyst.issue() == [("article", a["hi"]), ("signal", s["sep"])]

    # Снятие: «Убрать» на радаре и другой статус в ленте.
    analyst.mark_signal(s["sep"], selected=False)
    analyst.mark(a["hi"], status="new")
    assert analyst.issue() == []


# ---------------------------------------------------------------------------
#  2. Выпуск один на всех (#111): общие отметки и один черновик на месяц
# ---------------------------------------------------------------------------

def test_everyone_builds_the_same_issue_from_everyones_marks(issue):
    """Выпуск месяца — все отметки «в дайджест», кто бы их ни поставил и кто бы ни собирал."""
    analyst, colleague, admin, a = issue["analyst"], issue["colleague"], issue["admin"], issue["a"]
    analyst.mark(a["hi"])
    colleague.mark(a["mid"])
    admin.mark(a["low"])

    expected = [("article", a["hi"]), ("article", a["mid"]), ("article", a["low"])]
    assert analyst.issue() == colleague.issue() == admin.issue() == expected
    assert repository.issue_composition(SEPT) == {
        "article_ids": sorted([a["hi"], a["mid"], a["low"]]), "signal_ids": [],
    }

    # Коллега снимает отметку аналитика — статья уходит из выпуска у всех.
    colleague.mark(a["hi"], status="noise")
    assert analyst.issue() == admin.issue() == [("article", a["mid"]), ("article", a["low"])]


def test_one_draft_per_month_edited_by_anyone(issue):
    analyst, colleague, a = issue["analyst"], issue["colleague"], issue["a"]
    for key in ("hi", "mid"):
        analyst.mark(a[key])
    assert analyst.save(SEPT, [a["hi"], a["mid"]]).status_code == 200

    # Коллега открывает тот же черновик и переставляет порядок — аналитик видит его порядок.
    theirs = colleague.get(f"/api/monthly-digests/{SEPT}").json()
    assert [item["article_id"] for item in theirs["items"]] == [a["hi"], a["mid"]]
    assert colleague.save(SEPT, [a["mid"], a["hi"]]).status_code == 200
    mine = analyst.get(f"/api/monthly-digests/{SEPT}").json()
    assert [item["article_id"] for item in mine["items"]] == [a["mid"], a["hi"]]
    assert analyst.issue() == colleague.issue() == [("article", a["mid"]), ("article", a["hi"])]

    # Снятая коллегой после сохранения статья в выпуск не идёт, хотя в черновике осталась.
    colleague.mark(a["mid"], status="archive")
    assert analyst.issue() == [("article", a["hi"])]
    assert repository.issue_composition(SEPT)["article_ids"] == [a["hi"]]


# ---------------------------------------------------------------------------
#  3. Черновик: порядок, «Из выпуска», повторное открытие
# ---------------------------------------------------------------------------

def test_saved_issue_keeps_manual_order_and_removal_after_reopening(issue):
    analyst, a = issue["analyst"], issue["a"]
    for key in ("hi", "mid", "low"):
        analyst.mark(a[key])
    assert analyst.issue() == [("article", a["hi"]), ("article", a["mid"]), ("article", a["low"])]

    # Ручной порядок и «Из выпуска» для mid: отметка «в дайджест» у mid остаётся.
    assert analyst.save(SEPT, [a["low"], a["hi"]]).status_code == 200

    reopened = analyst.get(f"/api/monthly-digests/{SEPT}").json()
    assert [item["article_id"] for item in reopened["items"]] == [a["low"], a["hi"]]
    assert analyst.issue() == [("article", a["low"]), ("article", a["hi"])]
    still_marked = {row["id"] for row in analyst.get("/api/articles", params={"status": "digest", "limit": 5000}).json()}
    assert a["mid"] in still_marked


def test_unmarked_article_leaves_the_saved_issue_without_resaving(issue):
    """Дефект до 29.09. «Из дайджеста» на экране выпуска (статус archive) или другой статус в
    ленте: статья пропадала из очереди, экран не показывал несохранённых правок — а превью и
    выгрузка брали её из сохранённого черновика, пока человек не пересохранит его сам."""
    analyst, a = issue["analyst"], issue["a"]
    for key in ("hi", "mid", "low"):
        analyst.mark(a[key])
    assert analyst.save(SEPT, [a["low"], a["mid"], a["hi"]]).status_code == 200

    analyst.mark(a["mid"], status="archive")  # «Из дайджеста» на экране выпуска
    analyst.mark(a["hi"], status="noise")     # передумал в ленте

    assert analyst.issue() == [("article", a["low"])]
    html = analyst.export("html").decode("utf-8")
    assert "Материал low" in html
    assert "Материал mid" not in html and "Материал hi" not in html

    # Снова отмечена — возвращается на своё место в черновике.
    analyst.mark(a["mid"])
    assert analyst.issue() == [("article", a["low"]), ("article", a["mid"])]


def test_issue_draft_takes_only_articles_of_its_own_month(issue, monkeypatch):
    """Дефект до 29.09. 1–4 октября открыты сентябрь и октябрь. Черновик проверял только
    «не из архива»: «Все месяцы» + «Сохранить draft» клали в октябрьский черновик сентябрьские
    статьи (месяц черновика экран брал по часам браузера) — сентябрьский выпуск их не
    получал, а октябрьский выходил с чужими."""
    monkeypatch.setattr(feed_window, "_now", lambda: msk(2026, 10, 2, 12, 0))
    analyst, a = issue["analyst"], issue["a"]
    with connection.get_connection() as conn:
        october = add_article(conn, issue["source"], "oct", published=None, collected=utc(2026, 10, 2, 9))
        conn.commit()
    analyst.mark(a["hi"])
    analyst.mark(october)

    refused = analyst.save("2026-10", [october, a["hi"]])
    assert refused.status_code == 409
    assert "статьи другого месяца (сентябрь 2026)" in refused.json()["detail"]
    refused = analyst.save(SEPT, [a["hi"], october])
    assert refused.status_code == 409
    assert "статьи другого месяца (октябрь 2026)" in refused.json()["detail"]
    assert analyst.get("/api/monthly-digests/2026-10").status_code == 404

    assert analyst.save(SEPT, [a["hi"]]).status_code == 200
    assert analyst.save("2026-10", [october]).status_code == 200
    assert analyst.issue() == [("article", a["hi"])]
    assert analyst.issue("2026-10") == [("article", october)]


def test_issue_takes_only_its_own_month_even_from_a_draft_saved_before_the_check(issue, monkeypatch):
    """Ревью #84. Проверка месяца стоит на сохранении черновика (с 29.09), а чтение черновика месяц
    не проверяло: черновик, сохранённый до неё через «Все месяцы», отдал бы в превью и выгрузку
    сентября октябрьскую статью — и занял бы ею место в лимите выпуска. Черновик задаёт порядок,
    членство — отметка и месяц выпуска."""
    monkeypatch.setattr(feed_window, "_now", lambda: msk(2026, 10, 2, 12, 0))
    analyst, a = issue["analyst"], issue["a"]
    with connection.get_connection() as conn:
        october = add_article(conn, issue["source"], "oct", published=None, collected=utc(2026, 10, 2, 9))
        conn.commit()
    for article_id in (october, a["mid"], a["hi"]):
        analyst.mark(article_id)
    # Сохранён в обход проверки PUT — как черновик до 29.09.
    repository.save_monthly_digest(
        SEPT, "Нефтесервисный дайджест · 2026-09",
        [{"article_id": october}, {"article_id": a["mid"]}, {"article_id": a["hi"]}],
    )

    assert analyst.issue() == [("article", a["mid"]), ("article", a["hi"])]
    two = analyst.get("/api/digest-content", params={"month": SEPT, "limit": 2, "min_score": 0, "max_score": 100})
    assert [item["article_id"] for item in two.json()["news"]] == [a["mid"], a["hi"]]
    html = analyst.export("html").decode("utf-8")
    assert "Материал mid" in html and "Материал oct" not in html
    assert analyst.issue("2026-10") == [("article", october)]


# ---------------------------------------------------------------------------
#  4. Смешанный выпуск, только сигналы, только статьи
# ---------------------------------------------------------------------------

def test_issue_of_only_signals_then_mixed_with_and_without_a_draft(issue):
    analyst, a, s = issue["analyst"], issue["a"], issue["s"]
    analyst.mark_signal(s["sep"])
    assert analyst.issue() == [("signal", s["sep"])]

    # «Сохранить draft», когда в очереди статей нет: сигналы из выпуска не пропадают.
    assert analyst.save(SEPT, []).status_code == 200
    assert analyst.issue() == [("signal", s["sep"])]

    # Сигналы радара идут после статей и без черновика (решение владельца 24.10): на экране один список в порядке
    # PDF, а сигналы в черновике не хранятся, так что после сохранения порядок тот же.
    analyst.mark(a["low"])
    assert analyst.issue() == [("article", a["low"]), ("signal", s["sep"])]

    # Черновик хранит статьи; выбранные сигналы идут за ними.
    assert analyst.save(SEPT, [a["low"]]).status_code == 200
    assert analyst.issue() == [("article", a["low"]), ("signal", s["sep"])]


def test_issue_of_only_articles_without_date_and_image(issue):
    analyst, a = issue["analyst"], issue["a"]
    analyst.mark(a["nodate"])

    [item] = analyst.issue_items()
    # Без даты публикации статья относится к месяцу сбора и выходит без даты в карточке.
    assert item["article_id"] == a["nodate"] and item["published_at"] is None
    html = analyst.get("/api/digest-email", params={"month": SEPT, "limit": 500, "min_score": 0}).text
    assert "Материал nodate" in html
    assert 'class="news-card-meta">World Oil</div>' in html
    # Без картинки — плашка рубрики, а не пустое место.
    assert 'class="news-card-image" src="data:image/svg+xml;base64,' in html


# ---------------------------------------------------------------------------
#  5. Карточки радара: правки ОС, дубли, оценка модели, месяц
# ---------------------------------------------------------------------------

def test_colleague_corrections_of_a_radar_card_reach_the_issue(issue):
    """Правки заголовка и сути из ОС коллег накладываются при чтении (#77) — и в выпуске."""
    analyst, colleague, s = issue["analyst"], issue["colleague"], issue["s"]
    analyst.mark_signal(s["sep"])

    for person, title, thesis in (
        (colleague, "Правленый заголовок коллеги", "Правленая суть коллеги."),
        (analyst, "Последняя правка заголовка", "Последняя правка сути."),
    ):
        response = person.post("/api/signals/feedback", json={
            "signal_id": s["sep"], "corrected_title": title, "corrected_thesis": thesis,
        })
        assert response.status_code == 200, response.text

    [item] = analyst.issue_items()
    # Последнее слово — у последней правки, чья бы она ни была.
    assert item["title"] == "Последняя правка заголовка"
    assert item["summary"] == "Последняя правка сути."


def test_human_duplicate_verdict_does_not_take_a_chosen_card_out_of_the_issue(issue):
    analyst, colleague, s = issue["analyst"], issue["colleague"], issue["s"]
    main = add_signal("main", first_seen=msk(2026, 9, 14, 0, 20), evidence_published=utc(2026, 9, 13, 9))
    analyst.mark_signal(s["sep"])

    response = colleague.post("/api/signals/feedback", json={
        "signal_id": s["sep"], "verdict": "merge_duplicate", "duplicate_of_signal_id": main,
    })
    assert response.status_code == 200, response.text
    assert response.json()["merged"] is False
    # Судья дедупа радара тоже не прячет карточку, которую кто-то выбрал в дайджест.
    assert repository.mark_signal_merged(s["sep"], main, "судья дедупа", respect_review=True) is False

    assert analyst.issue() == [("signal", s["sep"])]


def test_radar_card_rated_reject_by_the_model_stays_in_the_issue_it_was_chosen_for(issue):
    """Дефект до 29.09. Экран радара показывает и карточки со зрелостью «Отклонено» — с
    кнопкой «В дайджест»; повторная находка перезаписывает зрелость и балл карточки. Сборщик
    выпуска такие карточки отбрасывал: радар отвечал «Сигнал добавлен в дайджест», а в выпуске
    его не было. Отметка человека — членство в выпуске, оценка модели — только порядок."""
    analyst, s = issue["analyst"], issue["s"]
    weak = add_signal("weak", first_seen=msk(2026, 9, 15, 0, 20), maturity="reject", score=18,
                      evidence_published=utc(2026, 9, 14, 9))
    analyst.mark_signal(weak)
    analyst.mark_signal(s["sep"])

    # Повторная находка выбранной карточки: модель теперь считает её слабой.
    repository.upsert_signal({
        "signal_key": "sep", "title": "Сигнал sep", "title_ru": "Сигнал sep", "theme": THEME,
        "summary": "Суть сигнала sep.", "maturity": "reject", "score": 4,
    })

    assert analyst.issue() == [("signal", weak), ("signal", s["sep"])]


def test_radar_card_belongs_to_the_issue_of_the_month_it_arrived_in(issue):
    """Дефект до 29.09. Месяц сигнала в выпуске считался по дате лучшей ссылки, а без даты —
    по last_seen_at, который сдвигает каждая повторная находка (touch_signal, upsert_signal).
    Ежедневный радар 1–4 октября уносил выбранную в сентябре карточку в октябрьский выпуск,
    а карточку со старой ссылкой (найдена в сентябре, статья августовская) сентябрьский
    выпуск не видел вовсе. Месяц карточки — месяц поступления на радар по Москве: эту дату и
    показывает экран радара («дата поступления»)."""
    analyst = issue["analyst"]
    undated = add_signal("undated", first_seen=msk(2026, 9, 20, 0, 15))
    old_link = add_signal("old-link", first_seen=msk(2026, 9, 3, 0, 15), evidence_published=utc(2026, 8, 28, 9))
    # Прогон радара 01.10 в 00:15 МСК — по UTC это ещё 30.09, а экран показывает 01.10.2026.
    october = add_signal("october", first_seen=msk(2026, 10, 1, 0, 15))
    for signal_id in (undated, old_link, october):
        analyst.mark_signal(signal_id)
    # Радар 02.10 снова нашёл сентябрьскую карточку.
    with connection.get_connection() as conn:
        conn.execute("UPDATE signals SET last_seen_at = %s WHERE id = %s", (msk(2026, 10, 2, 0, 15), undated))
        conn.commit()

    september = set(analyst.issue(SEPT))
    assert {("signal", undated), ("signal", old_link)} <= september
    assert ("signal", october) not in september
    assert analyst.issue("2026-10") == [("signal", october)]
    assert analyst.issue("2026-08") == []


# ---------------------------------------------------------------------------
#  6. Балл (п. 3) и роботы: отметку человека не уносят ни пересчёт, ни перепечатки
# ---------------------------------------------------------------------------

def test_new_scores_do_not_take_marked_items_out_of_the_issue(issue):
    """Владелец 29.09 меняет критерии скоринга и пересчитывает сентябрь. Отметка человека не
    должна пропадать из выпуска из-за нового балла: ни в превью и выгрузке, ни в черновике,
    сохранённом любым путём API. До 29.09 POST /api/monthly-digests по умолчанию сохранял
    черновик с полом 60 — после пересчёта из него выпадало бы всё, что опустилось ниже."""
    analyst, a, s = issue["analyst"], issue["a"], issue["s"]
    marked = [a["hi"], a["mid"], a["low"], a["unscored"]]
    for article_id in marked:
        analyst.mark(article_id)
    analyst.mark_signal(s["sep"])
    expected = {("article", article_id) for article_id in marked} | {("signal", s["sep"])}
    assert set(analyst.issue()) == expected

    # Пересчёт: баллы сентября упали до нуля, у одной статьи строки балла пока нет,
    # сигнал при повторной находке получил 3.
    with connection.get_connection() as conn:
        conn.execute("UPDATE article_scores SET total_score = 0, score_label = 'Низкая'")
        conn.execute("DELETE FROM article_scores WHERE article_id = %s", (a["hi"],))
        conn.commit()
    repository.upsert_signal({
        "signal_key": "sep", "title": "Сигнал sep", "title_ru": "Сигнал sep", "theme": THEME,
        "summary": "Суть сигнала sep.", "maturity": "watch", "score": 3,
    })

    assert set(analyst.issue()) == expected
    html = analyst.export("html").decode("utf-8")
    for title in ("Материал hi", "Материал mid", "Материал low", "Материал unscored", "Сигнал sep"):
        assert title in html

    # Черновик через POST с умолчаниями API — без «Оценки от».
    response = analyst.post("/api/monthly-digests", json={"month": SEPT})
    assert response.status_code == 200, response.text
    saved = analyst.get(f"/api/monthly-digests/{SEPT}").json()
    assert {item["article_id"] for item in saved["items"]} == set(marked)
    assert set(analyst.issue()) == expected


def _feed(person) -> set[int]:
    return {row["id"] for row in person.get("/api/articles", params={"limit": 5000}).json()}


def _reprints() -> dict[int, int]:
    """Пометки перепечаток: {спрятанная копия: главная}."""
    with connection.get_connection() as conn:
        return dict(conn.execute("SELECT article_id, primary_id FROM article_reprints").fetchall())


def _judge_says(duplicate: int, primary: int) -> None:
    """Вердикт судьи перепечаток: `duplicate` — перепечатка `primary` (reprints.review_candidates)."""
    repository.mark_article_reprint(
        article_id=duplicate, primary_id=primary, similarity=0.8, reason="одно событие", model="judge",
    )


def test_reprint_robot_makes_the_chosen_copy_primary_and_hides_the_other(issue):
    """Дефект до 29.09: судья перепечаток (последние 14 дней) прятал копию, выбранную в дайджест, —
    новость пропадала из выпуска и из ленты целиком. Правка 29.09 (отказ прятать выбранную)
    оставляла в ленте у всех обе копии (ревью #84). Выбор человека важнее выбора судьи: главной
    становится выбранная копия, а копию, которую не выбирал никто, робот прячет — вместе с её
    группой (группа всегда в один шаг, как у карточек радара)."""
    analyst, colleague, a = issue["analyst"], issue["colleague"], issue["a"]
    with connection.get_connection() as conn:
        copy = add_article(conn, issue["source"], "copy", published=utc(2026, 9, 10, 14), score=40)
        conn.commit()
    _judge_says(a["low"], a["mid"])  # у mid уже есть спрятанная копия
    analyst.mark(copy)

    _judge_says(copy, a["mid"])

    assert _reprints() == {a["mid"]: copy, a["low"]: copy}
    assert analyst.issue() == [("article", copy)]
    feed = _feed(colleague)
    assert copy in feed and a["mid"] not in feed and a["low"] not in feed
    # Перестановку видно в базе: причина — выбор в дайджест, а не довод судьи о главной.
    with connection.get_connection() as conn:
        reason, decided_by = conn.execute(
            "SELECT reason, decided_by FROM article_reprints WHERE article_id = %s", (a["mid"],)
        ).fetchone()
    assert reason == "главная — выбранная в дайджест; судья: одно событие"
    assert decided_by == "ai"


def test_reprint_robot_hides_neither_copy_when_different_people_chose_them(issue):
    """Разные люди выбрали в дайджест разные копии одной новости — робот не прячет ни одну: иначе
    выпуск потерял бы выбранную новость. Копию, которую не выбирал никто, он прячет, как
    раньше, — и в пользу главной, выбранной кем-то."""
    analyst, colleague, a = issue["analyst"], issue["colleague"], issue["a"]
    analyst.mark(a["hi"])
    colleague.mark(a["mid"])

    with pytest.raises(ValueError, match="обе копии"):
        _judge_says(a["hi"], a["mid"])
    assert _reprints() == {}
    # Выпуск общий (#111): обе выбранные копии — в нём у всех.
    assert analyst.issue() == colleague.issue() == [("article", a["hi"]), ("article", a["mid"])]

    _judge_says(a["low"], a["mid"])
    assert _reprints() == {a["low"]: a["mid"]}
    feed = _feed(analyst)
    assert {a["hi"], a["mid"]} <= feed and a["low"] not in feed


def test_chosen_copy_that_the_feed_no_longer_shows_does_not_become_primary(issue):
    """Выбранная копия, которую лента уже не показывает (перепроверка пометила её на удаление),
    главной не становится: спрятать ради неё видимую — убрать новость из ленты целиком. Отказ,
    как было с 29.09."""
    analyst, a = issue["analyst"], issue["a"]
    analyst.mark(a["hi"])
    with connection.get_connection() as conn:
        conn.execute("UPDATE articles SET pending_deletion = TRUE WHERE id = %s", (a["hi"],))
        conn.commit()

    with pytest.raises(ValueError, match="в дайджест"):
        _judge_says(a["hi"], a["mid"])
    assert _reprints() == {}
    assert a["mid"] in _feed(analyst)


def test_chosen_copy_of_a_closed_month_does_not_take_the_news_out_of_the_open_month_feed(issue, monkeypatch):
    """Ревью #88. Копия от 30.09 выбрана в сентябрьский выпуск до 05.10, копия от 01.10 не выбрана.
    После 05.10 судья считает главной октябрьскую. Перестановка сделала бы главной сентябрьскую, а
    сентябрь уже архив: новость пропала бы из октябрьской ленты у всех. Главную не переставляем —
    пара отбивается, как до перестановки: обе копии видны, каждая в своём месяце."""
    analyst, colleague = issue["analyst"], issue["colleague"]
    with connection.get_connection() as conn:
        september = add_article(conn, issue["source"], "sep-copy", published=utc(2026, 9, 30, 12))
        october = add_article(conn, issue["source"], "oct-copy", published=utc(2026, 10, 1, 9))
        conn.commit()
    monkeypatch.setattr(feed_window, "_now", lambda: msk(2026, 10, 3, 12, 0))
    analyst.mark(september)

    monkeypatch.setattr(feed_window, "_now", lambda: msk(2026, 10, 6, 12, 0))
    with pytest.raises(ValueError, match="закрытого месяца"):
        _judge_says(september, october)
    assert _reprints() == {}
    assert october in _feed(colleague)
    assert analyst.issue(SEPT) == [("article", september)]


def _marked_for_deletion(*article_ids: int) -> None:
    """Пометка на удаление, как её ставила перепроверка гейтом с --mark (снята 08.11; пометки остались)."""
    with connection.get_connection() as conn:
        conn.execute("UPDATE articles SET pending_deletion = TRUE, deletion_reason = 'не нефтесервис', "
                     "marked_for_deletion_at = now() WHERE id = ANY(%s)", (list(article_ids),))
        conn.commit()


def test_purge_of_marked_for_deletion_keeps_articles_marked_for_the_issue(issue):
    """Ревью #88. Разовая чистка помеченных на удаление (recheck-purge) удаляет статьи физически. Защищены были только
    статьи из сохранённых черновиков, а с #84 выпуск строится по отметкам: выбранная, но не сохранённая в черновик
    статья удалялась бы вместе с отметкой и уходила из выпуска. Отметка «в дайджест» защищает, как черновик: статья
    пропускается и возвращается в выпуск снятием пометки. --force, как раньше, без защиты."""
    analyst, a = issue["analyst"], issue["a"]
    analyst.mark(a["hi"])
    _marked_for_deletion(a["hi"], a["low"])

    assert repository.purge_pending_deletion() == 1
    with connection.get_connection() as conn:
        left = {row[0] for row in conn.execute("SELECT id FROM articles WHERE id = ANY(%s)", ([a["hi"], a["low"]],))}
    assert left == {a["hi"]}
    assert repository.unmark_all_pending_deletion() == 1
    assert analyst.issue() == [("article", a["hi"])]

    _marked_for_deletion(a["hi"])
    assert repository.purge_pending_deletion(force=True) == 1
    assert analyst.issue() == []


# ---------------------------------------------------------------------------
#  7. Тема и балл публикации: у сигнала ленты, у статьи без него у самой статьи (решение владельца 08.11)
# ---------------------------------------------------------------------------

def _tag(name: str, *, parent: str | None = None) -> int:
    """Тег экрана «Теги»: корневая тематика или дочерний тег под ней (корень заводится, если его нет)."""
    with connection.get_connection() as conn:
        parent_id = None
        if parent is not None:
            row = conn.execute("SELECT id FROM tags WHERE name = %s AND parent_id IS NULL", (parent,)).fetchone()
            parent_id = row[0] if row else conn.execute(
                "INSERT INTO tags (name, enabled) VALUES (%s, TRUE) RETURNING id", (parent,)).fetchone()[0]
        tag_id = conn.execute("INSERT INTO tags (name, parent_id, enabled) VALUES (%s, %s, TRUE) RETURNING id",
                              (name, parent_id)).fetchone()[0]
        conn.commit()
    return int(tag_id)


def _tag_article(article_id: int, tag_id: int) -> None:
    """Тег статьи, как его ставила старая цепочка."""
    with connection.get_connection() as conn:
        conn.execute("INSERT INTO article_tags (article_id, tag_id) VALUES (%s, %s)", (article_id, tag_id))
        conn.commit()


def _otbor_article(issue, slug: str, *, day: int, theme: str, score: float = 0, band: str | None = None,
                   article_score: float | None = None) -> tuple[int, int]:
    """Статья, принятая «Отбором», и её сигнал ленты (ключ feed:<статья>): тема «Карточки», балл и ступень её второго
    вызова. Без article_score своего балла у статьи нет, как после снятия старой цепочки; с ним старая цепочка успела
    поставить статье и свой балл (так на проде до 08.11)."""
    published = utc(2026, 9, day, 12)
    with connection.get_connection() as conn:
        article_id = add_article(conn, issue["source"], slug, published=published, score=article_score)
        conn.execute("UPDATE articles SET selector = 'otbor' WHERE id = %s", (article_id,))
        conn.execute("UPDATE article_cards SET relevant = NULL WHERE article_id = %s", (article_id,))
        signal_id = conn.execute(
            "INSERT INTO signals (signal_key, origin, origin_article_id, signal_category, title, theme, maturity,"
            " otbor_decision, score, score_band, first_seen_at, last_seen_at)"
            " VALUES (%s, 'feed', %s, 'business', %s, %s, 'watch', 'accept', %s, %s, %s, %s) RETURNING id",
            (f"feed:{article_id}", article_id, f"Материал {slug}", theme, score, band, published, published),
        ).fetchone()[0]
        conn.commit()
    return article_id, int(signal_id)


def _issue_items(person, **params) -> list[dict]:
    """Позиции выпуска сентября с фильтрами экрана «Дайджест» (тема, поиск, оценка от и до)."""
    response = person.get("/api/digest-content",
                          params={"month": SEPT, "limit": 500, "min_score": 0, "max_score": 100, **params})
    assert response.status_code == 200, response.text
    return response.json()["news"]


def _digest_screen(person, **params) -> dict[int, dict]:
    """Статьи экрана «Дайджест» (/api/articles), по номеру."""
    rows = person.get("/api/articles", params={"limit": 5000, **params}).json()
    return {row["id"]: row for row in rows}


def _materials(person) -> dict[int, dict]:
    """Карточки PDF выпуска сентября, по номеру статьи."""
    return {item["article_id"]: item for section in person.materials().values() for item in section
            if item["item_type"] == "article"}


def test_issue_takes_theme_and_score_of_a_publication_from_its_feed_signal(issue):
    """Решение владельца 08.11: тег и балл статьи снимаются вместе со старой цепочкой, тема и балл публикации живут в
    её сигнале ленты. До 08.11 старая цепочка ставила свой тег и балл и статьям «Отбора», и письмо, DOCX, фильтр по
    теме, порог и порядок выпуска шли по ним, а бизнес-лента и PDF по сигналу. Статья без сигнала ленты живёт по своим
    тегу и баллу, как раньше: рубрика «родитель / тег»."""
    analyst, a = issue["analyst"], issue["a"]
    deals = _tag("Сделки", parent=MARKET)
    # «Карточка» дала бурение и 91 («сильный»); старая цепочка той же статье дала «рынок / сделки» и 30.
    otbor, _ = _otbor_article(issue, "otbor", day=14, theme=DRILLING, score=91, band="strong", article_score=30)
    _tag_article(otbor, deals)
    _tag_article(a["mid"], deals)
    for article_id in (a["mid"], a["hi"], otbor):
        analyst.mark(article_id)

    # Без черновика статьи идут по баллу публикации: 91 у сигнала, затем 88 и 64 у статей без сигнала.
    items = _issue_items(analyst)
    assert [(item["article_id"], item["category"], item["score"], item["score_label"]) for item in items] == [
        (otbor, DRILLING, 91.0, "Сильный"),
        (a["hi"], "Без тега", 88.0, "Высокая"),
        (a["mid"], f"{MARKET} / Сделки", 64.0, "Средняя"),
    ]
    # Фильтр по теме, поиск и порог экрана: по теме и баллу публикации, у статьи без сигнала по её тегу (и родителю).
    assert [item["article_id"] for item in _issue_items(analyst, top_tag=DRILLING)] == [otbor]
    assert [item["article_id"] for item in _issue_items(analyst, top_tag=MARKET)] == [a["mid"]]
    assert [item["article_id"] for item in _issue_items(analyst, search="буровое оборудование")] == [otbor]
    assert [item["article_id"] for item in _issue_items(analyst, min_score=90)] == [otbor]
    assert [item["article_id"] for item in _issue_items(analyst, max_score=50)] == []
    # PDF ставит карточки по тому же баллу, что письмо.
    assert {key: item["score"] for key, item in _materials(analyst).items()} == {otbor: 91.0, a["hi"]: 88.0,
                                                                                a["mid"]: 64.0}

    # Письмо и DOCX: рубрика карточки по теме публикации.
    letter = analyst.get("/api/digest-email", params={"month": SEPT, "limit": 500, "min_score": 0}).text
    assert f'<span class="news-card-tag">{DRILLING}</span>' in letter
    assert '<span class="news-card-tag">Без тега</span>' in letter
    with ZipFile(BytesIO(analyst.export("docx"))) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")
    assert f"{DRILLING} · World Oil" in document_xml

    # Черновик задаёт порядок, тема и балл те же.
    assert analyst.save(SEPT, [a["mid"], otbor, a["hi"]]).status_code == 200
    assert [(item["article_id"], item["category"], item["score"]) for item in _issue_items(analyst)] == [
        (a["mid"], f"{MARKET} / Сделки", 64.0), (otbor, DRILLING, 91.0), (a["hi"], "Без тега", 88.0),
    ]


def test_digest_screen_shows_the_same_score_of_a_publication_as_the_business_feed(issue):
    """Экран «Дайджест» берёт статьи из /api/articles: тема, балл, подпись и разбивка по критериям там те же, что у
    сигнала ленты на бизнес-ленте. Подпись по правилу витрин: ступень второго вызова «Карточки», у прежнего балла
    пороги ленты 80/65/40."""
    analyst, a = issue["analyst"], issue["a"]
    otbor, signal_id = _otbor_article(issue, "otbor", day=14, theme=DRILLING, score=91, band="strong",
                                      article_score=30)
    # «Свежесть» без даты события в тексте в итог не входит: в разбивке её нет, «0» солгал бы.
    set_card(signal_id, **breakdown("business", 95, None, 80))
    for article_id in (otbor, a["mid"]):
        analyst.mark(article_id)

    rows = _digest_screen(analyst, status="digest")
    [card] = [card for card in analyst.get("/api/signals", params={"showcase": "business"}).json()
              if card["id"] == signal_id]
    assert (rows[otbor]["tag"], rows[otbor]["score"], rows[otbor]["rating"]) == (DRILLING, 91.0, "Сильный")
    assert (card["theme"], card["score"], card["score_label"]) == (DRILLING, 91.0, "Сильный")
    preset = SCORING_PRESETS["viktor"]["business"]
    assert [(item["name"], item["weight"], item["final_score"]) for item in rows[otbor]["score_items"]] == [
        (preset[0]["name"], float(preset[0]["weight"]), 95.0), (preset[2]["name"], float(preset[2]["weight"]), 80.0),
    ]
    # Статья без сигнала ленты: прежние тег и балл статьи, подпись по порогам ленты.
    assert (rows[a["mid"]]["tag"], rows[a["mid"]]["score"], rows[a["mid"]]["rating"]) == ("Без тега", 64.0,
                                                                                          "Средняя")


def test_otbor_article_without_its_own_tag_and_score_goes_by_its_feed_signal(issue):
    """Так будет после снятия стадий тега и балла статьи: у статьи «Отбора» нет ни тега, ни балла статьи, только тема
    «Карточки» и балл её второго вызова в сигнале ленты. Сигнал, которого второй вызов ещё не оценил, идёт «без
    баллов», как на бизнес-ленте: в выпуске последним, на экране «Дайджест» с нулём и «Без оценки»."""
    analyst, a = issue["analyst"], issue["a"]
    scored, _ = _otbor_article(issue, "energy", day=15, theme=ENERGY, score=66, band="useful")
    waiting, _ = _otbor_article(issue, "waiting", day=16, theme=repository.SYSTEM_TAG_UNCLASSIFIED)
    for article_id in (waiting, scored, a["hi"]):
        analyst.mark(article_id)

    assert [(item["article_id"], item["category"], item["score"], item["score_label"])
            for item in _issue_items(analyst)] == [
        (a["hi"], "Без тега", 88.0, "Высокая"),
        (scored, ENERGY, 66.0, "Полезный"),
        (waiting, repository.SYSTEM_TAG_UNCLASSIFIED, None, None),
    ]
    # Порог «Оценка от» выпуска балла, которого нет, не пропускает, как у статьи без балла до 08.11.
    assert [item["article_id"] for item in _issue_items(analyst, min_score=50)] == [a["hi"], scored]
    materials = _materials(analyst)
    assert (materials[scored]["score"], materials[scored]["rubric"]) == (66.0, "ЭНЕРГЕТИКА")
    assert materials[waiting]["score"] is None

    rows = _digest_screen(analyst, status="digest")
    assert (rows[scored]["tag"], rows[scored]["score"], rows[scored]["rating"]) == (ENERGY, 66.0, "Полезный")
    assert (rows[waiting]["tag"], rows[waiting]["score"], rows[waiting]["rating"]) == (
        repository.SYSTEM_TAG_UNCLASSIFIED, 0, "Без оценки")
    assert rows[waiting]["score_items"] == []
    # Лента с «Оценкой от» неоценённое не прячет: балла у него ещё нет (правило ленты для необработанного).
    feed = _digest_screen(analyst, min_score=50)
    assert {scored, waiting, a["unscored"]} <= set(feed) and a["low"] not in feed
