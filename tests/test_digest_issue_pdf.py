"""Выпуск в формате эксперта (#161, спека #160, ADR 0006): материалы и карточки PDF.

Шов 1 спеки: путь «отметить, собрать, выгрузить» обычным пользователем на настоящей базе, теми же запросами, что
шлёт экран «Дайджест», PDF читается обратно. Поля материала живут на его сигнале, у статьи ленты на её сигнале
ленты. Каждый тест называет один риск выпуска. Обложка и документ: test_digest_issue_cover.py.

Часы окна ленты заморожены на 29.09 12:00 МСК: сентябрь открыт.
"""

from __future__ import annotations

from oiltech_digest.db import connection, repository
from oiltech_digest.processing import digest as digest_module
from tests.digest_pdf_support import image_host  # noqa: F401 — фикстура
from tests.digest_pdf_support import pdf_figures, pdf_images, pdf_links, pdf_pages
from tests.digest_user_path_support import (AUTOMATION, DRILLING, ENERGY, HSE, MARKET, SEPT, add_article, add_signal,
                                            breakdown, feed_signal, issue_key, msk, set_card, utc)
from tests.digest_user_path_support import chromium, issue  # noqa: F401 — фикстуры


def _keys(materials: list[dict]) -> list[tuple[str, int]]:
    return [issue_key(item) for item in materials]


def _finding(key: str, title: str, *, day: int = 12, **card) -> int:
    """Находка радара сентября с полями карточки."""
    signal_id = add_signal(key, first_seen=msk(2026, 9, day, 0, 20), evidence_published=utc(2026, 9, day - 1, 9),
                           title=title)
    if card:
        set_card(signal_id, **card)
    return signal_id


def test_section_follows_the_signal_category_and_each_section_starts_a_new_sheet(issue, chromium):
    """Раздел по категории сигнала, как на экране радара: статья ленты берёт категорию у своего сигнала ленты, находка
    без категории и «другое» идут в технологии. Каждый раздел с нового листа, пустой раздел не выводится."""
    analyst, a = issue["analyst"], issue["a"]
    feed_signal(a["hi"])
    tech = _finding("tech", "Роботизированная буровая", signal_category="technology", theme=DRILLING)
    unknown = _finding("nocat", "Находка без категории", theme=DRILLING)
    other = _finding("other", "Находка другого блока", signal_category="other", theme=DRILLING)
    analyst.mark(a["hi"])
    for signal_id in (tech, unknown, other):
        analyst.mark_signal(signal_id)

    materials = analyst.materials()
    assert _keys(materials["business"]) == [("article", a["hi"])]
    assert set(_keys(materials["technology"])) == {("signal", tech), ("signal", unknown), ("signal", other)}

    pages = pdf_pages(analyst.preview_pdf())
    assert len(pages) == 3
    assert "01 Бизнес-сигналы" in pages[1] and "Материал hi" in pages[1]
    assert "Технологические сигналы" not in pages[1]
    assert "02 Технологические сигналы" in pages[2]
    assert all(title in pages[2] for title in ("Роботизированная буровая", "Находка без категории",
                                                "Находка другого блока"))
    # Выгрузка файлом — тот же выпуск, что предпросмотр на экране.
    assert pdf_pages(analyst.export("pdf")) == pages

    analyst.mark(a["hi"], status="new")
    pages = pdf_pages(analyst.preview_pdf())
    assert len(pages) == 2
    assert not any("Бизнес-сигналы" in text for text in pages)
    assert "02 Технологические сигналы" in pages[1]


def _rubrics(materials: list[dict]) -> list[tuple[str, str]]:
    return [(item["rubric"], item["title"]) for item in materials]


