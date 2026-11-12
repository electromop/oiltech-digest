"""Правки человека в выпуске (#162, спека #160, ADR 0006): материал, «Гипотеза для Компании», «Главное».

Шов 1 спеки: путь «отметить, править, собрать, выгрузить» обычным пользователем на настоящей базе, теми же запросами,
что шлют экран «Дайджест» и карточка сигнала, PDF читается обратно. Правки принадлежат сигналу материала (у статьи
ленты её сигналу ленты): витрина и выпуск показывают один текст. Каждый тест называет один риск.

Часы окна ленты заморожены на 29.09 12:00 МСК: сентябрь открыт; в тестах окно закрывает прошлый месяц 5-го
(tests/conftest.py).
"""

from __future__ import annotations

from oiltech_digest import feed_window
from oiltech_digest.db import connection
from oiltech_digest.processing import digest as digest_module
from tests.digest_pdf_support import image_host  # noqa: F401: фикстура
from tests.digest_pdf_support import pdf_figures, pdf_pages
from tests.digest_user_path_support import (DRILLING, MARKET, SEPT, Person, add_article, add_signal, feed_signal,
                                            issue_key, msk, set_card, utc)
from tests.digest_user_path_support import chromium, issue  # noqa: F401: фикстуры


def _material(person: Person, kind: str, item_id: int, month: str = "2026-09") -> dict:
    """Карточка материала выпуска, как её видит экран «Дайджест»."""
    [found] = [item for items in person.materials(month).values() for item in items
               if issue_key(item) == (kind, item_id)]
    return found


def _edit(person: Person, signal_id: int, **fields):
    """Правка полей материала на экране «Дайджест» или на карточке сигнала."""
    return person.patch(f"/api/signals/{signal_id}/issue-material", json=fields)


def _card(person: Person, signal_id: int, showcase: str = "technology") -> dict:
    response = person.get("/api/signals", params={"limit": 200, "showcase": showcase})
    assert response.status_code == 200, response.text
    [card] = [row for row in response.json() if row["id"] == signal_id]
    return card


def test_title_and_summary_of_a_feed_article_are_edited_once_and_shown_alike_on_the_feed_and_in_the_issue(
        issue, chromium):
    """У статьи ленты правки заголовка и сути не было (в октябре 4 карточки из 21). Правка с экрана «Дайджест» ложится
    на её сигнал ленты: бизнес-лента, состав выпуска и PDF показывают один текст."""
    analyst, admin, a = issue["analyst"], issue["admin"], issue["a"]
    signal_id = feed_signal(a["hi"])
    set_card(signal_id, signal_category="business", theme=MARKET)
    analyst.mark(a["hi"])
    assert _material(analyst, "article", a["hi"])["material_signal_id"] == signal_id

    response = _edit(admin, signal_id, title="Сервисный контракт на бурение", summary="Суть правки коллеги.")
    assert response.status_code == 200, response.text

    material = _material(analyst, "article", a["hi"])
    assert (material["title"], material["summary"]) == ("Сервисный контракт на бурение", "Суть правки коллеги.")
    card = _card(analyst, signal_id, "business")
    assert (card["title_ru"], card["summary"]) == ("Сервисный контракт на бурение", "Суть правки коллеги.")
    [news] = [item for item in analyst.issue_items() if issue_key(item) == ("article", a["hi"])]
    assert news["title"] == "Сервисный контракт на бурение"
    text = " ".join(pdf_pages(analyst.preview_pdf()))
    assert "Сервисный контракт на бурение" in text and "Суть правки коллеги." in text
    assert "Материал hi" not in text


def _finding(key: str, title: str, *, day: int = 12, **card) -> int:
    """Находка радара сентября с полями карточки."""
    signal_id = add_signal(key, first_seen=msk(2026, 9, day, 0, 20), evidence_published=utc(2026, 9, day - 1, 9),
                           title=title)
    if card:
        set_card(signal_id, **card)
    return signal_id


