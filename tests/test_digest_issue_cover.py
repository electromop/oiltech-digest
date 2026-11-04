"""Выпуск в формате эксперта (#161, спека #160): обложка и документ PDF.

Обложка занимает первый лист целиком: полоса (логотип и подпись справа) и вкладка на баннере из брендинга на сервере,
вступление, «Главное в выпуске» строкой на раздел и блок платформы. Значения брендинга и файлы баннера, логотипа и QR
живут только на сервере: в репозитории их нет, тесты берут заглушки. Колонтитул со второго листа, номер «N / M» на
каждом, закладки на два раздела, название и тема документа. Материалы и карточки: test_digest_issue_pdf.py.

Часы окна ленты заморожены на 29.09 12:00 МСК: сентябрь открыт.
"""

from __future__ import annotations

import json

import pytest

from oiltech_digest import config
from tests.digest_pdf_support import pdf_document, pdf_image_boxes, pdf_links, pdf_pages, png
from tests.digest_user_path_support import (AUTOMATION, DRILLING, ENERGY, MARKET, add_signal, feed_signal, msk,
                                            set_card, utc)
from tests.digest_user_path_support import chromium, issue  # noqa: F401 — фикстуры

# Заглушки файлов брендинга на сервере: баннер, логотип полосы, QR (размер в точках).
BANNER, LOGO, QR = (1180, 600), (640, 128), (330, 330)