def test_cards_go_by_rubric_of_the_spec_table_then_by_score_and_the_draft_does_not_reorder_them(issue, chromium):
    """Порядок считается сам (спека #160): раздел, рубрика по таблице, внутри рубрики итоговый балл по убыванию. Тема
    автоматизации в бизнес-разделе даёт «РЫНОК АВТОМАТИЗАЦИИ». Давнее событие (дата события старше 180 дней до начала
    месяца выпуска, порог экрана радара) даёт «СПРАВОЧНАЯ ПРАКТИКА» и место сразу за рубрикой своей темы; дата с
    точностью до месяца считается по последнему дню месяца, как у «Отбора». Черновик порядок PDF не задаёт: письмо и
    DOCX идут по черновику, как раньше."""
    analyst, a = issue["analyst"], issue["a"]
    for key in ("hi", "mid"):
        set_card(feed_signal(a[key]), theme=MARKET)
    signals = [
        _finding("automation-market", "Прогноз рынка автоматизации", signal_category="business", theme=AUTOMATION,
                 score=95),
        _finding("drilling-low", "Буровая 60", signal_category="technology", theme=DRILLING, score=60),
        _finding("drilling-high", "Буровая 80", signal_category="technology", theme=DRILLING, score=80),
        _finding("energy-old", "Давний кейс энергетики", signal_category="technology", theme=ENERGY, score=99,
                 event_date="2026-03-04"),
        _finding("energy-180", "Энергетика ровно 180 дней", signal_category="technology", theme=ENERGY, score=40,
                 event_date="2026-03-05"),
        _finding("energy-march", "Энергетика в марте", signal_category="technology", theme=ENERGY, score=30,
                 event_date="2026-03-01", event_date_precision="month"),
        _finding("automation", "Цифровой двойник", signal_category="technology", theme=AUTOMATION, score=97),
        _finding("hse", "Безопасность на промысле", signal_category="technology", theme=HSE, score=10),
    ]
    for article_id in (a["mid"], a["hi"]):
        analyst.mark(article_id)
    for signal_id in signals:
        analyst.mark_signal(signal_id)
    assert analyst.save(SEPT, [a["mid"], a["hi"]]).status_code == 200

    materials = analyst.materials()
    assert _rubrics(materials["business"]) == [
        ("РЫНОК И СЕРВИСНЫЕ МОДЕЛИ", "Материал hi"),
        ("РЫНОК И СЕРВИСНЫЕ МОДЕЛИ", "Материал mid"),
        ("РЫНОК АВТОМАТИЗАЦИИ", "Прогноз рынка автоматизации"),
    ]
    assert _rubrics(materials["technology"]) == [
        ("БУРЕНИЕ", "Буровая 80"),
        ("БУРЕНИЕ", "Буровая 60"),
        ("БЕЗОПАСНОСТЬ", "Безопасность на промысле"),
        ("ЭНЕРГЕТИКА", "Энергетика ровно 180 дней"),
        ("ЭНЕРГЕТИКА", "Энергетика в марте"),
        ("СПРАВОЧНАЯ ПРАКТИКА", "Давний кейс энергетики"),
        ("АВТОМАТИЗАЦИЯ", "Цифровой двойник"),
    ]
    assert materials["technology"][5]["label"] == "Практика прошлых периодов"
    # Письмо и DOCX — по черновику: статьи в его порядке, за ними сигналы.
    assert analyst.issue()[:2] == [("article", a["mid"]), ("article", a["hi"])]

    text = " ".join(pdf_pages(analyst.preview_pdf()))
    order = [text.index(title) for _, title in _rubrics(materials["business"]) + _rubrics(materials["technology"])]
    assert order == sorted(order)
    assert "СПРАВОЧНАЯ ПРАКТИКА / Практика прошлых периодов" in text
    assert "РЫНОК АВТОМАТИЗАЦИИ" in text and "БЕЗОПАСНОСТЬ" in text


