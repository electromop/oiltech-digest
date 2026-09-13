# -*- coding: utf-8 -*-
"""Снять офлайн-копию каждой статьи выпуска: чистый текст + встроенные картинки.

Зачем: читать будет ген. директор БЕЗ VPN, а большинство источников выпуска —
западные сайты. Копия должна открываться без интернета вообще, поэтому все
картинки вшиваются в документ как data-URI, внешних зависимостей не остаётся.
"""
from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import trafilatura
from lxml import html as lxml_html

REPO = Path("/Users/apple/Desktop/oiltech-digest")
sys.path.insert(0, str(REPO))
from oiltech_digest.ingestion.http_client import fetch  # noqa: E402

SCRATCH = Path(__file__).resolve().parent / "data"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126 Safari/537.36"}
MAX_IMG_BYTES = 1_500_000


def inline_image(src: str, base_url: str) -> str | None:
    if src.startswith("data:"):
        return src
    absolute = urljoin(base_url, src)
    if not absolute.startswith(("http://", "https://")):
        return None
    try:
        resp = requests.get(absolute, headers=UA, timeout=25)
    except Exception:
        return None
    ctype = resp.headers.get("content-type", "").split(";")[0].strip()
    if not resp.ok or not ctype.startswith("image/") or not resp.content:
        return None
    if len(resp.content) > MAX_IMG_BYTES:
        return None
    return f"data:{ctype};base64," + base64.b64encode(resp.content).decode("ascii")



# Пункты меню, по которым видно, что извлечение уехало в навигацию.
_NAV_MARKERS = ("contact us", "about us", "products & services", "products &amp; services",
                "privacy policy", "careers", "sitemap")


def _plain_len(fragment: str | None) -> int:
    if not fragment:
        return 0
    return len(" ".join(re.sub(r"<[^>]+>", " ", fragment).split()))


def _looks_like_navigation(fragment: str) -> bool:
    text = " ".join(re.sub(r"<[^>]+>", " ", fragment).split()).lower()
    return sum(1 for marker in _NAV_MARKERS if marker in text) >= 2


def extract_body(raw_str: str, url: str) -> tuple[str | None, str]:
    """Сначала строгое извлечение, favor_recall — только как запасной путь.

    Изначально здесь стоял безусловный favor_recall=True, и на самой тонкой статье
    выпуска (Vertechs, 970 знаков полезного текста) он втянул меню сайта: 1533 знака,
    из которых 563 — навигация, и она шла ПЕРВОЙ, до текста статьи. Замер на той же
    странице: без favor_recall — 970 знаков и ни одного пункта меню.

    Поэтому расширенный захват берём, только если он реально длиннее строгого И не
    состоит из навигации.
    """
    common = dict(output_format="html", include_images=True, include_links=False,
                  include_tables=True, url=url)
    strict = trafilatura.extract(raw_str, **common)
    if _plain_len(strict) >= 500:
        return strict, "strict"
    recall = trafilatura.extract(raw_str, favor_recall=True, **common)
    if recall and _plain_len(recall) > _plain_len(strict) and not _looks_like_navigation(recall):
        return recall, "recall"
    return strict or recall, "strict" if strict else "recall"


def capture(url: str) -> dict:
    out = {"url": url, "ok": False, "body_html": "", "images_inlined": 0,
           "images_dropped": 0, "chars": 0, "error": None, "mode": None}
    raw = fetch(url)
    if not raw:
        out["error"] = "страница не отдалась"
        return out
    raw_str = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw
    extracted, mode = extract_body(raw_str, url)
    out["mode"] = mode
    if not extracted:
        out["error"] = "trafilatura не выделила текст"
        return out
    tree = lxml_html.fromstring(extracted)
    for img in tree.xpath("//graphic|//img"):
        src = img.get("src") or img.get("data-src") or ""
        data_uri = inline_image(src, url) if src else None
        if data_uri:
            new = lxml_html.Element("img")
            new.set("src", data_uri)
            img.getparent().replace(img, new)
            out["images_inlined"] += 1
        else:
            out["images_dropped"] += 1
            parent = img.getparent()
            if parent is not None:
                parent.remove(img)
    body = lxml_html.tostring(tree, encoding="unicode")
    body = re.sub(r"</?(script|style|iframe|form|input|button)[^>]*>", "", body, flags=re.I)
    out["body_html"] = body
    out["chars"] = len(tree.text_content())
    out["ok"] = out["chars"] > 200
    if not out["ok"]:
        out["error"] = f"слишком мало текста: {out['chars']} знаков"
    return out


def main() -> None:
    signals = json.loads((SCRATCH / "signals_resolved.json").read_text(encoding="utf-8"))
    result = {}
    for sig in signals:
        n = str(sig["n"])
        url = sig["url"]
        if not url:
            result[n] = {"url": "", "ok": False, "error": "ссылки нет"}
            print(f"{n:>3} — ссылки нет"); continue
        rec = capture(url)
        result[n] = rec
        flag = "ok " if rec["ok"] else "FAIL"
        print(f"{n:>3} {flag} знаков={rec['chars']:<6} режим={rec.get('mode') or '-':<7} картинок={rec['images_inlined']}"
              f"(вырезано {rec['images_dropped']}) {rec['error'] or ''}")
    (SCRATCH / "articles.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    ok = sum(1 for r in result.values() if r.get("ok"))
    print(f"\nснято {ok} из {len(result)}")


if __name__ == "__main__":
    main()
