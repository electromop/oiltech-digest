"""Забрать по каждой ссылке из xlsx: дату публикации, og:image, заголовок страницы.
Только чтение. Результат — JSON в scratchpad, чтобы дальше собирать выпуск на фактах,
а не на догадках по виду URL."""
import json, sys, re
from pathlib import Path
import openpyxl

sys.path.insert(0, "/Users/apple/Desktop/oiltech-digest")
from oiltech_digest.ingestion.http_client import fetch
from oiltech_digest.ingestion.article_fetcher import extract_og_image
from oiltech_digest.ingestion.request_parser import parse_article_page

XLSX = "/Users/apple/Downloads/Сигналы_август_2026_на_выбор_v3.xlsx"
OUT = Path(__file__).resolve().parent / "data" / "meta.json"

wb = openpyxl.load_workbook(XLSX, data_only=True)
ws = wb["Сигналы на выбор"]
rows = list(ws.iter_rows(min_row=2, values_only=True))
rows = [r for r in rows if r[0]]

result = []
for r in rows:
    num, url = str(r[0]), (r[11] or "").strip()
    rec = {"n": num, "url": url, "ok": False, "title": None, "published_at": None,
           "image_url": None, "chars": 0, "error": None}
    if not url:
        rec["error"] = "нет ссылки в файле"
        result.append(rec); print(num, "SKIP no url"); continue
    try:
        content = fetch(url)
        if not content:
            rec["error"] = "fetch вернул пусто"
        else:
            title, published_at, text = parse_article_page(content, "")
            rec["ok"] = True
            rec["title"] = (title or "").strip()[:300]
            rec["published_at"] = published_at.isoformat() if published_at else None
            rec["image_url"] = extract_og_image(content)
            rec["chars"] = len(text or "")
    except Exception as exc:
        rec["error"] = f"{type(exc).__name__}: {exc}"
    result.append(rec)
    print(num, "ok" if rec["ok"] else "FAIL", rec["published_at"], (rec["error"] or "")[:80])

OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print("written", OUT)
