"""Monthly digest draft generation."""

from __future__ import annotations

import json
import re
from html import escape
from pathlib import Path
from datetime import UTC, datetime
from xml.sax.saxutils import escape as xml_escape
from zipfile import ZIP_DEFLATED, ZipFile

from oiltech_digest.config import EXPORTS_DIR
from oiltech_digest.db import repository

TEMPLATE_DIR = Path(__file__).resolve().parent
EMAIL_TEMPLATE = "digest_email_template.html"
BRANDING_CONFIG = "digest_branding.json"


def _load_digest_branding() -> dict:
    defaults = {
        "header": {
            "brand_text": "ГАЗПРОМ НЕФТЬ",
            "brand_suffix": "ЭНЕРГИЯ В ЛЮДЯХ",
            "department_text": "БЛОК РАЗВИТИЯ БИЗНЕСА",
        },
        "hero": {
            "badge": "НОВОСТИ",
            "headline": "НЕФТЕСЕРВИСНЫЙ ДАЙДЖЕСТ",
            "subtitle": "Технологии, рынок и возможности для бизнеса",
            "image_url": "",
        },
        "issue": {
            "title_template": "Нефтесервисный дайджест",
            "title_template_with_month": "Нефтесервисный дайджест · {month}",
            "period_label_all": "за всё время",
            "preheader": "Ключевые новости и обзоры нефтесервисного рынка",
            "intro_template": (
                "Уважаемые коллеги! Представляем ключевые новости и обзоры нефтесервисного рынка, "
                "которые помогают отслеживать технологические тренды, рыночную динамику "
                "и возможности для развития бизнеса."
            ),
            "intro_template_with_month": (
                "Уважаемые коллеги! Представляем ключевые новости и обзоры за {month}, "
                "которые помогают отслеживать технологические тренды, рыночную динамику "
                "и возможности для развития нефтесервисного бизнеса."
            ),
            "highlights_title": "Главное за период",
            "news_title": "Новости",
            "read_more_label": "ЧИТАТЬ ДАЛЕЕ",
            "empty_summary_text": "Суть ещё не сформирована.",
            "preview_empty_text": "В текущей выборке нет сигналов для превью.",
        },
        "footer": {
            "contact_text": "При возникновении вопросов обращайтесь в Блок развития бизнеса",
            "contact_email": "Rodionov.VVL@gazprom-neft.ru",
            "note": "Внутренняя корпоративная рассылка",
            "socials": [],
        },
        "highlights": {
            "analytics_source_keywords": [
                "mckinsey",
                "wood mac",
                "woodmac",
                "wood mackenzie",
                "rystad",
                "deloitte",
                "iea",
                "eia",
                "bcg",
                "petroleum economist",
                "hart",
                "rigzone",
                "s&p",
                "ihs",
                "mit",
                "bloomberg",
            ],
            "analytics_category_keywords": ["аналит", "обзор", "прогноз", "рынок", "исследов", "report"],
            "business_category_keywords": [
                "бизнес",
                "m&a",
                "сделк",
                "инвест",
                "контракт",
                "возможност",
                "партнёрств",
                "партнерств",
                "локализац",
                "экспорт",
            ],
            "cards": [
                {
                    "metric": "total",
                    "icon": "doc",
                    "prefix": "",
                    "suffix": "",
                    "noun_one": "новость",
                    "noun_few": "новости",
                    "noun_many": "новостей",
                },
                {
                    "metric": "analytics",
                    "icon": "chart",
                    "prefix": "аналитических",
                    "suffix": "",
                    "noun_one": "материал",
                    "noun_few": "материала",
                    "noun_many": "материалов",
                },
                {
                    "metric": "business",
                    "icon": "people",
                    "prefix": "",
                    "suffix": "для бизнеса",
                    "noun_one": "возможность",
                    "noun_few": "возможности",
                    "noun_many": "возможностей",
                },
            ],
        },
    }
    path = TEMPLATE_DIR / BRANDING_CONFIG
    if not path.exists():
        return defaults
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return {
        "header": {**defaults["header"], **(loaded.get("header") or {})},
        "hero": {**defaults["hero"], **(loaded.get("hero") or {})},
        "issue": {**defaults["issue"], **(loaded.get("issue") or {})},
        "footer": {**defaults["footer"], **(loaded.get("footer") or {})},
        "highlights": {**defaults["highlights"], **(loaded.get("highlights") or {})},
    }