@pytest.fixture()
def branding(tmp_path, monkeypatch):
    """Брендинг на сервере — заглушки: файл баннера, файл логотипа и подпись справа для полосы, вкладка, вступление,
    блок платформы (ссылка, Telegram, файл QR, адрес обратной связи, подпись блока). Файлы лежат рядом с файлом
    брендинга."""
    folder = tmp_path / "branding"
    folder.mkdir()
    for name, size in (("banner.png", BANNER), ("logo.png", LOGO), ("qr.png", QR)):
        (folder / name).write_bytes(png(*size))
    path = folder / "digest_branding.json"
    path.write_text(json.dumps({"cover": {
        "banner_file": "banner.png", "logo_file": "logo.png", "masthead_caption": "Подпись полосы",
        "tab_text": "Вкладка",
        "intro": "Вступление выпуска для читателей, постоянный текст обложки.",
        "platform_title": "Блок платформы выпуска",
        "platform_url": "https://platform.example.org", "telegram_url": "https://t.me/example_channel",
        "qr_file": "qr.png", "feedback_email": "feedback@example.org", "platform_note": "Подпись блока платформы",
    }}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "DIGEST_BRANDING_PATH", str(path))
    return path


@pytest.fixture()
def empty_branding(tmp_path, monkeypatch):
    """Брендинга на сервере нет: выпуск читает брендинг из репозитория, а в нём значения обложки пусты."""
    monkeypatch.setattr(config, "DIGEST_BRANDING_PATH", str(tmp_path / "server" / "digest_branding.json"))


def _edit_branding(path, **cover) -> None:
    """Правка брендинга на сервере: поля обложки; `issue` — блок выпуска."""
    data = json.loads(path.read_text(encoding="utf-8"))
    issue = cover.pop("issue", None)
    data["cover"].update(cover)
    if issue is not None:
        data["issue"] = issue
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _issue_of_three(issue) -> None:
    """Сентябрьский выпуск: статья ленты в бизнесе, две находки радара в технологиях (одна — давнее событие)."""
    analyst, a = issue["analyst"], issue["a"]
    set_card(feed_signal(a["hi"]), theme=MARKET)
    analyst.mark(a["hi"])
    for key, theme, event_date in (("drill", DRILLING, None), ("energy", ENERGY, "2026-01-15")):
        signal_id = add_signal(key, first_seen=msk(2026, 9, 12, 0, 20), evidence_published=utc(2026, 9, 11, 9),
                               title=f"Находка {key}")
        set_card(signal_id, signal_category="technology", theme=theme, event_date=event_date)
        analyst.mark_signal(signal_id)


def test_cover_takes_the_masthead_tab_intro_and_platform_block_from_the_branding_on_the_server(
    issue, chromium, branding,
):
    _issue_of_three(issue)
    pdf = issue["analyst"].preview_pdf()

    cover = pdf_pages(pdf)[0]
    # Подпись полосы и вкладку набор ставит прописными, как у образца.
    for text in ("ПОДПИСЬ ПОЛОСЫ", "ВКЛАДКА", "Вступление выпуска для читателей", "Блок платформы выпуска",
                 "Эти и другие сигналы доступны бесплатно на платформе platform.example.org", "Перейти в Telegram",
                 "feedback@example.org", "Подпись блока платформы", "Telegram-канал"):
        assert text in cover, text
    # Адрес корня сайта Chromium пишет с «/» на конце.
    assert set(pdf_links(pdf)[0]) == {"https://platform.example.org/", "https://t.me/example_channel",
                                      "mailto:feedback@example.org"}
    # Баннер, логотип полосы и QR — с сервера.
    assert sorted(size for size, _, _ in pdf_image_boxes(pdf)[0]) == sorted([BANNER, LOGO, QR])


def test_without_branding_the_cover_has_neither_masthead_nor_tab_nor_platform_block(issue, chromium, empty_branding):
    """Символика заказчика по умолчанию не попадает ни в репозиторий, ни на сайт: пустое поле — элемента нет, при
    пустом брендинге нет ни полосы, ни вкладки. Баннера в репозитории для PDF нет (у баннера письма вкладка нарисована
    на самой картинке): без файла на сервере обложка без баннера, а «Главное» на месте."""
    _issue_of_three(issue)
    pdf = issue["analyst"].preview_pdf()

    cover = pdf_pages(pdf)[0]
    assert "ГЛАВНОЕ В СЕНТЯБРЬСКОМ ВЫПУСКЕ" in cover
    for text in ("Новости", "НОВОСТИ", "Платформа", "Telegram", "Обратная связь"):
        assert text not in cover, text
    assert pdf_links(pdf)[0] == []
    assert pdf_image_boxes(pdf)[0] == []


def test_cover_fits_exactly_one_sheet_and_the_banner_spans_its_full_width(issue, chromium, branding):
    """Обложка фиксированной высоты, ровно в лист: на второй лист не переходит. Если обложка выше листа хоть немного,
    Chromium ужимает весь первый лист, и баннер перестаёт доходить до края (на макете 198 мм вместо 210). Длинное
    вступление обложку не растягивает: обрезается оно само, а «Главное» и блок платформы остаются целиком."""
    _edit_branding(branding,
                   intro=" ".join(["Очень длинное вступление обложки выпуска, которое не помещается в лист."] * 30))
    _issue_of_three(issue)
    pdf = issue["analyst"].preview_pdf()

    document, pages = pdf_document(pdf), pdf_pages(pdf)
    # A4 у Chromium — 594,96 × 841,92 pt, у всех листов один размер.
    [(sheet_width, sheet_height)] = document["sizes"]
    assert abs(sheet_width - 595.3) < 0.5 and abs(sheet_height - 841.9) < 0.5
    assert len(pages) == 3
    assert "01 Бизнес-сигналы" in pages[1]
    for text in ("ГЛАВНОЕ В СЕНТЯБРЬСКОМ ВЫПУСКЕ", "Технологические сигналы Бурение, энергетика.", "Обратная связь",
                 "feedback@example.org", "Подпись блока платформы"):
        assert text in pages[0], text
    # Баннер 210 мм от левого края и не уже листа (сжатая обложка дала бы около 561 pt, 198 мм).
    [(_, width, left)] = [box for box in pdf_image_boxes(pdf)[0] if box[0] == BANNER]
    assert left == 0.0 and width >= sheet_width


def test_running_header_from_the_second_sheet_numbers_on_every_sheet_bookmarks_title_and_subject(
    issue, chromium, empty_branding,
):
    """Колонтитул со второго листа: название слева, месяц справа; номер «N / M» на всех листах. Закладки на два
    раздела; название документа «Нефтесервисный дайджест. Сентябрь 2026», тема — числа и склонения считаются."""
    _issue_of_three(issue)
    pdf = issue["analyst"].preview_pdf()

    pages = pdf_pages(pdf)
    assert "1 / 3" in pages[0] and "НЕФТЕСЕРВИСНЫЙ ДАЙДЖЕСТ" not in pages[0]
    for number, text in enumerate(pages[1:], start=2):
        assert "НЕФТЕСЕРВИСНЫЙ ДАЙДЖЕСТ" in text and "Сентябрь 2026" in text and f"{number} / 3" in text
    document = pdf_document(pdf)
    assert document["bookmarks"] == [("Бизнес-сигналы", 2), ("Технологические сигналы", 3)]
    assert document["title"] == "Нефтесервисный дайджест. Сентябрь 2026"
    assert document["subject"] == "3 материала: 1 бизнес-сигнал и 2 технологических сигнала"
    # Набор как у образца эксперта: шрифт выпуска в трёх начертаниях, другого нет.
    assert document["fonts"] == {"GPN_DIN-Regular", "GPN_DIN-Bold", "GPN_DINCondensed-Bold"}


def test_highlights_default_to_the_rubrics_of_each_section_one_row_per_section(issue, chromium, empty_branding):
    """«Главное в выпуске» по умолчанию: перечень рубрик раздела, строка на раздел (правка человека поверх него:
    test_digest_issue_edits.py). У давнего события в перечне его тема, а не «справочная практика»."""
    _issue_of_three(issue)
    analyst = issue["analyst"]
    automation = add_signal("auto", first_seen=msk(2026, 9, 13, 0, 20), evidence_published=utc(2026, 9, 12, 9),
                            title="Рынок автоматизации")
    set_card(automation, signal_category="business", theme=AUTOMATION)
    analyst.mark_signal(automation)

    cover = analyst.issue_content()["cover"]
    assert cover["highlights_title"] == "Главное в сентябрьском выпуске"
    assert [(row["label"], row["text"], row["edited"]) for row in cover["highlights"]] == [
        ("Бизнес-сигналы", "Рынок и сервисные модели, рынок автоматизации.", False),
        ("Технологические сигналы", "Бурение, энергетика.", False),
    ]
    text = pdf_pages(analyst.preview_pdf())[0]
    assert "Бизнес-сигналы Рынок и сервисные модели, рынок автоматизации." in text
    assert "Технологические сигналы Бурение, энергетика." in text


@pytest.mark.parametrize(("business", "technology", "subject"), [
    (5, 16, "21 материал: 5 бизнес-сигналов и 16 технологических сигналов"),
    (1, 1, "2 материала: 1 бизнес-сигнал и 1 технологический сигнал"),
    (2, 3, "5 материалов: 2 бизнес-сигнала и 3 технологических сигнала"),
    (11, 0, "11 материалов: 11 бизнес-сигналов"),
    (0, 22, "22 материала: 22 технологических сигнала"),
    (0, 0, "0 материалов"),
])
def test_document_subject_counts_materials_with_russian_plurals(business, technology, subject):
    from oiltech_digest.processing import digest_materials

    sections = [{"key": "business", "materials": [{}] * business},
                {"key": "technology", "materials": [{}] * technology}]
    assert digest_materials.document("2026-10", sections, name="Нефтесервисный дайджест") == {
        "name": "Нефтесервисный дайджест", "month_label": "Октябрь 2026",
        "title": "Нефтесервисный дайджест. Октябрь 2026", "subject": subject,
    }


@pytest.mark.parametrize("name", ["../logo.png", ".logo.png", "logo.txt", "missing.png"])
def test_branding_file_outside_its_folder_or_not_a_picture_is_not_taken(issue, chromium, branding, name):
    """Файлы обложки брендинг называет по имени, а правит его админ: выпуск берёт только картинку из каталога
    брендинга. Путь наружу, скрытый файл, не картинка и отсутствующий файл не попадают в PDF, а PDF собирается."""
    (branding.parent.parent / "logo.png").write_bytes(png(*LOGO))
    (branding.parent / ".logo.png").write_bytes(png(*LOGO))
    (branding.parent / "logo.txt").write_text("не картинка", encoding="utf-8")
    _edit_branding(branding, logo_file=name)
    _issue_of_three(issue)

    pdf = issue["analyst"].preview_pdf()
    assert sorted(size for size, _, _ in pdf_image_boxes(pdf)[0]) == sorted([BANNER, QR])
    assert "ПОДПИСЬ ПОЛОСЫ" in pdf_pages(pdf)[0]


def test_issue_name_from_the_branding_cannot_inject_markup_into_the_pdf(issue, chromium, branding):
    """Название выпуска (брендинг, правит админ) стоит в колонтитуле, а колонтитул задаёт стиль листа: «</style>» в
    названии закрыл бы стиль, и разметка после него исполнилась бы в Chromium сервера. Название печатается как текст."""
    injected = 'Дайджест </style><a href="https://evil.example/">чужая ссылка</a>'
    _edit_branding(branding, issue={"title_template": injected})
    _issue_of_three(issue)

    pdf = issue["analyst"].preview_pdf()
    assert not any("evil.example" in target for page in pdf_links(pdf) for target in page)
    assert '</STYLE><A HREF="HTTPS://EVIL.EXAMPLE/">' in pdf_pages(pdf)[1]


def test_tab_stands_only_on_the_banner(issue, chromium, branding):
    """Вкладка стоит на баннере: без файла баннера на сервере её не на чем ставить, и она легла бы на вступление."""
    _edit_branding(branding, banner_file="")
    _issue_of_three(issue)

    cover = pdf_pages(issue["analyst"].preview_pdf())[0]
    assert "ВКЛАДКА" not in cover
    assert "ПОДПИСЬ ПОЛОСЫ" in cover and "Вступление выпуска для читателей" in cover
