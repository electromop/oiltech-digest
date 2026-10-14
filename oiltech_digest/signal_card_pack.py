"""Пакет эксперту для агента «Карточка сигнала» (#94, спека #93): справочники и разметка 50 сигналов.

Справочники — версионируемые данные в репозитории (`signal_card_dictionaries.json`): их же
потом читает агент, версия идёт в рецепт карточки. Таблица эксперту — лист справочников на
утверждение и лист разметки: 35 принятых статей ленты за август–октябрь и 15 карточек радара
(технологии и бизнес), по темам вперемешку.

Разметка вслепую: в таблице только то, что написал издатель, — заголовок, текст, ссылка, дата
публикации. Сути, перевода, темы, решений судьи радара в ней нет. Отсюда правила выборки радара:
главная публикация — статья корпуса или страница, которую докачали (`full_text_fetched`); без
докачки текст находки «режима ChatGPT» — сводка модели. Заголовок и дата — тоже издателя:
у находки «режима ChatGPT» дата ссылки — это дата события по версии модели.

Таблица и манифест (идентификаторы, отпечаток текста) пишутся вне репозитория — он публичный.
Манифест фиксирует входы экзамена: разметка сверяется с теми же текстами, что получит агент.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
from typing import Any

from oiltech_digest import config, feed_window
from oiltech_digest.db import repository

DICTIONARIES_PATH = Path(__file__).with_name("signal_card_dictionaries.json")
REPO_ROOT = Path(__file__).resolve().parents[1]
WORKBOOK_NAME = "signal_card_markup.xlsx"
MANIFEST_NAME = "signal_card_markup_manifest.json"
MARKUP_SHEET = "Разметка"
DICTIONARY_SHEET = "Справочники"
LISTS_SHEET = "_списки"
UNDEFINED = "не определено"
TEXT_LIMIT = 6000
FEED_COUNT, RADAR_COUNT = 35, 15
RADAR_QUOTA = {"technology": 10, "business": 5}
CATEGORY_RU = {"technology": "технология", "business": "бизнес"}
DEFAULT_SEED = 20261014
DEFAULT_SINCE, DEFAULT_UNTIL = "2026-08-01", "2026-11-01"
_RESEARCH_SOURCE = "openai_web_search"

# Колонки, которые заполняет эксперт: (заголовок, справочник для выпадающего списка или None).
MARKUP_COLUMNS = [
    ("Дата события", None),
    ("Источник даты события", "date_source"),
    ("Компании-участники", None),
    ("Тип события", "event_type"),
    ("Регион", "region"),
    ("Класс технологии", "technology_class"),
    ("Зрелость", "maturity"),
    ("Связь с нефтесервисом", "oilfield_relevance"),
    ("Применение в нефтесервисе", None),
    ("Почему важно нефтесервису", None),
    ("Комментарий эксперта", None),
]
_GIVEN_COLUMNS = ["ID", "Заголовок", "Текст публикации", "Ссылка", "Дата публикации"]
_APPROVAL = ["event_type", "region", "technology_class", "maturity"]

INSTRUCTION = [
    "Разметка 50 сигналов для проверки агента «Карточка сигнала».",
    "Лист «Справочники»: проверьте значения; в колонке «Решение» — «да», «убрать» или своя правка.",
    "Лист «Разметка»: по каждой строке прочитайте текст публикации и заполните колонки справа.",
    "Дата события — ГГГГ-ММ-ДД, если она названа в тексте или следует из него; иначе — дата публикации и",
    "  «по дате публикации» в «Источнике даты». Компании — только участники события, через «;»; издатель",
    "  публикации участником не считается.",
    "Колонки со справочниками — выпадающий список. Нельзя определить по тексту — «не определено».",
    "Применение и «почему важно» — одной фразой. Сомнения — в «Комментарий эксперта».",
]


def load_dictionaries() -> dict[str, Any]:
    return json.loads(DICTIONARIES_PATH.read_text(encoding="utf-8"))


def select_sample(*, seed: int = DEFAULT_SEED, since: str = DEFAULT_SINCE, until: str = DEFAULT_UNTIL,
                  feed_count: int = FEED_COUNT, radar_count: int = RADAR_COUNT) -> list[dict[str, Any]]:
    """50 сигналов: статьи ленты и карточки радара, темы по кругу. Тот же seed на тех же
    данных — те же сигналы: кандидаты упорядочены по номеру, перемешивает только seed."""
    rng = random.Random(seed)
    feed = _round_robin(_feed_candidates(since, until), feed_count, rng)
    taken = {item["article_id"] for item in feed}
    radar_pool = [item for item in _radar_candidates() if item.get("article_id") not in taken]
    radar: list[dict[str, Any]] = []
    for category, quota in RADAR_QUOTA.items():
        share = round(radar_count * quota / sum(RADAR_QUOTA.values()))
        radar += _round_robin([item for item in radar_pool if item["category_code"] == category], share, rng)
    if len(radar) < radar_count:  # одной категории не хватило — добираем другой
        chosen = {item["signal_id"] for item in radar}
        radar += _round_robin([item for item in radar_pool if item["signal_id"] not in chosen],
                              radar_count - len(radar), rng)
    return feed + radar[:radar_count]


def export_pack(out_dir: str | Path | None, *, seed: int = DEFAULT_SEED, since: str = DEFAULT_SINCE,
                until: str = DEFAULT_UNTIL, apply: bool = False) -> dict[str, Any]:
    sample = select_sample(seed=seed, since=since, until=until)
    result = {"composition": composition(sample), "applied": False, "count": len(sample)}
    if not apply:
        return result
    out = _checked_out_dir(out_dir)
    dictionaries = load_dictionaries()
    out.mkdir(parents=True, exist_ok=True)
    _write_workbook(out / WORKBOOK_NAME, sample, dictionaries)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed": seed, "since": since, "until": until,
        "dictionaries_version": dictionaries["version"], "text_limit": TEXT_LIMIT,
        "composition": result["composition"],
        "items": [{key: item.get(key) for key in (
            "id", "origin", "article_id", "signal_id", "evidence_id", "url", "category", "theme",
            "text_sha256", "text_chars")} for item in sample],
    }
    (out / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**result, "applied": True, "workbook": str(out / WORKBOOK_NAME), "manifest": str(out / MANIFEST_NAME)}


def composition(sample: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, dict[str, int]] = {"origin": defaultdict(int), "category": defaultdict(int),
                                          "theme": defaultdict(int)}
    for item in sample:
        counts["origin"][item["origin"]] += 1
        counts["category"][item["category"]] += 1
        counts["theme"][item["theme"]] += 1
    return {key: dict(sorted(value.items())) for key, value in counts.items()}


class PackError(ValueError):
    pass


def _checked_out_dir(out_dir: str | Path | None) -> Path:
    if not out_dir:
        raise PackError("нужен --out: папка вне репозитория")
    out = Path(out_dir).expanduser().resolve()
    if out == REPO_ROOT or REPO_ROOT in out.parents:
        raise PackError(f"{out} — внутри репозитория, а он публичный: укажите папку вне его")
    existing = [name for name in (WORKBOOK_NAME, MANIFEST_NAME) if (out / name).exists()]
    if existing:
        raise PackError(f"в {out} уже есть {', '.join(existing)} — не затираю разметку; укажите другую папку")
    return out


def _feed_candidates(since: str, until: str) -> list[dict[str, Any]]:
    """Принятые отбором статьи (relevant = TRUE), видимые в ленте, собранные в периоде."""
    with repository.get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT a.id, a.title, a.url, a.raw_text, COALESCE(a.published_at, a.collected_at) AS published,
                   COALESCE(p.name, t.name, 'без темы') AS theme
            FROM articles a
            JOIN article_cards c ON c.article_id = a.id
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN article_tags at ON at.article_id = a.id
            LEFT JOIN tags t ON t.id = at.tag_id
            LEFT JOIN tags p ON p.id = t.parent_id
            WHERE c.relevant IS TRUE AND {feed_window.visible_sql('a', 'c', 's')}
              AND a.collected_at >= %s::date AND a.collected_at < %s::date
              AND a.full_text_status IS DISTINCT FROM 'mismatch'
              AND length(btrim(COALESCE(a.raw_text, ''))) >= %s
            ORDER BY a.id
            """,
            (since, until, config.MIN_ARTICLE_TEXT_CHARS),
        ).fetchall()
    return [_item("feed", f"article:{row[0]}", article_id=row[0], title=row[1], url=row[2], text=row[3],
                  published=row[4], theme=row[5], category_code="business") for row in rows]


