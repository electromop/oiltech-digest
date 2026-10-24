"""Плашка «Технология / Бизнес» у элемента выпуска и предпросмотр PDF в окне (решения владельца 23.10).

Категория элемента — категория сигнала: у статьи — её сигнала ленты, у находки радара — своя. Плашка видна и на
экране сборки, и в самом выпуске: письмо и PDF (одна вёрстка), DOCX.
"""

from datetime import datetime, timezone
from io import BytesIO

from docx import Document
from fastapi.testclient import TestClient

from oiltech_digest import api
from oiltech_digest.db import repository
from oiltech_digest.processing import digest
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
    monkeypatch.setattr(api, "render_digest_pdf", lambda content: b"%PDF-1.4 preview")
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
