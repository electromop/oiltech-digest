"""Автопроверка правил эксперта (#190, спека #187, ADR 0007): правило до согласования меряется на эталоне ОС.

Новый вид задачи на внешнем воркере (rule_check, контракт 13, полоса пересчётов). Постановка (ядро): потолок расходов
контура в месяц (при превышении проверка ждёт следующего месяца, администратор видит предупреждение), снимок строк
эталона ОС агента и шага (rule_check_rows, до N последних). Пакет при выдаче: два набора правил агента («с правилом» и
«без правила»: у проверки предложения: действующий набор с правилом и без; у ежемесячной проверки набора: весь набор и
голая инструкция) и вход каждой строки. Ответ «без правила» берётся из принятых решений, если версия инструкции,
справочников и набора правил, по которой решение принято, та же, что у прогона; иначе воркер считает оба прогона. У
«Дедупа», «Сути и перевода» и поиска принятых ответов с версией нет, поэтому у них всегда оба прогона.

Счёт (ядро): у строки ответ без правила и с ним против эталона: к эталону (было неверно, стало верно), от эталона
(наоборот), без изменений; совет «согласовать» по настройке (к эталону больше, чем от него); меньше порога строк: «мало
данных: N». У «Сути и перевода» числа нет: «было / стало» рядом с текстом эксперта на последних правках. У поиска:
повторный поиск по теме правила за N дней с подсказками и без против ТОП-списка темы (решение владельца 13.11).
Расход: своей стадией в учёте расхода ИИ.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from oiltech_digest import config, expert_rules, feedback_settings, rule_check_agents, rule_check_rows
from oiltech_digest.db import connection, expert_rules_store, feedback_store, repository, rule_check_store
from oiltech_digest.db.feedback_store import DONE, PENDING, RULE_PROPOSAL, SET_CHECK
from oiltech_digest.db.rule_check_store import FAILED, POSTPONED, QUEUED, RULE, SET
from oiltech_digest.processing.external_ai import InvalidJobPayload, LeaseLost
from oiltech_digest.processing.pipeline import make_client

logger = logging.getLogger(__name__)

KIND = "rule_check"
STAGE = KIND
AUTHOR = "Автопроверка"
_SAMPLES = 20


class CheckRefused(Exception):
    """Проверку не поставить: причина словами (409)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- Постановка (ядро) ---------------------------------------------------------------------------


def check_proposal(action_id: int, *, now: datetime | None = None) -> dict[str, Any]:
    """Поставить автопроверку предложения правила (сразу после предложения, по шагу планировщика или кнопкой)."""
    proposal = feedback_store.get_action(action_id)
    if proposal is None or proposal["kind"] != RULE_PROPOSAL:
        raise CheckRefused(f"предложения правила №{int(action_id)} нет")
    details = proposal["details"] or {}
    rule = details.get("rule") or {}
    return _enqueue(RULE, rule.get("agent"), int(proposal["id"]),
                    {"rule": rule, "replaces": details.get("replaces") or []}, now=now)


def recheck(action_id: int) -> dict[str, Any]:
    """Кнопка «проверить снова» у администратора: предложение ждёт решения, проверка не идёт."""
    from oiltech_digest.feedback_journal import JournalNotFound, JournalRefused

    proposal = feedback_store.get_action(action_id)
    if proposal is None or proposal["kind"] != RULE_PROPOSAL:
        raise JournalNotFound(f"предложения правила №{int(action_id)} нет")
    if proposal["status"] != PENDING:
        raise JournalRefused("проверить можно только предложение, которое ждёт решения")
    rule_check_store.settle_failed()
    last = rule_check_store.latest([action_id]).get(int(action_id))
    if last is not None and last["status"] == QUEUED:
        raise JournalRefused("автопроверка этого предложения уже идёт")
    return check_proposal(action_id)


def check_set(agent: str, *, month: str, now: datetime | None = None) -> dict[str, Any] | None:
    """Ежемесячная проверка всего действующего набора правил агента против голой инструкции; итог строкой журнала.
    Правил у агента нет: проверять нечего."""
    current = expert_rules_store.current_set(agent)
    if current is None or not current["rules"]:
        return None
    action_id = feedback_store.create_action(
        SET_CHECK, PENDING, agents=[agent], author=AUTHOR,
        details={"agent": agent, "version": int(current["version"]), "month": month})
    return _enqueue(SET, agent, action_id, {"version": int(current["version"])}, now=now)


