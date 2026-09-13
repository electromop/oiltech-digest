# -*- coding: utf-8 -*-
"""Офлайн-пакет полного выпуска для чтения БЕЗ VPN и без интернета.

Что собирается:
  1. Один PDF: сам выпуск в текущем формате + полные тексты всех статей
     дальше по документу. «ЧИТАТЬ ДАЛЕЕ» ведёт ВНУТРЬ этого же PDF.
     (Ссылка на соседний html-файл в PDF невозможна: Chromium выбрасывает
      относительные ссылки при печати — проверено замером.)
  2. Папка «Статьи»: те же статьи отдельными самодостаточными html —
     открываются двойным кликом, картинки вшиты, интернет не нужен.
"""
from __future__ import annotations

import base64
import html as html_mod
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from urllib.parse import unquote

import requests
from lxml import html as lxml_html

REPO = Path("/Users/apple/Desktop/oiltech-digest")
sys.path.insert(0, str(REPO))
from oiltech_digest.processing.digest import (  # noqa: E402
    _PDF_FOOTER_TEMPLATE, _embedded_font_faces, _format_digest_date, render_digest_email,
)

SCRATCH = Path(__file__).resolve().parent / "data"
OUT_DIR = REPO / "exports" / "Дайджест_2026-08_офлайн"
UA = {"User-Agent": "Mozilla/5.0 OilTechDigest"}

ARTICLE_CSS = """
.article-page{page-break-before:always;break-before:page;padding:34px 46px 40px 46px;
  max-width:760px;margin:0 auto;background:#ffffff;}
.article-kicker{font-size:11px;font-weight:bold;letter-spacing:.08em;text-transform:uppercase;
  color:#e83d08;margin-bottom:10px;}
.article-title{font-family:'GPN Din Condensed','GPN Din',Arial,sans-serif;font-size:28px;
  line-height:32px;color:#003da6;font-weight:bold;margin:0 0 10px 0;}
.article-meta{font-size:11px;font-weight:bold;letter-spacing:.05em;text-transform:uppercase;
  color:#68768f;margin-bottom:6px;}
.article-orig{font-size:11px;color:#68768f;margin-bottom:18px;word-break:break-word;overflow-wrap:anywhere;}
.article-orig a{color:#003da6;}
.article-lead{display:block;width:100%;max-width:660px;border-radius:8px;margin:0 0 18px 0;}
.article-body{font-size:13.5px;line-height:20px;color:#262d3c;}
.article-body p{margin:0 0 11px 0;}
.article-body h1,.article-body h2,.article-body h3{font-family:'GPN Din Condensed','GPN Din',Arial,sans-serif;
  color:#003da6;font-size:17px;line-height:22px;margin:18px 0 8px 0;}
.article-body img{max-width:100%;height:auto;display:block;margin:12px 0;border-radius:6px;}
.article-body table{width:100%;border-collapse:collapse;font-size:12px;margin:12px 0;}
.article-body td,.article-body th{border:1px solid #d9e3f3;padding:5px 7px;text-align:left;}
.article-body li{margin-bottom:5px;}
.article-credit{font-size:11px;line-height:16px;color:#68768f;margin-top:18px;padding-top:12px;border-top:1px solid #d9e3f3;}
.article-note{border:1px solid #f2c9b8;background:#fff5f0;border-radius:8px;padding:14px 16px;
  font-size:13px;line-height:19px;color:#8a3208;}
"""


def data_uri(url: str) -> str:
    if not url:
        return ""
    if url.startswith("data:"):
        return url
    try:
        r = requests.get(url, headers=UA, timeout=25)
    except Exception:
        return ""
    ct = r.headers.get("content-type", "").split(";")[0].strip()
    if not r.ok or not ct.startswith("image/") or len(r.content) > 2_000_000:
        return ""
    return f"data:{ct};base64," + base64.b64encode(r.content).decode("ascii")