def _radar_candidates() -> list[dict[str, Any]]:
    """Карточки радара (технология и бизнес) с публикацией, текст которой написал издатель.
    Дубли, архив и отсеянное машиной — мимо: в экзамене нужны события, а не брак поиска."""
    with repository.get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (s.id)
                   s.id, s.signal_category, s.theme, e.id, a.id,
                   COALESCE(a.title, e.title), COALESCE(a.url, e.source_url),
                   COALESCE(a.raw_text, e.extracted_fact),
                   CASE WHEN a.id IS NOT NULL THEN COALESCE(a.published_at, a.collected_at)
                        WHEN e.raw_payload_json->>'evidence_source' IS DISTINCT FROM %s THEN e.published_at END
            FROM signals s
            JOIN signal_evidence e ON e.signal_id = s.id
            LEFT JOIN articles a ON a.id = e.article_id
            WHERE s.merged_into_signal_id IS NULL AND s.archived_at IS NULL AND s.filter_stage IS NULL
              AND s.signal_category IN ('technology', 'business')
              AND (length(btrim(COALESCE(a.raw_text, ''))) >= %s
                   OR (a.id IS NULL AND e.raw_payload_json->>'full_text_fetched' = 'true'
                       AND length(btrim(COALESCE(e.extracted_fact, ''))) >= %s))
            ORDER BY s.id, e.strength DESC, e.id
            """,
            (_RESEARCH_SOURCE, config.MIN_ARTICLE_TEXT_CHARS, config.MIN_ARTICLE_TEXT_CHARS),
        ).fetchall()
    return [_item("radar", f"signal:{row[0]}", signal_id=row[0], category_code=row[1], theme=row[2],
                  evidence_id=row[3], article_id=row[4], title=row[5], url=row[6], text=row[7], published=row[8])
            for row in rows]


def _item(origin: str, item_id: str, *, text: str | None, published: Any, category_code: str,
          **fields: Any) -> dict[str, Any]:
    body = (text or "").strip()[:TEXT_LIMIT]
    return {
        **fields, "origin": origin, "id": item_id, "text": body,
        "text_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(), "text_chars": len(body),
        "published": _date(published), "category_code": category_code,
        "category": CATEGORY_RU.get(category_code, category_code),
    }


def _round_robin(items: list[dict[str, Any]], count: int, rng: random.Random) -> list[dict[str, Any]]:
    """По одному из каждой темы по кругу — чтобы большая тема не съела выборку."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        groups[str(item["theme"])].append(item)
    queues = []
    for theme in sorted(groups):
        group = list(groups[theme])
        rng.shuffle(group)
        queues.append(group)
    chosen: list[dict[str, Any]] = []
    while len(chosen) < count and any(queues):
        for queue in queues:
            if queue and len(chosen) < count:
                chosen.append(queue.pop())
    return chosen