def test_label_comes_from_the_card_event_type_with_the_special_cases_of_the_spec(issue, chromium):
    """Подпись после рубрики говорит о стадии события: по типу события «Карточки» (таблица спеки), масштабирование при
    зрелости «масштабное внедрение» — «Масштабное применение», «нет события» — «Рыночный контекст» в бизнес-разделе и
    «Обзор практики» в технологиях. Тип не определён — подписи нет, и «/» в строке рубрики не висит."""
    analyst = issue["analyst"]
    cases = {
        "regulation": ("business", "регулирование", None, "Регуляторная инициатива"),
        "no-event-business": ("business", "нет события", None, "Рыночный контекст"),
        "scaled": ("technology", "масштабирование", "масштабное внедрение", "Масштабное применение"),
        "scaling": ("technology", "масштабирование", "коммерческое внедрение", "Масштабирование"),
        "field-trial": ("technology", "промысловые испытания", "пилот/ОПИ", "Промысловый кейс"),
        "no-event-tech": ("technology", "нет события", "неприменимо", "Обзор практики"),
        "undefined": ("technology", "не определено", None, ""),
        "unfilled": ("technology", None, None, ""),
    }
    ids = {}
    for index, (key, (category, event_type, maturity, _)) in enumerate(cases.items()):
        ids[key] = _finding(key, f"Событие {key}", signal_category=category, theme=DRILLING, score=90 - index,
                            event_type=event_type, technology_maturity=maturity)
        analyst.mark_signal(ids[key])

    labels = {item["signal_id"]: item["label"] for items in analyst.materials().values() for item in items}
    assert {key: labels[ids[key]] for key in cases} == {key: case[3] for key, case in cases.items()}

    text = " ".join(pdf_pages(analyst.preview_pdf()))
    assert "БУРЕНИЕ / Регуляторная инициатива" in text and "БУРЕНИЕ / Обзор практики" in text
    assert "БУРЕНИЕ / Событие" not in text  # чужая подпись не выдумана
    assert "БУРЕНИЕ Событие undefined" in text and "БУРЕНИЕ Событие unfilled" in text


def test_every_event_type_of_the_card_dictionary_has_a_label():
    """Справочник типов события «Карточки» пополнится, а таблица подписей отстанет: карточка молча останется без
    подписи. Каждое значение справочника есть в таблице (у «нет события» подпись по разделу)."""
    from oiltech_digest.processing import digest_materials
    from oiltech_digest.signal_card_pack import load_dictionaries

    for event_type in load_dictionaries()["event_type"]["values"]:
        for section in (digest_materials.BUSINESS, digest_materials.TECHNOLOGY):
            assert digest_materials.label_of(event_type, None, section), (event_type, section)


def test_card_shows_five_criteria_from_its_score_breakdown_and_the_link_with_oilfield_services(issue, chromium):
    """Таблица под карточкой: пять баллов из разбивки балла сигнала, той же, что на карточке витрины, имена из снимка
    набора на момент оценки, целые от 0 до 100; «Свежесть» без даты события «не входит»; без разбивки «нет оценки по
    критериям» под именами критериев профиля раздела; итога в таблице нет. Связь: «прямая», «переносимая», пусто — «не
    определено»."""
    from oiltech_digest.processing.seed import seed_default_scoring_criteria

    seed_default_scoring_criteria()
    analyst, a = issue["analyst"], issue["a"]
    renamed = breakdown("tech_radar", 84.6, 60, 75.4, 101, None)
    renamed["criteria_snapshot"][0]["name"] = "Ценность (имя на момент оценки)"
    scored = _finding("scored", "Оценённая находка", signal_category="technology", theme=DRILLING, score=80,
                      oilfield_relevance="transferable", **renamed)
    bare = _finding("bare", "Находка без разбивки", signal_category="technology", theme=DRILLING, score=70)
    set_card(feed_signal(a["hi"]), theme=MARKET, oilfield_relevance="direct", **breakdown("business", 90, 80, 70, 60, 50))
    analyst.mark(a["hi"])
    for signal_id in (scored, bare):
        analyst.mark_signal(signal_id)

    materials = analyst.materials()
    [article] = materials["business"]
    first, second = materials["technology"]
    assert [(row["name"], row["score"], row["excluded"]) for row in first["criteria"]] == [
        ("Ценность (имя на момент оценки)", 85, False), ("Технологическая новизна", 60, False),
        ("Зрелость / доказательность", 75, False), ("Переносимость", 100, False), ("Свежесть", None, True),
    ]
    assert (first["relevance"], second["relevance"], article["relevance"]) == ("переносимая", "не определено", "прямая")
    assert second["criteria"] == []
    assert [row["score"] for row in article["criteria"]] == [90, 80, 70, 60, 50]

    # Имена критериев переносятся внутри узких столбцов, и чтение PDF склеивает слова на переносе: строки таблицы
    # сверяются без пробелов.
    pages = ["".join(text.split()) for text in pdf_pages(analyst.preview_pdf())]
    assert _flat("Ценность (имя на момент оценки) Технологическая новизна Зрелость / доказательность Переносимость "
                 "Свежесть Связь с нефтесервисом 85 60 75 100 не входит переносимая") in pages[2]
    # Без разбивки — имена набора раздела (технологии: «Технологический радар») и «нет оценки по критериям».
    assert _flat("Ценность для нефтесервиса Технологическая новизна Зрелость / доказательность Переносимость Свежесть "
                 "Связь с нефтесервисом нет оценки по критериям не определено") in pages[2]
    assert _flat("Стратегическая значимость для нефтесервиса") in pages[1] and _flat("90 80 70 60 50 прямая") in pages[1]
    assert "Итог" not in "".join(pages)


