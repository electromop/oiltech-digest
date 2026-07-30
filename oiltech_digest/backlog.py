"""Markdown-backed project backlog helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import re
from pathlib import Path
from typing import Any


BACKLOG_PATH = Path(__file__).resolve().parent.parent / "BACKLOG.md"

PLAN_HEADER = "## 🔜 В работе и план (по приоритету)"
TECH_HEADER = "## 🛠 Технический долг и баги (аудит 2026-06-29)"
INBOX_HEADER = "## 📥 Входящие — пишите сюда"
META_START = "<!-- TASK_TRACKER_META_START"
META_END = "TASK_TRACKER_META_END -->"

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
    due_date: str | None = None
    comments: list[dict[str, str]] | None = None

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
            "due_date": self.due_date,
            "comments": self.comments or [],
        }


def read_backlog() -> dict[str, Any]:
    text = BACKLOG_PATH.read_text(encoding="utf-8")
    tasks = _parse_plan_tasks(text) + _parse_tech_tasks(text) + _parse_inbox_tasks(text)
    tasks = _apply_metadata(tasks, _read_metadata(text))
    counts = {status: sum(1 for task in tasks if task.status == status) for status in STATUS_ORDER}
    return {
        "tasks": [task.as_dict() for task in tasks],
        "counts": counts,
        "backlog_path": str(BACKLOG_PATH),
        "updated_at": date.today().isoformat(),
    }


def create_plan_task(
    title: str,
    priority: str = "P3",
    status: str = "new",
    details: str | None = None,
    due_date: str | None = None,
) -> dict[str, Any]:
    clean_title = _clean_cell(title)
    clean_details = _clean_cell(details or "")
    clean_priority = _normalize_priority(priority)
    clean_status = _normalize_status(status)
    clean_due_date = _normalize_due_date(due_date)
    if not clean_title:
        raise ValueError("Название задачи не может быть пустым")

    text = BACKLOG_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    table_start, table_end = _find_table(lines, PLAN_HEADER)
    next_id = _next_plan_id(lines[table_start + 2 : table_end])
    today = date.today().isoformat()
    row_title = _compose_plan_title(clean_title, clean_details)
    row = f"| {next_id} | **{clean_priority}** | {row_title} | {STATUS_LABELS[clean_status]} | {today} |"
    lines.insert(table_end, row)
    text = "\n".join(lines).rstrip() + "\n"
    if clean_due_date:
        metadata = _read_metadata(text)
        _metadata_for_task(metadata, str(next_id))["due_date"] = clean_due_date
        text = _replace_metadata(text, metadata)
    _write_text(text)
    return BacklogTask(
        id=str(next_id),
        section="plan",
        priority=clean_priority,
        title=clean_title,
        details=clean_details or None,
        due_date=clean_due_date,
        comments=[],
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
    tasks = _apply_metadata(tasks, _read_metadata(updated_text))
    return next(task.as_dict() for task in tasks if task.id == task_id)


def update_task_due_date(task_id: str, due_date: str | None) -> dict[str, Any]:
    text = BACKLOG_PATH.read_text(encoding="utf-8")
    tasks = _parse_plan_tasks(text) + _parse_tech_tasks(text) + _parse_inbox_tasks(text)
    if not any(task.id == task_id for task in tasks):
        raise KeyError(task_id)
    clean_due_date = _normalize_due_date(due_date)
    metadata = _read_metadata(text)
    task_meta = _metadata_for_task(metadata, task_id)
    if clean_due_date:
        task_meta["due_date"] = clean_due_date
    else:
        task_meta.pop("due_date", None)
    _write_text(_replace_metadata(text, metadata))
    updated_text = BACKLOG_PATH.read_text(encoding="utf-8")
    tasks = _apply_metadata(_parse_plan_tasks(updated_text) + _parse_tech_tasks(updated_text) + _parse_inbox_tasks(updated_text), _read_metadata(updated_text))
    return next(task.as_dict() for task in tasks if task.id == task_id)


def add_task_comment(task_id: str, text: str, author: str) -> dict[str, Any]:
    clean_text = _clean_cell(text)
    if not clean_text:
        raise ValueError("Комментарий не может быть пустым")
    backlog_text = BACKLOG_PATH.read_text(encoding="utf-8")
    tasks = _parse_plan_tasks(backlog_text) + _parse_tech_tasks(backlog_text) + _parse_inbox_tasks(backlog_text)
    if not any(task.id == task_id for task in tasks):
        raise KeyError(task_id)
    metadata = _read_metadata(backlog_text)
    comments = _metadata_for_task(metadata, task_id).setdefault("comments", [])
    comments.append(
        {
            "id": f"c{len(comments) + 1}",
            "author": _clean_cell(author) or "Пользователь",
            "text": clean_text,
            "created_at": date.today().isoformat(),
        }
    )
    _write_text(_replace_metadata(backlog_text, metadata))
    updated_text = BACKLOG_PATH.read_text(encoding="utf-8")
    tasks = _apply_metadata(_parse_plan_tasks(updated_text) + _parse_tech_tasks(updated_text) + _parse_inbox_tasks(updated_text), _read_metadata(updated_text))
    return next(task.as_dict() for task in tasks if task.id == task_id)


def _parse_plan_tasks(text: str) -> list[BacklogTask]:
    rows = _extract_table_rows(text, PLAN_HEADER)
    tasks: list[BacklogTask] = []
    for cells in rows:
        if len(cells) < 5:
            continue
        title, details = _split_plan_title(_strip_markdown(cells[2]))
        tasks.append(
            BacklogTask(
                id=cells[0],
                section="plan",
                priority=_strip_markdown(cells[1]),
                title=title,
                details=details,
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


def _compose_plan_title(title: str, details: str) -> str:
    if not details:
        return title
    return f"{title} — Описание: {details}"


def _split_plan_title(value: str) -> tuple[str, str | None]:
    marker = " — Описание: "
    if marker not in value:
        return value, None
    title, details = value.split(marker, 1)
    return title.strip(), details.strip() or None


def _apply_metadata(tasks: list[BacklogTask], metadata: dict[str, Any]) -> list[BacklogTask]:
    task_metadata = metadata.get("tasks", {})
    enriched: list[BacklogTask] = []
    for task in tasks:
        meta = task_metadata.get(task.id, {})
        enriched.append(
            BacklogTask(
                id=task.id,
                section=task.section,
                priority=task.priority,
                title=task.title,
                status=task.status,
                updated=task.updated,
                area=task.area,
                details=task.details,
                due_date=meta.get("due_date") or None,
                comments=_clean_comments(meta.get("comments", [])),
            )
        )
    return enriched


def _read_metadata(text: str) -> dict[str, Any]:
    match = re.search(rf"{re.escape(META_START)}\n(.*?)\n{re.escape(META_END)}", text, flags=re.DOTALL)
    if not match:
        return {"tasks": {}}
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {"tasks": {}}
    if not isinstance(payload, dict):
        return {"tasks": {}}
    tasks = payload.get("tasks")
    if not isinstance(tasks, dict):
        payload["tasks"] = {}
    return payload


def _replace_metadata(text: str, metadata: dict[str, Any]) -> str:
    clean_metadata = _prune_metadata(metadata)
    block = f"{META_START}\n{json.dumps(clean_metadata, ensure_ascii=False, indent=2, sort_keys=True)}\n{META_END}"
    pattern = rf"\n*{re.escape(META_START)}\n.*?\n{re.escape(META_END)}\n*"
    if re.search(pattern, text, flags=re.DOTALL):
        return re.sub(pattern, "\n\n" + block + "\n", text, flags=re.DOTALL).rstrip() + "\n"
    return text.rstrip() + "\n\n" + block + "\n"


def _metadata_for_task(metadata: dict[str, Any], task_id: str) -> dict[str, Any]:
    tasks = metadata.setdefault("tasks", {})
    task_meta = tasks.setdefault(task_id, {})
    if not isinstance(task_meta, dict):
        task_meta = {}
        tasks[task_id] = task_meta
    return task_meta


def _prune_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    tasks = metadata.get("tasks", {})
    clean_tasks: dict[str, dict[str, Any]] = {}
    if isinstance(tasks, dict):
        for task_id, value in tasks.items():
            if not isinstance(value, dict):
                continue
            clean_value: dict[str, Any] = {}
            due_date = value.get("due_date")
            if isinstance(due_date, str) and due_date:
                clean_value["due_date"] = due_date
            comments = _clean_comments(value.get("comments", []))
            if comments:
                clean_value["comments"] = comments
            if clean_value:
                clean_tasks[str(task_id)] = clean_value
    return {"tasks": clean_tasks}


def _clean_comments(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    comments: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        comments.append(
            {
                "id": str(item.get("id") or f"c{len(comments) + 1}"),
                "author": str(item.get("author") or "Пользователь"),
                "text": text,
                "created_at": str(item.get("created_at") or ""),
            }
        )
    return comments


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


def _normalize_due_date(value: str | None) -> str | None:
    if not value:
        return None
    clean = value.strip()
    if not clean:
        return None
    try:
        return date.fromisoformat(clean).isoformat()
    except ValueError as exc:
        raise ValueError("Дедлайн должен быть датой в формате YYYY-MM-DD") from exc


def _status_from_cell(value: str) -> str:
    for mark, status in STATUS_BY_MARK.items():
        if mark in value:
            return status
    return "new"


def _write_lines(lines: list[str]) -> None:
    _write_text("\n".join(lines).rstrip() + "\n")


def _write_text(text: str) -> None:
    BACKLOG_PATH.write_text(text.rstrip() + "\n", encoding="utf-8")
