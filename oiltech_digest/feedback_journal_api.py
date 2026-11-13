"""HTTP API журнала ОС (#189, спека #187): вкладка «Обратная связь».

Чтение журнала, состояния среза и настроек: любая допущенная учётка (если настройка «кто видит вкладку» не закрыла её
участникам). Решения по предложениям, отмена, закрытие задачи выборки, «собрать сейчас» и правка настроек: только
администратор (ADR 0004, поправка 12.11), остальным 403. Роутер подключает api.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from oiltech_digest import feedback_journal, feedback_rules, feedback_settings, feedback_slice, rule_check
from oiltech_digest.api_access import require_admin, require_user
from oiltech_digest.db import feedback_store

_KINDS = ("mark", *feedback_store.TOP_KINDS)
router = APIRouter()


def _reader(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Журнал видят все допущенные; настройка контура может закрыть вкладку участникам."""
    if feedback_settings.current()["tab_visible_to"] == "admin" and not _is_admin(user):
        raise HTTPException(status_code=403, detail="Вкладку «Обратная связь» настройка контура открыла только "
                                                    "администратору")
    return user


def _is_admin(user: dict[str, Any]) -> bool:
    return (user.get("role") or "user") == "admin"


@router.get("/api/feedback-journal")
def feedback_journal_entries(
    status: str | None = Query(None, max_length=20),
    showcase: str | None = Query(None, pattern="^(business|technology|other)$"),
    agent: str | None = Query(None, max_length=20),
    author: str | None = Query(None, max_length=200),
    signal_id: int | None = Query(None, ge=1),
    kind: str | None = Query(None, max_length=30),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=100_000),
    user: dict[str, Any] = Depends(_reader),
) -> dict[str, Any]:
    """Строки журнала ОС обеих витрин: отметки, предложения разбора ОС и задачи выборки, новые сверху, с фильтрами."""
    if status and status not in feedback_store.STATUSES:
        raise HTTPException(status_code=422, detail=f"Неизвестный статус журнала: {status}")
    if agent and agent not in feedback_store.AGENTS:
        raise HTTPException(status_code=422, detail=f"Неизвестный агент: {agent}")
    if kind and kind not in _KINDS:
        raise HTTPException(status_code=422, detail=f"Неизвестный вид строки журнала: {kind}")
    return feedback_journal.entries(is_admin=_is_admin(user), status=status, showcase=showcase, agent=agent,
                                    author=(author or "").strip() or None, signal_id=signal_id, kind=kind,
                                    limit=limit, offset=offset)


@router.get("/api/feedback-journal/overview")
def feedback_journal_overview(user: dict[str, Any] = Depends(_reader)) -> dict[str, Any]:
    """Срезы, следующий по расписанию, расход и потолок; предупреждение о потолке: администратору."""
    return feedback_slice.overview(is_admin=_is_admin(user))


@router.post("/api/feedback-journal/slices")
def feedback_journal_collect_now(user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """Кнопка «собрать сейчас»: недельный срез вне расписания."""
    try:
        return feedback_slice.collect_now(author=user.get("email"))
    except feedback_slice.SliceRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class ProposalChange(BaseModel):
    field: str = Field(max_length=40)
    value: str = Field(max_length=4000)


class ProposalDecision(BaseModel):
    decision: str = Field(pattern="^(approve|modify|dismiss)$")
    # «Изменить»: новая формулировка полей предложения.
    changes: list[ProposalChange] = Field(default_factory=list, max_length=20)
    # Предложение правила (#190): «изменить»: новая формулировка правила; confirm: отдельное подтверждение согласования
    # без готовой проверки, с «мало данных» или с перевесом «от эталона».
    text: str | None = Field(None, max_length=1000)
    confirm: bool = False


@router.post("/api/feedback-journal/proposals/{proposal_id}/decision")
def feedback_journal_decide(proposal_id: int, payload: ProposalDecision,
                            user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """«Согласовать», «изменить» или «убрать» предложение: согласованное система сразу применяет к карточке, а правило
    эксперта: новой версией набора правил агента."""
    with _refusals():
        return feedback_journal.decide(proposal_id, payload.decision, author=user.get("email"),
                                       user_id=int(user["id"]),
                                       changes=[change.model_dump() for change in payload.changes],
                                       text=payload.text, confirm=payload.confirm)


@router.post("/api/feedback-journal/proposals/{proposal_id}/rollback")
def feedback_journal_rollback(proposal_id: int, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """Откат версии набора правил, которую выпустило согласование предложения: действует прежняя (#190)."""
    with _refusals():
        return feedback_rules.rollback(proposal_id, author=user.get("email"), user_id=int(user["id"]))


@router.post("/api/feedback-journal/proposals/{proposal_id}/check")
def feedback_journal_recheck(proposal_id: int, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """Автопроверка предложения правила заново: после сбоя, отсрочки или смены инструкции (#190)."""
    with _refusals():
        return rule_check.recheck(proposal_id)


@router.get("/api/feedback-journal/rules")
def feedback_journal_rules(user: dict[str, Any] = Depends(_reader)) -> dict[str, Any]:
    """Правила эксперта по агентам: действующая версия набора и её правила, прежние версии (#190)."""
    return {**feedback_rules.overview(), "can_rollback": _is_admin(user)}


@router.post("/api/feedback-journal/actions/{action_id}/undo")
def feedback_journal_undo(action_id: int, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """Отменить явное действие администратора или применённое улучшение: прежнее состояние карточки."""
    with _refusals():
        return feedback_journal.undo(action_id, author=user.get("email"), user_id=int(user["id"]))


@router.post("/api/feedback-journal/tasks/{action_id}/close")
def feedback_journal_close_task(action_id: int, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """Задача выборки отклонённого просмотрена."""
    with _refusals():
        return feedback_journal.close_task(action_id, author=user.get("email"), user_id=int(user["id"]))


@router.get("/api/feedback-journal/settings")
def feedback_journal_settings(user: dict[str, Any] = Depends(_reader)) -> dict[str, Any]:
    """Настройки контура ОС: действующие значения, состав и умолчания; править может только администратор."""
    return {"values": feedback_settings.current(), "fields": feedback_settings.describe(),
            "can_edit": _is_admin(user)}


class SettingsPatch(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


@router.put("/api/feedback-journal/settings")
def feedback_journal_save_settings(payload: SettingsPatch,
                                   user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """Правка настроек контура администратором: действует сразу, без выката."""
    try:
        values = feedback_settings.save(payload.values, author=user.get("email"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"values": values, "fields": feedback_settings.describe(), "can_edit": True}


@contextmanager
def _refusals() -> Iterator[None]:
    """Отказ журнала: 404 (нет такой строки) или 409 (сейчас нельзя, причина словами)."""
    try:
        yield
    except feedback_journal.JournalNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except feedback_journal.JournalRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
