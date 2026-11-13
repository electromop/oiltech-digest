"""Правила эксперта в журнале ОС (#190, спека #187, ADR 0007): предложение правила агенту, решение администратора,
применение системой новой версией набора и откат.

Предложение правила (rule_proposal) приходит от разбора ОС (feedback_review_rules) или засеяно до кода (первое
предложение: три подсказки толкования для стадии «Карточки»); до решения его меряет автопроверка (rule_check).
«Согласовать» и «изменить» (новая формулировка администратора) система сразу применяет: новая версия набора правил
агента (expert_rules_store.apply_rule); «убрать» оставляет предложение в журнале. Согласование без готовой проверки, с
пометкой «мало данных» или с перевесом «от эталона» требует отдельного подтверждения (confirm). Больше LIMIT действующих
правил у агента согласовать нельзя. Откат делает действующей прежнюю версию набора. Прошлые решения агентов не
пересчитываются ни при согласовании, ни при откате (решение владельца 12.11): новая версия действует для новых вызовов.
"""

from __future__ import annotations

from typing import Any

from oiltech_digest import expert_rules
from oiltech_digest.db import connection, expert_rules_store, feedback_store, rule_check_store
from oiltech_digest.db.expert_rules_store import RulesRefused
from oiltech_digest.db.feedback_store import (APPLIED, APPLIED_STATUS, APPROVED, DECISION, DISMISSED, MODIFIED, PENDING,
                                              RULE_PROPOSAL, SET_CHECK, STALE, UNDO, UNDONE)

RULE_KINDS = {"new": "новое правило", "refine": "уточнение правила", "merge": "сведение правил"}


def topics() -> list[str]:
    """Темы поиска радара: у правила поиска тема только из них."""
    from oiltech_digest import signal_discovery

    return signal_discovery.search_topic_names()


def describe(rule: dict[str, Any]) -> dict[str, Any]:
    """Правило для экрана: подписи агента, шага и категории."""
    agent, step, category = rule.get("agent") or "", rule.get("step") or "", rule.get("category")
    return {"agent": agent, "agent_label": expert_rules.agent_label(agent), "step": step,
            "step_label": expert_rules.step_label(agent, step), "category": category,
            "category_label": expert_rules.CATEGORIES.get(category) if category else None,
            "topic": rule.get("topic"), "text": rule.get("text") or ""}


# --- Предложение ---------------------------------------------------------------------------------


def create_proposal(rule: dict[str, Any], *, rule_kind: str, replaces: list[int], event_ids: list[int],
                    quote: str | None, slice_id: int | None, author: str, details: dict[str, Any] | None = None,
                    conn) -> int:
    """Предложение правила в журнал ОС (ждёт решения администратора), внутри транзакции вызывающего. Тексты
    правил, которые оно уточняет или сводит, снимком: набор потом может смениться."""
    replaced = expert_rules_store.rules_by_ids(replaces, conn=conn)
    action_id = feedback_store.create_action(
        RULE_PROPOSAL, PENDING, slice_id=slice_id, event_ids=event_ids, quote=quote, agents=[rule["agent"]],
        author=author, conn=conn,
        details={**(details or {}), "rule": rule, "rule_kind": rule_kind, "replaces": sorted(set(replaces)),
                 "replaced": [describe(replaced[one]) | {"id": one} for one in sorted(set(replaces))
                              if one in replaced]})
    if slice_id is not None:
        feedback_store.add_proposals(int(slice_id), 1, conn=conn)
    return action_id


# --- Отдельное подтверждение ---------------------------------------------------------------------


def confirmation(check: dict[str, Any] | None) -> list[str]:
    """Почему согласование требует отдельного подтверждения: проверки нет или не готова, «мало данных», перевес «от
    эталона». У «Сути и перевода» числа нет («было / стало»): готовой проверки достаточно."""
    if check is None:
        return ["автопроверки ещё не было"]
    if check["status"] != rule_check_store.DONE:
        state = {"queued": "ещё идёт", "postponed": "отложена потолком расходов",
                 "failed": "не выполнена"}.get(check["status"], check["status"])
        return [f"автопроверка {state}"]
    details = check.get("details") or {}
    if details.get("no_count"):
        return []
    reasons = []
    if details.get("little_data"):
        reasons.append(f"мало данных: {int(check['row_count'])}")
    if int(check["away"]) > int(check["toward"]):
        reasons.append(f"от эталона {int(check['away'])}, к эталону {int(check['toward'])}")
    return reasons


# --- Решение администратора ----------------------------------------------------------------------