def test_edit_from_the_digest_screen_keeps_its_author_and_does_not_teach_the_radar_judge(issue):
    """Правка находки радара с экрана «Дайджест» меняет текст карточки на радаре и в выпуске и ложится в журнал ОС с
    автором; память агента не пишут ни выбор «в дайджест», ни правка (#188). Правит только администратор (#188)."""
    analyst, admin = issue["analyst"], issue["admin"]
    finding = _finding("find", "Находка радара", signal_category="technology", theme=DRILLING)
    analyst.mark_signal(finding)

    response = _edit(admin, finding, title="Правка заголовка", summary="Правка сути.")
    assert response.status_code == 200, response.text

    card = _card(analyst, finding)
    assert (card["title_ru"], card["summary"]) == ("Правка заголовка", "Правка сути.")
    # Запись только с правкой текста не блок ОС и не «Обратная связь: N» (решение владельца 03.11).
    assert (card["feedback_blocks"], card["feedback_count"]) == ([], 0)
    material = _material(analyst, "signal", finding)
    assert (material["title"], material["summary"]) == ("Правка заголовка", "Правка сути.")
    assert admin.get("/api/signals/memory", params={"status": ""}).json() == []
    with connection.get_connection() as conn:
        authors = conn.execute(
            "SELECT user_id FROM signal_feedback_events WHERE signal_id = %s AND corrected_title IS NOT NULL",
            (finding,),
        ).fetchall()
    assert authors == [(admin.id,)]

    # Заголовок и суть правятся у материала выпуска: у сигнала вне выпуска отказ (там правка: отметка ОС).
    outside = _finding("outside", "Находка вне выпуска", signal_category="technology", theme=DRILLING)
    response = _edit(admin, outside, title="Правка вне выпуска")
    assert response.status_code == 409, response.text
    assert _card(analyst, outside)["title_ru"] == "Находка вне выпуска"


def test_label_edited_by_a_person_replaces_the_event_type_label_and_an_empty_one_returns_it(issue, chromium):
    """Подпись по типу события бывает неточна: человек пишет свою, она хранится на сигнале и главнее таблицы, в том
    числе у давнего события. Стёр: снова подпись из таблицы."""
    analyst, admin = issue["analyst"], issue["admin"]
    finding = _finding("pilot", "Пилот роботизированной буровой", signal_category="technology", theme=DRILLING,
                       event_type="пилот")
    analyst.mark_signal(finding)
    assert _material(analyst, "signal", finding)["label"] == "Пилот"

    assert _edit(admin, finding, label="Опытная эксплуатация").status_code == 200
    material = _material(analyst, "signal", finding)
    assert (material["label"], material["label_default"]) == ("Опытная эксплуатация", "Пилот")
    assert _card(analyst, finding)["issue_label"] == "Опытная эксплуатация"
    assert "БУРЕНИЕ / Опытная эксплуатация" in " ".join(pdf_pages(analyst.preview_pdf()))

    assert _edit(admin, finding, label="  ").status_code == 200
    assert _material(analyst, "signal", finding)["label"] == "Пилот"


HYPOTHESIS_TECH = "Оценить применение роботизированной буровой на месторождениях Компании."
HYPOTHESIS_BUSINESS = "Рассмотреть выход на рынок сервисных контрактов на бурение."


