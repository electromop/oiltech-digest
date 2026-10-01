"""Эталон заказчика для радара: его таблица ТОП-сигналов — в обучение и в замер полноты.

Таблица «Сигналы_база_2026_ТОП…» (листы «ТОП ранних сигналов», «Август_2026», «Сентябрь_2026»):
направление, сигнал, краткая суть, почему важно для нефтесервиса, рекомендация, источник, ссылка.
Сам файл в репозиторий не кладём (он публичный): команда читает его с диска, а эталон живёт в
базе — в памяти радара, как одобренные заказчиком находки.

- import: строка таблицы — вердикт «сильный сигнал» в signal_agent_memory, причина — «почему
  важно» и рекомендация. Дальше его видят и судья (feedback_prompt_block), и поиск «режима
  ChatGPT» (research_feedback_block): «ищи события того же типа и уровня».
- recall: сколько событий эталона радар нашёл — по адресу ссылки или по названию (компания и
  продукт в карточке). Для замера после каждой правки отбора, а не сверки на глаз.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from oiltech_digest.db import repository

REFERENCE_ORIGIN = "customer_reference"
_COLUMNS = {
    "direction": "Направление",
    "title": "Сигнал / технология",
    "summary": "Краткая суть",
    "why": "Почему важно для нефтесервиса",
    "recommendation": "Рекомендация",
    "source": "Источник",
    "url": "Ссылка",
    "priority": "Приоритет",
    "maturity": "Стадия зрелости",
}


def read_reference(path: str | Path, *, sheets: list[str] | None = None) -> list[dict[str, Any]]:
    """Строки эталона: все листы с колонкой «Сигнал / технология» или только названные."""
    import openpyxl

    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows: list[dict[str, Any]] = []
    for sheet in book.worksheets:
        if sheets and sheet.title not in sheets:
            continue
        values = sheet.iter_rows(values_only=True)
        header = [str(cell or "").strip() for cell in next(values, ())]
        index = {key: header.index(name) for key, name in _COLUMNS.items() if name in header}
        if "title" not in index:
            continue
        for line in values:
            title = _cell(line, index, "title")
            if not title:
                continue
            row = {key: _cell(line, index, key) for key in _COLUMNS}
            row["sheet"] = sheet.title
            rows.append(row)
    return rows


def import_reference(rows: list[dict[str, Any]], *, apply: bool = False) -> dict[str, Any]:
    """Эталон — в память радара как «сильный сигнал». Повтор не плодит строк: ключ — ссылка
    (или название без ссылки), правка таблицы обновляет ту же память."""
    planned = []
    for row in rows:
        reason = " ".join(part for part in (row.get("why"), _recommendation(row.get("recommendation"))) if part)
        planned.append({
            "memory_key": f"signal_verdict:{REFERENCE_ORIGIN}:{_url_key(row.get('url')) or _slug(row['title'])}",
            "facts": {
                "signal_title": row["title"],
                "reason": reason,
                "topic": row.get("direction") or "",
                "source_url": row.get("url") or "",
                "summary": row.get("summary") or "",
                "origin": REFERENCE_ORIGIN,
                "sheet": row.get("sheet"),
            },
        })
    if apply:
        for item in planned:
            repository.upsert_signal_agent_memory(
                memory_key=item["memory_key"],
                memory_type="signal_verdict",
                subject="strong_signal",
                score=100,
                facts=item["facts"],
            )
    return {"rows": len(rows), "memories": len(planned), "applied": apply,
            "sheets": sorted({str(row.get("sheet")) for row in rows})}


def recall(rows: list[dict[str, Any]], cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Сколько событий эталона нашёл радар. Совпадение — та же ссылка или в карточке есть все
    ключевые имена из заголовка эталона (компания, продукт: «Expro SafeWells V5»)."""
    prepared = [{
        "id": card.get("id"),
        "text": _norm(" ".join(str(card.get(key) or "") for key in ("title", "title_ru", "summary"))
                      + " " + " ".join(str(item) for item in card.get("companies") or [])),
        "urls": {_url_key(url) for url in card.get("urls") or [] if url},
        "visible": card.get("visible", True),
    } for card in cards]
    found, missing = [], []
    for row in rows:
        url = _url_key(row.get("url"))
        names = _key_names(row["title"])
        match = next((card for card in prepared if url and url in card["urls"]), None)
        if match is None and names:
            match = next((card for card in prepared if all(name in card["text"] for name in names)), None)
        entry = {"title": row["title"], "sheet": row.get("sheet")}
        if match is None:
            missing.append(entry)
        else:
            found.append({**entry, "signal_id": match["id"], "visible": match["visible"]})
    total = len(rows)
    return {
        "total": total,
        "found": len(found),
        "found_visible": sum(1 for item in found if item["visible"]),
        "share": round(len(found) / total, 3) if total else 0.0,
        "found_items": found,
        "missing_items": missing,
    }


def _key_names(title: str) -> list[str]:
    """Имена из заголовка эталона до тире: латиница и слова с заглавной, без общих слов."""
    head = re.split(r"\s[—–-]\s", title, maxsplit=1)[0]
    words = re.findall(r"[A-Za-zА-ЯЁ][A-Za-zА-Яа-яЁё0-9\-]{2,}", head)
    names = [_norm(word) for word in words if _norm(word) not in _GENERIC]
    return names[:2]


_GENERIC = {"система", "технология", "новый", "новая", "первая", "единый", "компания"}


def _cell(line: tuple, index: dict[str, int], key: str) -> str:
    position = index.get(key)
    if position is None or position >= len(line) or line[position] is None:
        return ""
    return re.sub(r"\s+", " ", str(line[position])).strip()


def _recommendation(text: str | None) -> str:
    text = (text or "").strip()
    return f"Рекомендация заказчика: {text}" if text else ""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е")).strip()


def _url_key(url: str | None) -> str:
    value = re.sub(r"^https?://", "", str(url or "").strip().lower())
    value = value.split("#", 1)[0].split("?", 1)[0].rstrip("/").removeprefix("www.")
    return value


def _slug(text: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", "-", _norm(text)).strip("-")[:120]