def _enqueue(mode: str, agent: Any, action_id: int, spec: dict[str, Any], *, now: datetime | None) -> dict[str, Any]:
    from oiltech_digest import feedback_slice

    settings = feedback_settings.current()
    if agent not in expert_rules.AGENTS:
        check_id = rule_check_store.create(action_id=action_id, mode=mode, agent=str(agent), status=FAILED,
                                           note=f"агент «{agent}» не из списка")
        return {"check_id": check_id, "status": FAILED}
    spent, ceiling = feedback_slice.month_spend(now or _now()), float(settings["monthly_ceiling_usd"])
    if spent >= ceiling:
        note = (f"потолок расходов контура: ${spent:.2f} из ${ceiling:.2f} в этом месяце; автопроверка ждёт "
                "следующего месяца")
        check_id = rule_check_store.create(action_id=action_id, mode=mode, agent=agent, status=POSTPONED, note=note)
        return {"check_id": check_id, "status": POSTPONED, "note": note}
    if agent == "search":
        # Повторный поиск: тема правила; у проверки набора: темы его правил, не больше настройки (поиск платный).
        topics = ([(spec.get("rule") or {}).get("topic")] if mode == RULE
                  else _rule_topics(int(settings["search_check_topics"])))
        spec = {**spec, "topics": [topic for topic in topics if topic], "days": int(settings["search_check_days"])}
    rows = _rows(mode, agent, spec, settings)
    summary = agent == "summary" and mode == RULE
    details = {"spec": spec, "rows": rows, "little_data_rows": int(settings["little_data_rows"]),
               "advice_rule": settings["autocheck_advice"], "no_count": summary}
    check_id = rule_check_store.create(action_id=action_id, mode=mode, agent=agent, status=QUEUED, details=details)
    if not rows:
        # Сверять не с чем (у поиска: ТОП-список темы пуст): итог сразу, без оплаченных вызовов.
        rule_check_store.finish(check_id, status=DONE, advice=_little(0),
                                note="ТОП-список темы пуст" if agent == "search" else "строк эталона ОС нет",
                                details={"little_data": True})
        if mode == SET:
            _close_set_check(action_id)
        return {"check_id": check_id, "status": DONE, "rows": 0}
    from oiltech_digest import network_policy

    route = network_policy.route_ai_bulk()
    try:
        job = repository.create_background_job(KIND, {"check_id": check_id}, queue_name=route.queue_name,
                                               execution_region=route.execution_region, capability=route.capability)
    except Exception as exc:
        rule_check_store.finish(check_id, status=FAILED, note=f"задача автопроверки не поставлена: {exc}"[:500])
        raise
    rule_check_store.attach_job(check_id, int(job["id"]))
    return {"check_id": check_id, "status": QUEUED, "rows": len(rows), "job_id": int(job["id"])}


