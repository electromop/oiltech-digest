"""Seed источников из Excel (лист Sources_Expanded) → таблица sources."""

from __future__ import annotations

import logging

import openpyxl

from oiltech_digest.config import SOURCES_SHEET, SOURCES_XLSX
from oiltech_digest.db.repository import upsert_source

logger = logging.getLogger(__name__)

# Заголовки колонок листа Sources_Expanded → ключи
COL = {
    "group": "Группа",
    "name": "Источник",
    "type": "Тип",
    "url": "Ссылка",
    "rating": "Рейтинг источника",
    "frequency": "Частота мониторинга",
    "role": "Роль в дайджесте",
    "directions": "Покрываемые направления",
}


def _norm_url(raw) -> str | None:
    if raw is None:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    return raw


def _preliminary_strategy(source_type: str) -> str:
    """Предварительная стратегия по типу. Telegram — отдельно (на RSS-этапе пропускается)."""
    if (source_type or "").strip().lower().startswith("telegram"):
        return "telegram"
    return "rss"  # кандидат; финально уточнит discover-rss


def seed_sources_from_excel(path=SOURCES_XLSX, sheet: str = SOURCES_SHEET) -> dict:
    """Прочитать xlsx и upsert-нуть источники по name. Возвращает статистику."""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        ws = wb[sheet]
        rows = ws.iter_rows(values_only=True)
        header = [str(c).strip() if c is not None else "" for c in next(rows)]
        idx = {key: header.index(col) for key, col in COL.items() if col in header}

        missing = [col for key, col in COL.items() if col not in header]
        if missing:
            logger.warning("В листе %s не найдены колонки: %s", sheet, missing)

        stats = {"inserted": 0, "updated": 0, "telegram_flagged": 0, "total": 0}

        for row in rows:
            if row is None:
                continue

            def cell(key):
                i = idx.get(key)
                return row[i] if (i is not None and i < len(row)) else None

            name = cell("name")
            if not name or not str(name).strip():
                continue
            name = str(name).strip()

            source_type = str(cell("type")).strip() if cell("type") else "Unknown"
            strategy = _preliminary_strategy(source_type)

            rating = cell("rating")
            try:
                priority = float(rating) if rating not in (None, "") else 1.0
            except (ValueError, TypeError):
                priority = 1.0

            group = str(cell("group")).strip() if cell("group") else ""
            directions = str(cell("directions")).strip() if cell("directions") else ""
            role = str(cell("role")).strip() if cell("role") else ""
            category = " | ".join(x for x in (group, role, directions) if x) or None

            rec = {
                "name": name,
                "source_type": source_type,
                "url": _norm_url(cell("url")),
                "rss_url": None,
                "enabled": True,
                "parse_strategy": strategy,
                "category": category,
                "update_frequency": str(cell("frequency")).strip() if cell("frequency") else None,
                "priority": priority,
            }
            result = upsert_source(rec)  # 'inserted' | 'updated'
            stats[result] += 1
            stats["total"] += 1
            if strategy == "telegram":
                stats["telegram_flagged"] += 1

        return stats
    finally:
        wb.close()
