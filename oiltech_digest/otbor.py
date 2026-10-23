"""Агент «Отбор» (#130, спека #128, ADR 0005): единственный решает, брать ли публикацию.

Один вызов модели на публикацию, без инструментов и без выхода в сеть, — не автономный агент: путь известен,
обязательное проверяет код. Ядро собирает пакет при выдаче задачи (публикация, справочники «Отбора», приёмщик ОС),
воркер без базы зовёт модель со справочниками в каждом вызове, а если шаги ответа не сошлись (conflicts), — ещё
один раз, с указанием конфликта. Ответы воркер возвращает как есть, ядро проверяет их само (ответ воркера —
недоверенный ввод) и записывает решение в сигнал публикации. Происхождение публикации модель не получает: его
использует только код. Модель — явная настройка агента (OTBOR_MODEL), без отката на модель ленты: едет в задаче и
попадает в рецепт решения вместе с версией инструкции и справочников.

Сбой — по причине (решение владельца 23.10): технический (модель не ответила) шаг планировщика повторяет до
OTBOR_ATTEMPTS задач; модель ответила, но не по схеме или не из справочника, — без повтора: такое надо понять, а
не оплачивать ещё раз. Оба видны админу в разделах отклонённого.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from oiltech_digest import config
from oiltech_digest.db import otbor_store, repository
from oiltech_digest.otbor_pack_format import load_dictionaries
from oiltech_digest.processing.external_ai import StopRequested
from oiltech_digest.processing.openai_client import output_budget
from oiltech_digest.processing.pipeline import make_client
from oiltech_digest.signal_card_pack import publication_day, publication_text

KIND = otbor_store.KIND
# Версия инструкции (и таблицы конфликтов) — в рецепте решения: другая инструкция — другие решения.
PROMPT_VERSION = "2026-10-23.2"
_OUTPUT_TOKENS = 600
# Шаг «Отбора» в «Ходе решения» сигнала.
STAGE = "«Отбор»"
# Справочники, из которых модель выбирает коды, — в порядке её рассуждения (поля ответа).
CHOICES = ("industry", "oilfield_relevance", "decision", "reject_reason", "category")
# Код «причины нет» у решения «брать»: в справочнике причин его нет, в схеме ответа — есть.
NO_REASON = "none"
ACCEPT, REJECT = "accept", "reject"
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
# не из таблицы (новая версия справочника) — при любой паре. Тексты причин — в справочнике, здесь только коды.
DECISION_STEPS = {
    ACCEPT: (None, LINKED),
    "oil_gas_no_link": ({OIL_GAS}, {NO_LINK}),
    "other_industry_no_transfer": ({OTHER_INDUSTRY}, {NO_LINK}),
    "incident_war_politics": (None, {NO_LINK}),
    "corporate_promo": (None, None),
    "old_event": (None, LINKED),
    "mixed_events": (None, LINKED),
}
# «Разные события в одной находке» — только у публикации с двумя и более ссылками (находка радара, #131).
MIXED_EVENTS = "mixed_events"


class OtborSetupError(ValueError):
    """Агента нельзя поставить в работу: выключен флаг или не задана модель."""


# --- Постановка (ядро) ---------------------------------------------------------------------------


def enqueue_new_articles() -> dict[str, Any]:
    """Шаг планировщика «Отбор»: новые статьи, которые решает он, — по полосе ежедневной обработки. За флагом
    OTBOR_ENABLED."""
    if not config.OTBOR_ENABLED:
        return {"articles": 0, "jobs": [], "reason": "disabled"}
    # Без модели шаг отказывает в каждом цикле, даже когда новых статей нет: настройка видна сразу.
    configured_model()
    # Попытки кончились, а итога нет (задачи падали целиком): статья — в раздел «„Отбор“ не ответил».
    unanswered = otbor_store.unanswered_articles(days=config.OTBOR_NEW_DAYS, attempts=config.OTBOR_ATTEMPTS,
                                                 limit=config.OTBOR_STEP_LIMIT)
    for article_id, reason in unanswered:
        otbor_store.write_failure(article_id, otbor_store.FAILURE_TECHNICAL, reason,
                                  log=[_step(f"Сбой, «Отбор» не ответил: {reason}")])
    ids = otbor_store.new_article_ids(days=config.OTBOR_NEW_DAYS, limit=config.OTBOR_STEP_LIMIT,
                                      attempts=config.OTBOR_ATTEMPTS)
    return {"articles": len(ids), "jobs": enqueue(ids, trigger="daily"), "unanswered": len(unanswered)}


def enqueue(article_ids: list[int], *, trigger: str, bulk: bool = False) -> list[dict[str, Any]]:
    """Задачи агента пачками по OTBOR_BATCH. bulk — названные статьи (повтор после разбора сбоя): по полосе
    пересчётов, чтобы не стоять перед потоком дня. Только при включённом флаге: до экзамена (#133) агент на проде
    не работает."""
    from oiltech_digest import network_policy

    if not config.OTBOR_ENABLED:
        raise OtborSetupError("OTBOR_ENABLED выключен: «Отбор» выходит в работу после экзамена (#133)")
    model = configured_model()
    route = network_policy.route_ai_bulk() if bulk else network_policy.route_ai_processing()
    batch = config.OTBOR_BATCH
    return [
        repository.create_background_job(
            KIND,
            {"article_ids": article_ids[start:start + batch], "model": model,
             "reasoning": config.OTBOR_REASONING, "trigger": trigger},
            queue_name=route.queue_name, execution_region=route.execution_region, capability=route.capability,
        )
        for start in range(0, len(article_ids), batch)
    ]


def configured_model() -> str:
    if not config.OTBOR_MODEL:
        raise OtborSetupError("OTBOR_MODEL не задан: модель ленты «Отбор» не берёт")
    return config.OTBOR_MODEL


# --- Пакет (ядро, при выдаче) --------------------------------------------------------------------


def build_payload(job_payload: dict[str, Any]) -> dict[str, Any]:
    """Самодостаточный пакет для воркера без базы: публикации, справочники, приёмщик ОС."""
    return {
        "kind": KIND, "model": job_payload.get("model"), "reasoning": job_payload.get("reasoning"),
        "dictionaries": load_dictionaries(),
        "items": [_item(row) for row in otbor_store.article_inputs(job_payload.get("article_ids") or [])],
    }


def _item(row: dict[str, Any]) -> dict[str, Any]:
    """Публикация так, как её видит модель: поля как у входа пакета эксперту (otbor_pack), тот же текст
    (publication_text) и даты днём по UTC. Приёмщик ОС (решение владельца 21.10) — фиксированная форма: похожие
    сигналы, оценённые людьми (заголовок, решение, код причины, категория, тема), и текст оценок, если правила
    сервиса ОС его пропустят. «Отбор» сам ОС не собирает; до сервиса ОС приёмщик пуст."""
    return {
        "ref": f"article:{int(row['article_id'])}",
        "title": publication_text(row.get("title"))[:500],
        "text": publication_text(row.get("text")),
        "publisher": row.get("publisher") or "",
        "language": row.get("language") or "",
        "published": publication_day(row.get("published")),
        "received": publication_day(row.get("received")),
        "feedback": {"similar_signals": [], "text": ""},
    }


# --- Вызов модели (воркер, без базы) -------------------------------------------------------------


def process_payload(payload: dict[str, Any], heartbeat: Callable[..., None] | None = None) -> dict[str, Any]:
    """Сторона воркера: вызов модели на публикацию, справочники — в каждом; шаги не сошлись — второй вызов с
    указанием конфликта. Ответы модели — как есть."""
    model = str(payload.get("model") or "").strip()
    if not model:
        # Клиент без модели взял бы OPENAI_MODEL ленты — тихий откат, которого быть не должно.
        raise ValueError("otbor: в задаче нет модели (OTBOR_MODEL на ядре) — модель ленты агент не берёт")
    dictionaries = payload.get("dictionaries") or {}
    instructions = _instructions(dictionaries)
    schema = _answer_schema(dictionaries)
    client = make_client()
    decisions: list[dict[str, Any]] = []
    result = {"otbor": True, "kind": KIND, "prompt_version": PROMPT_VERSION,
              "dictionaries_version": dictionaries.get("version"), "decisions": decisions}
    for item in payload.get("items") or []:
        if heartbeat:
            try:
                heartbeat(result)
            except StopRequested:
                # Остановка воркера: сделанное уходит ядру частичным итогом, остальное — в очередь.
                result["partial"] = True
                break
        decisions.append(_decide(client, item, instructions, schema, dictionaries, model, payload.get("reasoning")))
    return result


def _decide(client: Any, item: dict[str, Any], instructions: str, schema: dict[str, Any],
            dictionaries: dict[str, Any], model: str, reasoning: Any) -> dict[str, Any]:
    """Ответы модели о публикации: каждый вызов — ответ или ошибка; токены — за все вызовы."""
    decision: dict[str, Any] = {"ref": item.get("ref"), "calls": [], "model": None,
                                "input_tokens": 0, "output_tokens": 0}
    found: list[str] = []
    for _ in range(2):
        try:
            response = client.complete_json(
                instructions, model_input(item, conflict=found), schema,
                max_output_tokens=output_budget(_OUTPUT_TOKENS, reasoning), model=model, reasoning_effort=reasoning,
            )
        except Exception as exc:  # noqa: BLE001 - одна публикация не роняет пачку
            decision["calls"].append({"error": str(exc)[:300]})
            break
        decision["calls"].append({"answer": response.data})
        decision["model"] = response.model
        decision["input_tokens"] += int(response.input_tokens or 0)
        decision["output_tokens"] += int(response.output_tokens or 0)
        # Ответ не по правилам не повторяется (решение владельца 23.10); повтор — только при конфликте шагов.
        found = ([] if answer_problem(response.data, dictionaries)
                 else conflicts(response.data, dictionaries, links=len(item.get("links") or [])))
        if not found:
            break
    return decision


_TASK = """Ты решаешь, брать ли публикацию на платформу мониторинга рынка нефтесервиса, по тексту ОДНОЙ
публикации. Опирайся только на вход; не выдумывай фактов и дат.

Иди по шагам в порядке полей ответа, каждый следующий шаг опирается на предыдущие:
1. industry — отрасль, где произошло событие (где работает его главный участник).
2. oilfield_relevance — связь с нефтесервисом. oilfield_application — при переносимой связи одной фразой
   по-русски, какую задачу нефтесервиса решит технология; при прямой связи и без связи — пусто.
3. decision — брать (accept) или не брать (reject).
4. reject_reason — у reject один код причины отказа; у accept — none.
5. justification — одной-двумя фразами по-русски, до 200 знаков, почему такое решение.
6. category — категория публикации.

Коды — строго из справочников ниже, буквально. Дата публикации и дата поступления — когда о событии написали и
когда публикация пришла на платформу; дата самого события — только та, что названа в тексте или из него
следует. Похожие сигналы, оценённые людьми, и оценки людей — справка о том, как люди оценивали похожее, а не
правило."""


def _instructions(dictionaries: dict[str, Any]) -> str:
    lines = [_TASK, "", f"Справочники (версия {dictionaries.get('version')}):"]
    for name in CHOICES:
        block = dictionaries.get(name) or {}
        lines.append(f"{name} — {block.get('title', name)}. {block.get('rule', '')}".rstrip())
        for value in block.get("values") or []:
            examples = "; ".join(value.get("examples") or [])
            lines.append(f"- {value['code']} — {value['label']}: {value.get('rule', '')}"
                         + (f" Примеры: {examples}." if examples else ""))
        if name == "reject_reason":
            lines.append(f"- {NO_REASON} — причины нет (решение accept).")
    return "\n".join(lines)


def _codes(dictionaries: dict[str, Any], name: str) -> list[str]:
    return [value["code"] for value in (dictionaries.get(name) or {}).get("values") or []]


def _answer_schema(dictionaries: dict[str, Any]) -> dict[str, Any]:
    """Схема ответа модели (строгий JSON): порядок полей — порядок рассуждения «Отбора». По ней же ядро проверяет
    ответ (answer_problem)."""
    def enum(name: str, *extra: str) -> dict[str, Any]:
        return {"type": "string", "enum": [*_codes(dictionaries, name), *extra]}

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


def model_input(item: dict[str, Any], *, conflict: list[str] | None = None) -> str:
    """Вход модели о публикации: она сама, приёмщик ОС и, при повторе, конфликт прошлого ответа. Происхождения
    нет (решение владельца 22.10): «радар — значит технология» тянуло бы к старой мерке."""
    feedback = item.get("feedback") or {}
    similar = feedback.get("similar_signals") or []
    lines = [
        f"Заголовок: {item.get('title') or ''}",
        f"Источник: {item.get('publisher') or 'не указан'}",
        f"Язык: {item.get('language') or 'не указан'}",
        f"Дата публикации: {item.get('published') or 'не указана'}",
        f"Дата поступления: {item.get('received') or 'не указана'}",
        "Похожие сигналы, оценённые людьми: " + ("нет" if not similar else ""),
        *[f"- {one.get('title') or ''}; решение: {one.get('decision') or ''}; причина: {one.get('reason') or ''};"
          f" категория: {one.get('category') or ''}; тема: {one.get('theme') or ''}" for one in similar],
        f"Оценки людей текстом: {feedback.get('text') or 'нет'}",
    ]
    if conflict:
        lines.append("Прошлый ответ: шаги не сошлись (" + "; ".join(conflict) + "). Проверь шаги и ответь заново.")
    return "\n".join([*lines, "Текст публикации:", item.get("text") or ""])


# --- Проверка ответа (воркер и ядро) -------------------------------------------------------------


def answer_problem(answer: Any, dictionaries: dict[str, Any]) -> str | None:
    """Чем ответ не по правилам: не по схеме или значение не из справочника; None — по правилам."""
    if not isinstance(answer, dict):
        return "не объект"
    for name, spec in _answer_schema(dictionaries)["schema"]["properties"].items():
        if name == "category" and answer.get("decision") == REJECT:
            # Категория выбирает витрину, у отклонённого её нет: проверка её не требует (решение владельца 23.10).
            continue
        value = answer.get(name)
        if not isinstance(value, str):
            return f"нет строки {name}"
        if "enum" in spec and value not in spec["enum"]:
            return f"значение не из справочника: {name} = «{value[:60]}»"
    return None


def conflicts(answer: dict[str, Any], dictionaries: dict[str, Any], *, links: int = 0) -> list[str]:
    """Где шаги ответа не сошлись друг с другом (таблица конфликтов, DECISION_STEPS). links — ссылок у
    публикации: «разные события» — только у находки с двумя и более ссылками."""
    def label(name: str, code: str) -> str:
        values = (dictionaries.get(name) or {}).get("values") or []
        return next((value["label"] for value in values if value["code"] == code), code)

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
    if step == ACCEPT and relevance not in relevances:
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


@dataclass
class Outcome:
    """Что из ответов воркера дойдёт до сигнала: решение (values) или сбой (failure и почему)."""

    values: dict[str, Any] | None = None
    disputed: str | None = None
    failure: str | None = None
    reason: str | None = None
    log: list[dict[str, str]] = field(default_factory=list)


def outcome(decision: dict[str, Any], dictionaries: dict[str, Any], *, links: int = 0) -> Outcome:
    """Строка итога воркера → решение или сбой. Через неё пишет ядро (apply_result) и будет мерить экзамен
    (#133): мерится то, что дойдёт до сигнала. Конфликт шагов: в силе ответ повтора, если он по правилам; снова
    конфликт или повтора нет — решение остаётся с пометкой «спорное»."""
    calls = [call for call in decision.get("calls") or [] if isinstance(call, dict)]
    if not calls or calls[0].get("error"):
        reason = f"модель: {str(calls[0].get('error'))[:300]}" if calls else "модель не ответила"
        return Outcome(failure=otbor_store.FAILURE_TECHNICAL, reason=reason,
                       log=[_step(f"Сбой, «Отбор» не ответил: {reason}")])
    answer = calls[0].get("answer")
    problem = answer_problem(answer, dictionaries)
    if problem:
        reason = f"ответ не по правилам: {problem}"
        return Outcome(failure=otbor_store.FAILURE_INVALID, reason=reason, log=[_step(reason)])
    log: list[dict[str, str]] = []
    found = conflicts(answer, dictionaries, links=links)
    disputed = None
    if found:
        log.append(_step(f"Шаги ответа не сошлись ({'; '.join(found)}): повторный вызов с указанием конфликта"))
        retry = calls[1] if len(calls) > 1 else {}
        if "answer" in retry and answer_problem(retry["answer"], dictionaries) is None:
            answer = retry["answer"]
            found = conflicts(answer, dictionaries, links=links)
        if found:
            disputed = "шаги ответа не сошлись и после повтора: " + "; ".join(found)
    values = _values(answer, dictionaries)
    log.append(_step(_decision_text(values, dictionaries)))
    if disputed:
        log.append(_step(f"Спорное: {disputed}"))
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
        "signal_category": category if category in _codes(dictionaries, "category") else None,
        "verdict_reason": _phrase(answer["justification"], _JUSTIFICATION_CHARS),
    }


def _decision_text(values: dict[str, Any], dictionaries: dict[str, Any]) -> str:
    def label(name: str, code: str | None) -> str:
        values_ = (dictionaries.get(name) or {}).get("values") or []
        return next((value["label"] for value in values_ if value["code"] == code), code or "")

    relevance = label("oilfield_relevance", values["oilfield_relevance"])
    if values["oilfield_application"]:
        relevance += f", применение: {values['oilfield_application']}"
    decision = label("decision", values["otbor_decision"])
    if values["reject_reason"]:
        decision += f", причина «{label('reject_reason', values['reject_reason'])}»"
    return (f"Отрасль: {label('industry', values['industry'])}. Связь с нефтесервисом: {relevance}. "
            f"Решение: {decision}. Категория: {label('category', values['signal_category'])}."
            + (f" Почему: {values['verdict_reason']}" if values["verdict_reason"] else ""))


def _step(text: str) -> dict[str, str]:
    return {"stage": STAGE, "text": text}


def _phrase(value: str, limit: int) -> str | None:
    return " ".join(value.split())[:limit] or None


# --- Применение (ядро) ---------------------------------------------------------------------------


def apply_result(result: dict[str, Any], *, job_id: int | None = None,
                 article_ids: list[Any] | None = None) -> dict[str, Any]:
    """Сторона ядра: проверить ответы кодом и записать решения или сбои. Строка не о статье задачи не пишется.
    Справочники сменились после выдачи — модель выбирала из других: технический сбой, шаг повторит."""
    dictionaries = load_dictionaries()
    decisions = [decision for decision in result.get("decisions") or [] if isinstance(decision, dict)]
    expected = None if article_ids is None else {str(value) for value in article_ids}
    otbor_store.record_usage(job_id, [usage for decision in decisions if (usage := _usage(decision, expected))])
    stale = result.get("dictionaries_version") != dictionaries.get("version")
    recipe = {"agent": KIND, "prompt_version": str(result.get("prompt_version") or "")[:50] or None,
              "dictionaries_version": dictionaries.get("version"), "job_id": job_id}
    applied = {"decided": 0, "failed": 0, "kept_in_digest": 0, "errors": []}
    for decision in decisions:
        article_id = _article_id(decision)
        if not _in_job(article_id, expected):
            applied["errors"].append({"ref": str(decision.get("ref"))[:60], "error": "публикации нет в этой задаче"})
            continue
        if stale:
            reason = (f"справочники сменились после выдачи задачи ({result.get('dictionaries_version')} → "
                      f"{dictionaries.get('version')})")
            checked = Outcome(failure=otbor_store.FAILURE_TECHNICAL, reason=reason,
                              log=[_step(f"Сбой, «Отбор» не ответил: {reason}")])
        else:
            # У статьи ленты одна публикация: «разные события» ей не ставятся (conflicts).
            checked = outcome(decision, dictionaries)
        if checked.values is None:
            if otbor_store.write_failure(article_id, checked.failure, checked.reason, log=checked.log):
                applied["failed"] += 1
            applied["errors"].append({"article_id": article_id, "error": checked.reason})
            continue
        decided_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        written = otbor_store.write_decision(article_id, checked.values, disputed=checked.disputed, log=checked.log,
                                             recipe={**recipe, "model": _model_name(decision), "decided_at": decided_at})
        if written:
            applied[written] += 1
    return applied


def _article_id(decision: dict[str, Any]) -> int | None:
    kind, _, number = str(decision.get("ref") or "").partition(":")
    if kind != "article":
        return None
    try:
        return int(number)
    except ValueError:
        return None


def _in_job(article_id: int | None, expected: set[str] | None) -> bool:
    return article_id is not None and (expected is None or str(article_id) in expected)


def _usage(decision: dict[str, Any], expected: set[str] | None) -> dict[str, Any] | None:
    """Оплаченные вызовы модели — и с ответом не по правилам: модель за них уже взяла деньги. Цена — по ставкам
    ядра. Строка не о статье задачи — не расход этой задачи."""
    article_id = _article_id(decision)
    try:
        tokens_in = max(0, int(decision.get("input_tokens") or 0))
        tokens_out = max(0, int(decision.get("output_tokens") or 0))
    except (TypeError, ValueError):
        return None
    if not _in_job(article_id, expected) or not tokens_in + tokens_out:
        return None
    model = _model_name(decision)
    price_in, price_out = config.price_for_model(model)
    return {"article_id": article_id, "model": model, "input_tokens": tokens_in, "output_tokens": tokens_out,
            "cost_usd": round((tokens_in * price_in + tokens_out * price_out) / 1_000_000, 6)}


def _model_name(decision: dict[str, Any]) -> str | None:
    return str(decision.get("model") or "")[:100] or None