def link_label(url: str, limit: int = 105) -> str:
    """Подпись для ссылки: без процент-кодировки и не длиннее строки.

    У angi.ru адрес содержит заголовок в percent-encoding — как есть он занимал
    пять строк нечитаемой каши прямо в документе для ген. директора."""
    try:
        readable = unquote(url)
    except Exception:
        readable = url
    readable = readable.replace("http://", "").replace("https://", "").rstrip("/")
    if len(readable) > limit:
        readable = readable[: limit - 1].rstrip(" -/") + "…"
    return readable


def esc(v: object) -> str:
    return html_mod.escape("" if v is None else str(v), quote=True)




# Служебные хвосты сайтов-источников. Заказчик 25.08 (#429019, «вот это ненужный
# кусок») показал конец статьи CorrosionRADAR: там осталось «Read the article
# online at…» и блок «You might also like» с посторонней статьёй про Норвегию.
# Замер по всем 15 копиям: так у №2, №3 и №14 — все с oilfieldtechnology.com.
_TAIL_MARKERS = ("you might also like", "read the article online at")


def strip_site_tail(body_html: str) -> tuple[str, str | None]:
    """Отрезать хвост статьи начиная с первого служебного блока сайта.

    Режем по ДЕРЕВУ, а не регуляркой по строке: маркер сидит внутри тега, и
    обрезание по позиции подстроки оставило бы незакрытые элементы.
    Возвращает (очищенный html, что именно сработало) — второе для отчёта,
    чтобы «почистилось» не приходилось принимать на веру.
    """
    try:
        tree = lxml_html.fromstring(body_html)
    except Exception:
        return body_html, None
    root = tree.body if tree.tag == "html" and tree.body is not None else tree
    cut_from, marker_hit = None, None
    for child in list(root):
        text = " ".join((child.text_content() or "").split()).lower()
        for marker in _TAIL_MARKERS:
            if text.startswith(marker) or f" {marker}" in text[:200]:
                cut_from, marker_hit = child, marker
                break
        if cut_from is not None:
            break
    if cut_from is None:
        return body_html, None
    dropping = False
    for child in list(root):
        if child is cut_from:
            dropping = True
        if dropping:
            root.remove(child)
    return lxml_html.tostring(root, encoding="unicode"), marker_hit


def manual_body(num: int) -> str | None:
    """Текст статьи, перенесённый вручную, если автоматически снять не удалось.
    Файл `data/article_<NN>_*.html` перекрывает результат capture_articles.py."""
    for path in sorted(SCRATCH.glob(f"article_{num:02d}_*.html")):
        return path.read_text(encoding="utf-8")
    return None


def manual_figure(num: int) -> str:
    """Иллюстрация к перенесённой вручную статье: положить файл
    `data/figure_<NN>.<png|jpg|jpeg|webp>` — он будет вшит в документ."""
    for ext in ("png", "jpg", "jpeg", "webp"):
        path = SCRATCH / f"figure_{num:02d}.{ext}"
        if path.exists():
            mime = "jpeg" if ext in ("jpg", "jpeg") else ext
            return f"data:image/{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")
    return ""


TAILS_STRIPPED: list[tuple[int, str]] = []


def article_block(sig: dict, cap: dict, lead: str) -> str:
    n = sig["n"]
    meta = " · ".join(p for p in [sig["source"], _format_digest_date(sig["published_at"])] if p)
    manual = manual_body(n)
    if manual:
        body = f'<div class="article-body">{manual}</div>'
    elif cap.get("ok"):
        cleaned, hit = strip_site_tail(cap["body_html"])
        if hit:
            TAILS_STRIPPED.append((n, hit))
        body = f'<div class="article-body">{cleaned}</div>'
    else:
        body = (
            '<div class="article-note"><b>Офлайн-копия не снята.</b><br>'
            f'Причина: {esc(cap.get("error") or "источник не отдал текст")}. '
            'Источник российский — ссылка ниже открывается напрямую, без VPN.</div>'
        )
    lead = manual_figure(n) or lead
    lead_html = f'<img class="article-lead" src="{lead}" alt="">' if lead else ""
    return f"""
<div class="article-page" id="a{n}">
  <div class="article-kicker">{esc(sig["category"])} · приоритет {esc(sig["priority"])} · {esc(sig["recommendation"])}</div>
  <div class="article-title">{esc(sig["title"])}</div>
  <div class="article-meta">{esc(meta)}</div>
  <div class="article-orig">Оригинал публикации: <a href="{esc(sig["url"])}">{esc(link_label(sig["url"]))}</a></div>
  {lead_html}
  {body}
</div>"""