def _flat(text: str) -> str:
    return "".join(text.split())


def test_summary_is_printed_whole_and_an_empty_field_leaves_no_empty_caption(issue, chromium):
    """Суть целиком, без обрезки до 250 знаков, как у письма. Незаполненное поле не оставляет пустой подписи: PDF
    собирается и с ним."""
    analyst = issue["analyst"]
    long_summary = " ".join(f"Предложение номер {n} о новой технологии бурения на промысле." for n in range(1, 13))
    whole = _finding("whole", "Длинная суть", signal_category="technology", theme=DRILLING, summary=long_summary)
    empty = _finding("empty", "Пустая суть", signal_category="technology", theme=DRILLING, summary="",
                     thesis="")
    for signal_id in (whole, empty):
        analyst.mark_signal(signal_id)

    materials = {item["signal_id"]: item for item in analyst.materials()["technology"]}
    assert materials[whole]["summary"] == long_summary and len(long_summary) > 600
    assert materials[empty]["summary"] == ""
    text = " ".join(pdf_pages(analyst.preview_pdf()))
    assert "Предложение номер 12 о новой технологии бурения на промысле." in text
    assert "Суть ещё не сформирована" not in text


def test_title_and_read_link_lead_to_the_main_publication_and_nothing_links_inside_the_pdf(issue, chromium):
    """Заголовок и «Читать материал» ведут на главную публикацию: у статьи ленты — на неё саму, у находки радара — на
    лучшую ссылку. У материала коллеги без адреса заголовок без ссылки и «Читать материал» нет. Переходов внутри PDF,
    «Стр. N» и содержания нет."""
    analyst, a = issue["analyst"], issue["a"]
    feed_signal(a["hi"])
    finding = _finding("linked", "Находка со ссылкой", signal_category="technology", theme=DRILLING)
    material = repository.upsert_signal({"signal_key": "material:test-1", "title": "Материал коллеги без адреса",
                                         "theme": "Не классифицировано / новая тема", "summary": "Описание события."})
    set_card(material, origin="material", first_seen_at=msk(2026, 9, 14, 10, 0))
    analyst.mark(a["hi"])
    for signal_id in (finding, material):
        analyst.mark_signal(signal_id)

    urls = {issue_key(item): item["url"] for items in analyst.materials().values() for item in items}
    assert urls == {("article", a["hi"]): "https://news.example.org/hi",
                    ("signal", finding): "https://radar.example.org/linked", ("signal", material): ""}

    pdf = analyst.preview_pdf()
    links = [target for page in pdf_links(pdf) for target in page]
    assert set(links) == {"https://news.example.org/hi", "https://radar.example.org/linked"}
    assert "#внутренняя" not in links
    text = " ".join(pdf_pages(pdf))
    assert text.count("Читать материал") == 2
    assert "Материал коллеги без адреса" in text
    assert "Стр." not in text and "Содержание" not in text