def decide(proposal_id: int, decision: str, *, author: str | None, user_id: int | None, text: str | None = None,
           confirm: bool = False) -> dict[str, Any]:
    """«Согласовать», «изменить» (новая формулировка правила) или «убрать». Согласованное и изменённое система сразу
    применяет: новая версия набора агента. Отказ (JournalRefused): решение не записано, предложение ждёт."""
    from oiltech_digest.feedback_journal import DECISIONS, STATUS_RU, JournalNotFound, JournalRefused

    status = DECISIONS.get(decision)
    if status is None:
        raise JournalRefused("решение: согласовать, изменить или убрать")
    with connection.get_connection() as conn:
        proposal = feedback_store.get_action(proposal_id, for_update=True, conn=conn)
        if proposal is None or proposal["kind"] != RULE_PROPOSAL:
            raise JournalNotFound(f"предложения правила №{int(proposal_id)} нет")
        if proposal["status"] != PENDING:
            raise JournalRefused(f"предложение уже не ждёт решения: {STATUS_RU.get(proposal['status'])}")
        details = proposal["details"] or {}
        if status == DISMISSED:
            feedback_store.set_status(proposal["id"], DISMISSED, conn=conn)
            feedback_store.create_action(DECISION, DISMISSED, parent_id=proposal["id"], agents=proposal["agents"],
                                         author=author, user_id=user_id, conn=conn)
            conn.commit()
            return {"status": DISMISSED}
        rule = dict(details.get("rule") or {})
        if status == MODIFIED:
            try:
                rule = expert_rules.checked_rule(rule.get("agent"), rule.get("step"), rule.get("category"),
                                                 rule.get("topic"), text, topics=topics())
            except expert_rules.RuleProblem as exc:
                raise JournalRefused(str(exc)) from exc
            if rule["text"] == (details.get("rule") or {}).get("text"):
                raise JournalRefused("новая формулировка та же, что в предложении: согласуйте его")
        check = rule_check_store.latest([proposal["id"]]).get(int(proposal["id"]))
        reasons = confirmation(check)
        if reasons and not confirm:
            raise JournalRefused("нужно отдельное подтверждение: " + "; ".join(reasons))
        try:
            applied = expert_rules_store.apply_rule(
                rule, replaces=details.get("replaces") or [], action_id=proposal["id"],
                event_ids=proposal["event_ids"] or [], quote=proposal["quote"], author=author, user_id=user_id,
                conn=conn)
        except RulesRefused as exc:
            raise JournalRefused(str(exc)) from exc
        feedback_store.create_action(
            DECISION, status, parent_id=proposal["id"], agents=proposal["agents"], author=author, user_id=user_id,
            details={**({"rule": rule} if status == MODIFIED else {}), **({"confirmed": reasons} if reasons else {})},
            conn=conn)
        feedback_store.create_action(APPLIED, APPLIED_STATUS, parent_id=proposal["id"], agents=proposal["agents"],
                                     author=author, user_id=user_id, details=applied, conn=conn)
        feedback_store.set_status(proposal["id"], APPLIED_STATUS, details={"applied_rule": rule}, conn=conn)
        conn.commit()
    return {"status": APPLIED_STATUS, **applied}


def rollback(proposal_id: int, *, author: str | None, user_id: int | None) -> dict[str, Any]:
    """Откатить версию набора, которую выпустило согласование предложения: действующей снова становится прежняя.
    Откатить можно только действующую версию; прошлые решения агентов не пересчитываются."""
    from oiltech_digest.feedback_journal import STATUS_RU, JournalNotFound, JournalRefused

    with connection.get_connection() as conn:
        proposal = feedback_store.get_action(proposal_id, for_update=True, conn=conn)
        if proposal is None or proposal["kind"] != RULE_PROPOSAL:
            raise JournalNotFound(f"предложения правила №{int(proposal_id)} нет")
        if proposal["status"] != APPLIED_STATUS:
            raise JournalRefused(f"откатить можно только применённое правило: сейчас {STATUS_RU.get(proposal['status'])}")
        applied = expert_rules_store.set_of_action(proposal["id"], conn=conn)
        if applied is None:
            raise JournalRefused("у предложения нет версии набора, откатывать нечего")
        try:
            done = expert_rules_store.rollback(applied["id"], conn=conn)
        except RulesRefused as exc:
            raise JournalRefused(str(exc)) from exc
        feedback_store.set_status(proposal["id"], UNDONE, conn=conn)
        feedback_store.create_action(UNDO, APPLIED_STATUS, parent_id=proposal["id"], agents=proposal["agents"],
                                     author=author, user_id=user_id, details={"rollback": done}, conn=conn)
        conn.commit()
    return {"status": UNDONE, **done}


# --- Строки журнала ------------------------------------------------------------------------------