def _rows(mode: str, agent: str, spec: dict[str, Any], settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Снимок строк эталона ОС: у проверки предложения: шаг и категория правила; у проверки набора: все шаги."""
    rule = spec.get("rule") or {}
    steps = [rule["step"]] if mode == RULE else list(expert_rules.AGENTS[agent].steps)
    category = rule.get("category") if mode == RULE else None
    limit = int(settings["autocheck_rows"])
    if agent == "otbor":
        from oiltech_digest.otbor_pack_format import load_dictionaries

        reasons = {value["code"]: value["label"] for value in (load_dictionaries().get("reject_reason") or {}).get(
            "values") or []}
        return rule_check_rows.otbor_rows(steps, category, limit, reasons=reasons)
    if agent == "scores":
        return rule_check_rows.scores_rows(category, limit)
    if agent == "card":
        return rule_check_rows.card_rows(steps, category, limit)
    if agent == "dedup":
        return rule_check_rows.dedup_rows(limit)
    if agent == "summary":
        return rule_check_rows.summary_rows(steps, category, int(settings["summary_check_edits"]))
    return [{**row, "topic": topic} for topic in spec.get("topics") or []
            for row in rule_check_rows.search_rows(topic, int(spec["days"]))]


def _rule_topics(limit: int) -> list[str]:
    """Темы действующих правил поиска, начиная с последнего согласованного: проверке набора не больше limit тем."""
    found = expert_rules_store.current_set("search")
    topics = [rule["topic"] for rule in reversed((found or {}).get("rules") or []) if rule.get("topic")]
    return list(dict.fromkeys(topics))[:max(1, int(limit))]


# --- Пакет (ядро, при выдаче) --------------------------------------------------------------------


def blocks(mode: str, agent: str, spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Два набора правил агента: «с правилом» и «без правила». Проверка предложения: действующий набор без правил,
    которые предложение уточняет или сводит, и с новым правилом (версия: следующая) против действующего. Проверка
    набора: действующий против голой инструкции (версия 0)."""
    current = expert_rules_store.payload_block(agent)
    if mode == SET:
        return {"with": current, "without": expert_rules.block(agent, 0, [], priority=current["priority"])}
    rule = spec.get("rule") or {}
    replaced = {int(one) for one in spec.get("replaces") or []}
    rules = [one for one in current["rules"] if int(one.get("id") or 0) not in replaced]
    candidate = {"id": 0, **{key: rule.get(key) for key in ("step", "category", "topic", "text")}}
    return {"with": expert_rules.block(agent, current["version"] + 1, [*rules, candidate], priority=current["priority"]),
            "without": current}


def _identity(mode: str, side: str, sets: dict[str, dict[str, Any]]) -> int | None:
    """Какой версии набора соответствует прогон: принятый ответ этой версии годится вместо прогона. Набор «с
    предлагаемым правилом» ни одной принятой версии не соответствует."""
    if mode == RULE and side == "with":
        return None
    return expert_rules.version(sets[side])


def build_payload(job_payload: dict[str, Any]) -> dict[str, Any]:
    """Самодостаточный пакет для воркера без базы: два набора правил, общее агента и вход строк; у строки: какие
    прогоны нужны (принятого ответа нужной версии нет)."""
    check = rule_check_store.get(int(job_payload.get("check_id") or 0))
    if check is None or check["status"] != QUEUED:
        raise InvalidJobPayload("автопроверки нет или она уже не ждёт прогона")
    details = check["details"] or {}
    mode, agent, spec = check["mode"], check["agent"], details.get("spec") or {}
    sets = blocks(mode, agent, spec)
    try:
        common = rule_check_agents.shared(agent, spec)
    except Exception as exc:
        raise InvalidJobPayload(str(exc)) from exc
    rows = details.get("rows") or []
    if agent == "search":
        return {"kind": KIND, "check_id": int(check["id"]), "mode": mode, "agent": agent, "blocks": sets,
                "shared": {**common, "topics": sorted({row["topic"] for row in rows})}, "rows": []}
    found = rule_check_agents.inputs(agent, rows)
    items = []
    for row in rows:
        if row["key"] not in found:
            continue
        need = [side for side in ("with", "without")
                if row.get("accepted") is None or row.get("accepted_version") != _identity(mode, side, sets)]
        if need:
            items.append({"key": row["key"], "need": need, "input": found[row["key"]]})
    return {"kind": KIND, "check_id": int(check["id"]), "mode": mode, "agent": agent, "blocks": sets,
            "shared": common, "rows": items}


# --- Прогоны (воркер, без базы) ------------------------------------------------------------------


def process_payload(payload: dict[str, Any], heartbeat: Callable[..., None] | None = None) -> dict[str, Any]:
    """Сторона воркера: прогоны агента по строкам с нужными наборами правил; ответы: как есть."""
    agent, sets, common = str(payload.get("agent") or ""), payload.get("blocks") or {}, payload.get("shared") or {}
    answers: list[dict[str, Any]] = []
    result: dict[str, Any] = {"rule_check": True, "kind": KIND, "check_id": payload.get("check_id"), "answers": answers,
                              "versions": {side: expert_rules.version(block) for side, block in sets.items()}}
    if agent == "search":
        beat = (lambda: heartbeat(result)) if heartbeat else None
        result["search"] = {topic: {side: _safe(lambda side=side, topic=topic: rule_check_agents.run_search(
            {**common, "topic": topic}, sets.get(side), beat)) for side in ("with", "without")}
            for topic in common.get("topics") or []}
        return result
    client = make_client()
    for row in payload.get("rows") or []:
        if heartbeat:
            heartbeat(result)
        answer: dict[str, Any] = {"key": row.get("key")}
        for side in row.get("need") or []:
            answer[side] = _safe(lambda side=side: rule_check_agents.run(agent, client, common, row.get("input") or {},
                                                                         sets.get(side)))
        answers.append(answer)
    return result


def _safe(call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return call()
    except LeaseLost:
        # Аренду отозвали или воркер останавливается (признак продвижения внутри поиска): дальше платить нельзя.
        raise
    except Exception as exc:  # noqa: BLE001 - одна строка не роняет проверку
        return {"error": str(exc)[:300]}


# --- Счёт (ядро) ---------------------------------------------------------------------------------


def apply_result(result: dict[str, Any], *, job_id: int | None, job_payload: dict[str, Any]) -> dict[str, Any]:
    """Сторона ядра: ответы прогонов против эталона: к эталону, от эталона, без изменений; совет; расход своей стадией.
    Итог чужой проверки не пишется."""
    check = rule_check_store.get(int(job_payload.get("check_id") or 0))
    if check is None or str(result.get("check_id")) != str(check["id"]):
        return {"error": "итог не этой проверки"}
    details = check["details"] or {}
    rows = details.get("rows") or []
    versions = result.get("versions") if isinstance(result.get("versions"), dict) else {}
    _record_usage(job_id, check, result)
    if check["agent"] == "search":
        summary = _search_summary(rows, result.get("search") or {}, int(details.get("little_data_rows") or 30))
    else:
        summary = _count(check, rows, result.get("answers") or [], versions)
    rule_check_store.finish(int(check["id"]), status=DONE, row_count=summary["rows"], toward=summary["toward"],
                            away=summary["away"], unchanged=summary["unchanged"], advice=summary["advice"],
                            note=summary.get("note"), details={key: value for key, value in summary.items()
                                                               if key not in ("rows", "toward", "away", "unchanged",
                                                                              "advice", "note")})
    if check["mode"] == SET and check["action_id"]:
        _close_set_check(int(check["action_id"]))
    return {"check_id": int(check["id"]), **{key: summary[key] for key in ("rows", "toward", "away", "unchanged")}}


def _close_set_check(action_id: int) -> None:
    """Строка журнала ежемесячной проверки набора: «сделано», итог в её прогоне."""
    with connection.get_connection() as conn:
        feedback_store.set_status(int(action_id), DONE, conn=conn)
        conn.commit()


def _count(check: dict[str, Any], rows: list[dict[str, Any]], answers: list[dict[str, Any]],
           versions: dict[str, Any]) -> dict[str, Any]:
    details = check["details"] or {}
    mode, agent = check["mode"], check["agent"]
    by_key = {str(one.get("key")): one for one in answers if isinstance(one, dict)}
    judge = rule_check_agents.Judge(agent, rows)
    identity = {"with": None if mode == RULE else _int(versions.get("with")), "without": _int(versions.get("without"))}
    toward = away = unchanged = errors = 0
    samples, baseline = [], {"accepted": 0, "computed": 0}
    for row in rows:
        answer = by_key.get(row["key"]) or {}
        values = {}
        for side in ("with", "without"):
            if side in answer:
                values[side] = judge.value(row, answer[side])
            elif row.get("accepted") is not None and identity[side] is not None \
                    and row.get("accepted_version") == identity[side]:
                values[side] = judge.accepted(row)
            else:
                values[side] = None
        if "without" not in answer and values["without"] is not None:
            baseline["accepted"] += 1
        elif "without" in answer:
            baseline["computed"] += 1
        before, after = values["without"], values["with"]
        if details.get("no_count"):
            if before is not None or after is not None:
                samples.append({"signal_id": row["signal_id"], "label": row.get("label"), "field": row.get("field"),
                                "expert": row["expected"], "before": before, "after": after})
            else:
                errors += 1
            continue
        if before is None or after is None:
            errors += 1
            continue
        expected = judge.expected(row)
        good_before, good_after = before == expected, after == expected
        direction = "toward" if good_after and not good_before else "away" if good_before and not good_after else None
        if direction == "toward":
            toward += 1
        elif direction == "away":
            away += 1
        else:
            unchanged += 1
        if (direction or before != after) and len(samples) < _SAMPLES:
            samples.append({"signal_id": row["signal_id"], "label": row.get("label"), "field": row.get("field"),
                            "kind": row.get("kind"), "expected": expected, "before": before, "after": after,
                            "direction": direction})
    counted = toward + away + unchanged
    if details.get("no_count"):
        return {"rows": len(samples), "toward": 0, "away": 0, "unchanged": 0, "no_count": True, "samples": samples,
                "advice": "числа нет: сравните «было / стало» с текстом эксперта", "errors": errors,
                "baseline": baseline, "little_data": False}
    little = counted < int(details.get("little_data_rows") or 30)
    return {"rows": counted, "toward": toward, "away": away, "unchanged": unchanged, "samples": samples,
            "errors": errors, "baseline": baseline, "little_data": little,
            "advice": _advice(mode, toward, away, counted, little, str(details.get("advice_rule") or "more_toward"))}


def _advice(mode: str, toward: int, away: int, counted: int, little: bool, rule: str) -> str:
    if little:
        return _little(counted)
    if mode == SET:
        return ("набор помогает: решений к эталону больше" if toward > away else
                f"набор перестал помогать: от эталона {away}, к эталону {toward}")
    good = toward > away if rule == "more_toward" else (away == 0 and toward > 0)
    if good:
        return "согласовать"
    if toward == away == 0:
        return "не согласовывать: правило не меняет решений"
    return "не согласовывать: к эталону не больше, чем от него" if rule == "more_toward" else \
        "не согласовывать: есть решения, сдвинутые от эталона"


def _little(rows: int) -> str:
    return f"мало данных: {int(rows)}"


def _search_summary(rows: list[dict[str, Any]], search: dict[str, Any], little_rows: int) -> dict[str, Any]:
    """Сверка повторного поиска с ТОП-списком темы: элемент ТОП найден, если ссылка совпала (ключ адреса) или
    заголовок одного события (основы слов)."""
    by_topic = {topic: {side: (runs.get(side) or {}) for side in ("with", "without")}
                for topic, runs in search.items() if isinstance(runs, dict)}
    errors = [f"{topic}, {side}: {one.get('error')}" for topic, runs in by_topic.items() for side, one in runs.items()
              if one.get("error") or one.get("status") == "error"]
    toward = away = unchanged = 0
    samples = []
    for row in rows:
        found = by_topic.get(row.get("topic")) or {"with": {}, "without": {}}
        hit = {side: _found(row, one.get("items") or []) for side, one in found.items()}
        direction = ("toward" if hit["with"] and not hit["without"] else "away" if hit["without"] and not hit["with"]
                     else None)
        toward += direction == "toward"
        away += direction == "away"
        unchanged += direction is None
        samples.append({"signal_id": row["signal_id"], "label": row.get("label"), "with": hit["with"],
                        "without": hit["without"], "direction": direction})
    counted = len(rows)
    little = counted < little_rows
    return {"rows": counted, "toward": toward, "away": away, "unchanged": unchanged, "samples": samples[:_SAMPLES],
            "little_data": little, "advice": _little(counted) if little else (
                "согласовать" if toward > away else "не согласовывать: к эталону не больше, чем от него"),
            "search": {f"{topic}: {side}": {"found": len(one.get("items") or []), "error": one.get("error")}
                       for topic, runs in by_topic.items() for side, one in runs.items()},
            "note": "; ".join(errors) or None}


def _found(row: dict[str, Any], items: list[dict[str, Any]]) -> bool:
    from oiltech_digest import word_forms
    from oiltech_digest.ingestion.normalize import url_key

    keys = {url_key(url) for url in row.get("urls") or [] if url}
    stems = word_forms.title_stems(str(row.get("title") or row.get("label") or ""))
    for item in items:
        if item.get("url") and url_key(str(item["url"])) in keys:
            return True
        other = word_forms.title_stems(str(item.get("title") or ""))
        if stems and other and len(stems & other) / max(1, min(len(stems), len(other))) >= 0.6:
            return True
    return False


def _record_usage(job_id: int | None, check: dict[str, Any], result: dict[str, Any]) -> None:
    """Расход прогонов: по сигналу строки (вызовы с правилом и без вместе); у поиска: строка на задачу с платой за
    вызовы поиска."""
    rows_by_key = {row["key"]: row for row in (check["details"] or {}).get("rows") or []}
    totals: dict[int | None, dict[str, Any]] = {}

    def add(signal_id: int | None, part: dict[str, Any], web_calls: int = 0) -> None:
        if not isinstance(part, dict):
            return
        try:
            tokens_in, tokens_out = max(0, int(part.get("input_tokens") or 0)), max(0, int(part.get("output_tokens") or 0))
        except (TypeError, ValueError):
            return
        if not tokens_in + tokens_out:
            return
        model = str(part.get("model") or "")[:100] or None
        price_in, price_out = config.price_for_model(model)
        row = totals.setdefault(signal_id, {"signal_id": signal_id, "model": model, "input_tokens": 0,
                                            "output_tokens": 0, "cost_usd": 0.0})
        row["input_tokens"] += tokens_in
        row["output_tokens"] += tokens_out
        row["cost_usd"] = round(row["cost_usd"] + (tokens_in * price_in + tokens_out * price_out) / 1_000_000
                                + web_calls * config.SIGNAL_WEB_SEARCH_USD_PER_CALL, 6)

    for answer in result.get("answers") or []:
        row = rows_by_key.get(str((answer or {}).get("key")))
        if row is None:
            continue
        for side in ("with", "without"):
            add(int(row["signal_id"]), answer.get(side) or {})
    for runs in (result.get("search") or {}).values():
        for one in (runs or {}).values() if isinstance(runs, dict) else []:
            add(None, one or {}, web_calls=_int((one or {}).get("web_search_calls")) or 0)
    rule_check_store.record_usage(job_id, list(totals.values()), stage=STAGE,
                                  recipe={"check_id": int(check["id"]), "mode": check["mode"], "agent": check["agent"]})


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --- Шаг планировщика (ядро) ---------------------------------------------------------------------


def scheduler_step(now: datetime, *, monthly: bool) -> dict[str, Any]:
    """Каждый цикл: проверки, чья задача пропала, не выполнены; предложения правил без проверки (засеянное, отложенное
    потолком в прошлом месяце) ставятся; monthly (первый срез месяца): проверка всего набора правил каждого агента."""
    rule_check_store.settle_failed()
    month = now.astimezone(_msk()).strftime("%Y-%m")
    done: dict[str, Any] = {"proposals": [], "sets": []}
    for action_id in rule_check_store.proposals_waiting_check(kind=RULE_PROPOSAL,
                                                              retry_postponed=_new_month_for_postponed(now)):
        try:
            done["proposals"].append(check_proposal(action_id, now=now))
        except Exception:  # noqa: BLE001 - одна проверка не держит остальные
            logger.exception("rule_check_enqueue_failed action=%s", action_id)
    settings = feedback_settings.current()
    if monthly and settings["set_check_monthly"] and not feedback_store.task_in_month(SET_CHECK, month):
        for agent in expert_rules.AGENTS:
            try:
                started = check_set(agent, month=month, now=now)
            except Exception:  # noqa: BLE001
                logger.exception("rule_set_check_failed agent=%s", agent)
                continue
            if started is not None:
                done["sets"].append({"agent": agent, **started})
    return done


def _new_month_for_postponed(now: datetime) -> bool:
    """Отложенные потолком проверки ставятся снова, когда расход месяца ниже потолка (новый месяц или поднятый
    потолок)."""
    from oiltech_digest import feedback_slice

    return feedback_slice.month_spend(now) < float(feedback_settings.current()["monthly_ceiling_usd"])


def _msk():
    from oiltech_digest.feed_window import MSK

    return MSK

