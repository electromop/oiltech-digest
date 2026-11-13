"""Изменения карточки контура ОС (#189, спека #187): как система применяет явное действие администратора и согласованное
улучшение карточки и как отменяет их из журнала ОС.

Каждое применённое изменение оставляет запись (change) в действии журнала: что поменяно и каким было до него. По ней
отмена возвращает прежнее состояние: правку заголовка и сути (запись журнала отметок больше не ложится на карточку:
снова текст модели или прежняя правка), категорию и баллы карточки («Не тот блок»), склейку (снимается, прежняя группа
восстанавливается, пишется пара «не одно событие» для «Дедупа»), решение «Отбора» («Убрано людьми»: «Вернуть»), поле
карточки (прежнее значение и прежняя отметка, кто его поставил). Отмена не трогает то, что после действия поменял
кто-то ещё: тогда она отказывает с причиной.

ВНИМАНИЕ про тесты: соединение: `connection.get_connection()` атрибутом модуля: изоляция тестов держится на подмене
этого атрибута.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from oiltech_digest.db import connection, feedback_store, repository
from oiltech_digest.db.repository import CARD_SET_BY_HUMAN, refresh_platform_decisions


@dataclass(frozen=True)
class Field:
    """Поле улучшения карточки: подпись, агент, чьё это поле, вид значения, колонки сигнала и ключ card_set_by."""
    label: str
    agent: str
    kind: str  # text, theme, choice, date, countries, companies, category, duplicate, removal
    columns: tuple[str, ...] = ()
    dictionary: str | None = None
    limit: int = 0


FIELDS: dict[str, Field] = {
    "title": Field("Заголовок", "summary", "text", limit=200),
    "summary": Field("Суть", "summary", "text", limit=1500),
    "theme": Field("Тема", "card", "theme", ("theme",)),
    "event_type": Field("Тип события", "card", "choice", ("event_type",), "event_type"),
    "technology_maturity": Field("Стадия (зрелость)", "card", "choice", ("technology_maturity",), "maturity"),
    "event_date": Field("Дата события", "card", "date", ("event_date", "event_date_precision")),
    "event_countries": Field("Страны события", "card", "countries", ("event_countries",)),
    "region": Field("Регион", "card", "choice", ("region",), "region"),
    "technology_class": Field("Класс технологии", "card", "choice", ("technology_class",), "technology_class"),
    "participants": Field("Компании-участники", "card", "companies", ("participants_json",)),
    "signal_category": Field("Категория (витрина)", "otbor", "category"),
    "duplicate_of": Field("Дубль карточки", "dedup", "duplicate"),
    "removed": Field("Убрать в «Убрано людьми»", "otbor", "removal"),
}
CARD_KINDS = frozenset({"theme", "choice", "date", "countries", "companies"})
CATEGORY_RU = {"business": "бизнес", "technology": "технология"}
# Правка заголовка и сути, применённая системой по согласованному улучшению: запись журнала отметок своего вида, чтобы
# журнал ОС не показывал её отдельной отметкой.
PROPOSAL_EVENT = "proposal_applied"
_SCORE_COLUMNS = ("score", "score_profile", "score_items_json", "criteria_snapshot", "score_band", "scores_recipe",
                  "scores_stale_since")
_JSON_COLUMNS = frozenset({"participants_json", "score_items_json", "criteria_snapshot", "scores_recipe"})


class ChangeRefused(Exception):
    """Изменение не применено или не отменено: причина словами для журнала и ответа администратору."""


# --- Явные действия администратора из формы ОС (#188) ---------------------------------------------


def correction_change(event_id: int, *, title: bool, summary: bool) -> dict[str, Any]:
    """Правка заголовка и сути из отметки администратора: уже лежит в журнале отметок, отмена её гасит."""
    return {"field": "correction", "event_id": int(event_id), "title": bool(title), "summary": bool(summary)}


def category_snapshot(signal_id: int) -> dict[str, Any] | None:
    """Категория, отметка, кто её поставил, и баллы карточки до «Не того блока»: их вернёт отмена."""
    with connection.get_connection() as conn:
        row = conn.cursor(row_factory=dict_row).execute(
            f"SELECT signal_category, card_set_by, {', '.join(_SCORE_COLUMNS)} FROM signals WHERE id = %s",
            (int(signal_id),)).fetchone()
    if row is None:
        return None
    return _jsonable({"signal_category": row["signal_category"],
                      "set_by": (row["card_set_by"] or {}).get("signal_category"),
                      **{column: row[column] for column in _SCORE_COLUMNS}})


def category_change(before: dict[str, Any] | None, category: str) -> dict[str, Any]:
    return {"field": "signal_category", "before": before, "after": category}


def merge_children(signal_id: int) -> list[int]:
    """Карточки, склеенные с этой до её склейки: склейка уводит их к корню группы, отмена возвращает."""
    with connection.get_connection() as conn:
        rows = conn.execute("SELECT id FROM signals WHERE merged_into_signal_id = %s ORDER BY id",
                            (int(signal_id),)).fetchall()
    return [int(row[0]) for row in rows]


def merge_change(signal_id: int, children: list[int]) -> dict[str, Any] | None:
    root = _merged_into(signal_id)
    return {"field": "duplicate_of", "root": root, "children": children} if root is not None else None


def removal_change() -> dict[str, Any]:
    return {"field": "removed"}


# --- Применение согласованного улучшения ---------------------------------------------------------


def apply_change(signal_id: int, field: str, value: Any, *, author: str | None, user_id: int | None) -> dict[str, Any]:
    """Применить одно проверенное изменение улучшения карточки. Запись для отмены; ChangeRefused: не применено."""
    spec = FIELDS[field]
    if spec.kind == "text":
        return _apply_text(signal_id, field, value, user_id=user_id)
    if spec.kind in CARD_KINDS:
        return _apply_card_field(signal_id, field, value, author=author)
    if spec.kind == "category":
        return _apply_category(signal_id, value, author=author)
    if spec.kind == "duplicate":
        return _apply_merge(signal_id, int(value), author=author)
    return _apply_removal(signal_id, author=author)


def _apply_text(signal_id: int, field: str, value: str, *, user_id: int | None) -> dict[str, Any]:
    column = "corrected_title" if field == "title" else "corrected_thesis"
    with connection.get_connection() as conn:
        if conn.execute("SELECT 1 FROM signals WHERE id = %s", (int(signal_id),)).fetchone() is None:
            raise ChangeRefused(f"карточки №{int(signal_id)} нет")
        event_id = int(conn.execute(
            f"INSERT INTO signal_feedback_events (signal_id, user_id, event_type, {column}, applied)"
            " VALUES (%s, %s, %s, %s, TRUE) RETURNING id", (int(signal_id), user_id, PROPOSAL_EVENT, value)).fetchone()[0])
        conn.commit()
    return {"field": field, "event_id": event_id, "after": value}


def _apply_card_field(signal_id: int, field: str, value: dict[str, Any], *, author: str | None) -> dict[str, Any]:
    spec = FIELDS[field]
    with connection.get_connection() as conn:
        row = conn.cursor(row_factory=dict_row).execute(
            f"SELECT card_set_by, {', '.join(spec.columns)} FROM signals WHERE id = %s FOR UPDATE",
            (int(signal_id),)).fetchone()
        if row is None:
            raise ChangeRefused(f"карточки №{int(signal_id)} нет")
        set_by = dict(row["card_set_by"] or {})
        before = {"columns": _jsonable({column: row[column] for column in spec.columns}), "set_by": set_by.get(field)}
        set_by[field] = CARD_SET_BY_HUMAN
        step = {"stage": "Человек", "text": f"Улучшение карточки из журнала ОС ({author or 'без имени'}): "
                                            f"{spec.label.lower()}"}
        conn.execute(
            f"UPDATE signals SET {', '.join(f'{column} = %s' for column in spec.columns)}, card_set_by = %s,"
            " decision_log = COALESCE(decision_log, '[]'::jsonb) || %s::jsonb, updated_at = now() WHERE id = %s",
            (*(_db_value(column, value[column]) for column in spec.columns), Json(set_by), Json([step]),
             int(signal_id)))
        conn.commit()
    return {"field": field, "before": before, "after": {"columns": _jsonable(value)}}


def _apply_category(signal_id: int, category: str, *, author: str | None) -> dict[str, Any]:
    from oiltech_digest import signal_scores
    from oiltech_digest.signal_feedback import NOT_RECATEGORIZED

    before = category_snapshot(signal_id)
    if before is None:
        raise ChangeRefused(f"карточки №{int(signal_id)} нет")
    if before["signal_category"] == category:
        raise ChangeRefused(f"категория уже {CATEGORY_RU.get(category, category)}")
    changed = signal_scores.wrong_block(signal_id, author)
    if changed is None:
        raise ChangeRefused(NOT_RECATEGORIZED)
    return category_change(before, changed)


def _apply_merge(signal_id: int, into: int, *, author: str | None) -> dict[str, Any]:
    refusal = repository.merge_refusal(signal_id, into)
    if refusal:
        raise ChangeRefused(refusal)
    children = merge_children(signal_id)
    reason = f"журнал ОС: дубль #{int(into)}" + (f", согласовал {author}" if author else "")
    if not repository.mark_signal_merged(signal_id, into, reason=reason, respect_review=False):
        raise ChangeRefused("карточка изменилась, пока применялось улучшение: обновите журнал")
    change = merge_change(signal_id, children)
    if change is None:
        raise ChangeRefused("склейка не записалась")
    return change


def _apply_removal(signal_id: int, *, author: str | None) -> dict[str, Any]:
    from oiltech_digest.db import removed_by_people

    refusal = removed_by_people.remove(signal_id, author=author, reason="согласовано в журнале ОС")
    if refusal:
        raise ChangeRefused(refusal)
    return removal_change()


# --- Отмена --------------------------------------------------------------------------------------


def undo_problem(signal_id: int, change: dict[str, Any]) -> str | None:
    """Почему изменение не отменить сейчас; None: можно."""
    field = change.get("field")
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        if field in ("correction", "title", "summary"):
            row = cur.execute("SELECT correction_undone_at FROM signal_feedback_events WHERE id = %s",
                              (int(change["event_id"]),)).fetchone()
            return None if row and row["correction_undone_at"] is None else "правка уже отменена"
        row = cur.execute("SELECT * FROM signals WHERE id = %s", (int(signal_id),)).fetchone()
    if row is None:
        return f"карточки №{int(signal_id)} нет"
    if field == "signal_category":
        return None if row["signal_category"] == change.get("after") else "категорию с тех пор сменили"
    if field == "duplicate_of":
        return None if row["merged_into_signal_id"] == change.get("root") else "склейку с тех пор сняли или изменили"
    if field == "removed":
        return None if row["human_removal"] is not None else "карточку уже вернули из «Убрано людьми»"
    spec = FIELDS.get(str(field))
    if spec is None or spec.kind not in CARD_KINDS:
        return "неизвестное изменение"
    after = (change.get("after") or {}).get("columns") or {}
    current = _jsonable({column: row[column] for column in spec.columns})
    if current != after:
        return f"поле «{spec.label}» с тех пор поменяли"
    return None


def undo_change(signal_id: int, change: dict[str, Any], *, author: str | None, action_id: int | None) -> None:
    """Вернуть прежнее состояние по записи изменения. ChangeRefused: не отменено (причина)."""
    problem = undo_problem(signal_id, change)
    if problem:
        raise ChangeRefused(problem)
    field = change["field"]
    if field in ("correction", "title", "summary"):
        with connection.get_connection() as conn:
            conn.execute("UPDATE signal_feedback_events SET correction_undone_at = now() WHERE id = %s"
                         " AND correction_undone_at IS NULL", (int(change["event_id"]),))
            conn.commit()
        return
    if field == "signal_category":
        _undo_category(signal_id, change, author=author)
        return
    if field == "duplicate_of":
        _undo_merge(signal_id, change, author=author, action_id=action_id)
        return
    if field == "removed":
        from oiltech_digest.db import removed_by_people

        if removed_by_people.restore(signal_id, author=author) != removed_by_people.RESTORED:
            raise ChangeRefused("карточку уже вернули из «Убрано людьми»")
        return
    _undo_card_field(signal_id, change, author=author)


def _undo_card_field(signal_id: int, change: dict[str, Any], *, author: str | None) -> None:
    spec = FIELDS[change["field"]]
    before = change.get("before") or {}
    columns = before.get("columns") or {}
    with connection.get_connection() as conn:
        row = conn.cursor(row_factory=dict_row).execute(
            "SELECT card_set_by FROM signals WHERE id = %s FOR UPDATE", (int(signal_id),)).fetchone()
        set_by = dict(row["card_set_by"] or {}) if row else {}
        _restore_set_by(set_by, change["field"], before.get("set_by"))
        step = {"stage": "Человек", "text": f"Отмена из журнала ОС ({author or 'без имени'}): "
                                            f"{spec.label.lower()} как было"}
        conn.execute(
            f"UPDATE signals SET {', '.join(f'{column} = %s' for column in spec.columns)}, card_set_by = %s,"
            " decision_log = COALESCE(decision_log, '[]'::jsonb) || %s::jsonb, updated_at = now() WHERE id = %s",
            (*(_db_value(column, columns.get(column)) for column in spec.columns), Json(set_by), Json([step]),
             int(signal_id)))
        conn.commit()


def _undo_category(signal_id: int, change: dict[str, Any], *, author: str | None) -> None:
    before = change.get("before") or {}
    with connection.get_connection() as conn:
        row = conn.cursor(row_factory=dict_row).execute(
            "SELECT card_set_by FROM signals WHERE id = %s FOR UPDATE", (int(signal_id),)).fetchone()
        set_by = dict(row["card_set_by"] or {})
        _restore_set_by(set_by, "signal_category", before.get("set_by"))
        step = {"stage": "Человек", "text": f"Отмена «Не того блока» из журнала ОС ({author or 'без имени'}): "
                                            "прежняя категория и баллы"}
        values = {column: before.get(column) for column in _SCORE_COLUMNS}
        if values["score"] is None:
            values["score"] = 0
        conn.execute(
            f"UPDATE signals SET signal_category = %s, card_set_by = %s,"
            f" {', '.join(f'{column} = %s' for column in _SCORE_COLUMNS)},"
            " decision_log = COALESCE(decision_log, '[]'::jsonb) || %s::jsonb, updated_at = now() WHERE id = %s",
            (before.get("signal_category"), Json(set_by), *(_db_value(column, values[column]) for column in _SCORE_COLUMNS),
             Json([step]), int(signal_id)))
        refresh_platform_decisions([int(signal_id)], conn=conn)
        conn.commit()


def _undo_merge(signal_id: int, change: dict[str, Any], *, author: str | None, action_id: int | None) -> None:
    root = int(change["root"])
    with connection.get_connection() as conn:
        step = {"stage": "Человек", "text": f"Отмена склейки из журнала ОС ({author or 'без имени'}): не одно событие "
                                            f"с №{root}"}
        updated = conn.execute(
            "UPDATE signals SET merged_into_signal_id = NULL, merge_reason = NULL,"
            " decision_log = COALESCE(decision_log, '[]'::jsonb) || %s::jsonb, updated_at = now()"
            " WHERE id = %s AND merged_into_signal_id = %s", (Json([step]), int(signal_id), root)).rowcount
        if not updated:
            conn.rollback()
            raise ChangeRefused("склейку с тех пор сняли или изменили")
        children = [int(one) for one in change.get("children") or []]
        if children:
            conn.execute("UPDATE signals SET merged_into_signal_id = %s, updated_at = now()"
                         " WHERE id = ANY(%s) AND merged_into_signal_id = %s", (int(signal_id), children, root))
        feedback_store.write_pair(signal_id, root, same_event=False, source="undo_merge", action_id=action_id,
                                  author=author, conn=conn)
        conn.commit()
    repository.refresh_signal_evidence_count(int(signal_id))
    repository.refresh_signal_evidence_count(root)


# --- Значения ------------------------------------------------------------------------------------


def describe(change: dict[str, Any]) -> str:
    """Изменение словами для журнала."""
    field = change.get("field")
    if field == "correction":
        parts = [name for name, flag in (("заголовок", change.get("title")), ("суть", change.get("summary"))) if flag]
        return "Правка: " + " и ".join(parts or ["текст"])
    if field == "signal_category":
        return f"Категория: {CATEGORY_RU.get(change.get('after'), change.get('after'))}"
    if field == "duplicate_of":
        return f"Склеено с №{change.get('root')}"
    if field == "removed":
        return "Убрано в «Убрано людьми»"
    spec = FIELDS.get(str(field))
    label = spec.label if spec else str(field)
    if field in ("title", "summary"):
        return f"{label}: {change.get('after')}"
    return f"{label}: {display_value(str(field), (change.get('after') or {}).get('columns') or {})}"


def display_value(field: str, columns: dict[str, Any]) -> str:
    """Значение поля карточки для человека."""
    if field == "event_date":
        day, precision = columns.get("event_date"), columns.get("event_date_precision")
        if not day:
            return "не названа"
        text = str(day)
        return text[:4] if precision == "year" else text[:7] if precision == "month" else text[:10]
    if field == "event_countries":
        return "; ".join(columns.get("event_countries") or []) or "нет"
    if field == "participants":
        return "; ".join(one.get("name", "") for one in columns.get("participants_json") or []) or "нет"
    spec = FIELDS[field]
    return str(columns.get(spec.columns[0]) or "не определено")


def current_values(signal_id: int, fields: list[str]) -> dict[str, Any]:
    """Нынешние значения полей карточки (до применения улучшения): «было» в журнале."""
    columns = sorted({column for field in fields if field in FIELDS for column in FIELDS[field].columns})
    if not columns:
        return {}
    with connection.get_connection() as conn:
        row = conn.cursor(row_factory=dict_row).execute(
            f"SELECT {', '.join(columns)} FROM signals WHERE id = %s", (int(signal_id),)).fetchone()
    return _jsonable(dict(row)) if row else {}


def _merged_into(signal_id: int) -> int | None:
    with connection.get_connection() as conn:
        row = conn.execute("SELECT merged_into_signal_id FROM signals WHERE id = %s", (int(signal_id),)).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _restore_set_by(set_by: dict[str, Any], key: str, previous: Any) -> None:
    if previous:
        set_by[key] = previous
    else:
        set_by.pop(key, None)


def _db_value(column: str, value: Any) -> Any:
    if column in _JSON_COLUMNS:
        return Json(value) if value is not None else None
    if column == "event_date" and isinstance(value, str) and value:
        return date.fromisoformat(value[:10])
    if column == "scores_stale_since" and isinstance(value, str) and value:
        return datetime.fromisoformat(value)
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if value.__class__.__name__ == "Decimal":
        return float(value)
    return value
