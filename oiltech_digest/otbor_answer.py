"""Ответ агента «Отбор» (#130, #131, спека #128): схема ответа, проверка кодом и итог строки воркера.

Общий для воркера (повтор при конфликте шагов), ядра (запись решения: ответ воркера — недоверенный ввод, ядро
проверяет его само) и экзамена (#133: мерится то, что дойдёт до сигнала). Ответ не по схеме или со значением не из
справочника — сбой без повтора; шаги не сошлись по таблице конфликтов — один повтор, затем пометка «спорное».
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from typing import Any

from oiltech_digest import feed_window
from oiltech_digest.db import otbor_store

# Шаг «Отбора» в «Ходе решения» сигнала.
STAGE = "«Отбор»"
# Код «причины нет» у решения «брать»: в справочнике причин его нет, в схеме ответа — есть.
NO_REASON = "none"
ACCEPT, REJECT = feed_window.OTBOR_ACCEPT, feed_window.OTBOR_REJECT
DIRECT, TRANSFERABLE, NO_LINK = "direct", "transferable", "none"
OILFIELD_SERVICES, OIL_GAS, OTHER_INDUSTRY = "oilfield_services", "oil_gas", "other_industry"
# Потолки длины ответа воркера (недоверенный ввод): обоснование — до 200 знаков (спека), применение — как фраза
# судьи радара.
_JUSTIFICATION_CHARS, _APPLICATION_CHARS = 200, 400

# Таблица конфликтов шагов утверждена владельцем 23.10 (решение 29 журнала гриля; раздел 5 разбора разметки
# эксперта). Пар «отрасль + связь», которых не бывает, две: у нефтесервиса связь прямая, другая отрасль только
# переносится. Переносимая бывает и у нефтегаза (смежные операции: переработка, транспорт, хранение) при любой
# категории.
_NO_PAIRS = {(OILFIELD_SERVICES, TRANSFERABLE): "переносимая связь у нефтесервиса: его связь прямая",
             (OTHER_INDUSTRY, DIRECT): "прямая связь у другой отрасли"}
LINKED = frozenset({DIRECT, TRANSFERABLE})
# При каких отрасли и связи допустимы «брать» и каждая причина отказа; None — при любой из допустимых пар. Причина
# справочника без строки таблицы — конфликт шагов: таблицу правят в том же изменении, что и коды справочника (#133).
# Тексты причин — в справочнике, здесь только коды.
DECISION_STEPS = {
    ACCEPT: (None, LINKED),
    "oil_gas_no_link": ({OIL_GAS}, {NO_LINK}),
    "other_industry_no_transfer": ({OTHER_INDUSTRY}, {NO_LINK}),
    "incident_war_politics": (None, {NO_LINK}),
    "corporate_promo": (None, None),
    "old_event": (None, LINKED),
    "mixed_events": (None, LINKED),
}
# «Разные события в одной находке» — только у находки с двумя и более ссылками (#131): у статьи ленты, материала и
# находки с одной ссылкой публикация одна, и эта причина у них — конфликт шагов.
MIXED_EVENTS = "mixed_events"


def codes(dictionaries: dict[str, Any], name: str) -> list[str]:
    return [value["code"] for value in (dictionaries.get(name) or {}).get("values") or []]


def answer_schema(dictionaries: dict[str, Any]) -> dict[str, Any]:
    """Схема ответа модели (строгий JSON): порядок полей — порядок рассуждения «Отбора». По ней же ядро проверяет
    ответ (answer_problem)."""
    def enum(name: str, *extra: str) -> dict[str, Any]:
        return {"type": "string", "enum": [*codes(dictionaries, name), *extra]}

    properties: dict[str, Any] = {
        "industry": enum("industry"),
        "oilfield_relevance": enum("oilfield_relevance"),
        "oilfield_application": {"type": "string"},
        "decision": enum("decision"),
        "reject_reason": enum("reject_reason", NO_REASON),
        "justification": {"type": "string"},
        "category": enum("category"),
    }
    return {"name": "otbor", "schema": {
        "type": "object", "additionalProperties": False, "required": list(properties), "properties": properties,
    }}


def answer_problem(answer: Any, dictionaries: dict[str, Any]) -> str | None:
    """Чем ответ не по правилам: не по схеме или значение не из справочника; None — по правилам."""
    if not isinstance(answer, dict):
        return "не объект"
    for name, spec in answer_schema(dictionaries)["schema"]["properties"].items():
        if name == "category" and answer.get("decision") == REJECT:
            # Категория выбирает витрину, у отклонённого её нет: проверка её не требует (решение владельца 23.10).
            continue
        value = answer.get(name)
        if not isinstance(value, str):
            return f"нет строки {name}"
        if "enum" in spec and value not in spec["enum"]:
            return f"значение не из справочника: {name} = «{value[:60]}»"
    return None


def code_label(dictionaries: dict[str, Any], name: str, code: str | None) -> str:
    """Подпись кода по справочнику; кода в справочнике нет — сам код, нет и кода — пусто."""
    values = (dictionaries.get(name) or {}).get("values") or []
    return next((value["label"] for value in values if value["code"] == code), code or "")


def conflicts(answer: dict[str, Any], dictionaries: dict[str, Any], *, links: int = 0) -> list[str]:
    """Где шаги ответа не сошлись друг с другом (таблица конфликтов, DECISION_STEPS). links — сколько ссылок у
    находки во входе (0 — публикация одна)."""
    label = partial(code_label, dictionaries)
    industry, relevance = answer["industry"], answer["oilfield_relevance"]
    decision, reason = answer["decision"], answer["reject_reason"]
    found = []
    if (industry, relevance) in _NO_PAIRS:
        found.append(_NO_PAIRS[(industry, relevance)])
    if decision == ACCEPT and reason != NO_REASON:
        found.append(f"у «брать» есть причина отказа «{label('reject_reason', reason)}»")
    if decision == REJECT and reason == NO_REASON:
        found.append("у «не брать» нет причины отказа")
    step = ACCEPT if decision == ACCEPT else reason
    industries, relevances = DECISION_STEPS.get(step, (None, None))
    if step not in DECISION_STEPS:
        if step != NO_REASON:
            found.append(f"причины «{label('reject_reason', reason)}» нет в таблице конфликтов шагов")
    elif step == ACCEPT and relevance not in relevances:
        found.append(f"«брать» при связи «{label('oilfield_relevance', relevance)}»")
    elif (industries is not None and industry not in industries) or (relevances is not None
                                                                      and relevance not in relevances):
        found.append(f"причина «{label('reject_reason', reason)}» при отрасли «{label('industry', industry)}» "
                     f"и связи «{label('oilfield_relevance', relevance)}»")
    if relevance == TRANSFERABLE and not answer["oilfield_application"].strip():
        found.append("переносимая связь без применения в нефтесервисе")
    if reason == MIXED_EVENTS and links < 2:
        found.append(f"причина «{label('reject_reason', reason)}» у публикации без нескольких ссылок")
    return found


def return_choices(dictionaries: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """Что человек может вписать, возвращая отклонённое (#132): коды и подписи справочника «Отбора» — отрасль, связь
    (вернуть значит взять: только прямая и переносимая) и категория. Экран берёт их отсюда, а не держит у себя."""
    def options(name: str, allowed: frozenset[str] | None = None) -> list[dict[str, str]]:
        return [{"code": value["code"], "label": value["label"]} for value in (dictionaries.get(name) or {}).get("values")
                or [] if allowed is None or value["code"] in allowed]

    return {"industry": options("industry"), "oilfield_relevance": options("oilfield_relevance", LINKED),
            "category": options("category")}


def return_problems(values: dict[str, Any], dictionaries: dict[str, Any]) -> list[str]:
    """Чем поля «Отбора», которые вписал человек, возвращая отклонённое (#132, спека #128), не по правилам: значение не
    из справочника или шаги не сходятся по той же таблице конфликтов, что у «Отбора» (вернуть — значит взять: связь
    прямая или переносимая, у переносимой — применение). Пусто — по правилам."""
    problems = [f"{name}: «{str(values.get(name))[:60]}» нет в справочнике" for name in ("industry", "oilfield_relevance",
                                                                                      "category")
                if values.get(name) not in codes(dictionaries, name)]
    if problems:
        return problems
    return conflicts({"industry": values["industry"], "oilfield_relevance": values["oilfield_relevance"],
                      "oilfield_application": values.get("oilfield_application") or "", "decision": ACCEPT,
                      "reject_reason": NO_REASON, "category": values["category"]}, dictionaries)


@dataclass
class Outcome:
    """Что из ответов воркера дойдёт до сигнала: решение (values) или сбой (failure и почему)."""

    values: dict[str, Any] | None = None
    disputed: str | None = None
    failure: str | None = None
    failure_reason: str | None = None
    log: list[dict[str, str]] = field(default_factory=list)


def technical_failure(reason: str) -> Outcome:
    """Технический сбой: «Отбор» не ответил, шаг повторит до OTBOR_ATTEMPTS задач."""
    return Outcome(failure=otbor_store.FAILURE_TECHNICAL, failure_reason=reason,
                   log=[step(f"Сбой, «Отбор» не ответил: {reason}")])


def outcome(decision: dict[str, Any], dictionaries: dict[str, Any], *, links: int = 0) -> Outcome:
    """Строка итога воркера → решение или сбой. Через неё пишет ядро (otbor.apply_result) и будет мерить экзамен
    (#133): мерится то, что дойдёт до сигнала. Конфликт шагов: в силе ответ повтора, если он по правилам; снова
    конфликт или повтора нет — решение остаётся с пометкой «спорное». links — ссылок у находки во входе."""
    check = partial(conflicts, links=links)
    calls = [call for call in decision.get("calls") or [] if isinstance(call, dict)]
    if not calls or calls[0].get("error"):
        return technical_failure(f"модель: {str(calls[0].get('error'))[:300]}" if calls else "модель не ответила")
    answer = calls[0].get("answer")
    problem = answer_problem(answer, dictionaries)
    if problem:
        reason = f"ответ не по правилам: {problem}"
        return Outcome(failure=otbor_store.FAILURE_INVALID, failure_reason=reason, log=[step(reason)])
    log: list[dict[str, str]] = []
    found = check(answer, dictionaries)
    disputed = None
    if found:
        log.append(step(f"Шаги ответа не сошлись ({'; '.join(found)}): повторный вызов с указанием конфликта"))
        retry = calls[1] if len(calls) > 1 else {}
        if "answer" in retry and answer_problem(retry["answer"], dictionaries) is None:
            answer = retry["answer"]
            found = check(answer, dictionaries)
        if found:
            disputed = "шаги ответа не сошлись и после повтора: " + "; ".join(found)
    values = _values(answer, dictionaries)
    if values["reject_reason"] == MIXED_EVENTS and links < 2:
        # Причина невозможна (публикация одна) и после повтора не пишется: отказ остаётся спорным, но без неё.
        values["reject_reason"] = None
    log.append(step(_decision_text(values, dictionaries)))
    if disputed:
        log.append(step(f"Спорное: {disputed}"))
    return Outcome(values=values, disputed=disputed, log=log)


def _values(answer: dict[str, Any], dictionaries: dict[str, Any]) -> dict[str, Any]:
    """Проверенный ответ → значения сигнала. Применение — только у переносимой связи; причина — только у отказа;
    категория у отказа — если модель её написала из справочника (её никто не читает)."""
    relevance = answer["oilfield_relevance"]
    reason = answer["reject_reason"]
    category = answer.get("category")
    return {
        "industry": answer["industry"],
        "oilfield_relevance": relevance,
        "oilfield_application": _phrase(answer["oilfield_application"], _APPLICATION_CHARS)
        if relevance == TRANSFERABLE else None,
        "otbor_decision": answer["decision"],
        "reject_reason": reason if answer["decision"] == REJECT and reason != NO_REASON else None,
        "signal_category": category if category in codes(dictionaries, "category") else None,
        "verdict_reason": _phrase(answer["justification"], _JUSTIFICATION_CHARS),
    }


def _decision_text(values: dict[str, Any], dictionaries: dict[str, Any]) -> str:
    label = partial(code_label, dictionaries)

    relevance = label("oilfield_relevance", values["oilfield_relevance"])
    if values["oilfield_application"]:
        relevance += f", применение: {values['oilfield_application']}"
    decision = label("decision", values["otbor_decision"])
    if values["reject_reason"]:
        decision += f", причина «{label('reject_reason', values['reject_reason'])}»"
    return (f"Отрасль: {label('industry', values['industry'])}. Связь с нефтесервисом: {relevance}. "
            f"Решение: {decision}. Категория: {label('category', values['signal_category'])}."
            + (f" Почему: {values['verdict_reason']}" if values["verdict_reason"] else ""))


def step(text: str) -> dict[str, str]:
    return {"stage": STAGE, "text": text}


def _phrase(value: str, limit: int) -> str | None:
    return " ".join(value.split())[:limit] or None