def test_hypothesis_of_a_person_keeps_who_and_when_and_is_printed_under_the_summary_only_where_it_is(
        issue, chromium):
    """Гипотезу пишут на карточке сигнала и на экране «Дайджест»: она на сигнале с отметкой «человек», его почтой и
    временем, и обе витрины, экран и PDF показывают одну. Нет гипотезы: в карточке PDF нет и строки."""
    analyst, admin, a = issue["analyst"], issue["admin"], issue["a"]
    finding = _finding("robot", "Роботизированная буровая", signal_category="technology", theme=DRILLING)
    bare = _finding("bare", "Находка без гипотезы", signal_category="technology", theme=DRILLING, score=60)
    article_signal = feed_signal(a["hi"])
    set_card(article_signal, signal_category="business", theme=MARKET)
    analyst.mark(a["hi"])
    for signal_id in (finding, bare):
        analyst.mark_signal(signal_id)

    assert _edit(admin, finding, hypothesis=HYPOTHESIS_TECH).status_code == 200  # на карточке сигнала
    assert _edit(admin, article_signal, hypothesis=HYPOTHESIS_BUSINESS).status_code == 200  # на «Дайджесте»

    material = _material(analyst, "signal", finding)
    assert (material["hypothesis"], material["hypothesis_set_by"], material["hypothesis_author"]) == (
        HYPOTHESIS_TECH, "human", "user%s@example.test" % admin.id)
    assert material["hypothesis_set_at"].startswith("20")
    assert _material(analyst, "article", a["hi"])["hypothesis_author"] == "user%s@example.test" % admin.id
    assert _material(analyst, "signal", bare)["hypothesis"] is None
    card = _card(analyst, finding)
    assert (card["hypothesis"], card["card_set_by"]["hypothesis"]) == (HYPOTHESIS_TECH, "human")
    assert _card(analyst, article_signal, "business")["hypothesis"] == HYPOTHESIS_BUSINESS

    text = " ".join(pdf_pages(analyst.preview_pdf()))
    assert f"Гипотеза для Компании {HYPOTHESIS_TECH}" in text
    assert f"Гипотеза для Компании {HYPOTHESIS_BUSINESS}" in text
    assert text.count("Гипотеза для Компании") == 2

    # Стёр: гипотезы нет, но это решение человека, агент её не вернёт (#163).
    assert _edit(admin, finding, hypothesis="").status_code == 200
    material = _material(analyst, "signal", finding)
    assert (material["hypothesis"], material["hypothesis_set_by"]) == (None, "human")
    assert " ".join(pdf_pages(analyst.preview_pdf())).count("Гипотеза для Компании") == 1


def test_person_replaces_the_own_picture_with_the_placeholder_of_the_theme_and_brings_it_back(
        issue, chromium, image_host, monkeypatch):
    """og-картинка бывает баннером издателя, размером его не отсечь: человек меняет её на заглушку темы, выбор хранится
    на сигнале, и PDF рисует заглушку. «Своя» возвращает картинку публикации."""
    monkeypatch.setattr(digest_module, "_is_unusable_digest_image_url", lambda url: not url)
    analyst = issue["analyst"]
    with connection.get_connection() as conn:
        article = add_article(conn, issue["source"], "banner", published=utc(2026, 9, 9, 12), score=80,
                              image_url=f"{image_host}/fit.png")
        conn.commit()
    signal_id = feed_signal(article)
    set_card(signal_id, theme=DRILLING)
    analyst.mark(article)
    assert pdf_figures_of(analyst) == ["Картинка публикации"]

    assert _edit(issue["admin"], signal_id, picture="placeholder").status_code == 200
    material = _material(analyst, "article", article)
    assert (material["image_own"], material["picture"]) == (True, "placeholder")
    assert pdf_figures_of(analyst) == ["Рисунок темы: бурение"]

    assert _edit(issue["admin"], signal_id, picture="own").status_code == 200
    assert _material(analyst, "article", article)["picture"] == "own"
    assert pdf_figures_of(analyst) == ["Картинка публикации"]


def pdf_figures_of(person: Person) -> list[str]:
    """Что нарисовано на месте картинки каждой карточки PDF, по порядку карточек."""
    return [alt for alt in pdf_figures(person.preview_pdf()) if alt]


