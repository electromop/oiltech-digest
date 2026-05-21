"""Seed tags and scoring criteria from domain workbooks/defaults."""

from __future__ import annotations

import re

import openpyxl

from oiltech_digest.config import DIRECTIONS_XLSX
from oiltech_digest.db import repository


DIRECTIONS_SHEET = "Направления"
KEYWORDS_SHEET = "Ключевые слова"


DEFAULT_SCORING_CRITERIA = [
    {
        "name": "Технологическая новизна",
        "description": "Насколько материал описывает новую или заметно улучшенную технологию, сервис, оборудование или метод.",
        "weight": 35,
        "keywords_json": ["новая технология", "пилот", "разработка", "автоматизация", "инновация"],
        "keywords_en_json": ["new technology", "pilot", "development", "automation", "innovation"],
        "sort_order": 10,
    },
    {
        "name": "Применимость для РФ и зрелых активов",
        "description": "Насколько решение потенциально применимо в российских нефтегазовых условиях, на зрелых месторождениях или в сложной логистике.",
        "weight": 30,
        "keywords_json": ["зрелые месторождения", "импортозамещение", "трудноизвлекаемые", "снижение затрат"],
        "keywords_en_json": ["mature fields", "hard-to-recover", "cost reduction", "remote operations"],
        "sort_order": 20,
    },
    {
        "name": "Бизнес-эффект",
        "description": "Ожидаемый эффект по добыче, срокам, безопасности, CAPEX/OPEX, НПВ или операционной устойчивости.",
        "weight": 25,
        "keywords_json": ["эффект", "экономия", "снижение затрат", "рост добычи", "безопасность"],
        "keywords_en_json": ["efficiency", "cost savings", "production increase", "safety", "NPT reduction"],
        "sort_order": 30,
    },
    {
        "name": "Достоверность и зрелость сигнала",
        "description": "Надёжность источника и зрелость события: промышленный запуск, контракт, результаты испытаний важнее ранних заявлений.",
        "weight": 10,
        "keywords_json": ["контракт", "промышленный", "результаты испытаний", "внедрение"],
        "keywords_en_json": ["contract", "commercial deployment", "field trial", "test results", "implementation"],
        "sort_order": 40,
    },
]


def seed_tags_from_directions(path=DIRECTIONS_XLSX) -> dict:
    """Load D01-D18 as top-level tags with RU/EN keywords."""
    wb = openpyxl.load_workbook(path, data_only=True)
    try:
        directions = _sheet_dicts(wb[DIRECTIONS_SHEET])
        keyword_rows = {row["ID направления"]: row for row in _sheet_dicts(wb[KEYWORDS_SHEET])}

        total = 0
        for order, row in enumerate(directions, start=10):
            direction_id = row.get("ID")
            if not direction_id:
                continue
            keywords = keyword_rows.get(direction_id, {})
            repository.upsert_tag(
                {
                    "parent_id": None,
                    "name": row.get("Направление RU") or row.get("Direction EN"),
                    "name_en": row.get("Direction EN"),
                    "description": row.get("Что покрывает"),
                    "keywords_json": _split_keywords(keywords.get("Ключевые слова RU")),
                    "keywords_en_json": _split_keywords(keywords.get("Keywords EN")),
                    "sort_order": order,
                }
            )
            total += 1
        return {"tags": total}
    finally:
        wb.close()


def seed_default_scoring_criteria() -> dict:
    for rec in DEFAULT_SCORING_CRITERIA:
        repository.upsert_scoring_criterion(rec)
    return {"criteria": len(DEFAULT_SCORING_CRITERIA), "weight_sum": 100}


def _sheet_dicts(ws) -> list[dict]:
    rows = list(ws.iter_rows(values_only=True))
    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    result = []
    for row in rows[1:]:
        rec = {}
        for i, name in enumerate(header):
            if not name:
                continue
            value = row[i] if i < len(row) else None
            rec[name] = str(value).strip() if value is not None else ""
        result.append(rec)
    return result


def _split_keywords(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in re.split(r";|\n", value) if part and part.strip()]