def get_digest_branding() -> dict:
    return _load_digest_branding()


def save_digest_branding(payload: dict) -> dict:
    current = _load_digest_branding()
    merged = {
        "header": {**current["header"], **(payload.get("header") or {})},
        "hero": {**current["hero"], **(payload.get("hero") or {})},
        "issue": {**current["issue"], **(payload.get("issue") or {})},
        "footer": {
            **current["footer"],
            **(payload.get("footer") or {}),
            "socials": list((payload.get("footer") or {}).get("socials") or current["footer"].get("socials") or []),
        },
        "highlights": {
            **current["highlights"],
            **(payload.get("highlights") or {}),
        },
    }
    path = TEMPLATE_DIR / BRANDING_CONFIG
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


def build_digest_content(month: str | None = None, limit: int = 20, min_score: float = 60) -> dict:
    branding = _load_digest_branding()
    issue_cfg = branding["issue"]
    rows = repository.digest_candidates(month=month, limit=limit, min_score=min_score)
    news = []
    for row in rows:
        tag = row.get("tag_name") or "Без тега"
        if row.get("parent_tag_name"):
            tag = f"{row['parent_tag_name']} / {tag}"
        published = row["published_at"].date().isoformat() if row.get("published_at") else None
        news.append(
            {
                "category": tag,
                "article_id": row["id"],
                "title": row["title"],
                "source": row["source_name"],
                "url": row["url"],
                "published_at": published,
                "tag": tag,
                "score": float(row["total_score"]) if row.get("total_score") is not None else None,
                "score_label": row.get("score_label"),
                "summary": _compact_digest_summary(row.get("summary") or "", row.get("title") or ""),
                "image_url": row.get("image_url") or "",
            }
        )
    title_template = issue_cfg["title_template_with_month"] if month else issue_cfg["title_template"]
    intro_template = issue_cfg["intro_template_with_month"] if month else issue_cfg["intro_template"]
    title = title_template.format(month=month or "")
    intro = intro_template.format(month=month or "")
    return {
        "month": month,
        "title": title,
        "issue": {
            "title": title,
            "period": month or issue_cfg["period_label_all"],
            "preheader": issue_cfg["preheader"],
            "intro": intro,
            "highlights_title": issue_cfg["highlights_title"],
            "news_title": issue_cfg["news_title"],
            "read_more_label": issue_cfg["read_more_label"],
            "empty_summary_text": issue_cfg["empty_summary_text"],
            "preview_empty_text": issue_cfg["preview_empty_text"],
        },
        "hero": {
            "badge": branding["hero"]["badge"],
            "headline": branding["hero"]["headline"],
            "subtitle": branding["hero"]["subtitle"],
            "image_url": branding["hero"]["image_url"],
        },
        "news": news,
        "items": news,
        "highlights": _digest_highlights(news, branding.get("highlights")),
        "footer": {
            "contact_text": branding["footer"]["contact_text"],
            "contact_email": branding["footer"]["contact_email"],
            "note": branding["footer"]["note"],
            "socials": branding["footer"]["socials"],
        },
        "branding": branding,
    }


def render_digest_email(content: dict) -> str:
    """Render the branded Gazprom Neft digest HTML from issue/hero/news/footer.

    The same render is used for the on-screen HTML, the file export and the PDF —
    one template, identical to the reference (digest_email_claude_pack).
    """
    template = (TEMPLATE_DIR / EMAIL_TEMPLATE).read_text(encoding="utf-8")
    news_items = content.get("news", [])
    branding = content.get("branding") or _load_digest_branding()
    # Карточки идут единым потоком; разрыв страницы НЕ форсируем — за «карточка
    # не рвётся между страницами» отвечает CSS .news-card { break-inside: avoid }.
    news_html = "\n".join(_render_news_item(item, content.get("issue") or {}) for item in news_items)
    highlights = content.get("highlights") or _digest_highlights(news_items)
    values = {
        "header_brand_text": _html(branding.get("header", {}).get("brand_text")),
        "header_brand_suffix": _html(branding.get("header", {}).get("brand_suffix")),
        "header_department_text": _html(branding.get("header", {}).get("department_text")),
        "issue_title": _html(content.get("issue", {}).get("title")),
        "issue_preheader": _html(content.get("issue", {}).get("preheader")),
        "issue_intro": _html(content.get("issue", {}).get("intro")),
        "issue_highlights_title": _html(content.get("issue", {}).get("highlights_title") or "Главное за период"),
        "issue_news_title": _html(content.get("issue", {}).get("news_title") or "Новости"),
        "hero_image_url": _html(content.get("hero", {}).get("image_url")),
        "hero_badge": _html(content.get("hero", {}).get("badge")),
        "hero_headline": _html(content.get("hero", {}).get("headline")),
        "hero_subtitle": _html(content.get("hero", {}).get("subtitle")),
        "highlights_html": _render_highlights(highlights),
        "footer_contact_text": _html(content.get("footer", {}).get("contact_text")),
        "footer_contact_email": _html(content.get("footer", {}).get("contact_email")),
        "footer_note": _html(content.get("footer", {}).get("note")),
        "footer_socials_html": _render_footer_socials(content.get("footer", {}).get("socials") or branding.get("footer", {}).get("socials") or []),
        "news_items": news_html,
    }
    return template.format(**values)


