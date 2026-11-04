"""Плашка «Технология / Бизнес» у элемента выпуска и предпросмотр PDF в окне (решения владельца 23.10).

Категория элемента — категория сигнала: у статьи — её сигнала ленты, у находки радара — своя. Плашка видна и на
экране сборки, и в самом выпуске: письмо и PDF (одна вёрстка), DOCX.
"""

from collections import OrderedDict
from datetime import datetime, timezone
from io import BytesIO
import threading
import time

from docx import Document
from fastapi.testclient import TestClient
import pytest

from oiltech_digest import api
from oiltech_digest.db import repository
from oiltech_digest.processing import digest, digest_pdf
from oiltech_digest.processing.digest import build_digest_content, render_digest_docx, render_digest_export_html


def _row(row_id, *, item_type="article", summary="Суть материала."):
    return {
        "id": row_id, "item_type": item_type, "title": f"{item_type} {row_id}", "source_name": "World Oil",
        "url": f"https://example.com/{item_type}/{row_id}", "published_at": None, "tag_name": "Бурение",
        "parent_tag_name": None, "total_score": 80, "score_label": "High", "summary": summary, "image_url": "",
    }


def _stub(monkeypatch, rows, kinds=({}, {})):
    monkeypatch.setattr(digest.repository, "get_monthly_digest", lambda month: None)
    monkeypatch.setattr(digest.repository, "digest_candidates", lambda month, limit=20, min_score=60, **kw: rows)
    monkeypatch.setattr(digest.repository, "digest_item_kinds", lambda articles, signals: kinds)
    monkeypatch.setattr(digest.repository, "issue_material_fields", lambda articles, signals: ({}, {}))
    # «Главное в выпуске», написанное человеком (#162), тоже из базы.
    monkeypatch.setattr("oiltech_digest.db.issue_edits_store.highlights", lambda month: {})
    monkeypatch.setattr(digest.repository, "list_enabled_scoring_criteria", lambda profile: [])


def test_items_carry_kind_and_the_issue_shows_the_badge(monkeypatch):
    _stub(monkeypatch, [_row(1), _row(2, item_type="signal"), _row(3)], kinds=({1: "business"}, {2: "technology"}))

    content = build_digest_content("2026-10")

    # Сигнал радара — после статей (24.10), у статьи без сигнала ленты плашки нет.
    assert [(item["kind"], item["kind_label"]) for item in content["news"]] == [
        ("business", "Бизнес"), (None, None), ("technology", "Технология"),
    ]
    html = render_digest_export_html(content)
    assert html.count('class="news-card-kind"') == 2
    assert ">Бизнес</div>" in html and ">Технология</div>" in html
    text = "\n".join(paragraph.text for paragraph in Document(BytesIO(render_digest_docx(content))).paragraphs)
    assert "БИЗНЕС" in text and "ТЕХНОЛОГИЯ" in text


def test_summary_keeps_whole_sentences_that_fit_the_card(monkeypatch):
    # Суть ровно вмещается в площадь карточки (решение владельца 23.10): целые предложения, сколько входит.
    sentence = "Компания испытала новую систему на трёх скважинах месторождения."  # 65 знаков
    _stub(monkeypatch, [_row(1, summary=" ".join([sentence] * 5))])

    summary = build_digest_content("2026-10")["news"][0]["summary"]

    assert summary == " ".join([sentence] * 3)
    assert len(summary) <= 250 and not summary.endswith("…")


def test_kind_comes_from_the_feed_signal_of_the_article(isolated_db):
    with repository.get_connection() as conn:
        source_id = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy)"
            " VALUES ('Нефтегаз', 'Media', 'https://neftegaz.example', TRUE, 'rss') RETURNING id").fetchone()[0]
        article_id = conn.execute(
            "INSERT INTO articles (source_id, title, url, published_at, raw_text, language, content_hash)"
            " VALUES (%s, 'Статья', 'https://neftegaz.example/1', %s, 'текст', 'ru', 'kind-1') RETURNING id",
            (source_id, datetime(2026, 10, 1, tzinfo=timezone.utc))).fetchone()[0]
        feed_id = conn.execute(
            "INSERT INTO signals (signal_key, title, theme, origin, origin_article_id, signal_category)"
            " VALUES ('feed-1', 'Сигнал ленты', 'Бурение', 'feed', %s, 'business') RETURNING id",
            (article_id,)).fetchone()[0]
        radar_id = conn.execute(
            "INSERT INTO signals (signal_key, title, theme, origin)"
            " VALUES ('radar-1', 'Находка радара', 'Бурение', 'radar') RETURNING id").fetchone()[0]
        conn.commit()

    by_article, by_signal = repository.digest_item_kinds([article_id], [radar_id, feed_id])

    assert by_article == {article_id: "business"}
    # Находка радара без категории — технология, как на экране радара.
    assert by_signal == {radar_id: "technology", feed_id: "business"}


