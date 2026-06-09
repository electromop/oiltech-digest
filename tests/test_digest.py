from pathlib import Path

from oiltech_digest.processing.digest import (
    build_digest_content,
    render_digest_email,
    render_digest_export_html,
    save_digest_draft,
    write_digest_export,
)


def test_render_digest_email_contains_news_and_escapes_html():
    html = render_digest_email(
        {
            "issue": {
                "title": "Нефтесервисный дайджест · 2026-05",
                "period": "2026-05",
                "preheader": "Ключевые новости",
                "intro": "Тестовый выпуск",
            },
            "hero": {
                "badge": "НОВОСТИ",
                "headline": "НЕФТЕСЕРВИСНЫЙ ДАЙДЖЕСТ",
                "subtitle": "Технологии и рынок",
                "image_url": "",
            },
            "news": [
                {
                    "category": "Добыча",
                    "title": "Новая технология бурения <script>alert(1)</script>",
                    "source": "Oil News",
                    "url": "https://example.com/article",
                    "published_at": "2026-05-20",
                    "score": 87,
                    "score_label": "High",
                    "summary": "Краткая суть статьи",
                    "image_url": "",
                }
            ],
            "footer": {
                "contact_text": "Вопросы по дайджесту",
                "contact_email": "digest@example.com",
                "note": "Внутренняя рассылка",
            },
        }
    )

    assert "НЕФТЕСЕРВИСНЫЙ ДАЙДЖЕСТ" in html
    assert "ГАЗПРОМ НЕФТЬ" in html  # фирменный шаблон-референс
    assert "Добыча" in html  # категория новости
    assert "https://example.com/article" in html
    assert "Краткая суть статьи" in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_render_digest_export_html_contains_cards():
    html = render_digest_export_html(
        {
            "issue": {"title": "Test digest", "intro": "Export intro"},
            "hero": {"badge": "TEST", "headline": "DIGEST", "subtitle": "Sandbox"},
            "news": [
                {
                    "category": "Drilling",
                    "title": "Export article",
                    "source": "World Oil",
                    "url": "https://example.com/export-article",
                    "published_at": "2026-05-20",
                    "score": 91,
                    "summary": "Useful export summary",
                }
            ],
            "footer": {
                "contact_text": "Contact",
                "contact_email": "digest@example.com",
                "note": "Test note",
            },
        }
    )

    assert "ГАЗПРОМ НЕФТЬ" in html  # экспорт = тот же фирменный шаблон, что и письмо
    assert "Export article" in html
    assert "https://example.com/export-article" in html


def test_write_digest_export_json(monkeypatch, tmp_path):
    monkeypatch.setattr("oiltech_digest.processing.digest.EXPORTS_DIR", tmp_path)
    monkeypatch.setattr(
        "oiltech_digest.processing.digest.build_digest_content",
        lambda month, limit=20, min_score=60: {
            "issue": {"title": f"Digest {month}", "intro": "Test intro"},
            "hero": {"badge": "TEST", "headline": "DIGEST", "subtitle": "Sandbox"},
            "news": [],
            "items": [],
            "footer": {"contact_text": "", "contact_email": "", "note": ""},
        },
    )

    result = write_digest_export("2026-05", export_format="json")

    path = Path(result["path"])
    assert path.exists()
    assert path.suffix == ".json"
    assert result["format"] == "json"


def test_write_digest_export_html_and_doc(monkeypatch, tmp_path):
    monkeypatch.setattr("oiltech_digest.processing.digest.EXPORTS_DIR", tmp_path)
    monkeypatch.setattr(
        "oiltech_digest.processing.digest.build_digest_content",
        lambda month, limit=100, min_score=0: {
            "issue": {"title": f"Digest {month}", "intro": "Test intro"},
            "hero": {"badge": "TEST", "headline": "DIGEST", "subtitle": "Sandbox"},
            "news": [
                {
                    "category": "Drilling",
                    "title": "Export article",
                    "source": "World Oil",
                    "url": "https://example.com/export-article",
                    "published_at": "2026-05-20",
                    "score": 91,
                    "summary": "Useful export summary",
                    "image_url": "",
                }
            ],
            "items": [{"article_id": 1}],
            "footer": {"contact_text": "", "contact_email": "", "note": ""},
        },
    )

    html_result = write_digest_export("2026-05", export_format="html")
    doc_result = write_digest_export("2026-05", export_format="doc")

    html_path = Path(html_result["path"])
    doc_path = Path(doc_result["path"])
    assert html_path.exists()
    assert doc_path.exists()
    assert html_path.suffix == ".html"
    assert doc_path.suffix == ".doc"
    assert html_result["media_type"] == "text/html; charset=utf-8"
    assert doc_result["media_type"] == "application/msword"
    assert "Export article" in html_path.read_text(encoding="utf-8")
    assert "Export article" in doc_path.read_text(encoding="utf-8")