# The export is the exact same branded document as the email — one source of truth.
def render_digest_export_html(content: dict) -> str:
    return render_digest_email(content)


def _html(value: object) -> str:
    return escape("" if value is None else str(value), quote=True)


def _compact_digest_summary(summary: str, title: str, max_chars: int = 170) -> str:
    """Shorten AI summary for digest cards so layout stays tight and readable."""
    text = re.sub(r"\s+", " ", (summary or "").strip())
    if not text:
        return ""

    # Our summaries often start with the article title prefix. Remove it for the digest.
    title_prefix = f"{(title or '').strip()}:"
    if title and text.lower().startswith(title_prefix.lower()):
        text = text[len(title_prefix):].strip()

    # Prefer the first sentence if it is already compact enough.
    first_sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0].strip()
    candidate = first_sentence or text
    if len(candidate) <= max_chars:
        return candidate

    clipped = candidate[: max_chars - 1].rstrip(" ,;:-")
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return clipped + "…"


_HL_ICONS = {
    "doc": '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#ffffff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4"/><line x1="10" y1="13" x2="15" y2="13"/><line x1="10" y1="17" x2="15" y2="17"/></svg>',
    "chart": '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#ffffff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 16 9 11 13 14 20 6"/><polyline points="15 6 20 6 20 11"/></svg>',
    "people": '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#ffffff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="9" r="3"/><path d="M3.5 20a5.5 5.5 0 0 1 11 0"/><path d="M16 7.5a3 3 0 0 1 0 5.5"/><path d="M15.5 14.5a5.5 5.5 0 0 1 5 5.5"/></svg>',
}


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение существительного по числу (1 новость / 2 новости / 5 новостей)."""
    n = abs(int(n)) % 100
    if 10 < n < 20:
        return many
    n1 = n % 10
    if n1 == 1:
        return one
    if 2 <= n1 <= 4:
        return few
    return many


def _digest_highlights(news: list[dict], config: dict | None = None) -> list[dict]:
    """KPI выпуска для блока «Главное за период»: всего новостей, аналитических
    материалов, бизнес-возможностей. Аналитику/возможности оцениваем эвристикой
    по источнику/категории (правила можно уточнить позже)."""
    defaults = _load_digest_branding().get("highlights") or {}
    config = {**defaults, **(config or {})}

    def cat(n: dict) -> str:
        return (n.get("category") or "").lower()

    def src(n: dict) -> str:
        return (n.get("source") or "").lower()

    analytic_src = tuple(str(item).lower() for item in config.get("analytics_source_keywords") or [])
    analytic_kw = tuple(str(item).lower() for item in config.get("analytics_category_keywords") or [])
    business_kw = tuple(str(item).lower() for item in config.get("business_category_keywords") or [])
    analytics = sum(
        1
        for n in news
        if any(k in cat(n) for k in analytic_kw) or any(s in src(n) for s in analytic_src)
    )
    business = sum(1 for n in news if any(k in cat(n) for k in business_kw))
    total = len(news)
    metric_values = {
        "total": total,
        "analytics": analytics,
        "business": business,
    }
    cards = config.get("cards") or []
    if not cards:
        cards = _load_digest_branding()["highlights"]["cards"]
    result = []
    for card in cards:
        metric = str(card.get("metric") or "total")
        value = int(metric_values.get(metric, 0))
        prefix = str(card.get("prefix") or "").strip()
        suffix = str(card.get("suffix") or "").strip()
        noun = _plural(
            value,
            str(card.get("noun_one") or "материал"),
            str(card.get("noun_few") or "материала"),
            str(card.get("noun_many") or "материалов"),
        )
        label = " ".join(part for part in [prefix, noun, suffix] if part).strip()
        result.append(
            {
                "metric": metric,
                "value": value,
                "icon": str(card.get("icon") or "doc"),
                "label": label,
            }
        )
    return result


def _render_highlights(highlights: list[dict]) -> str:
    """KPI-плашки: иконка + крупное число + подпись."""
    if not highlights:
        return ""
    width = f"{max(1, int(100 / max(1, len(highlights))))}%"
    cells = []
    for h in highlights:
        icon = _HL_ICONS.get(h.get("icon", ""), "")
        cells.append(
            f'<td width="{width}" valign="top" style="padding:0 10px 0 0;">'
            '<table role="presentation" cellspacing="0" cellpadding="0" border="0"><tr>'
            '<td valign="middle" width="58">'
            '<div style="width:46px;height:46px;background:#003da6;border-radius:8px;text-align:center;line-height:46px;">'
            f'{icon}</div></td>'
            '<td valign="middle" style="padding-left:10px;">'
            f'<div style="font-size:26px;line-height:28px;color:#003da6;font-weight:bold;">{_html(h.get("value"))}</div>'
            '<div style="font-size:11px;line-height:14px;color:#262d3c;text-transform:uppercase;'
            f'letter-spacing:.04em;font-weight:bold;">{_html(h.get("label"))}</div>'
            '</td></tr></table></td>'
        )
    return (
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">'
        f'<tr>{"".join(cells)}</tr></table>'
    )


def _render_news_item(item: dict, issue: dict | None = None) -> str:
    """Карточка новости (формат референса коллеги): фото + заголовок сверху,
    ниже — краткое описание и строка «Читать далее | теги». Карточка не должна
    разрываться между страницами (CSS .news-card { break-inside: avoid })."""
    issue = issue or {}
    image_url = item.get("image_url") or ""
    summary = item.get("summary") or issue.get("empty_summary_text") or "Суть ещё не сформирована."
    read_more_label = issue.get("read_more_label") or "ЧИТАТЬ ДАЛЕЕ"
    if image_url:
        media = (
            f'<img class="news-card-image" src="{_html(image_url)}" width="210" height="118" alt="{_html(item.get("title"))}" '
            'style="display:block;width:210px;height:118px;object-fit:cover;border-radius:6px;border:0;">'
        )
    else:
        # SVG-заглушка вместо <img>: ведёт себя как картинка (фиксирует ширину ячейки
        # 210×118 в табличной вёрстке — в отличие от <div>, который ужимается).
        placeholder = (
            "data:image/svg+xml;utf8,"
            "<svg xmlns='http://www.w3.org/2000/svg' width='210' height='118'>"
            "<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>"
            "<stop offset='0' stop-color='%23003da6'/><stop offset='1' stop-color='%23001d50'/>"
            "</linearGradient></defs><rect width='210' height='118' rx='6' fill='url(%23g)'/></svg>"
        )
        media = (
            f'<img class="news-card-image" src="{placeholder}" width="210" height="118" alt="" '
            'style="display:block;width:210px;height:118px;border-radius:6px;border:0;">'
        )
    return f"""
              <table class="news-card" role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin:0 0 18px 0;border:1px solid #d9e3f3;border-radius:8px;background:#ffffff;">
                <tr>
                  <td width="230" valign="top" style="padding:12px 16px 8px 12px;">
                    {media}
                  </td>
                  <td valign="top" style="padding:14px 16px 8px 0;">
                    <div class="news-card-title">{_html(item.get("title"))}</div>
                  </td>
                </tr>
                <tr>
                  <td colspan="2" valign="top" style="padding:0 16px 14px 16px;">
                    <div class="news-card-summary">{_html(summary)}</div>
                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-top:8px;">
                      <tr>
                        <td align="left" valign="middle">
                          <a href="{_html(item.get("url"))}" style="color:#e83d08;text-decoration:none;font-size:13px;line-height:18px;font-weight:bold;letter-spacing:.06em;text-transform:uppercase;">{_html(read_more_label)} &#8594;</a>
                        </td>
                        <td align="right" valign="middle">
                          <span class="news-card-tag">{_html(item.get("category"))}</span>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>"""


def _render_footer_socials(socials: list[dict]) -> str:
    if not socials:
        return ""
    cells = []
    for item in socials:
        cells.append(
            '<td style="padding:0 7px;">'
            '<div style="width:36px;height:36px;background:#ffffff;border-radius:50%;'
            'text-align:center;line-height:36px;'
            f'color:{_html(item.get("accent") or "#262d3c")};'
            'font-weight:bold;font-size:13px;font-family:Arial,Helvetica,sans-serif;"'
            f' title="{_html(item.get("label"))}">{_html(item.get("text"))}</div>'
            "</td>"
        )
    return "".join(cells)


def write_digest_content(path: str | Path, month: str, limit: int = 20,
                         min_score: float = 60, html_path: str | Path | None = None) -> dict:
    content = build_digest_content(month=month, limit=limit, min_score=min_score)
    output_path = Path(path)
    output_path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    result = {"path": str(output_path), "items": len(content["items"])}
    if html_path:
        html_output_path = Path(html_path)
        html_output_path.write_text(render_digest_email(content), encoding="utf-8")
        result["html_path"] = str(html_output_path)
    return result


def save_digest_draft(month: str, limit: int = 20, min_score: float = 60) -> dict:
    """Build digest content from current selected articles and persist it as a draft."""
    content = build_digest_content(month=month, limit=limit, min_score=min_score)
    items = [
        {
            "article_id": item["article_id"],
            "section": item.get("category"),
            "editor_note": item.get("summary"),
        }
        for item in content.get("items", [])
        if item.get("article_id") is not None
    ]
    saved = repository.save_monthly_digest(
        month=month,
        title=content.get("title") or f"Нефтесервисный дайджест · {month}",
        items=items,
        status="draft",
    )
    return {**saved, "content_items": len(content.get("items", []))}


def render_digest_pdf(content: dict) -> bytes:
    """Render the branded digest to PDF via headless Chromium (pixel-perfect).

    Playwright/Chromium is an optional, server-side dependency (it is heavy and not
    needed for tests or for the HTML/Word paths), so it is imported lazily and a
    clear, actionable error is raised when it is missing.
    """
    html_str = render_digest_email(content)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on optional dep
        raise RuntimeError(
            "PDF-экспорт требует Playwright с Chromium. Установите на сервере: "
            "pip install playwright && python -m playwright install --with-deps chromium"
        ) from exc

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        try:
            page = browser.new_page()
            page.set_content(html_str, wait_until="load")
            pdf_bytes = page.pdf(
                format="A4",
                print_background=True,
                margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
            )
        finally:
            browser.close()
    return pdf_bytes


def render_digest_docx(content: dict) -> bytes:
    """Render digest to a minimal OOXML .docx package without external deps."""
    issue = content.get("issue") or {}
    news_items = content.get("news") or []
    highlights = content.get("highlights") or _digest_highlights(news_items)

    def text_run(text: str, *, bold: bool = False) -> str:
        if not text:
            return ""
        body = f"<w:t xml:space=\"preserve\">{xml_escape(text)}</w:t>"
        if bold:
            return f"<w:r><w:rPr><w:b/></w:rPr>{body}</w:r>"
        return f"<w:r>{body}</w:r>"

    def paragraph(text: str = "", *, style: str | None = None, bold: bool = False, page_break: bool = False) -> str:
        props = f"<w:pPr><w:pStyle w:val=\"{style}\"/></w:pPr>" if style else ""
        if page_break:
            run = "<w:r><w:br w:type=\"page\"/></w:r>"
        else:
            run = text_run(text, bold=bold) if text else ""
        return f"<w:p>{props}{run}</w:p>"

    body_parts = [
        paragraph(issue.get("title") or "Нефтесервисный дайджест", style="Title"),
        paragraph(issue.get("intro") or ""),
        paragraph("Главное за период", style="Heading1"),
    ]
    for item in highlights:
        body_parts.append(paragraph(f"{item.get('value', 0)} — {item.get('label', '')}"))

    body_parts.append(paragraph("Новости", style="Heading1"))
    for index, item in enumerate(news_items, start=1):
        body_parts.append(paragraph(item.get("title") or f"Материал {index}", style="Heading2"))
        meta = " | ".join(part for part in [item.get("category"), item.get("source"), item.get("published_at")] if part)
        if meta:
            body_parts.append(paragraph(meta))
        if item.get("summary"):
            body_parts.append(paragraph(item["summary"]))
        if item.get("url"):
            body_parts.append(paragraph(f"Читать далее: {item['url']}"))
        if index != len(news_items):
            body_parts.append(paragraph())

    document_xml = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<w:document xmlns:wpc=\"http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas\" "
        "xmlns:mc=\"http://schemas.openxmlformats.org/markup-compatibility/2006\" "
        "xmlns:o=\"urn:schemas-microsoft-com:office:office\" "
        "xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\" "
        "xmlns:m=\"http://schemas.openxmlformats.org/officeDocument/2006/math\" "
        "xmlns:v=\"urn:schemas-microsoft-com:vml\" "
        "xmlns:wp14=\"http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing\" "
        "xmlns:wp=\"http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing\" "
        "xmlns:w10=\"urn:schemas-microsoft-com:office:word\" "
        "xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\" "
        "xmlns:w14=\"http://schemas.microsoft.com/office/word/2010/wordml\" "
        "xmlns:wpg=\"http://schemas.microsoft.com/office/word/2010/wordprocessingGroup\" "
        "xmlns:wpi=\"http://schemas.microsoft.com/office/word/2010/wordprocessingInk\" "
        "xmlns:wne=\"http://schemas.microsoft.com/office/word/2006/wordml\" "
        "xmlns:wps=\"http://schemas.microsoft.com/office/word/2010/wordprocessingShape\" "
        "mc:Ignorable=\"w14 wp14\">"
        f"<w:body>{''.join(body_parts)}"
        "<w:sectPr><w:pgSz w:w=\"11906\" w:h=\"16838\"/><w:pgMar w:top=\"1134\" w:right=\"1134\" w:bottom=\"1134\" w:left=\"1134\" w:header=\"708\" w:footer=\"708\" w:gutter=\"0\"/></w:sectPr>"
        "</w:body></w:document>"
    )

    styles_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:qFormat/>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:rPr><w:b/><w:sz w:val="36"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="Heading 1"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:rPr><w:b/><w:sz w:val="28"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="Heading 2"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:rPr><w:b/><w:color w:val="003DA6"/><w:sz w:val="24"/></w:rPr>
  </w:style>
</w:styles>"""

    content_types_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""

    root_rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""

    document_rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    core_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:dcterms="http://purl.org/dc/terms/"
 xmlns:dcmitype="http://purl.org/dc/dcmitype/"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>{xml_escape(issue.get("title") or "Нефтесервисный дайджест")}</dc:title>
  <dc:creator>OilTech Digest</dc:creator>
  <cp:lastModifiedBy>OilTech Digest</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>
