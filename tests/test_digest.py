from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from oiltech_digest.processing.digest import (
    build_digest_content,
    render_digest_email,
    render_digest_docx,
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
                "read_more_label": "ОТКРЫТЬ",
                "empty_summary_text": "Нет сути",
            },
            "hero": {
                "badge": "НОВОСТИ",
                "headline": "НЕФТЕСЕРВИСНЫЙ ДАЙДЖЕСТ",
                "subtitle": "Технологии и рынок",
                "image_url": "https://example.com/hero.jpg",
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
    assert "https://example.com/hero.jpg" in html
    assert "ОТКРЫТЬ" in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_build_digest_content_uses_branding_config(monkeypatch):
    monkeypatch.setattr(
        "oiltech_digest.processing.digest._load_digest_branding",
        lambda: {
            "header": {
                "brand_text": "ТЕСТ БРЕНД",
                "brand_suffix": "ТЕСТ СЛОГАН",
                "department_text": "ТЕСТ ДЕПАРТАМЕНТ",
            },
            "hero": {
                "badge": "ТЕСТ",
                "headline": "ТЕСТОВЫЙ ДАЙДЖЕСТ",
                "subtitle": "Подзаголовок",
                "image_url": "https://example.com/hero.jpg",
            },
            "issue": {
                "title_template": "Тестовый дайджест",
                "title_template_with_month": "Тестовый дайджест · {month}",
                "period_label_all": "все время",
                "preheader": "Тестовый прехедер",
                "intro_template": "Общее интро",
                "intro_template_with_month": "Интро за {month}",
                "highlights_title": "Ключевое",
                "news_title": "Материалы",
                "read_more_label": "Открыть",
                "empty_summary_text": "Нет сути",
                "preview_empty_text": "Пусто",
            },
            "footer": {
                "contact_text": "Напишите в тестовый блок",
                "contact_email": "brand@example.com",
                "note": "Тестовая рассылка",
                "socials": [{"label": "Portal", "accent": "#111111", "text": "P"}],
            },
            "highlights": {
                "analytics_source_keywords": [],
                "analytics_category_keywords": [],
                "business_category_keywords": [],
                "cards": [],
            },
        },
    )
    monkeypatch.setattr("oiltech_digest.processing.digest.repository.digest_candidates", lambda month, limit=20, min_score=60: [])

    content = build_digest_content("2026-05")

    assert content["branding"]["header"]["brand_text"] == "ТЕСТ БРЕНД"
    assert content["hero"]["headline"] == "ТЕСТОВЫЙ ДАЙДЖЕСТ"
    assert content["hero"]["image_url"] == "https://example.com/hero.jpg"
    assert content["issue"]["preheader"] == "Тестовый прехедер"
    assert content["issue"]["intro"] == "Интро за 2026-05"
    assert content["issue"]["highlights_title"] == "Ключевое"
    assert content["issue"]["news_title"] == "Материалы"
    assert content["issue"]["read_more_label"] == "Открыть"
    assert content["issue"]["empty_summary_text"] == "Нет сути"
    assert content["footer"]["contact_email"] == "brand@example.com"
    assert content["footer"]["socials"][0]["text"] == "P"


def test_render_digest_email_renders_branding_from_content():
    html = render_digest_email(
        {
            "branding": {
                "header": {
                    "brand_text": "ТЕСТ БРЕНД",
                    "brand_suffix": "ТЕСТ СЛОГАН",
                    "department_text": "ТЕСТ ДЕПАРТАМЕНТ",
                }
            },
            "issue": {"title": "Digest", "preheader": "Preheader", "intro": "Intro"},
            "hero": {"badge": "NEWS", "headline": "DIGEST", "subtitle": "Subtitle", "image_url": ""},
            "news": [],
            "footer": {
                "contact_text": "Contact",
                "contact_email": "digest@example.com",
                "note": "Note",
                "socials": [{"label": "Portal", "accent": "#111111", "text": "P"}],
            },
        }
    )

    assert "ТЕСТ БРЕНД" in html
    assert "ТЕСТ СЛОГАН" in html
    assert "ТЕСТ ДЕПАРТАМЕНТ" in html
    assert 'title="Portal"' in html


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


def test_write_digest_export_html_and_docx(monkeypatch, tmp_path):
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
    doc_result = write_digest_export("2026-05", export_format="docx")

    html_path = Path(html_result["path"])
    doc_path = Path(doc_result["path"])
    assert html_path.exists()
    assert doc_path.exists()
    assert html_path.suffix == ".html"
    assert doc_path.suffix == ".docx"
    assert html_result["media_type"] == "text/html; charset=utf-8"
    assert doc_result["media_type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert "Export article" in html_path.read_text(encoding="utf-8")
    with ZipFile(doc_path) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")
    assert "Export article" in document_xml


def test_write_digest_export_doc_alias_writes_real_docx(monkeypatch, tmp_path):
    monkeypatch.setattr("oiltech_digest.processing.digest.EXPORTS_DIR", tmp_path)
    monkeypatch.setattr(
        "oiltech_digest.processing.digest.build_digest_content",
        lambda month, limit=100, min_score=0: {
            "issue": {"title": f"Digest {month}", "intro": "Test intro"},
            "hero": {"badge": "TEST", "headline": "DIGEST", "subtitle": "Sandbox"},
            "news": [],
            "items": [],
            "footer": {"contact_text": "", "contact_email": "", "note": ""},
        },
    )

    result = write_digest_export("2026-05", export_format="doc")

    path = Path(result["path"])
    assert path.suffix == ".docx"
    assert result["format"] == "docx"


def test_render_digest_docx_contains_digest_content():
    content = {
        "issue": {"title": "Digest title", "intro": "Digest intro"},
        "news": [
            {
                "category": "Рынок / LNG",
                "title": "Export article",
                "source": "World Oil",
                "url": "https://example.com/export-article",
                "published_at": "2026-05-20",
                "summary": "Useful export summary",
            }
        ],
        "highlights": [{"value": 1, "label": "новость", "icon": "doc"}],
    }

    payload = render_digest_docx(content)

    with ZipFile(BytesIO(payload)) as archive:
        names = set(archive.namelist())
        document_xml = archive.read("word/document.xml").decode("utf-8")

    assert "[Content_Types].xml" in names
    assert "word/document.xml" in names
    assert "word/styles.xml" in names
    assert "Digest title" in document_xml
    assert "Export article" in document_xml


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
            "issue": {"title": "Digest", "preheader": "P", "intro": "Intro", "highlights_title": "Итоги периода", "news_title": "Сигналы"},
            "hero": {"badge": "NEWS", "headline": "DIGEST", "subtitle": "Sub", "image_url": ""},
            "news": [
                {"category": "Технологии", "title": "A", "source": "SLB",
                 "url": "https://e.com/a", "summary": "s", "image_url": ""},
            ],
            "footer": {"contact_text": "C", "contact_email": "d@e.com", "note": "N"},
        }
    )
    assert "Итоги периода" in html
    assert "Сигналы" in html


