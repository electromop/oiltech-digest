"""Журнал ОС (#189, спека #187): вкладка «Обратная связь», один журнал отметок и действий по обеим витринам.

Строка журнала: отметка (из журнала отметок: кто, когда, что пришло, что система сделала: сразу явным действием
администратора или ждёт недельного среза), предложение разбора ОС по сигналу (на каких отметках основано, цитата, что
предлагает) или задача выборки отклонённого «Отбором». У строки статус и путь действия: «сделано сразу → можно
отменить» или «ждёт решения → согласовано или изменено → применено системой → можно отменить», либо «ждёт решения →
убрано, остаётся в журнале», «устарело». Вид «отметка» журнал строит из журнала отметок при чтении, копии отметок в
действиях нет. Журнал видят все допущенные (настройка «кто видит вкладку»); решения, отмена и закрытие задачи только у
администратора (ADR 0004, поправка 12.11).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from oiltech_digest import feedback_changes, feedback_settings
from oiltech_digest.db import connection, feedback_store
from oiltech_digest.db.feedback_store import (APPLIED, APPLIED_STATUS, APPROVED, DECISION, DISMISSED, DONE, IMMEDIATE,
                                              MODIFIED, PENDING, PROPOSAL, SAMPLE_TASK, STALE, UNDO, UNDONE)
from oiltech_digest.feedback_review import REVIEW_AUTHOR

logger = logging.getLogger(__name__)

STATUS_RU = {DONE: "сделано", PENDING: "ждёт решения", APPROVED: "согласовано", MODIFIED: "изменено",
             DISMISSED: "убрано", APPLIED_STATUS: "применено", UNDONE: "отменено", STALE: "устарело"}
DECISIONS = {"approve": APPROVED, "modify": MODIFIED, "dismiss": DISMISSED}
_DECISION_RU = {APPROVED: "Согласовано", MODIFIED: "Изменено", DISMISSED: "Убрано, остаётся в журнале"}
# Оценки, у которых у администратора есть явное действие (#188): «Не тот блок», «Дубль», «Не релевантно».
_EXPLICIT_VERDICTS = ("wrong_block", "merge_duplicate", "wrong_domain")
_AWAITS = "Ждёт недельного среза"


class JournalRefused(Exception):
    """Действие в журнале не выполнено: причина словами для ответа (409)."""


class JournalNotFound(Exception):
    pass


# --- Явное действие администратора из формы ОС ---------------------------------------------------


def record_immediate(event_id: int, signal_id: int, changes: list[dict[str, Any]], *, author: str | None,
                     user_id: int | None) -> int | None:
    """Строка «сразу» к отметке администратора: что применено и каким было до него; по ней отмена из журнала."""
    changes = [change for change in changes if change]
    if not changes:
        return None
    agents = [_agent_of(change["field"]) for change in changes]
    return feedback_store.create_action(IMMEDIATE, DONE, signal_id=signal_id, event_ids=[event_id],
                                        agents=[agent for agent in agents if agent], details={"changes": changes},
                                        author=author, user_id=user_id)


def after_restore(signal_id: int, *, author: str | None, user_id: int | None) -> None:
    """«Вернуть» в «Убрано людьми» мимо журнала: уборка, которую вернули, в журнале отменена."""
    action = feedback_store.latest_with_change(signal_id, "removed")
    if action is None:
        return
    # Уборка согласованным улучшением: отменено и само улучшение, путь ведёт его строка.
    owner = action["parent_id"] if action["kind"] == APPLIED else action["id"]
    with connection.get_connection() as conn:
        feedback_store.set_status(action["id"], UNDONE, conn=conn)
        if owner != action["id"]:
            feedback_store.set_status(owner, UNDONE, conn=conn)
        feedback_store.create_action(UNDO, DONE, signal_id=signal_id, parent_id=owner, details={"via": "restore"},
                                     author=author, user_id=user_id, conn=conn)
        conn.commit()


def _agent_of(field: str) -> str | None:
    if field == "correction":
        return "summary"
    if field == "returned":
        return "otbor"
    spec = feedback_changes.FIELDS.get(field)
    return spec.agent if spec else None


# --- Строки журнала ------------------------------------------------------------------------------


def entries(*, is_admin: bool, status: str | None = None, showcase: str | None = None, agent: str | None = None,
            author: str | None = None, signal_id: int | None = None, kind: str | None = None, limit: int = 100,
            offset: int = 0) -> dict[str, Any]:
    """Строки журнала по фильтрам, новые сверху: отметки, предложения и задачи вместе."""
    take = int(limit) + int(offset)
    history_end, slice_end = feedback_store.slice_bounds()
    marks = [] if kind not in (None, "mark") else feedback_store.journal_marks(
        status=status, showcase=showcase, agent=agent, author=author, signal_id=signal_id, limit=take,
        history_end=history_end, slice_end=slice_end)
    kinds = [kind] if kind in feedback_store.TOP_KINDS else list(feedback_store.TOP_KINDS)
    actions = [] if kind == "mark" else feedback_store.journal_actions(
        status=status, showcase=showcase, agent=agent, author=author, signal_id=signal_id, kinds=kinds, limit=take)
    rows = sorted([("mark", row) for row in marks] + [("action", row) for row in actions],
                  key=lambda item: (item[1]["created_at"], item[0] == "action", item[1]["id"]), reverse=True)
    rows = rows[int(offset):int(offset) + int(limit)]
    mark_rows = [row for kind_, row in rows if kind_ == "mark"]
    action_rows = [row for kind_, row in rows if kind_ == "action"]
    steps = feedback_store.children([row["id"] for row in action_rows]
                                    + [row["im_id"] for row in mark_rows if row["im_id"]])
    proposals = feedback_store.proposals_for_events([row["id"] for row in mark_rows])
    basis = feedback_store.marks_by_id([one for row in action_rows for one in row["event_ids"]])
    built = [(_mark_entry(row, steps, proposals, (history_end, slice_end), is_admin) if kind_ == "mark"
              else _action_entry(row, steps, basis, is_admin)) for kind_, row in rows]
    return {"entries": built, "authors": feedback_store.mark_authors()}


def _mark_entry(row: dict[str, Any], steps: dict[int, list[dict[str, Any]]], proposals: dict[int, list[dict[str, Any]]],
                bounds: tuple[datetime | None, datetime | None], is_admin: bool) -> dict[str, Any]:
    """Отметка: что пришло и что с ней сделала система. bounds: конец истории (выкат одной формы ОС, до него среза не
    было) и конец окна последнего выполненного среза."""
    history_end, slice_end = bounds
    history = history_end is not None and row["created_at"] <= history_end
    path = [_step("Записано" if row["role"] != "admin" else "Записано: отметка администратора", row["created_at"],
                  row["author"])]
    system = _done_at_once(row, history)
    if row["im_id"]:
        path.append(_step("Сразу: " + "; ".join(system), row["created_at"], None))
        path += [_step(_undo_label(step), step["created_at"], step["author"]) for step in steps.get(row["im_id"], [])]
    elif system:
        path.append(_step(system[0], row["created_at"], None))
    found = proposals.get(int(row["id"])) or []
    for proposal in found:
        path.append(_step(f"В предложении №{proposal['id']}: {STATUS_RU.get(proposal['status'], proposal['status'])}",
                          None, None))
    # Срез читает записи блока ОС после выката одной формы ОС; путь отметки с явным действием ведёт «сразу».
    if row["signal_id"] and row["is_block"] and not found and not system and not history:
        waits = slice_end is None or row["created_at"] > slice_end
        label = _AWAITS if waits else "Учтено недельным срезом: без предложения"
        path.append(_step(label, None, None))
        system = [label] if waits else []
    return {
        "key": f"mark:{row['id']}", "kind": "mark", "id": int(row["id"]), "at": _iso(row["created_at"]),
        "author": row["author"], "role": row["role"], "signal_id": row["signal_id"],
        "signal_title": row["signal_title"], "showcase": row["showcase"], "agents": list(row["agents"] or []),
        "status": row["status"], "verdict": row["verdict"], "reason": row["reason"], "comment": row["comment"],
        "corrected_title": row["corrected_title"], "corrected_thesis": row["corrected_thesis"],
        "duplicate_of_signal_id": row["duplicate_of_signal_id"], "applied": row["applied"], "system": system,
        "awaits_slice": system == [_AWAITS], "path": path, "action_id": row["im_id"],
        "can_undo": is_admin and row["im_id"] is not None and row["im_status"] == DONE, "proposals": found,
    }


def _done_at_once(row: dict[str, Any], history: bool) -> list[str]:
    """Что легло на карточку сразу: по строке «сразу»; у правок и явных действий до журнала ОС (строки «сразу» и отмены
    из журнала у них нет): словами."""
    if row["im_id"]:
        return [feedback_changes.describe(change) for change in (row["im_details"] or {}).get("changes") or []]
    has_correction = bool((row["corrected_title"] or "").strip() or (row["corrected_thesis"] or "").strip())
    if row["applied"] and has_correction and (history or row["role"] == "admin"):
        return ["Правка легла на карточку сразу (до журнала ОС)"]
    if row["applied"] and row["role"] == "admin" and row["verdict"] in _EXPLICIT_VERDICTS and not history:
        return ["Явное действие администратора (до журнала ОС)"]
    return []


def _action_entry(row: dict[str, Any], steps: dict[int, list[dict[str, Any]]], basis: dict[int, dict[str, Any]],
                  is_admin: bool) -> dict[str, Any]:
    details = row["details"] or {}
    entry = {
        "key": f"action:{row['id']}", "kind": row["kind"], "id": int(row["id"]), "at": _iso(row["created_at"]),
        "author": row["author"] or REVIEW_AUTHOR, "role": None, "signal_id": row["signal_id"],
        "signal_title": row["signal_title"], "showcase": row["showcase"], "agents": list(row["agents"] or []),
        "status": row["status"], "quote": row["quote"], "event_ids": [int(one) for one in row["event_ids"] or []],
    }
    own_steps = steps.get(row["id"], [])
    if row["kind"] == IMMEDIATE:
        system = [feedback_changes.describe(change) for change in details.get("changes") or []]
        path = [_step("Сразу: " + "; ".join(system), row["created_at"], row["author"])]
        path += [_step(_undo_label(step), step["created_at"], step["author"]) for step in own_steps]
        return {**entry, "system": system, "path": path, "action_id": int(row["id"]),
                "can_undo": is_admin and row["status"] == DONE}
    if row["kind"] == SAMPLE_TASK:
        publications = details.get("publications") or []
        path = [_step(f"Задача выборки: {len(publications)} отклонённых «Отбором» публикаций на просмотр",
                      row["created_at"], None)]
        path += [_step("Просмотрено", step["created_at"], step["author"]) for step in own_steps]
        return {**entry, "publications": publications, "month": details.get("month"), "path": path,
                "can_close": is_admin and row["status"] == PENDING}
    path = [_step(f"Предложено разбором ОС (срез №{row['slice_id']})" if row["slice_id"] else
                  "Предложено разбором ОС", row["created_at"], None)]
    path += [_path_step(step) for step in own_steps]
    if row["status"] == STALE:
        reason = details.get("stale_reason") or (f"заменено предложением №{details['superseded_by']}"
                                                if details.get("superseded_by") else "без решения")
        path.append(_step(f"Устарело: {reason}", row["updated_at"], None))
    proposed = details.get("changes") or []
    # «Изменить»: на карточку легла формулировка администратора, её и показываем; предложение разбора рядом.
    modified = next(((step["details"] or {}).get("changes") for step in own_steps
                     if step["kind"] == DECISION and step["status"] == MODIFIED), None)
    return {
        **entry, "changes": modified or proposed, "proposed_changes": proposed if modified else None, "path": path,
        "basis": [_basis(basis[one]) for one in entry["event_ids"] if one in basis],
        "can_decide": is_admin and _decidable(row, own_steps),
        "can_undo": is_admin and row["status"] == APPLIED_STATUS,
    }


def _path_step(step: dict[str, Any]) -> dict[str, Any]:
    """Шаг пути предложения: подпись по тому, что записано в шаге, а не по его статусу (статус меняет отмена)."""
    details = step["details"] or {}
    if step["kind"] == DECISION:
        return _step(_DECISION_RU.get(step["status"], step["status"]), step["created_at"], step["author"])
    if step["kind"] == APPLIED:
        refused = "; ".join(f"{one['label']}: {one['reason']}" for one in details.get("refused") or [])
        if details.get("changes"):
            text = "Применено системой" + (f". Не применено: {refused}" if refused else "")
        else:
            text = "Не применено" + (f": {refused}" if refused else "")
        return _step(text, step["created_at"], step["author"])
    return _step(_undo_label(step), step["created_at"], step["author"])


def _undo_label(step: dict[str, Any]) -> str:
    details = step.get("details") or {}
    text = "Отменено кнопкой «Вернуть»" if details.get("via") == "restore" else "Отменено"
    pairs = [change["root"] for change in details.get("undone") or [] if change.get("field") == "duplicate_of"]
    if pairs:
        text += "; для «Дедупа» записано «не одно событие» с №" + ", №".join(str(one) for one in pairs)
    if details.get("failed"):
        text += f"; не целиком: {details['failed']}"
    return text


def _basis(mark: dict[str, Any]) -> dict[str, Any]:
    return {"id": int(mark["id"]), "author": mark["author"], "role": mark["role"], "at": _iso(mark["created_at"]),
            "verdict": mark["verdict"], "reason": mark["reason"], "comment": mark["comment"],
            "corrected_title": mark["corrected_title"], "corrected_thesis": mark["corrected_thesis"],
            "duplicate_of_signal_id": mark["duplicate_of_signal_id"]}


def _step(label: str, at: Any, author: str | None) -> dict[str, Any]:
    return {"label": label, "at": _iso(at), "author": author}


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else value


# --- Решения администратора ----------------------------------------------------------------------


def _decidable(proposal: dict[str, Any], steps: list[dict[str, Any]]) -> bool:
    """Ждёт решения; или согласовано, а применить не вышло ничего (отказ по всем полям, сбой): решить можно снова."""
    if proposal["status"] == PENDING:
        return True
    applied = any(step["kind"] == APPLIED and (step["details"] or {}).get("changes") for step in steps)
    return proposal["status"] in (APPROVED, MODIFIED) and not applied


def decide(proposal_id: int, decision: str, *, author: str | None, user_id: int | None,
           changes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """«Согласовать», «изменить» (новая формулировка тех же полей) или «убрать». Согласованное и изменённое система
    применяет к карточке сразу записью журнала с именем администратора; убранное остаётся в журнале."""
    from oiltech_digest import feedback_review

    status = DECISIONS.get(decision)
    if status is None:
        raise JournalRefused("решение: согласовать, изменить или убрать")
    with connection.get_connection() as conn:
        proposal = feedback_store.get_action(proposal_id, for_update=True, conn=conn)
        if proposal is None or proposal["kind"] != PROPOSAL:
            raise JournalNotFound(f"предложения №{int(proposal_id)} нет")
        if not _decidable(proposal, feedback_store.children([proposal["id"]])[proposal["id"]]):
            raise JournalRefused(f"предложение уже не ждёт решения: {STATUS_RU.get(proposal['status'])}")
        proposed = (proposal["details"] or {}).get("changes") or []
        final = proposed
        if status == MODIFIED:
            final = feedback_review.admin_changes(proposal["signal_id"], proposed, changes or [])
        feedback_store.set_status(proposal["id"], status, conn=conn)
        feedback_store.create_action(DECISION, status, signal_id=proposal["signal_id"], parent_id=proposal["id"],
                                     details={"changes": final} if status == MODIFIED else {}, author=author,
                                     user_id=user_id, conn=conn)
        conn.commit()
    if status == DISMISSED:
        return {"status": DISMISSED}
    return _apply_proposal(proposal, final, status=status, author=author, user_id=user_id)


def _apply_proposal(proposal: dict[str, Any], changes: list[dict[str, Any]], *, status: str, author: str | None,
                    user_id: int | None) -> dict[str, Any]:
    """Применить изменения по одному; что применено и что нет, ложится строкой «применение» в любом случае: и при
    отказе поля, и при сбое (иначе применённое осталось бы без записи и без отмены)."""
    applied, refused = [], []
    for change in sorted(changes, key=lambda one: one["field"] == "removed"):
        label = feedback_changes.FIELDS[change["field"]].label
        try:
            applied.append(feedback_changes.apply_change(proposal["signal_id"], change["field"], change["value"],
                                                         author=author, user_id=user_id))
        except feedback_changes.ChangeRefused as exc:
            refused.append({"field": change["field"], "label": label, "reason": str(exc)})
        except Exception as exc:  # noqa: BLE001 - сбой одного поля не теряет записи о применённых
            logger.exception("feedback_apply_failed proposal=%s field=%s", proposal["id"], change["field"])
            refused.append({"field": change["field"], "label": label, "reason": f"сбой: {str(exc)[:200]}"})
    final = APPLIED_STATUS if applied else status
    with connection.get_connection() as conn:
        feedback_store.create_action(APPLIED, final, signal_id=proposal["signal_id"], parent_id=proposal["id"],
                                     details={"changes": applied, "refused": refused}, author=author,
                                     user_id=user_id, conn=conn)
        feedback_store.set_status(proposal["id"], final, details={"refused": refused} if refused else None, conn=conn)
        conn.commit()
    return {"status": final, "applied": len(applied), "refused": refused}


def undo(action_id: int, *, author: str | None, user_id: int | None) -> dict[str, Any]:
    """Отменить явное действие администратора или применённое улучшение: прежнее состояние карточки. Строка
    захватывается под замком («отменено»), чтобы два запроса не отменили её дважды; не вышло ничего: прежний статус."""
    with connection.get_connection() as conn:
        action = feedback_store.get_action(action_id, for_update=True, conn=conn)
        if action is None:
            raise JournalNotFound(f"действия №{int(action_id)} нет")
        target = _undo_target(action)
        changes = (target["details"] or {}).get("changes") or []
        problems = [problem for change in changes
                    if (problem := feedback_changes.undo_problem(action["signal_id"], change))]
        if problems:
            raise JournalRefused("; ".join(problems))
        previous = action["status"]
        feedback_store.set_status(action["id"], UNDONE, conn=conn)
        conn.commit()
    undone, failed = [], None
    for change in reversed(changes):
        try:
            feedback_changes.undo_change(action["signal_id"], change, author=author, action_id=action["id"])
        except feedback_changes.ChangeRefused as exc:
            failed = str(exc)
            break
        undone.append(change)
    with connection.get_connection() as conn:
        if not undone:
            feedback_store.set_status(action["id"], previous, conn=conn)
            conn.commit()
            raise JournalRefused(failed or "отменять нечего")
        if target["id"] != action["id"]:
            feedback_store.set_status(target["id"], UNDONE, conn=conn)
        feedback_store.create_action(UNDO, DONE, signal_id=action["signal_id"], parent_id=action["id"],
                                     details={"undone": undone, "failed": failed}, author=author, user_id=user_id,
                                     conn=conn)
        conn.commit()
    return {"status": UNDONE, "undone": len(undone), "failed": failed}


def _undo_target(action: dict[str, Any]) -> dict[str, Any]:
    """Где записаны отменяемые изменения: у явного действия в нём самом, у предложения в его применении."""
    if action["kind"] == IMMEDIATE and action["status"] == DONE:
        return action
    if action["kind"] == PROPOSAL and action["status"] == APPLIED_STATUS:
        target = next((step for step in feedback_store.children([action["id"]])[action["id"]]
                       if step["kind"] == APPLIED and step["status"] == APPLIED_STATUS), None)
        if target is not None:
            return target
        raise JournalRefused("у предложения нет применения, отменять нечего")
    raise JournalRefused(f"отменить можно только сделанное или применённое: сейчас {STATUS_RU.get(action['status'])}")


def close_task(action_id: int, *, author: str | None, user_id: int | None) -> dict[str, Any]:
    """Задача выборки просмотрена: «сделано»."""
    with connection.get_connection() as conn:
        action = feedback_store.get_action(action_id, for_update=True, conn=conn)
        if action is None or action["kind"] != SAMPLE_TASK:
            raise JournalNotFound(f"задачи №{int(action_id)} нет")
        if action["status"] != PENDING:
            raise JournalRefused(f"задача уже не ждёт: {STATUS_RU.get(action['status'])}")
        feedback_store.set_status(action["id"], DONE, conn=conn)
        feedback_store.create_action(DECISION, DONE, parent_id=action["id"], author=author, user_id=user_id,
                                     conn=conn)
        conn.commit()
    return {"status": DONE}


def expire_stale() -> int:
    """Предложения без решения дольше срока жизни из настроек: «устарело», остаются в журнале."""
    days = int(feedback_settings.current()["proposal_lifetime_days"])
    return feedback_store.expire_pending(kinds=(PROPOSAL,), older_than_days=days)