</cp:coreProperties>"""

    app_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
 xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>OilTech Digest</Application>
</Properties>"""

    from io import BytesIO

    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types_xml)
        archive.writestr("_rels/.rels", root_rels_xml)
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/_rels/document.xml.rels", document_rels_xml)
        archive.writestr("word/styles.xml", styles_xml)
        archive.writestr("docProps/core.xml", core_xml)
        archive.writestr("docProps/app.xml", app_xml)
    return buffer.getvalue()


def write_digest_export(month: str | None = None, export_format: str = "pdf", limit: int = 100,
                        min_score: float = 0) -> dict:
    content = build_digest_content(month=month, limit=limit, min_score=min_score)
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base_name = f"digest-{month or 'all'}-{stamp}"
    normalized_format = "docx" if export_format == "doc" else export_format

    if normalized_format == "pdf":
        path = EXPORTS_DIR / f"{base_name}.pdf"
        path.write_bytes(render_digest_pdf(content))
        media_type = "application/pdf"
    elif normalized_format == "docx":
        path = EXPORTS_DIR / f"{base_name}.docx"
        path.write_bytes(render_digest_docx(content))
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif normalized_format == "json":
        path = EXPORTS_DIR / f"{base_name}.json"
        path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
        media_type = "application/json"
    else:  # html
        path = EXPORTS_DIR / f"{base_name}.html"
        path.write_text(render_digest_export_html(content), encoding="utf-8")
        media_type = "text/html; charset=utf-8"

    return {
        "path": str(path),
        "filename": path.name,
        "media_type": media_type,
        "items": len(content["items"]),
        "format": normalized_format,
    }