def test_preview_pdf_opens_inline_without_files_or_jobs(monkeypatch, tmp_path):
    _stub(monkeypatch, [_row(1)])
    monkeypatch.setattr(digest, "render_digest_pdf", lambda content: b"%PDF-1.4 preview")
    monkeypatch.setattr(digest, "_PREVIEW_PDF_CACHE", OrderedDict())
    monkeypatch.setattr(digest, "EXPORTS_DIR", tmp_path)
    api.app.dependency_overrides[api.require_user] = lambda: {"id": 1, "role": "user"}
    try:
        response = TestClient(api.app).get("/api/digest-preview.pdf", params={"month": "2026-10"})
    finally:
        api.app.dependency_overrides.pop(api.require_user, None)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline")
    assert response.content == b"%PDF-1.4 preview"
    assert list(tmp_path.iterdir()) == []


def test_preview_pdf_builds_one_issue_once_and_one_at_a_time(monkeypatch):
    """Ревью 24.10: предпросмотр собирает Chromium в процессе API. Тот же выпуск (возврат на вкладку, повторное
    открытие архива) отдаётся без новой сборки, разные — по одной за раз, а память — на четыре последних."""
    calls = []
    running = []

    def fake_render(content):
        running.append(1)
        assert len(running) == 1, "две сборки Chromium одновременно"
        time.sleep(0.05)
        calls.append(content["month"])
        running.pop()
        return f"%PDF {content['month']}".encode()

    monkeypatch.setattr(digest, "render_digest_pdf", fake_render)
    monkeypatch.setattr(digest, "_PREVIEW_PDF_CACHE", OrderedDict())
    issue = {"month": "2026-10", "news": [{"title": "Пилот"}]}

    results = []
    threads = [threading.Thread(target=lambda: results.append(digest.render_digest_preview_pdf(dict(issue)))) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [b"%PDF 2026-10"] * 3
    assert calls == ["2026-10"]

    # Сменилось содержание (черновик, отметка, фильтр) — новая сборка.
    digest.render_digest_preview_pdf({"month": "2026-10", "news": [{"title": "Другой"}]})
    assert calls == ["2026-10", "2026-10"]

    for month in ("2026-06", "2026-07", "2026-08", "2026-09"):
        digest.render_digest_preview_pdf({"month": month, "news": []})
    assert len(digest._PREVIEW_PDF_CACHE) == 4


def test_preview_pdf_refuses_readably_while_a_build_holds_the_lock(monkeypatch):
    """Ревью PR #155: у печати Chromium предела нет, зависшая сборка держит замок. Запрос ждёт не дольше предела и
    получает понятный отказ: иначе каждое открытие экрана навсегда занимало бы поток API, и на 40-м вставал весь API."""
    _stub(monkeypatch, [_row(1)])
    lock = threading.Lock()
    monkeypatch.setattr(digest, "_PREVIEW_PDF_LOCK", lock)
    monkeypatch.setattr(digest, "_PREVIEW_PDF_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(digest, "_PREVIEW_PDF_CACHE", OrderedDict())
    monkeypatch.setattr(digest, "render_digest_pdf", lambda content: b"%PDF-1.4 preview")
    api.app.dependency_overrides[api.require_user] = lambda: {"id": 1, "role": "user"}
    client = TestClient(api.app)
    try:
        with lock:
            refused = client.get("/api/digest-preview.pdf", params={"month": "2026-10"})
        served = client.get("/api/digest-preview.pdf", params={"month": "2026-10"})
    finally:
        api.app.dependency_overrides.pop(api.require_user, None)

    assert refused.status_code == 503
    assert refused.json()["detail"] == "PDF сейчас собирается. Повторите через минуту."
    assert served.status_code == 200, served.text
    assert served.content == b"%PDF-1.4 preview"


def test_failed_preview_build_does_not_keep_the_lock(monkeypatch):
    def broken(content):
        raise RuntimeError("Браузер не смог собрать PDF. Повторите через минуту.")

    lock = threading.Lock()
    monkeypatch.setattr(digest, "_PREVIEW_PDF_LOCK", lock)
    monkeypatch.setattr(digest, "_PREVIEW_PDF_CACHE", OrderedDict())
    monkeypatch.setattr(digest, "render_digest_pdf", broken)

    with pytest.raises(RuntimeError, match="Браузер не смог"):
        digest.render_digest_preview_pdf({"month": "2026-10", "news": []})

    assert not lock.locked()


def test_browser_failure_is_a_readable_refusal_not_500(monkeypatch, caplog):
    """Ревью PR #155: ошибка Playwright не наследует RuntimeError, упавший Chromium давал 500 и «Internal Server
    Error». Человеку понятный отказ, причина в журнале."""
    playwright = pytest.importorskip("playwright.sync_api")

    class CrashedBrowser:
        def __enter__(self):
            raise playwright.Error("Target crashed")

        def __exit__(self, *exc):
            return False

    _stub(monkeypatch, [_row(1)])
    monkeypatch.setattr(playwright, "sync_playwright", CrashedBrowser)
    monkeypatch.setattr(digest, "_PREVIEW_PDF_CACHE", OrderedDict())
    api.app.dependency_overrides[api.require_user] = lambda: {"id": 1, "role": "user"}
    try:
        with caplog.at_level("ERROR", logger=digest_pdf.__name__):
            response = TestClient(api.app).get("/api/digest-preview.pdf", params={"month": "2026-10"})
    finally:
        api.app.dependency_overrides.pop(api.require_user, None)

    assert response.status_code == 503
    assert response.json()["detail"] == "Браузер не смог собрать PDF. Повторите через минуту."
    assert "Target crashed" in caplog.text

