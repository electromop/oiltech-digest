# -*- coding: utf-8 -*-
"""Собрать выпуск дайджеста из xlsx «Сигналы август 2026 на выбор v3»
СТРОГО в текущем формате платформы: тот же шаблон digest_email_template.html,
тот же брендинг, тот же рендер render_digest_email / render_digest_docx.

Никакой код продукта не правится — здесь только подготовка content-словаря
той же формы, что возвращает build_digest_content().
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import edits_victor  # noqa: E402

REPO = Path("/Users/apple/Desktop/oiltech-digest")
sys.path.insert(0, str(REPO))

from oiltech_digest.processing.digest import (  # noqa: E402
    _load_digest_branding,
    render_digest_docx,
    render_digest_email,
)

XLSX = "/Users/apple/Downloads/Сигналы_август_2026_на_выбор_v3.xlsx"
MONTH = "2026-08"           # для имён файлов
MONTH_LABEL = "август 2026 г"  # для шапки выпуска — правка Виктора #428915
SCRATCH = Path(__file__).resolve().parent / "data"
EXPORTS = REPO / "exports"

ANGI_URL = ("http://www.angi.ru/news/2935314-"
            + quote("На Южно-Приобском месторождении начали использовать беспилотные грузовики") + "/")

# Дата и картинка по каждому сигналу + откуда взято.
# source: "страница" — прочитано со страницы публикации; "адрес" — из адреса публикации;
# "нет" — установить не удалось (в карточке дата не показывается).
ENRICH: dict[str, dict] = {
    "1":  {"date": "2026-08-01", "date_src": "страница (JPT)",},
    "2":  {"date": "2026-08-19", "date_src": "страница (Oilfield Technology)",},
    "3":  {"date": "2026-08-03", "date_src": "страница (Oilfield Technology)",
           "url_override": "https://www.oilfieldtechnology.com/special-reports/03082026/corrosionradar-monitoring-solution-selected-to-support-dows-alberta-project/",
           "url_note": "в файле стояла главная страница издания — заменено на конкретную публикацию"},
    "4":  {"date": "2026-08-17", "date_src": "заголовок документа SEC (INVESTOR PRESENTATION, August 17, 2026)",},
    "5":  {"date": "2026-08-19", "date_src": "страница (Vertechs)", "img": ""},
    "6":  {"date": "2026-08-05", "date_src": "страница (JPT)",},
    "7":  {"date": "2026-08-17", "date_src": "страница публикации (Scientific Russia, 17.08.2026 15:30) — прислана владельцем, сайт из внешней сети не открывается"},
    "8":  {"date": "2026-08-19", "date_src": "страница (U.S. EIA)",},
    "9":  {"date": "2026-08-07", "date_src": "страница (Интерфакс)",},
    "10": {"date": "2026-08-04", "date_src": "адрес публикации SLB (2026-0804) — на странице даты нет", "img": ""},
    "11": {"date": "2026-08-05", "date_src": "страница (Oilfield Technology)",},
    "12": {"date": "2026-08-11", "date_src": "страница (World Oil)",},
    "13": {"date": "2026-08-07", "date_src": "страница (Агентство нефтегазовой информации, 07 августа 13:32)",
           "url_override": ANGI_URL,
           "url_note": "в файле ссылки не было — найдена публикация того же источника, совпадает по фактам (24 т, Южно-Приобское)"},
    "14": {"date": "2026-08-17", "date_src": "страница (Oilfield Technology)",},
    "15": {"date": "", "date_src": "нет — страница решения, а не датированная новость",},
}


def load_images() -> dict[str, str]:
    """Картинки берём ИЗ РЕЗУЛЬТАТОВ ЗАМЕРА (og:image, прочитанный со страницы),
    а не переписываем руками: ручной перенос уже дал 4 битых адреса из 15."""
    images: dict[str, str] = {}
    meta = json.loads((SCRATCH / "meta.json").read_text(encoding="utf-8"))
    for rec in meta:
        if rec.get("image_url"):
            images[rec["n"]] = rec["image_url"]
    extra = json.loads((SCRATCH / "meta_extra.json").read_text(encoding="utf-8"))
    for key, rec in extra.items():
        if key in {"3", "13"} and rec.get("image_url"):
            images[key] = rec["image_url"]
    # у №3 в файле стояла главная страница издания — og:image главной не годится
    images.pop("3", None)
    if extra.get("3", {}).get("image_url"):
        images["3"] = extra["3"]["image_url"]
    # №8 (EIA): og:image самой EIA указывает на .png, которого на сервере нет (404) —
    # реальный график лежит как .svg, а SVG почтовые клиенты не рисуют. Ставим
    # фирменную плашку вместо заведомо битой картинки.
    images.pop("8", None)
    # №3 (CorrosionRADAR): og:image издания — это карта Саскачевана, хотя проект Dow
    # находится во Fort Saskatchewan, то есть в АЛЬБЕРТЕ. Издание промахнулось само,
    # а в выпуске это читалось как «кусок карты Канады» без связи с темой (#429006 п.3).
    images.pop("3", None)
    return images


IMAGES = load_images()



_NBSP = "\u00A0"
_UNITS = r"т|кг|км|м|мм|см|МПа|°C|футов|фут|ft|тыс\.|млн|млрд|%"


def protect_numbers(text: str) -> str:
    """Склеить неразрывным пробелом то, что нельзя разрывать между строками.

    Понадобилось из-за выравнивания по ширине: строка рвётся именно по пробелу,
    и «15 000 футов» в собранном выпуске разъехалось на «15» в конце строки и
    «000 футов» в начале следующей. Правило общее, а не подстановка трёх строк:
    в следующем выпуске числа будут другие.
    """
    # Просмотры назад/вперёд, а не захват: захватывающая версия съедала цифру,
    # и в «1 234 567» второй паре её уже не хватало — склеивалась только первая.
    text = re.sub(r"(?<=\d)[ \u00A0](?=\d{3}(?!\d))", _NBSP, text)
    # Без \b на конце: у «%» и «°C» границы слова нет, и правило молча не срабатывало.
    return re.sub(rf"(?<=\d)[ \u00A0](?=(?:{_UNITS})(?!\w))", _NBSP, text)


def read_signals() -> list[dict]:
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    ws = wb["Сигналы на выбор"]
    out = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row[0]:
            continue
        n = str(row[0]).strip()
        enrich = ENRICH.get(n, {})
        url = (enrich.get("url_override") or (row[11] or "")).strip()
        gist = str(row[4] or "").strip()
        why = str(row[5] or "").strip()
        summary = gist if not why else f"{gist} Почему важно: {why}"
        summary = protect_numbers(edits_victor.apply_summary(int(n), summary))
        title = str(row[3] or "").strip().replace("Супедлинные", "Сверхдлинные")
        title = protect_numbers(edits_victor.apply_title(int(n), title))
        out.append({
            "n": int(n),
            "category": str(row[1] or "").strip(),
            "nsr": str(row[2] or "").strip(),
            "title": title,
            "gist": gist,
            "why": why,
            "signal_type": str(row[6] or "").strip(),
            "maturity": str(row[7] or "").strip(),
            "priority": float(row[8]) if row[8] not in (None, "") else 0.0,
            "recommendation": str(row[9] or "").strip().upper(),
            "source": str(row[10] or "").strip(),
            "url": url,
            "url_original": str(row[11] or "").strip(),
            "url_note": enrich.get("url_note", ""),
            "published_at": enrich.get("date", "") or None,
            "date_src": enrich.get("date_src", ""),
            "image_url": IMAGES.get(n, ""),
            "summary": summary,
        })
    return out


def to_card(sig: dict) -> dict:
    """Карточка в той же форме, что отдаёт build_digest_content()."""
    return {
        "category": sig["category"],
        "article_id": None,
        "title": sig["title"],
        "source": sig["source"],
        "url": sig["url"],
        "published_at": sig["published_at"],
        "tag": sig["category"],
        "score": sig["priority"],
        "score_label": sig["recommendation"],
        "summary": sig["summary"],
        "image_url": sig["image_url"],
    }


def build_content(signals: list[dict]) -> dict:
    branding = _load_digest_branding()
    issue_cfg = branding["issue"]
    news = [to_card(s) for s in signals]
    title = issue_cfg["title_template_with_month"].format(month=MONTH_LABEL)
    intro = issue_cfg["intro_template_with_month"].format(month=MONTH_LABEL)
    return {
        "month": MONTH,
        "title": title,
        "issue": {
            "title": title,
            "period": MONTH_LABEL,
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
        "highlights": [],   # текущий шаблон блок «Главное за период» не рендерит
        "footer": {
            "contact_text": branding["footer"]["contact_text"],
            "contact_email": branding["footer"]["contact_email"],
            "note": branding["footer"]["note"],
            "socials": branding["footer"]["socials"],
        },
        "branding": branding,
    }


def emit(signals: list[dict], slug: str, stamp: str) -> dict:
    content = build_content(signals)
    EXPORTS.mkdir(parents=True, exist_ok=True)
    base = EXPORTS / f"digest-{MONTH}-{slug}-{stamp}"
    html_path = base.with_suffix(".html")
    html_path.write_text(render_digest_email(content), encoding="utf-8")
    json_path = base.with_suffix(".json")
    json_path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    docx_path = base.with_suffix(".docx")
    docx_path.write_bytes(render_digest_docx(content))
    return {"html": str(html_path), "json": str(json_path), "docx": str(docx_path),
            "items": len(content["items"])}


def main() -> None:
    signals = read_signals()
    # порядок как в дайджесте платформы — по убыванию балла, при равенстве сохраняем
    # исходный порядок аналитика (sorted стабилен)
    ordered = sorted(signals, key=lambda s: -s["priority"])
    top = [s for s in ordered if s["recommendation"] == "ТОП"]

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    res_all = emit(ordered, "all15", stamp)
    res_top = emit(top, "top6", stamp)

    (SCRATCH / "signals_resolved.json").write_text(
        json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8")

    print("ВЫПУСК: все 15 сигналов ->", json.dumps(res_all, ensure_ascii=False, indent=2))
    print("ВЫПУСК: только ТОП      ->", json.dumps(res_top, ensure_ascii=False, indent=2))
    print("\nПорядок в полном выпуске:")
    for i, s in enumerate(ordered, 1):
        date = s["published_at"] or "дата не установлена"
        print(f"{i:>2}. [{s['priority']:>4}] {s['recommendation']:<9} {date}  {s['title'][:62]}")


if __name__ == "__main__":
    main()