def test_edit_of_a_closed_month_material_is_refused_everywhere_and_the_closed_issue_keeps_its_text(
        issue, monkeypatch):
    """Выпуск закрытого месяца только на просмотр: правку любого поля материала сервер отклоняет и с экрана «Дайджест»,
    и с карточки сигнала; выпуск берёт то, что было до закрытия. Сентябрь закрыт с 05.10 00:00 МСК."""
    analyst, a = issue["analyst"], issue["a"]
    finding = _finding("closed", "Сентябрьская находка", signal_category="technology", theme=DRILLING)
    article_signal = feed_signal(a["hi"])
    analyst.mark(a["hi"])
    analyst.mark_signal(finding)
    assert _edit(issue["admin"], finding, hypothesis=HYPOTHESIS_TECH).status_code == 200

    monkeypatch.setattr(feed_window, "_now", lambda: msk(2026, 10, 12, 12, 0))
    for signal_id in (finding, article_signal):
        for fields in ({"title": "Поздняя правка"}, {"summary": "Поздняя суть."}, {"label": "Поздняя подпись"},
                       {"hypothesis": "Изучить позднюю гипотезу для проверки отказа закрытого месяца."},
                       {"picture": "placeholder"}):
            response = _edit(issue["admin"], signal_id, **fields)
            assert response.status_code == 409, (signal_id, fields, response.text)
            assert "архив" in response.json()["detail"]

    finding_material = _material(analyst, "signal", finding)
    assert (finding_material["title"], finding_material["hypothesis"], finding_material["label"]) == (
        "Сентябрьская находка", HYPOTHESIS_TECH, "")
    article_material = _material(analyst, "article", a["hi"])
    assert (article_material["title"], article_material["picture"]) == ("Материал hi", "own")
    # Карточка на витрине знает, что месяц закрыт: экран гасит правку, а не ждёт отказа.
    assert _card(analyst, finding)["digest_locked"] is True


def test_screen_names_what_each_material_lacks(issue, image_host, monkeypatch):
    """У каждого материала видно, чего не хватает до выпуска: гипотезы, баллов по критериям, связи с нефтесервисом,
    своей картинки. Заглушку, выбранную человеком, нехваткой не считают."""
    from tests.digest_user_path_support import breakdown

    monkeypatch.setattr(digest_module, "_is_unusable_digest_image_url", lambda url: not url)
    analyst = issue["analyst"]
    full = _finding("full", "Заполненная находка", signal_category="technology", theme=DRILLING,
                    oilfield_relevance="direct", **breakdown("tech_radar", 80, 70, 60, 50, 40))
    empty = _finding("empty", "Пустая находка", signal_category="technology", theme=DRILLING, score=50)
    with connection.get_connection() as conn:
        article = add_article(conn, issue["source"], "pic", published=utc(2026, 9, 9, 12), score=80,
                              image_url=f"{image_host}/fit.png")
        conn.commit()
    article_signal = feed_signal(article)
    set_card(article_signal, theme=DRILLING, oilfield_relevance="transferable")
    analyst.mark(article)
    for signal_id in (full, empty):
        analyst.mark_signal(signal_id)
    assert _edit(issue["admin"], full, hypothesis=HYPOTHESIS_TECH, picture="placeholder").status_code == 200

    assert _material(analyst, "signal", full)["missing"] == []
    assert _material(analyst, "signal", empty)["missing"] == ["hypothesis", "criteria", "relevance", "picture"]
    assert _material(analyst, "article", article)["missing"] == ["hypothesis", "criteria"]


def _highlights(person: Person, month: str = "2026-09") -> list[tuple[str, str, bool]]:
    return [(row["label"], row["text"], row["edited"]) for row in person.issue_content(month)["cover"]["highlights"]]