def standalone_page(sig: dict, block: str, fonts: str) -> str:
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(sig["title"])}</title>
<style>{fonts}
body{{margin:0;background:#F3F5F8;font-family:'GPN Din',Arial,Helvetica,sans-serif;color:#262d3c;}}
.article-page{{page-break-before:auto;}}
{ARTICLE_CSS}</style></head>
<body>{block}</body></html>"""


def slug(text: str, limit: int = 48) -> str:
    table = {"а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z","и":"i",
             "й":"y","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t",
             "у":"u","ф":"f","х":"h","ц":"c","ч":"ch","ш":"sh","щ":"sch","ъ":"","ы":"y","ь":"",
             "э":"e","ю":"yu","я":"ya"}
    low = "".join(table.get(ch, ch) for ch in text.lower())
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", low)).strip("-")[:limit] or "article"


def main() -> None:
    signals = json.loads((SCRATCH / "signals_resolved.json").read_text(encoding="utf-8"))
    caps = json.loads((SCRATCH / "articles.json").read_text(encoding="utf-8"))
    content = json.loads(sorted((REPO / "exports").glob("digest-2026-08-all15-*.json"))[-1]
                         .read_text(encoding="utf-8"))

    by_title = {s["title"]: s for s in signals}
    leads: dict[int, str] = {}
    for item in content["news"]:
        sig = by_title[item["title"]]
        leads[sig["n"]] = data_uri(item.get("image_url") or "")
        item["url"] = f"#a{sig['n']}"          # «Читать далее» ведёт внутрь документа

    digest_html = render_digest_email(content)
    fonts = _embedded_font_faces()

    blocks, files = [], []
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    art_dir = OUT_DIR / "Статьи"
    art_dir.mkdir(parents=True)

    for item in content["news"]:
        sig = by_title[item["title"]]
        cap = caps.get(str(sig["n"]), {"ok": False, "error": "не снималась"})
        block = article_block(sig, cap, leads.get(sig["n"], ""))
        blocks.append(block)
        name = f"{sig['n']:02d}-{slug(sig['title'])}.html"
        (art_dir / name).write_text(standalone_page(sig, block, fonts), encoding="utf-8")
        files.append(name)

    combined = digest_html.replace(
        "</head>", f"<style>{ARTICLE_CSS}</style></head>", 1
    ).replace("</body>", "\n".join(blocks) + "\n</body>", 1)
    combined_path = SCRATCH / "combined.html"
    combined_path.write_text(combined, encoding="utf-8")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--no-sandbox"])
        pg = b.new_page()
        pg.set_content(combined, wait_until="load")
        pdf = pg.pdf(format="A4", print_background=True,
                     # та же нумерация, что и в render_digest_pdf продукта
                     display_header_footer=True,
                     header_template="<div></div>",
                     footer_template=_PDF_FOOTER_TEMPLATE,
                     margin={"top": "0", "bottom": "12mm", "left": "0", "right": "0"})
        b.close()

    pdf_path = OUT_DIR / "Нефтесервисный_дайджест_2026-08_полный.pdf"
    pdf_path.write_bytes(pdf)

    links = len(re.findall(rb"/Link", pdf))
    uris = len(re.findall(rb"/URI\s*\(", pdf))
    print(f"PDF: {pdf_path.name}  {len(pdf)/1024/1024:.1f} MB")
    print(f"  ссылок-аннотаций в PDF: {links} (внешних /URI: {uris}, остальное — переходы внутрь)")
    print(f"  отдельных html-статей: {len(files)} в «{art_dir.name}»")
    if TAILS_STRIPPED:
        print(f"  вырезано служебных хвостов: {len(TAILS_STRIPPED)}")
        for num, hit in TAILS_STRIPPED:
            print(f"    №{num}: по маркеру «{hit}»")
    for f in files:
        size = (art_dir / f).stat().st_size / 1024
        print(f"    {f}  {size:.0f} KB")


if __name__ == "__main__":
    main()