def test_render_digest_email_uses_empty_summary_fallback():
    html = render_digest_email(
        {
            "issue": {"title": "Digest", "preheader": "P", "intro": "Intro", "empty_summary_text": "Нет краткой сути", "read_more_label": "Открыть"},
            "hero": {"badge": "NEWS", "headline": "DIGEST", "subtitle": "Sub", "image_url": ""},
            "news": [
                {"category": "Технологии", "title": "A", "source": "SLB", "url": "https://e.com/a", "summary": "", "image_url": ""},
            ],
            "footer": {"contact_text": "C", "contact_email": "d@e.com", "note": "N"},
        }
    )
    assert "Нет краткой сути" in html
    assert "Открыть" in html


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


def test_digest_highlights_use_configurable_rules():
    from oiltech_digest.processing.digest import _digest_highlights

    news = [
        {"category": "Технологии", "source": "Custom Research Lab"},
        {"category": "Стратегия и сделки", "source": "SLB"},
        {"category": "Операции", "source": "Other"},
    ]
    hl = _digest_highlights(
        news,
        {
            "analytics_source_keywords": ["custom research"],
            "analytics_category_keywords": ["стратег"],
            "business_category_keywords": ["сделк"],
        },
    )
    assert hl[0]["value"] == 3
    assert hl[1]["value"] == 2
    assert hl[2]["value"] == 1


def test_digest_highlights_use_configurable_cards():
    from oiltech_digest.processing.digest import _digest_highlights

    news = [
        {"category": "Аналитика", "source": "Custom Research"},
        {"category": "Контракты", "source": "Other"},
    ]
    hl = _digest_highlights(
        news,
        {
            "analytics_source_keywords": ["custom research"],
            "analytics_category_keywords": ["аналит"],
            "business_category_keywords": ["контракт"],
            "cards": [
                {"metric": "business", "icon": "people", "prefix": "новых", "suffix": "на рынке", "noun_one": "сделка", "noun_few": "сделки", "noun_many": "сделок"},
                {"metric": "total", "icon": "doc", "prefix": "", "suffix": "в выпуске", "noun_one": "сигнал", "noun_few": "сигнала", "noun_many": "сигналов"},
            ],
        },
    )
    assert hl == [
        {"metric": "business", "value": 1, "icon": "people", "label": "новых сделка на рынке"},
        {"metric": "total", "value": 2, "icon": "doc", "label": "сигнала в выпуске"},
    ]


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
