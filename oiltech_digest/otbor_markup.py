"""Загрузка разметки эксперта для «Отбора» (#129, спека #128): проверка целостности и отчёт о нарушениях.

Таблица сверяется с манифестом своего пакета (otbor_pack): номера строк и отпечатки текстов; значения — из тех
списков, что видел эксперт (они в манифесте, справочники с тех пор могли поменяться); правила колонок — те же, что
на листе «Правила»: у строки «не брать» причина отказа, у «брать» тема, общая оценка и баллы по пяти критериям
профиля своей категории с обоснованием. Нарушения собираются все сразу: эксперт правит таблицу за один заход.
Строки с метками в кодах справочников — вход экзамена (#133).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from oiltech_digest import otbor_pack_format as pack_format
from oiltech_digest.otbor_pack_format import (APPROVAL_SHEET, CATEGORY_COLUMN, CATEGORY_PROFILE, CHOICE_COLUMNS,
                                              COMMENT_COLUMN, DECISION_COLUMN, INDUSTRY_COLUMN, MANIFEST_KIND,
                                              MANIFEST_NAME, MARKUP_SHEET, NO_DATE, REASON_COLUMN, RELEVANCE_COLUMN,
                                              RUBRICS_SHEET, SCALE_COLUMN, THEME_COLUMN, WHY_COLUMN, PackError,
                                              fingerprint, open_book, sheet_rows, text)
from oiltech_digest.signal_card import _plain

# Шаги «Отбора», без которых решения нет, — у каждой строки.
REQUIRED = (DECISION_COLUMN, INDUSTRY_COLUMN, RELEVANCE_COLUMN, CATEGORY_COLUMN)
# Колонка списка → метка строки для экзамена: справочник колонки, у общей оценки — overall.
LABEL_KEYS = {**dict(CHOICE_COLUMNS), SCALE_COLUMN: "overall"}


def check(markup_path: str | Path, manifest_path: str | Path | None = None) -> dict[str, Any]:
    markup = Path(markup_path).expanduser().resolve()
    manifest_file = Path(manifest_path).expanduser().resolve() if manifest_path else markup.with_name(MANIFEST_NAME)
    manifest = _manifest(manifest_file)
    book = open_book(markup)
    rows = sheet_rows(book, MARKUP_SHEET, ["ID", "Текст публикации", *manifest["columns"]["answers"]], markup.name)
    expected = {item["id"]: item for item in manifest["items"]}
    seen: set[str] = set()
    violations: list[str] = []
    items: list[dict[str, Any]] = []
    for cells in rows:
        number = cells["ID"]
        if number not in expected:
            violations.append(f"{number}: строки нет в манифесте")
            continue
        if number in seen:
            violations.append(f"{number}: номер повторяется")
            continue
        seen.add(number)
        problems: list[str] = []
        if fingerprint(cells.get("Текст публикации")) != expected[number]["text_sha256"]:
            problems.append("текст публикации не тот, что в манифесте")
        items.append({"id": number, "group": expected[number]["group"],
                      **_labels(cells, expected[number], manifest, problems)})
        violations += [f"{number}: {problem}" for problem in problems]
    violations += [f"{number}: строки нет в таблице" for number in expected if number not in seen]
    return {"rows": len(rows), "violations": violations, "items": items, "approvals": _approvals(book),
            "versions": _versions(manifest), "manifest": str(manifest_file)}


def _labels(cells: dict[str, Any], item: dict[str, Any], manifest: dict[str, Any],
            problems: list[str]) -> dict[str, Any]:
    """Метки строки в кодах справочников и нарушения правил колонок."""
    labels: dict[str, Any] = {}
    invalid = set()
    for column, options in manifest["lists"].items():
        value = text(cells.get(column))
        code = next((one["code"] for one in options if _plain(one["label"]) == _plain(value)), None) if value else None
        if value and code is None:
            problems.append(f"«{column}»: «{value}» нет в списке")
            invalid.add(column)
        labels[LABEL_KEYS.get(column, column)] = code
    problems += [f"«{column}» не заполнена" for column in REQUIRED if not text(cells.get(column))]
    columns = manifest["score_columns"]
    scores = {one["criterion"]: _score(cells.get(one["header"]), one, problems) for one in columns}
    scored = [one for one in columns if text(cells.get(one["header"]))]
    decision, category = labels.get("decision"), labels.get("category")
    if decision == "reject":
        if not text(cells.get(REASON_COLUMN)):
            problems.append(f"«{REASON_COLUMN}» не заполнена")
        if scored:
            problems.append("баллы у строки «не брать»")
    elif decision == "accept":
        if text(cells.get(REASON_COLUMN)):
            problems.append("у строки «брать» не должно быть причины отказа")
        problems += [f"«{column}» не заполнена" for column in (THEME_COLUMN, SCALE_COLUMN)
                     if not text(cells.get(column)) and column not in invalid]
        profile = CATEGORY_PROFILE.get(category) if category else None
        if category == "other" and scored:
            problems.append("баллы у категории «другое»")
        if profile:
            problems += [f"«{one['header']}» не заполнена" for one in columns
                         if one["profile"] == profile and not text(cells.get(one["header"]))]
            problems += [f"баллы не той категории: «{one['header']}»" for one in scored if one["profile"] != profile]
            if not text(cells.get(WHY_COLUMN)):
                problems.append(f"«{WHY_COLUMN}» не заполнена")
    if labels.get("reject_reason") == "mixed_events" and not item.get("links"):
        problems.append("«Разные события в одной находке» только у строки со ссылками находки")
    return {**labels, "scores": scores, "why": text(cells.get(WHY_COLUMN)) or None,
            "comment": text(cells.get(COMMENT_COLUMN)) or None}


def _score(raw: Any, column: dict[str, Any], problems: list[str]) -> int | str | None:
    """Балл — целое число от 0 до 100; у «Свежести» ещё «нет даты» (даты события в тексте нет)."""
    value = text(raw)
    if not value:
        return None
    if column["allows_no_date"] and _plain(value) == _plain(NO_DATE):
        return NO_DATE
    try:
        number = float(value.replace(",", "."))
    except ValueError:
        number = -1.0
    if number.is_integer() and 0 <= number <= 100:
        return int(number)
    shown = int(number) if isinstance(raw, (int, float)) and float(raw).is_integer() else value
    problems.append(f"«{column['header']}»: «{shown}» не целое число от 0 до 100"
                    + (f" и не «{NO_DATE}»" if column["allows_no_date"] else ""))
    return None


def _approvals(book: Any) -> dict[str, Any]:
    """Решения эксперта на листах утверждения: «да», правка или «убрать», без решения."""
    approved, pending, changes, missing = 0, 0, [], []
    for title in (APPROVAL_SHEET, RUBRICS_SHEET):
        rows = list(book[title].iter_rows(values_only=True)) if title in book.sheetnames else []
        header = [text(cell) for cell in rows[0]] if rows else []
        if "Решение" not in header:
            missing.append(f"лист «{title}» с колонкой «Решение»")
            continue
        decision_at = header.index("Решение")
        comment_at = header.index("Комментарий эксперта") if "Комментарий эксперта" in header else None
        for row in rows[1:]:
            if len(row) < 2 or not text(row[1]):
                continue
            decision = text(row[decision_at]) if decision_at < len(row) else ""
            comment = text(row[comment_at]) if comment_at is not None and comment_at < len(row) else ""
            if not decision:
                pending += 1
            elif _plain(decision) == "да":
                approved += 1
            else:
                changes.append(f"{title}: {text(row[1])}: {decision}" + (f" ({comment})" if comment else ""))
    return {"approved": approved, "pending": pending, "changes": changes, "missing": missing}


def _versions(manifest: dict[str, Any]) -> list[str]:
    """Справочники и линейки, сменившиеся с выгрузки пакета: экзамену нужны те, по которым размечал эксперт."""
    current = {"dictionaries_version": ("справочники", pack_format.load_dictionaries()["version"]),
               "rubrics_version": ("линейки", pack_format.load_rubrics()["version"])}
    return [f"{name} пакета {manifest['recipe'].get(key)}, сейчас {version}"
            for key, (name, version) in current.items() if manifest["recipe"].get(key) != version]


def _manifest(path: Path) -> dict[str, Any]:
    """Манифест пакета: всё, чем проверяется разметка, — до первой строки, а не KeyError на середине."""
    if not path.exists():
        raise PackError(f"нет манифеста пакета {path}: он лежит рядом с таблицей, иначе укажите --manifest")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("kind") != MANIFEST_KIND:
        raise PackError(f"{path.name}: не манифест пакета «Отбора» (otbor-expert-pack)")
    problems = []
    if not isinstance(data.get("items"), list) or not all(
            isinstance(item, dict) and {"id", "group", "text_sha256", "links"} <= item.keys() for item in data["items"]):
        problems.append("строки items с id, group, text_sha256 и links")
    if not isinstance(data.get("lists"), dict) or not all(
            isinstance(options, list) and all(isinstance(one, dict) and {"code", "label"} <= one.keys()
                                              for one in options) for options in data["lists"].values()):
        problems.append("списки lists с code и label")
    if not isinstance(data.get("columns"), dict) or not isinstance(data["columns"].get("answers"), list):
        problems.append("колонки columns.answers")
    if not isinstance(data.get("score_columns"), list) or not all(
            isinstance(one, dict) and {"header", "profile", "criterion", "allows_no_date"} <= one.keys()
            for one in data["score_columns"]):
        problems.append("колонки баллов score_columns")
    if not isinstance(data.get("recipe"), dict):
        problems.append("рецепт recipe")
    if problems:
        raise PackError(f"{path.name}: манифест пакета «Отбора» испорчен, нет: " + "; ".join(problems))
    return data