def entry(row: dict[str, Any], own_steps: list[dict[str, Any]], basis: list[dict[str, Any]],
          check: dict[str, Any] | None, is_admin: bool, base: dict[str, Any]) -> dict[str, Any]:
    """Предложение правила: правило, на чём основано, что показала автопроверка, путь и решения."""
    from oiltech_digest.feedback_journal import _iso, _step

    details = row["details"] or {}
    rule = details.get("rule") or {}
    path = [_step(_origin(row), row["created_at"], None)]
    for step in own_steps:
        step_details = step["details"] or {}
        if step["kind"] == DECISION:
            label = {APPROVED: "Согласовано", MODIFIED: "Изменено", DISMISSED: "Убрано, остаётся в журнале"}.get(
                step["status"], step["status"])
            if step_details.get("confirmed"):
                label += " с отдельным подтверждением (" + "; ".join(step_details["confirmed"]) + ")"
            path.append(_step(label, step["created_at"], step["author"]))
        elif step["kind"] == APPLIED:
            path.append(_step(f"Применено системой: версия {step_details.get('version')} набора правил агента "
                              f"{expert_rules.agent_label(rule.get('agent') or '')}", step["created_at"],
                              step["author"]))
        elif step["kind"] == UNDO:
            back = step_details.get("rollback") or {}
            path.append(_step(f"Откат: действует версия {back.get('to_version')} вместо {back.get('from_version')}",
                              step["created_at"], step["author"]))
    if row["status"] == STALE:
        path.append(_step(f"Устарело: {details.get('stale_reason') or 'без решения'}", row["updated_at"], None))
    applied_set = expert_rules_store.set_of_action(row["id"]) if row["status"] == APPLIED_STATUS else None
    modified = next(((step["details"] or {}).get("rule") for step in own_steps
                     if step["kind"] == DECISION and step["status"] == MODIFIED), None)
    return {
        **base, "rule": describe(modified or rule), "proposed_rule": describe(rule) if modified else None,
        "rule_kind": details.get("rule_kind") or "new", "replaced": details.get("replaced") or [],
        "check": check_view(check), "confirm": confirmation(check) if row["status"] == PENDING else [],
        "version": int(applied_set["version"]) if applied_set else None, "basis": basis, "path": path,
        "can_decide": is_admin and row["status"] == PENDING,
        "can_rollback": is_admin and applied_set is not None and bool(applied_set["current"]),
        "can_recheck": is_admin and row["status"] == PENDING and (check is None or check["status"] in (
            rule_check_store.FAILED, rule_check_store.POSTPONED, rule_check_store.DONE)),
        "seed": details.get("seed"), "at": _iso(row["created_at"]),
    }


def set_check_entry(row: dict[str, Any], check: dict[str, Any] | None, base: dict[str, Any]) -> dict[str, Any]:
    """Итог ежемесячной проверки всего набора правил агента против голой инструкции."""
    from oiltech_digest.feedback_journal import _step

    details = row["details"] or {}
    agent = details.get("agent") or ""
    label = (f"Проверка набора правил агента {expert_rules.agent_label(agent)} (версия {details.get('version')}) "
             "против голой инструкции")
    return {**base, "rule": None, "check": check_view(check), "version": details.get("version"),
            "set_agent": agent, "set_agent_label": expert_rules.agent_label(agent),
            "path": [_step(label, row["created_at"], None)]}


def check_view(check: dict[str, Any] | None) -> dict[str, Any] | None:
    if check is None:
        return None
    details = check.get("details") or {}
    return {"id": int(check["id"]), "status": check["status"], "mode": check["mode"], "rows": int(check["row_count"]),
            "toward": int(check["toward"]), "away": int(check["away"]), "unchanged": int(check["unchanged"]),
            "advice": check.get("advice"), "note": check.get("note"),
            "little_data": bool(details.get("little_data")), "no_count": bool(details.get("no_count")),
            "baseline": details.get("baseline"), "samples": details.get("samples") or [],
            "search": details.get("search"), "finished_at": check["finished_at"].isoformat()
            if check.get("finished_at") else None}


def _origin(row: dict[str, Any]) -> str:
    details = row["details"] or {}
    if details.get("seed"):
        return f"Предложено: {details['seed']}"
    return f"Предложено разбором ОС (срез №{row['slice_id']})" if row["slice_id"] else "Предложено разбором ОС"


# --- Набор правил для вкладки --------------------------------------------------------------------


def overview() -> dict[str, Any]:
    """Правила эксперта по агентам: шаги, действующая версия и её правила, прежние версии, потолок."""
    current = expert_rules_store.current_sets()
    agents = []
    for agent, spec in expert_rules.AGENTS.items():
        found = current.get(agent)
        agents.append({
            "agent": agent, "label": spec.label, "steps": [{"step": key, "label": label}
                                                           for key, label in spec.steps.items()],
            "version": int(found["version"]) if found else 0, "action_id": found["action_id"] if found else None,
            "rules": [describe(rule) | {"id": int(rule["id"]), "approved_by": rule["approved_by"],
                                        "approved_at": rule["created_at"].isoformat()} for rule in (found or {}).get(
                "rules") or []],
            "history": [{"version": int(one["version"]), "rules": len(one["rule_ids"]), "current": one["current"],
                         "previous_version": one["previous_version"] or 0, "author": one["author"],
                         "at": one["created_at"].isoformat(), "action_id": one["action_id"]}
                        for one in expert_rules_store.history(agent)],
        })
    return {"agents": agents, "limit": expert_rules.LIMIT}