def test_build_digest_content_includes_article_ids(monkeypatch):
    class PublishedAt:
        def date(self):
            return self

        def isoformat(self):
            return "2026-05-20"

    monkeypatch.setattr(
        "oiltech_digest.processing.digest.repository.digest_candidates",
        lambda month, limit=20, min_score=60: [
            {
                "id": 123,
                "title": "Digest candidate",
                "source_name": "World Oil",
                "url": "https://example.com/a",
                "published_at": PublishedAt(),
                "tag_name": "Drilling",
                "parent_tag_name": None,
                "total_score": 88,
                "score_label": "High",
                "summary": "Useful summary",
            }
        ],
    )

    content = build_digest_content("2026-05")

    assert content["items"][0]["article_id"] == 123
    assert content["news"][0]["article_id"] == 123


def test_build_digest_content_compacts_long_summary_and_removes_title_prefix(monkeypatch):
    long_summary = (
        "Digest candidate: Wood Mackenzie развернула три сценария рынков СПГ после закрытия "
        "Ормузского пролива, при котором мировая поставка сократилась на 80 млн т/год. "
        "Вторая фраза не должна попадать в короткую карточку."
    )
    monkeypatch.setattr(
        "oiltech_digest.processing.digest.repository.digest_candidates",
        lambda month, limit=20, min_score=60: [
            {
                "id": 123,
                "title": "Digest candidate",
                "source_name": "World Oil",
                "url": "https://example.com/a",
                "published_at": None,
                "tag_name": "LNG",
                "parent_tag_name": "Рынок",
                "total_score": 88,
                "score_label": "High",
                "summary": long_summary,
                "image_url": "",
            }
        ],
    )

    content = build_digest_content("2026-05")
    summary = content["news"][0]["summary"]

    assert not summary.startswith("Digest candidate:")
    assert len(summary) <= 170
    assert "Вторая фраза" not in summary
    assert content["news"][0]["category"] == "Рынок / LNG"


def test_render_digest_email_card_tag_and_cta_at_bottom():
    html = render_digest_email(
        {
            "issue": {"title": "Digest", "preheader": "Preheader", "intro": "Intro"},
            "hero": {"badge": "NEWS", "headline": "DIGEST", "subtitle": "Subtitle", "image_url": ""},
            "news": [
                {
                    "category": "Рынок / LNG",
                    "title": "Very long article title that should remain in the right column",
                    "source": "World Oil",
                    "url": "https://example.com/article",
                    "published_at": "2026-05-20",
                    "score": 91,
                    "summary": "Short summary",
                    "image_url": "",
                }
            ],
            "footer": {"contact_text": "Contact", "contact_email": "digest@example.com", "note": "Note"},
        }
    )

    # Формат коллеги: заголовок сверху → описание → строка «Читать далее | тег» внизу.
    assert "ЧИТАТЬ ДАЛЕЕ" in html
    assert "news-card-tag" in html
    assert "Very long article title" in html
    assert html.index("Very long article title") < html.index("ЧИТАТЬ ДАЛЕЕ")
    assert html.index("ЧИТАТЬ ДАЛЕЕ") < html.index("Рынок / LNG")


def test_render_digest_email_renders_highlights_block():
    html = render_digest_email(
        {
            "issue": {"title": "Digest", "preheader": "P", "intro": "Intro"},
            "hero": {"badge": "NEWS", "headline": "DIGEST", "subtitle": "Sub", "image_url": ""},
            "news": [
                {"category": "Технологии", "title": "A", "source": "SLB",
                 "url": "https://e.com/a", "summary": "s", "image_url": ""},
            ],
            "footer": {"contact_text": "C", "contact_email": "d@e.com", "note": "N"},
        }
    )
    assert "Главное за период" in html  # заголовок KPI-блока (uppercase делает CSS)


def test_digest_highlights_counts_and_plural():
    from oiltech_digest.processing.digest import _digest_highlights

    news = [
        {"category": "Технологии", "source": "SLB"},
        {"category": "Аналитика", "source": "X"},
        {"category": "Бизнес-сигнал", "source": "Y"},
        {"category": "Россия", "source": "Rystad"},
    ]
    hl = _digest_highlights(news)
    assert hl[0]["value"] == 4 and hl[0]["label"] == "новости"
    assert hl[1]["value"] == 2  # «Аналитика» (kw) + Rystad (analytic source)
    assert hl[2]["value"] == 1  # «Бизнес-сигнал» (kw)


def test_save_digest_draft_persists_ordered_items(monkeypatch):
    saved = {}

    monkeypatch.setattr(
        "oiltech_digest.processing.digest.build_digest_content",
        lambda month, limit=20, min_score=60: {
            "month": month,
            "title": f"Digest {month}",
            "items": [
                {"article_id": 10, "category": "Drilling", "summary": "First"},
                {"article_id": 11, "category": "Production", "summary": "Second"},
            ],
        },
    )
    monkeypatch.setattr(
        "oiltech_digest.processing.digest.repository.save_monthly_digest",
        lambda **kwargs: saved.update(kwargs) or {
            "id": 5,
            "month": kwargs["month"],
            "title": kwargs["title"],
            "status": kwargs["status"],
            "items": len(kwargs["items"]),
        },
    )

    result = save_digest_draft("2026-05", limit=2, min_score=70)

    assert result["id"] == 5
    assert saved["month"] == "2026-05"
    assert saved["items"] == [
        {"article_id": 10, "section": "Drilling", "editor_note": "First"},
        {"article_id": 11, "section": "Production", "editor_note": "Second"},
    ]