def test_highlights_are_written_per_section_over_the_rubric_default_and_only_while_the_month_is_open(
        issue, chromium, monkeypatch):
    """«Главное в выпуске»: строка на раздел месяца, пишет человек на экране «Дайджест»; пока не написано или стёрто,
    перечень рубрик раздела (решение владельца 04.11). В закрытом месяце только просмотр."""
    analyst, colleague = issue["analyst"], issue["colleague"]
    for signal_id in (_finding("market", "Сделка сервисных компаний", signal_category="business", theme=MARKET),
                      _finding("drill", "Буровая с автоматикой", signal_category="technology", theme=DRILLING)):
        analyst.mark_signal(signal_id)
    assert _highlights(analyst) == [("Бизнес-сигналы", "Рынок и сервисные модели.", False),
                                    ("Технологические сигналы", "Бурение.", False)]

    written = "Сервисные компании делят рынок бурения и ремонта скважин."
    response = colleague.put("/api/issue-highlights/2026-09", json={"section": "business", "text": written})
    assert response.status_code == 200, response.text
    assert _highlights(analyst) == [("Бизнес-сигналы", written, True), ("Технологические сигналы", "Бурение.", False)]
    [business, _] = analyst.issue_content()["cover"]["highlights"]
    assert (business["default"], business["edited_by"]) == ("Рынок и сервисные модели.", "colleague@example.test")
    assert f"Бизнес-сигналы {written}" in pdf_pages(analyst.preview_pdf())[0]

    assert colleague.put("/api/issue-highlights/2026-09", json={"section": "business", "text": " "}).status_code == 200
    assert _highlights(analyst)[0] == ("Бизнес-сигналы", "Рынок и сервисные модели.", False)

    assert colleague.put("/api/issue-highlights/2026-09",
                         json={"section": "technology", "text": "Буровые без людей."}).status_code == 200
    monkeypatch.setattr(feed_window, "_now", lambda: msk(2026, 10, 12, 12, 0))
    response = colleague.put("/api/issue-highlights/2026-09", json={"section": "technology", "text": "Поздно."})
    assert response.status_code == 409, response.text
    assert _highlights(analyst)[1] == ("Технологические сигналы", "Буровые без людей.", True)
    assert colleague.put("/api/issue-highlights/2026-11",
                         json={"section": "nowhere", "text": "Раздела нет."}).status_code == 422


# ---------------------------------------------------------------------------
#  Мелочи экрана «Дайджест» (ревью PR #155, #162)
# ---------------------------------------------------------------------------

def test_archive_counts_exactly_the_materials_of_the_issue_export(issue, monkeypatch):
    """LOW-4: число в таблице архива равно числу материалов выпуска месяца, тот же состав, что у выгрузки: статьи,
    убранные из черновика кнопкой «Из выпуска», в него не идут, сигналы идут."""
    analyst, a, s = issue["analyst"], issue["a"], issue["s"]
    for name in ("hi", "mid", "low"):
        analyst.mark(a[name])
    analyst.mark_signal(s["sep"])
    assert analyst.save(SEPT, [a["hi"], a["mid"]]).status_code == 200

    monkeypatch.setattr(feed_window, "_now", lambda: msk(2026, 10, 12, 12, 0))
    archive = {row["month"]: row for row in analyst.get("/api/feed-window").json()["archive"]}
    exported = analyst.issue(SEPT)
    assert sorted(exported) == sorted([("article", a["hi"]), ("article", a["mid"]), ("signal", s["sep"])])
    assert archive[SEPT]["digest"] == len(exported)


def test_until_the_rollover_the_screen_says_how_many_are_marked_for_the_next_issue_and_when_it_opens(
        issue, monkeypatch):
    """LOW-5: с 1-го до дня смены окна экран собирает прошлый выпуск, а отметки на материалах нового месяца уже
    копятся. Экран одной строкой называет, сколько их и с какого числа выпуск откроется (в тестах окно меняется 5-го);
    отметки не теряются и в прошлый выпуск не попадают."""
    analyst = issue["analyst"]
    monkeypatch.setattr(feed_window, "_now", lambda: msk(2026, 10, 2, 12, 0))
    with connection.get_connection() as conn:
        october = add_article(conn, issue["source"], "oct", published=utc(2026, 10, 1, 12), score=70)
        conn.commit()
    finding = add_signal("oct", first_seen=msk(2026, 10, 1, 9, 0), evidence_published=utc(2026, 10, 1, 8))
    analyst.mark(october)
    analyst.mark_signal(finding)

    window = analyst.get("/api/feed-window").json()
    assert window["months"] == [SEPT, "2026-10"]
    assert window["next_issue"] == {"month": "2026-10", "materials": 2, "opens_on": "2026-10-05"}
    assert analyst.issue(SEPT) == []

    monkeypatch.setattr(feed_window, "_now", lambda: msk(2026, 10, 5, 0, 1))
    window = analyst.get("/api/feed-window").json()
    assert window["months"] == ["2026-10"] and window.get("next_issue") is None
    assert sorted(analyst.issue("2026-10")) == [("article", october), ("signal", finding)]
