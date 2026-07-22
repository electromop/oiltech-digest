"""Markdown-backed project backlog helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from pathlib import Path
from typing import Any


BACKLOG_PATH = Path(__file__).resolve().parent.parent / "BACKLOG.md"

PLAN_HEADER = "## 🔜 В работе и план (по приоритету)"
TECH_HEADER = "## 🛠 Технический долг и баги (аудит 2026-06-29)"
INBOX_HEADER = "## 📥 Входящие — пишите сюда"

STATUS_LABELS = {
    "new": "🆕",
    "in_progress": "🔵",
    "done": "✅",
    "paused": "⏸",
    "rejected": "❌",
}
STATUS_BY_MARK = {value: key for key, value in STATUS_LABELS.items()}
STATUS_ORDER = ["new", "in_progress", "paused", "done", "rejected"]


@dataclass(frozen=True)
class BacklogTask:
    id: str
    section: str
    priority: str
    title: str
    status: str
    updated: str
    area: str | None = None
    details: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "section": self.section,
            "priority": self.priority,
            "title": self.title,
            "status": self.status,
            "status_label": STATUS_LABELS.get(self.status, self.status),
            "updated": self.updated,
            "area": self.area,
            "details": self.details,
        }


def read_backlog() -> dict[str, Any]:
    text = BACKLOG_PATH.read_text(encoding="utf-8")
    tasks = _parse_plan_tasks(text) + _parse_tech_tasks(text) + _parse_inbox_tasks(text)
    counts = {status: sum(1 for task in tasks if task.status == status) for status in STATUS_ORDER}
    return {
        "tasks": [task.as_dict() for task in tasks],
        "counts": counts,
        "backlog_path": str(BACKLOG_PATH),
        "updated_at": date.today().isoformat(),
    }


def create_plan_task(title: str, priority: str = "P3", status: str = "new") -> dict[str, Any]:
    clean_title = _clean_cell(title)
    clean_priority = _normalize_priority(priority)
    clean_status = _normalize_status(status)
    if not clean_title:
        raise ValueError("Название задачи не может быть пустым")

    text = BACKLOG_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    table_start, table_end = _find_table(lines, PLAN_HEADER)
    next_id = _next_plan_id(lines[table_start + 2 : table_end])
    today = date.today().isoformat()
    row = f"| {next_id} | **{clean_priority}** | {clean_title} | {STATUS_LABELS[clean_status]} | {today} |"
    lines.insert(table_end, row)
    _write_lines(lines)
    return BacklogTask(
        id=str(next_id),
        section="plan",
        priority=clean_priority,
        title=clean_title,
        status=clean_status,
        updated=today,
    ).as_dict()


def update_task_status(task_id: str, status: str) -> dict[str, Any]:
    clean_status = _normalize_status(status)
    text = BACKLOG_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    today = date.today().isoformat()

    changed = _update_plan_status(lines, task_id, clean_status, today)
    if not changed:
        changed = _update_tech_status(lines, task_id, clean_status)
    if not changed:
        changed = _update_inbox_status(lines, task_id, clean_status)
    if not changed:
        raise KeyError(task_id)

    _write_lines(lines)
    updated_text = "\n".join(lines)
    tasks = _parse_plan_tasks(updated_text) + _parse_tech_tasks(updated_text) + _parse_inbox_tasks(updated_text)
    return next(task.as_dict() for task in tasks if task.id == task_id)


def _parse_plan_tasks(text: str) -> list[BacklogTask]:
    rows = _extract_table_rows(text, PLAN_HEADER)
    tasks: list[BacklogTask] = []
    for cells in rows:
        if len(cells) < 5:
            continue
        tasks.append(
            BacklogTask(
                id=cells[0],
                section="plan",
                priority=_strip_markdown(cells[1]),
                title=_strip_markdown(cells[2]),
                status=_status_from_cell(cells[3]),
                updated=_strip_markdown(cells[4]),
            )
        )
    return tasks


def _parse_tech_tasks(text: str) -> list[BacklogTask]:
    rows = _extract_table_rows(text, TECH_HEADER)
    tasks: list[BacklogTask] = []
    for cells in rows:
        if len(cells) < 6:
            continue
        tasks.append(
            BacklogTask(
                id=cells[0],
                section="tech",
                priority=_strip_markdown(cells[1])
                .replace(" 🔴", "")
                .replace(" 🟠", "")
                .replace(" 🟡", "")
                .replace(" ⚪", ""),
                title=_strip_markdown(cells[2]),
                area=_strip_markdown(cells[3]),
                details=_strip_markdown(cells[4]),
                status=_status_from_cell(cells[5]),
                updated="2026-06-29",
            )
        )
    return tasks


def _parse_inbox_tasks(text: str) -> list[BacklogTask]:
    rows = _extract_table_rows(text, INBOX_HEADER)
    tasks: list[BacklogTask] = []
    for index, cells in enumerate(rows, start=1):
        if len(cells) < 4 or not any(cell.strip() for cell in cells[:3]):
            continue
        tasks.append(
            BacklogTask(
                id=f"I{index}",
                section="inbox",
                priority="Входящие",
                title=_strip_markdown(cells[2]),
                area=_strip_markdown(cells[1]),
                status=_status_from_cell(cells[3]),
                updated=_strip_markdown(cells[0]),
            )
        )
    return tasks


def _extract_table_rows(text: str, header: str) -> list[list[str]]:
    lines = text.splitlines()
    table_start, table_end = _find_table(lines, header)
    rows: list[list[str]] = []
    for line in lines[table_start + 2 : table_end]:
        cells = _split_row(line)
        if cells:
            rows.append(cells)
    return rows


def _find_table(lines: list[str], header: str) -> tuple[int, int]:
    try:
        header_index = lines.index(header)
    except ValueError as exc:
        raise ValueError(f"Не найден раздел беклога: {header}") from exc

    table_start = -1
    for index in range(header_index + 1, len(lines)):
        if lines[index].startswith("|"):
            table_start = index
            break
    if table_start == -1:
        raise ValueError(f"Не найдена таблица в разделе: {header}")

    table_end = table_start
    while table_end < len(lines) and lines[table_end].startswith("|"):
        table_end += 1
    return table_start, table_end


def _update_plan_status(lines: list[str], task_id: str, status: str, updated: str) -> bool:
    table_start, table_end = _find_table(lines, PLAN_HEADER)
    for index in range(table_start + 2, table_end):
        cells = _split_row(lines[index])
        if len(cells) >= 5 and cells[0] == task_id:
            cells[3] = STATUS_LABELS[status]
            cells[4] = updated
            lines[index] = _format_row(cells)
            return True
    return False


def _update_tech_status(lines: list[str], task_id: str, status: str) -> bool:
    table_start, table_end = _find_table(lines, TECH_HEADER)
    for index in range(table_start + 2, table_end):
        cells = _split_row(lines[index])
        if len(cells) >= 6 and cells[0] == task_id:
            cells[5] = STATUS_LABELS[status]
            lines[index] = _format_row(cells)
            return True
    return False


def _update_inbox_status(lines: list[str], task_id: str, status: str) -> bool:
    table_start, table_end = _find_table(lines, INBOX_HEADER)
    inbox_index = 0
    for index in range(table_start + 2, table_end):
        cells = _split_row(lines[index])
        if len(cells) < 4 or not any(cell.strip() for cell in cells[:3]):
            continue
        inbox_index += 1
        if f"I{inbox_index}" == task_id:
            cells[3] = STATUS_LABELS[status]
            lines[index] = _format_row(cells)
            return True
    return False


def _next_plan_id(rows: list[str]) -> int:
    numbers = []
    for line in rows:
        cells = _split_row(line)
        if cells and cells[0].isdigit():
            numbers.append(int(cells[0]))
    return max(numbers, default=0) + 1


def _split_row(line: str) -> list[str]:
    if not line.startswith("|"):
        return []
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _format_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _clean_cell(value: str) -> str:
    return " ".join(value.replace("|", "/").split())


def _strip_markdown(value: str) -> str:
    clean = re.sub(r"\*\*(.*?)\*\*", r"\1", value)
    clean = clean.replace("`", "")
    return " ".join(clean.split())


def _normalize_priority(value: str) -> str:
    match = re.search(r"P[1-4]", value.upper())
    return match.group(0) if match else "P3"


def _normalize_status(value: str) -> str:
    if value in STATUS_LABELS:
        return value
    if value in STATUS_BY_MARK:
        return STATUS_BY_MARK[value]
    raise ValueError("Неизвестный статус задачи")


def _status_from_cell(value: str) -> str:
    for mark, status in STATUS_BY_MARK.items():
        if mark in value:
            return status
    return "new"


def _write_lines(lines: list[str]) -> None:
    BACKLOG_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
