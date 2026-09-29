"""Сентябрьский выпуск собирают сами пользователи (решение владельца 29.09): сборка.

Путь «отметить → собрать» обычным пользователем, на настоящей базе и через те же
запросы, что шлёт экран: отметка в ленте и на радаре, конструктор выпуска (месяц,
лимит 500, «Оценка от 0 до 100»), черновик. Каждый тест — один риск пути; тексты тестов
называют риск, а не механику. Выгрузка — test_digest_user_export.py.

Часы окна ленты заморожены на 29.09 12:00 МСК: сентябрь открыт, август — архив.
"""

from __future__ import annotations

import pytest

from oiltech_digest import feed_window
from oiltech_digest.db import connection, repository
from tests.digest_user_path_support import SEPT, THEME, add_article, add_signal, msk, utc
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
#  2. Кандидаты — только своё; черновик — свой (п. 5: изоляция, админ — как в коде)
# ---------------------------------------------------------------------------

def test_each_person_sees_and_saves_only_own_issue_admin_included(issue):
    analyst, colleague, admin, a = issue["analyst"], issue["colleague"], issue["admin"], issue["a"]
    analyst.mark(a["hi"])
    colleague.mark(a["mid"])
    admin.mark(a["low"])

    assert analyst.issue() == [("article", a["hi"])]
    assert colleague.issue() == [("article", a["mid"])]
    # Админ собирает свой выпуск по тем же правилам: чужие отметки ему не видны.
    assert admin.issue() == [("article", a["low"])]

    assert analyst.save(SEPT, [a["hi"]]).status_code == 200
    # Чужой черновик не открывается ни коллеге, ни админу: у каждого свой выпуск за месяц.
    assert colleague.get(f"/api/monthly-digests/{SEPT}").status_code == 404
    assert admin.get(f"/api/monthly-digests/{SEPT}").status_code == 404

    # Сохранение коллеги пишет его черновик и не трогает черновик аналитика.
    assert colleague.save(SEPT, [a["mid"]]).status_code == 200
    mine = analyst.get(f"/api/monthly-digests/{SEPT}").json()
    assert [item["article_id"] for item in mine["items"]] == [a["hi"]]
    assert mine["user_id"] == analyst.id
    assert analyst.issue() == [("article", a["hi"])]
    assert colleague.issue() == [("article", a["mid"])]


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

    analyst.mark(a["low"])
    assert analyst.issue() == [("signal", s["sep"]), ("article", a["low"])]

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


def test_reprint_robot_does_not_hide_a_copy_chosen_for_the_issue(issue):
    """Дефект до 29.09. Судья перепечаток (последние 14 дней) прячет копию в пользу главной.
    Если человек выбрал в дайджест именно копию, а главную не отмечал, новость пропадала из
    его выпуска целиком — и из ленты, откуда её не вернуть. Радар для карточек это уже
    соблюдает: выбранную в дайджест не прячет даже решение человека (mark_signal_merged)."""
    analyst, a = issue["analyst"], issue["a"]
    with connection.get_connection() as conn:
        copy = add_article(conn, issue["source"], "copy", published=utc(2026, 9, 10, 14), score=40)
        conn.commit()
    analyst.mark(copy)

    with pytest.raises(ValueError, match="в дайджест"):
        repository.mark_article_reprint(
            article_id=copy, primary_id=a["mid"], similarity=0.8, reason="одно событие", model="judge",
        )
    assert analyst.issue() == [("article", copy)]

    # Копию, которую никто не выбрал, робот прячет как раньше.
    repository.mark_article_reprint(
        article_id=a["low"], primary_id=a["mid"], similarity=0.8, reason="одно событие", model="judge",
    )
    feed = {row["id"] for row in analyst.get("/api/articles", params={"limit": 5000}).json()}
    assert a["low"] not in feed and a["mid"] in feed and copy in feed