def _date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date().isoformat()
    return str(value)[:10]


def _write_workbook(path: Path, sample: list[dict[str, Any]], dictionaries: dict[str, Any]) -> None:
    import openpyxl
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    book = openpyxl.Workbook()
    intro = book.active
    intro.title = "Инструкция"
    for line in INSTRUCTION + ["", f"Версия справочников: {dictionaries['version']}"]:
        intro.append([line])
    intro.column_dimensions["A"].width = 110

    approve = book.create_sheet(DICTIONARY_SHEET)
    approve.append(["Справочник", "Значение", "Правило / алиасы", "Решение", "Комментарий эксперта"])
    for key in _APPROVAL:
        block = dictionaries[key]
        for number, value in enumerate(block["values"]):
            approve.append([block["title"], value, block.get("rule", "") if number == 0 else "", None, None])
    for company in dictionaries["companies"]:
        approve.append(["Компании", company["name"], "; ".join(company["aliases"]), None, None])
    for letter, width in zip("ABCDE", (20, 34, 60, 14, 40)):
        approve.column_dimensions[letter].width = width

    lists = book.create_sheet(LISTS_SHEET)
    lists.sheet_state = "hidden"
    list_refs: dict[str, str] = {}
    for column, key in enumerate(sorted({key for _, key in MARKUP_COLUMNS if key}), start=1):
        values = [*dictionaries[key]["values"], UNDEFINED]
        letter = get_column_letter(column)
        lists.cell(row=1, column=column, value=key)
        for row, value in enumerate(values, start=2):
            lists.cell(row=row, column=column, value=value)
        list_refs[key] = f"'{LISTS_SHEET}'!${letter}$2:${letter}${len(values) + 1}"

    sheet = book.create_sheet(MARKUP_SHEET, index=1)
    header = _GIVEN_COLUMNS + [name for name, _ in MARKUP_COLUMNS]
    sheet.append(header)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for item in sample:
        sheet.append([item["id"], item.get("title") or "", item["text"], item.get("url") or "", item["published"]])
    last = len(sample) + 1
    for offset, (_, key) in enumerate(MARKUP_COLUMNS):
        if not key:
            continue
        letter = get_column_letter(len(_GIVEN_COLUMNS) + offset + 1)
        validation = DataValidation(type="list", formula1=list_refs[key], allow_blank=True)
        validation.add(f"{letter}2:{letter}{last}")
        sheet.add_data_validation(validation)
    widths = [14, 40, 90, 30, 14] + [18] * len(MARKUP_COLUMNS)
    for column, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(column)].width = width
    for row in sheet.iter_rows(min_row=2, min_col=2, max_col=3):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    sheet.freeze_panes = "C2"
    book.save(path)