def test_card_shows_its_own_fit_picture_otherwise_the_placeholder_of_its_theme(issue, chromium, image_host,
                                                                               monkeypatch):
    """Своя картинка публикации годна, если она не уже 400 px и не вытянута сильнее 3:1 (иначе это логотип или
    полоска). Без своей, с негодной и с не загрузившейся при сборке PDF карточка показывает заглушку своей темы."""
    # Локальный адрес сборщик считает тестовым и сам меняет на заглушку: здесь сайт издателя локальный.
    monkeypatch.setattr(digest_module, "_is_unusable_digest_image_url", lambda url: not url)
    analyst = issue["analyst"]
    with connection.get_connection() as conn:
        articles = {
            name: add_article(conn, issue["source"], name, published=utc(2026, 9, 9 + index, 12), score=90 - index,
                              image_url=f"{image_host}/{name}.png")
            for index, name in enumerate(("fit", "narrow", "strip", "refused"))
        }
        conn.commit()
    for article_id in articles.values():
        set_card(feed_signal(article_id), theme=DRILLING)
        analyst.mark(article_id)
    finding = _finding("no-picture", "Находка без картинки", signal_category="technology", theme=ENERGY)
    analyst.mark_signal(finding)

    materials = {issue_key(item): item for items in analyst.materials().values() for item in items}
    pictures = {key: (item["image_own"], item["placeholder"]) for key, item in materials.items()}
    assert pictures == {("article", articles["fit"]): (True, "drilling"),
                        ("article", articles["narrow"]): (True, "drilling"),
                        ("article", articles["strip"]): (True, "drilling"),
                        ("article", articles["refused"]): (True, "drilling"),
                        ("signal", finding): (False, "energy")}

    pdf = analyst.preview_pdf()
    # На листах карточек из своих картинок осталась только годная 800×500 (Chromium кладёт её обрезанной под рамку
    # 4:3, высота та же), прочие нарисованы вектором заглушки.
    assert [height for page in pdf_images(pdf)[1:] for _, height in page] == [500]
    # Что нарисовано на месте картинки каждой карточки, по порядку карточек: подпись рисунка в размеченном PDF.
    assert [alt for alt in pdf_figures(pdf) if alt] == [
        "Картинка публикации", "Рисунок темы: бурение", "Рисунок темы: бурение", "Рисунок темы: бурение",
        "Рисунок темы: энергетика",
    ]


def test_every_theme_has_its_own_placeholder_picture():
    """13 заглушек SVG, по одной на тему (тема равна корневому тегу), лежат рядом со шрифтами выпуска."""
    import xml.etree.ElementTree as ElementTree

    from oiltech_digest.processing import digest_materials, digest_pdf

    keys = [theme.placeholder for theme in digest_materials.THEMES]
    assert len(keys) == len(set(keys)) == 13
    assert sorted(path.stem for path in digest_pdf.PLACEHOLDERS_DIR.glob("*.svg")) == sorted(keys)
    for key in keys:
        root = ElementTree.parse(digest_pdf.PLACEHOLDERS_DIR / f"{key}.svg").getroot()
        assert root.tag == "{http://www.w3.org/2000/svg}svg" and root.get("viewBox") == "0 0 400 300"


