"""Monthly digest draft generation."""

from __future__ import annotations

import json
import re
from html import escape
from pathlib import Path
from datetime import datetime

from oiltech_digest.config import EXPORTS_DIR
from oiltech_digest.db import repository

TEMPLATE_DIR = Path(__file__).resolve().parent
EMAIL_TEMPLATE = "digest_email_template.html"


def build_digest_content(month: str | None = None, limit: int = 20, min_score: float = 60) -> dict:
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
    title = f"Нефтесервисный дайджест · {month}" if month else "Нефтесервисный дайджест"
    intro = (
        f"Уважаемые коллеги! Представляем ключевые новости и обзоры за {month}, "
        "которые помогают отслеживать технологические тренды, рыночную динамику "
        "и возможности для развития нефтесервисного бизнеса."
    ) if month else (
        "Уважаемые коллеги! Представляем ключевые новости и обзоры нефтесервисного рынка, "
        "которые помогают отслеживать технологические тренды, рыночную динамику "
        "и возможности для развития бизнеса."
    )
    return {
        "month": month,
        "title": title,
        "issue": {
            "title": title,
            "period": month or "за всё время",
            "preheader": "Ключевые новости и обзоры нефтесервисного рынка",
            "intro": intro,
        },
        "hero": {
            "badge": "НОВОСТИ",
            "headline": "НЕФТЕСЕРВИСНЫЙ ДАЙДЖЕСТ",
            "subtitle": "Технологии, рынок и возможности для бизнеса",
            "image_url": "",
        },
        "news": news,
        "items": news,
        "footer": {
            "contact_text": "При возникновении вопросов обращайтесь в Блок развития бизнеса",
            "contact_email": "Rodionov.VVL@gazprom-neft.ru",
            "note": "Внутренняя корпоративная рассылка",
        },
    }


def render_digest_email(content: dict) -> str:
    """Render the branded Gazprom Neft digest HTML from issue/hero/news/footer.

    The same render is used for the on-screen HTML, the file export and the PDF —
    one template, identical to the reference (digest_email_claude_pack).
    """
    template = (TEMPLATE_DIR / EMAIL_TEMPLATE).read_text(encoding="utf-8")
    news_html = "\n".join(_render_news_item(item) for item in content.get("news", []))
    values = {
        "issue_title": _html(content.get("issue", {}).get("title")),
        "issue_preheader": _html(content.get("issue", {}).get("preheader")),
        "issue_intro": _html(content.get("issue", {}).get("intro")),
        "hero_image_url": _html(content.get("hero", {}).get("image_url")),
        "hero_badge": _html(content.get("hero", {}).get("badge")),
        "hero_headline": _html(content.get("hero", {}).get("headline")),
        "hero_subtitle": _html(content.get("hero", {}).get("subtitle")),
        "footer_contact_text": _html(content.get("footer", {}).get("contact_text")),
        "footer_contact_email": _html(content.get("footer", {}).get("contact_email")),
        "footer_note": _html(content.get("footer", {}).get("note")),
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


def _render_news_item(item: dict) -> str:
    """One news card: image/title row, summary below, actions and tag at bottom."""
    image_url = item.get("image_url") or ""
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
                  <td width="230" valign="top" style="padding:10px 16px 8px 10px;">
                    {media}
                  </td>
                  <td valign="top" style="padding:12px 14px 8px 0;">
                    <div class="news-card-title">{_html(item.get("title"))}</div>
                  </td>
                </tr>
                <tr>
                  <td colspan="2" valign="top" style="padding:0 14px 12px 14px;">
                    <div class="news-card-summary">{_html(item.get("summary"))}</div>
                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-top:8px;">
                      <tr>
                        <td align="left" valign="middle">
                          <a href="{_html(item.get("url"))}" style="color:#e83d08;text-decoration:none;font-size:13px;line-height:18px;font-weight:bold;letter-spacing:.06em;text-transform:uppercase;">ЧИТАТЬ ДАЛЕЕ &#8594;</a>
                        </td>
                        <td align="right" valign="middle">
                          <span class="news-card-tag">{_html(item.get("category"))}</span>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>"""


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


def write_digest_export(month: str | None = None, export_format: str = "pdf", limit: int = 100,
                        min_score: float = 0) -> dict:
    content = build_digest_content(month=month, limit=limit, min_score=min_score)
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base_name = f"digest-{month or 'all'}-{stamp}"

    if export_format == "pdf":
        path = EXPORTS_DIR / f"{base_name}.pdf"
        path.write_bytes(render_digest_pdf(content))
        media_type = "application/pdf"
    elif export_format == "doc":
        path = EXPORTS_DIR / f"{base_name}.doc"
        path.write_text(render_digest_export_html(content), encoding="utf-8")
        media_type = "application/msword"
    elif export_format == "json":
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
        "format": export_format,
    }
