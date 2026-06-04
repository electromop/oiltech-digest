"""Monthly digest draft generation."""

from __future__ import annotations

import json
from html import escape
from pathlib import Path
from datetime import datetime

from oiltech_digest.config import EXPORTS_DIR
from oiltech_digest.db import repository

TEMPLATE_DIR = Path(__file__).resolve().parent
EMAIL_TEMPLATE = "digest_email_template.html"
EXPORT_TEMPLATE = "digest_export_template.html"


def build_digest_content(month: str, limit: int = 20, min_score: float = 60) -> dict:
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
                "summary": row.get("summary") or "",
                "image_url": "",
            }
        )
    title = f"Нефтесервисный дайджест · {month}"
    return {
        "month": month,
        "title": title,
        "issue": {
            "title": title,
            "period": month,
            "preheader": "Ключевые новости и обзоры нефтесервисного рынка",
            "intro": (
                f"Уважаемые коллеги! Представляем ключевые новости и обзоры за {month}, "
                "которые помогают отслеживать технологические тренды, рыночную динамику "
                "и возможности для развития нефтесервисного бизнеса."
            ),
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
    template = (TEMPLATE_DIR / EMAIL_TEMPLATE).read_text(encoding="utf-8")
    news_html = "\n".join(_render_news_item(item) for item in content.get("news", [])[:5])
    values = {
        "issue_title": _html(content.get("issue", {}).get("title")),
        "issue_preheader": _html(content.get("issue", {}).get("preheader")),
        "issue_intro": _html(content.get("issue", {}).get("intro")),
        "hero_badge": _html(content.get("hero", {}).get("badge")),
        "hero_headline": _html(content.get("hero", {}).get("headline")),
        "hero_subtitle": _html(content.get("hero", {}).get("subtitle")),
        "footer_contact_text": _html(content.get("footer", {}).get("contact_text")),
        "footer_contact_email": _html(content.get("footer", {}).get("contact_email")),
        "footer_note": _html(content.get("footer", {}).get("note")),
        "news_items": news_html,
    }
    return template.format(**values)


def render_digest_export_html(content: dict) -> str:
    template = (TEMPLATE_DIR / EXPORT_TEMPLATE).read_text(encoding="utf-8")
    news_html = "\n".join(_render_export_news_item(item, idx + 1) for idx, item in enumerate(content.get("news", [])))
    values = {
        "issue_title": _html(content.get("issue", {}).get("title")),
        "issue_intro": _html(content.get("issue", {}).get("intro")),
        "hero_badge": _html(content.get("hero", {}).get("badge")),
        "hero_headline": _html(content.get("hero", {}).get("headline")),
        "hero_subtitle": _html(content.get("hero", {}).get("subtitle")),
        "footer_contact_text": _html(content.get("footer", {}).get("contact_text")),
        "footer_contact_email": _html(content.get("footer", {}).get("contact_email")),
        "footer_note": _html(content.get("footer", {}).get("note")),
        "news_items": news_html,
    }
    return template.format(**values)


def _html(value: object) -> str:
    return escape("" if value is None else str(value), quote=True)


def _render_news_item(item: dict) -> str:
    published = item.get("published_at")
    score = item.get("score")
    meta = [_html(item.get("source"))]
    if published:
        meta.append(_html(published))
    if score is not None:
        meta.append(f"score {round(float(score))}")
    return f"""
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin:0 0 22px 0;border:1px solid #d9e3f3;border-radius:8px;background:#ffffff;">
                <tr>
                  <td valign="top" style="padding:14px 16px 14px 16px;">
                    <div style="font-size:12px;line-height:16px;color:#003da6;font-weight:bold;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px;">{_html(item.get("category"))}</div>
                    <div style="font-size:22px;line-height:26px;color:#003da6;font-weight:bold;letter-spacing:.03em;text-transform:uppercase;">{_html(item.get("title"))}</div>
                    <div style="font-size:14px;line-height:20px;color:#333333;margin-top:10px;">{_html(item.get("summary"))}</div>
                    <div style="font-size:12px;line-height:18px;color:#6b7280;margin-top:10px;">{" · ".join(meta)}</div>
                    <div style="margin-top:12px;">
                      <a href="{_html(item.get("url"))}" style="font-size:13px;line-height:18px;color:#e83d08;font-weight:bold;text-decoration:none;">Читать источник →</a>
                    </div>
                  </td>
                </tr>
              </table>"""


def _render_export_news_item(item: dict, index: int) -> str:
    published = item.get("published_at")
    score = item.get("score")
    meta = [_html(item.get("source"))]
    if published:
        meta.append(_html(published))
    if score is not None:
        meta.append(f"score {round(float(score))}")
    return f"""
    <div class="card">
      <div class="category">{index}. {_html(item.get("category"))}</div>
      <div class="title">{_html(item.get("title"))}</div>
      <div class="summary">{_html(item.get("summary"))}</div>
      <div class="meta">{" · ".join(meta)}</div>
      <a class="link" href="{_html(item.get("url"))}">Открыть источник</a>
    </div>"""


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


def write_digest_export(month: str, export_format: str = "html", limit: int = 20,
                        min_score: float = 60) -> dict:
    content = build_digest_content(month=month, limit=limit, min_score=min_score)
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base_name = f"digest-{month}-{stamp}"

    if export_format == "json":
        path = EXPORTS_DIR / f"{base_name}.json"
        payload = json.dumps(content, ensure_ascii=False, indent=2)
        media_type = "application/json"
    elif export_format == "doc":
        path = EXPORTS_DIR / f"{base_name}.doc"
        payload = render_digest_export_html(content)
        media_type = "application/msword"
    else:
        path = EXPORTS_DIR / f"{base_name}.html"
        payload = render_digest_export_html(content)
        media_type = "text/html; charset=utf-8"

    path.write_text(payload, encoding="utf-8")
    return {
        "path": str(path),
        "filename": path.name,
        "media_type": media_type,
        "items": len(content["items"]),
        "format": export_format,
    }
