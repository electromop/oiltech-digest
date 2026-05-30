from pathlib import Path

from oiltech_digest.processing.digest import render_digest_email, render_digest_export_html, write_digest_export


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
    assert "Oil News" in html
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

    assert "Тестовый экспорт дайджеста" in html
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