def test_refresh_pdf_rebuilds_past_the_preview_memory_and_replaces_the_remembered_file(
    issue, chromium, image_host, monkeypatch,
):
    """LOW-2 ревью PR #155. При первой сборке сайт картинки не ответил, в PDF заглушка. Сайт ожил, человек жмёт
    «Обновить PDF»: содержание выпуска то же, и память предпросмотра отдавала прежний файл, пока не сменится состав,
    а выгрузка уже шла с картинкой. «Обновить PDF» собирает заново мимо памяти, и новый файл заменяет прежний."""
    from collections import OrderedDict

    monkeypatch.setattr(digest_module, "_is_unusable_digest_image_url", lambda url: not url)
    monkeypatch.setattr(digest_module, "_PREVIEW_PDF_CACHE", OrderedDict())
    analyst = issue["analyst"]
    with connection.get_connection() as conn:
        article = add_article(conn, issue["source"], "revives", published=utc(2026, 9, 9, 12),
                              image_url=f"{image_host}/revives.png")
        conn.commit()
    set_card(feed_signal(article), theme=DRILLING)
    analyst.mark(article)

    def own_pictures(pdf: bytes) -> int:
        return sum(len(page) for page in pdf_images(pdf)[1:])

    assert own_pictures(analyst.preview_pdf()) == 0
    image_host.revive()
    assert own_pictures(analyst.preview_pdf()) == 0  # тот же выпуск — из памяти предпросмотра

    assert own_pictures(analyst.preview_pdf(fresh=1)) == 1
    assert own_pictures(analyst.preview_pdf()) == 1
    assert own_pictures(analyst.export("pdf")) == 1


def test_article_without_a_feed_signal_goes_as_a_signal_without_category_by_its_root_tag(issue, chromium):
    """Статья, собранная до 01.08.2026, сигнала ленты не имеет (бывает только в закрытых месяцах): она не ломает
    сборку и идёт как сигнал без категории, то есть в технологии, а рубрика — по её корневому тегу (тег статьи
    дочерний, корень — тема)."""
    analyst, a = issue["analyst"], issue["a"]
    with connection.get_connection() as conn:
        root = conn.execute("INSERT INTO tags (name) VALUES (%s) RETURNING id", (DRILLING,)).fetchone()[0]
        child = conn.execute("INSERT INTO tags (name, parent_id) VALUES ('Буровые растворы', %s) RETURNING id",
                             (root,)).fetchone()[0]
        conn.execute("INSERT INTO article_tags (article_id, tag_id) VALUES (%s, %s)", (a["mid"], child))
        conn.commit()
    analyst.mark(a["mid"])

    [material] = analyst.materials()["technology"]
    assert (issue_key(material), material["rubric"], material["label"], material["criteria"],
            material["relevance"]) == (("article", a["mid"]), "БУРЕНИЕ", "", [], "не определено")
    assert "БУРЕНИЕ Материал mid" in pdf_pages(analyst.preview_pdf())[1]


def test_card_is_not_split_between_sheets(issue, chromium):
    """Карточка между листами не разрывается: заголовок, суть и таблица карточки на одном листе."""
    analyst = issue["analyst"]
    summary = " ".join(f"Фраза {n} о внедрении технологии на промысле и её результатах." for n in range(1, 6))
    ids = []
    for n in range(1, 8):
        # У каждой карточки своё имя первого критерия: по нему видно, на каком листе её таблица.
        scores = breakdown("tech_radar", 80, 70, 60, 50, 40)
        scores["criteria_snapshot"][0]["name"] = f"Ценность карточки {n}"
        ids.append(_finding(f"card-{n}", f"Карточка номер {n}", signal_category="technology", theme=DRILLING,
                            score=90 - n, summary=f"{summary} Конец карточки {n}.", **scores))
    for signal_id in ids:
        analyst.mark_signal(signal_id)

    pages = pdf_pages(analyst.preview_pdf())
    for n in range(1, 8):
        [sheet] = [index for index, text in enumerate(pages) if f"Карточка номер {n}" in text]
        assert f"Конец карточки {n}." in pages[sheet], n
        assert _flat(f"Ценность карточки {n}") in _flat(pages[sheet]), n
    assert len(pages) > 2  # карточки заняли больше одного листа: разрыв был возможен
