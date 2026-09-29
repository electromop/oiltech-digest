"""Сентябрьский выпуск собирают сами пользователи (решение владельца 29.09): выгрузка.

Кнопки PDF / DOCX / HTML экрана выпуска: фоновая задача, файл, скачивание. HTML и DOCX
рендерятся прямо в тесте; PDF — на настоящем Chromium (без него тест пропускается).
Сборка выпуска — test_digest_user_path.py.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from io import BytesIO
from zipfile import ZipFile

from oiltech_digest.db import connection
from oiltech_digest.processing import digest as digest_module
from tests.digest_user_path_support import SEPT, add_article, add_signal, issue_key, msk, pdf_content, utc
from tests.digest_user_path_support import chromium, issue, silent_image_host  # noqa: F401 — фикстуры


def test_export_of_a_45_item_issue_in_html_and_docx_matches_the_preview(issue):
    analyst = issue["analyst"]
    with connection.get_connection() as conn:
        big = [
            # Номер с нулём: «big-1» не должен находиться внутри «big-10».
            add_article(
                conn, issue["source"], f"big-{n:02d}",
                published=None if n % 10 == 0 else utc(2026, 9, 1 + n % 28, 12),
                collected=utc(2026, 9, 1 + n % 28, 13),
                score=None if n % 7 == 0 else 20 + n,
                image_url="" if n % 3 else f"https://img.example.org/{n}.jpg",
            )
            for n in range(40)
        ]
        conn.commit()
    signals = [
        add_signal(f"big-{n}", first_seen=msk(2026, 9, 2 + n, 0, 20), score=30 + n,
                   evidence_published=None if n % 2 else utc(2026, 9, 1 + n, 9))
        for n in range(5)
    ]
    for article_id in big:
        analyst.mark(article_id)
    for signal_id in signals:
        analyst.mark_signal(signal_id)

    preview = analyst.issue_items()
    assert len(preview) == 45
    assert {issue_key(item) for item in preview} == {("article", i) for i in big} | {("signal", i) for i in signals}

    html = analyst.export("html").decode("utf-8")
    assert html.count('class="news-card"') == 45
    for item in preview:
        assert item["title"] in html
    # Chromium выбрасывает из PDF относительные ссылки (урок выпуска за август): у каждой
    # карточки ссылка «Читать далее» — абсолютная.
    links = re.findall(r'<a href="([^"]*)" style="color:#e83d08', html)
    assert len(links) == 45 and all(link.startswith("https://") for link in links)
    # Порядок выгрузки — порядок превью (тот же сборщик).
    positions = [html.index(item["title"]) for item in preview]
    assert positions == sorted(positions)

    docx = analyst.export("docx")
    with ZipFile(BytesIO(docx)) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")
    for item in preview:
        assert item["title"] in document_xml


def test_issue_header_names_the_month_in_words(issue):
    """Дефект до 29.09. Заказчик 24.08 (#428915) в выпуске за август: «за 2026-08» → «за
    август 2026 г», и в заголовке выпуска. Август собирали скриптом с этой правкой, а продукт
    так и писал «2026-09» — первый выпуск, собранный самими пользователями, повторил бы
    замечание."""
    analyst = issue["analyst"]
    analyst.mark(issue["a"]["hi"])

    content = analyst.get("/api/digest-content", params={"month": SEPT, "limit": 500, "min_score": 0}).json()
    assert "обзоры за сентябрь 2026 г," in content["issue"]["intro"]
    assert content["title"] == "Нефтесервисный дайджест · сентябрь 2026 г"
    # Для имён файлов и запросов месяц прежний.
    assert content["month"] == SEPT

    html = analyst.export("html").decode("utf-8")
    assert "обзоры за сентябрь 2026 г," in html
    assert "2026-09" not in html


class _SameSecond(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 3, 12, 0, 0, tzinfo=tz)


def test_exports_of_two_people_in_the_same_second_do_not_overwrite_each_other(issue, monkeypatch):
    """Дефект до 29.09 (п. 5, изоляция). Файл выгрузки назывался digest-<месяц>-<секунда>:
    две выгрузки одного месяца в одну секунду (двадцать человек собирают сентябрь до 05.10,
    HTML готовится за доли секунды) писали в один файл — и первый скачивал выпуск второго."""
    analyst, colleague, a = issue["analyst"], issue["colleague"], issue["a"]
    analyst.mark(a["hi"])
    colleague.mark(a["low"])
    monkeypatch.setattr(digest_module, "datetime", _SameSecond)

    mine = analyst.start_export("html")
    theirs = colleague.start_export("html")

    mine_html = analyst.download(mine).decode("utf-8")
    theirs_html = colleague.download(theirs).decode("utf-8")
    assert "Материал hi" in mine_html and "Материал low" not in mine_html
    assert "Материал low" in theirs_html and "Материал hi" not in theirs_html


def test_pdf_of_40_items_is_printed_although_an_article_image_never_answers(chromium, silent_image_host, monkeypatch):
    """Дефект до 29.09. Картинки статей Chromium грузит сам, с РФ-ядра. Замер 29.09: 40 позиций
    без внешних картинок — PDF за 1,9 с; две «молчащие» картинки из сорока — set_content(
    wait_until="load") ждал до потолка Playwright и через 30 с падал TimeoutError: выгрузка PDF
    не получалась ни с одной из трёх попыток задачи. Теперь картинки ждём ограниченное время,
    недогрузившуюся меняем на плашку рубрики — как у статьи без картинки."""
    # Локальный адрес сборщик считает «тестовым» и сам меняет на плашку — здесь пропускаем его.
    monkeypatch.setattr(digest_module, "_is_unusable_digest_image_url", lambda url: not url)
    monkeypatch.setattr(digest_module, "_PDF_IMAGES_WAIT_MS", 2000)
    content = pdf_content(40, {3: f"{silent_image_host}/a.jpg", 17: f"{silent_image_host}/b.jpg"})

    started = time.monotonic()
    pdf = digest_module.render_digest_pdf(content)
    elapsed = time.monotonic() - started

    assert pdf.startswith(b"%PDF")
    assert elapsed < 20, f"PDF печатался {elapsed:.1f} с"


def test_docx_spends_a_bounded_time_on_silent_article_images(silent_image_host, monkeypatch):
    """Дефект до 29.09. Картинки для Word сервер тянет сам, по одной, с таймаутом 8 с на запрос:
    замер 29.09 — 40 позиций без картинок 0,1 с, две молчащие картинки из сорока — 16,2 с.
    Экран ждёт документ не дольше 160 с: от двадцати молчащих картинок выгрузка на экране
    обрывалась. Теперь на все картинки общий бюджет, остальные карточки идут без картинки."""
    monkeypatch.setattr(digest_module, "_DOCX_IMAGES_BUDGET_SECONDS", 2.0)
    content = pdf_content(40, {n: f"{silent_image_host}/{n}.jpg" for n in (0, 9, 18, 27)})

    started = time.monotonic()
    docx = digest_module.render_digest_docx(content)
    elapsed = time.monotonic() - started

    with ZipFile(BytesIO(docx)) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")
    assert all(f"Материал {n:02d}" in document_xml for n in range(1, 41))
    assert elapsed < 10, f"DOCX собирался {elapsed:.1f} с"
