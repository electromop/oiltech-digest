"""«Разбор ОС» предлагает правила эксперта (#190, спека #187, ADR 0007): часть задачи недельного среза.

После разбора сигналов воркер делает ещё один вызов модели: отметки людей за окно похожих отметок (30 дней) по всем
карточкам, без имён авторов, и действующие правила всех шести агентов. Модель предлагает правило агенту и шагу: от двух
отметок об одном за окно или от одной явной просьбы («переводить X как Y», «всегда…»); расхождение оценки администратора
с баллом платформы: правилом для баллов; похожее на действующее: уточнением его, а не дублем; у агента с полным набором
(LIMIT): сведением похожих. Код ядра проверяет ответ: агент и шаг из списка, категория и тема поиска из справочников,
правило поиска только подсказками по теме (свободный текст не пишется, граница О-7), номера отметок из окна (хоть одна
новая, с прошлого среза), порог отметок, цитата дословно из текста отметок, уточняемые и сводимые правила действуют у
этого агента. Прошедшее ложится в журнал ОС предложением правила, и сразу ставится его автопроверка.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from oiltech_digest import expert_rules, feedback_rules, feedback_settings
from oiltech_digest.colleague_materials import VERDICTS
from oiltech_digest.db import connection, expert_rules_store
from oiltech_digest.db.feedback_store import RULE_PROPOSAL

logger = logging.getLogger(__name__)

PROMPT_VERSION = "2026-11-13.1"
OUTPUT_TOKENS = 3000
_MARKS_LIMIT = 400
_TEXT_CHARS = 600
# Оценка шкалы коротко (у разбора ОС по сигналам: та же таблица).
VERDICT_RU = {code: short for code, short, _full in VERDICTS}
# Явная просьба в тексте отметки: правило по одной отметке (настройка «правило по одной явной просьбе»). Только
# указания («всегда…», «переводить X как Y», «не путать»), а не любые формы глаголов («относится», «называется»).
_EXPLICIT = re.compile(r"(?<!\w)(всегда|никогда|переводить|переводи|не\s+переводить|не\s+переводи|называть|называй"
                       r"|не\s+путать|не\s+путай|считать|считай|писать|пиши|относить|относи)(?!\w)", re.IGNORECASE)
_BAND_RU = {"strong": "сильный", "useful": "полезный", "watch": "наблюдать", "weak": "слабый"}
_DECISION_RU = {"accept": "брать", "reject": "не брать"}


# --- Пакет (ядро, при выдаче) --------------------------------------------------------------------


def payload(job_payload: dict[str, Any]) -> dict[str, Any] | None:
    """Вход вызова о правилах для задачи среза; задача, поставленная до #190, без начала окна среза: все отметки
    новые."""
    window_from = job_payload.get("window_from")
    return build(_moment(job_payload.get("since")), _moment(job_payload.get("until")),
                 _moment(window_from) if window_from else None)


def _moment(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def build(since: datetime, until: datetime, window_from: datetime | None) -> dict[str, Any] | None:
    """Вход вызова о правилах: отметки окна по всем карточкам (новые помечены), действующие правила агентов, шаги,
    категории и темы поиска. Отметок нет: вызова нет."""
    marks = expert_rules_store.window_marks(since, until, limit=_MARKS_LIMIT)
    if not marks:
        return None
    from oiltech_digest.db.rule_check_rows import VERDICT_BAND

    current = expert_rules_store.current_sets()
    settings = feedback_settings.current()
    return {
        "prompt_version": PROMPT_VERSION, "limit": expert_rules.LIMIT, "topics": feedback_rules.topics(),
        "threshold": int(settings["rule_threshold_marks"]), "explicit": bool(settings["rule_explicit_request"]),
        "agents": [{"agent": agent, "label": spec.label, "steps": spec.steps,
                    "rules": [{"id": int(rule["id"]), "step": rule["step"], "category": rule["category"],
                               "topic": rule["topic"], "text": rule["text"]}
                              for rule in (current.get(agent) or {}).get("rules") or []]}
                   for agent, spec in expert_rules.AGENTS.items()],
        "marks": [_mark(row, window_from, VERDICT_BAND) for row in marks],
    }


def _mark(row: dict[str, Any], window_from: datetime | None, bands: dict[str, str]) -> dict[str, Any]:
    undone = row["correction_undone_at"] is not None
    admin = row["role"] == "admin"
    expected = bands.get(row["verdict"] or "")
    return {
        "id": int(row["id"]), "signal_id": int(row["signal_id"]), "day": str(row["created_at"])[:10],
        "new": window_from is None or row["created_at"] > window_from,
        "role": "администратор" if admin else "участник" if row["role"] else "автор материала",
        "verdict": VERDICT_RU.get(row["verdict"] or "", row["verdict"] or ""), "verdict_code": row["verdict"],
        "reason": _cut(row["reason"]), "comment": _cut(row["comment"]),
        "proposed_title": "" if undone else _cut(row["corrected_title"]),
        "proposed_summary": "" if undone else _cut(row["corrected_thesis"]),
        "card": {"title": _cut(row["card_title"]), "category": expert_rules.CATEGORIES.get(row["signal_category"] or "",
                                                                                          row["signal_category"] or ""),
                 "theme": row["theme"] or "", "stage": row["technology_maturity"] or "",
                 "band": _BAND_RU.get(row["score_band"] or "", ""),
                 "decision": _DECISION_RU.get(row["otbor_decision"] or "", "")},
        # Расхождение оценки администратора с баллом платформы: материал правила для баллов (решение 6 гриля);
        # «Оценка завышена» само и есть расхождение.
        "band_gap": bool(admin and row["score_band"] and (row["verdict"] == "overrated"
                                                          or (expected and expected != row["score_band"]))),
    }


def _cut(value: Any) -> str:
    return " ".join(str(value or "").split())[:_TEXT_CHARS]


# --- Вызов модели (воркер, без базы) -------------------------------------------------------------


_TASK = """Ты разбираешь обратную связь экспертов (отметки ОС) о карточках сигналов рынка нефтесервиса за последние
недели и предлагаешь правила эксперта агентам платформы. Правило эксперта: короткое указание одному агенту на одном его
шаге, которое платформа добавит к его инструкции. Сам ты ничего не меняешь: правило согласует администратор, а до этого
его проверят на прошлых оценках.

Когда предлагать правило:
- есть хотя бы две отметки об одном и том же (одна и та же ошибка агента на разных карточках) или одна отметка с явной
  просьбой («переводить X как Y», «всегда…», «никогда…»); хотя бы одна из них помечена «новая»;
- оценка администратора расходится с баллом платформы на нескольких карточках по одной причине: правило для баллов.
Как предлагать:
- agent и step: только из списка агентов и шагов ниже; category: «бизнес», «технология» или пусто (для любой);
- text: одно короткое правило по-русски, до 300 знаков, без ссылок на конкретные карточки;
- если похожее правило уже действует, предложи его уточнение: kind = refine, replaces = [номер того правила];
- если у агента уже столько правил, сколько можно, новых не предлагай: сведи похожие в одно, kind = merge, replaces =
  номера сводимых правил; иначе kind = new и replaces пусто;
- поиск радара (agent search): только подсказки по теме: topic из списка тем, text: поисковые термины через «;» (что
  искать ещё), без указаний, запретов, сайтов и ссылок;
- marks: номера отметок, на которых стоит правило; quote: дословный фрагмент текста одной из них.
Правил нет: пустой список. Не предлагай правило, которое уже действует теми же словами."""


def instructions() -> str:
    return _TASK


def answer_schema() -> dict[str, Any]:
    return {"name": "feedback_rules", "schema": {
        "type": "object", "additionalProperties": False, "required": ["rules"],
        "properties": {"rules": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["agent", "step", "category", "topic", "text", "kind", "replaces", "marks", "quote"],
            "properties": {"agent": {"type": "string", "enum": list(expert_rules.AGENTS)},
                           "step": {"type": "string"}, "category": {"type": "string"},
                           "topic": {"type": "string"}, "text": {"type": "string"},
                           "kind": {"type": "string", "enum": list(feedback_rules.RULE_KINDS)},
                           "replaces": {"type": "array", "items": {"type": "integer"}},
                           "marks": {"type": "array", "items": {"type": "integer"}},
                           "quote": {"type": "string"}},
        }}},
    }}


def model_input(item: dict[str, Any]) -> str:
    """Вход вызова о правилах: агенты, шаги и действующие правила; темы поиска; отметки людей без имён авторов."""
    lines = [f"Порог: правило от {item.get('threshold')} отметок об одном"
             + (" или от одной явной просьбы" if item.get("explicit") else ""),
             f"Не больше {item.get('limit')} действующих правил у агента.", "", "Агенты, шаги и действующие правила:"]
    for agent in item.get("agents") or []:
        steps = "; ".join(f"{key} ({label})" for key, label in (agent.get("steps") or {}).items())
        lines.append(f"- {agent['agent']} ({agent['label']}): шаги {steps}; правил {len(agent.get('rules') or [])}")
        for rule in agent.get("rules") or []:
            where = "; ".join(part for part in (rule.get("step"), expert_rules.CATEGORIES.get(rule.get("category") or ""),
                                                rule.get("topic")) if part)
            lines.append(f"  №{rule['id']} [{where}] {rule['text']}")
    lines += ["", "Темы поиска радара: " + "; ".join(item.get("topics") or []), "", "Отметки людей:"]
    for mark in item.get("marks") or []:
        card = mark.get("card") or {}
        context = "; ".join(part for part in (card.get("category"), f"тема {card['theme']}" if card.get("theme") else "",
                                              f"стадия {card['stage']}" if card.get("stage") else "",
                                              f"балл платформы {card['band']}" if card.get("band") else "",
                                              f"«Отбор»: {card['decision']}" if card.get("decision") else "") if part)
        lines.append(f"Отметка №{mark['id']} ({mark['role']}, {mark['day']}{', новая' if mark.get('new') else ''}) "
                     f"о карточке №{mark['signal_id']} «{card.get('title') or ''}» ({context}):")
        for label, key in (("Оценка", "verdict"), ("Обоснование оценки", "reason"), ("Рекомендации AI-агенту", "comment"),
                           ("Предложенный заголовок", "proposed_title"), ("Предложенная суть", "proposed_summary")):
            if mark.get(key):
                lines.append(f"  {label}: {mark[key]}")
        if mark.get("band_gap"):
            lines.append("  Оценка администратора расходится с баллом платформы")
    return "\n".join(lines)


def run(client: Any, payload: dict[str, Any], result: dict[str, Any], heartbeat: Any, *, model: str) -> None:
    """Сторона воркера, после сигналов: вызов о правилах в итог задачи. Воркер остановили раньше: пометка
    rules_pending, и задача вернётся без сигналов, только за правилами (contract.remaining_after_partial)."""
    from oiltech_digest.processing.external_ai import StopRequested

    if not payload.get("rules_review"):
        return
    try:
        if heartbeat and not result.get("partial"):
            heartbeat(result)
    except StopRequested:
        result["partial"] = True
    if result.get("partial"):
        result["rules_pending"] = True
        return
    result["rules"] = call(client, payload["rules_review"], model=model, reasoning=payload.get("reasoning"))


def call(client: Any, item: dict[str, Any], *, model: str, reasoning: Any) -> dict[str, Any]:
    """Сторона воркера: один вызов модели о правилах; ответ как есть. Сбой вызова не губит разбор сигналов."""
    from oiltech_digest.processing.external_ai import StopRequested
    from oiltech_digest.processing.openai_client import output_budget

    try:
        response = client.complete_json(instructions(), model_input(item), answer_schema(),
                                        max_output_tokens=output_budget(OUTPUT_TOKENS, reasoning), model=model,
                                        reasoning_effort=reasoning)
    except StopRequested:
        raise
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:300]}
    return {"answer": response.data, "model": response.model, "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens}


# --- Применение (ядро) ---------------------------------------------------------------------------


def apply_result(rules: dict[str, Any], *, job_id: int | None, job_payload: dict[str, Any], slice_id: Any,
                 stage: str) -> dict[str, Any]:
    """Итог вызова о правилах: расход той же стадией разбора ОС (строкой задачи без сигнала), проверка кодом и запись
    предложений. Вход проверки собирается заново по окну задачи: отметки окна те же, а уточняемые правила должны
    действовать сейчас."""
    from oiltech_digest import config
    from oiltech_digest.db import rule_check_store

    try:
        tokens_in, tokens_out = max(0, int(rules.get("input_tokens") or 0)), max(0, int(rules.get("output_tokens") or 0))
    except (TypeError, ValueError):
        tokens_in = tokens_out = 0
    if tokens_in + tokens_out:
        model = str(rules.get("model") or "")[:100] or None
        price_in, price_out = config.price_for_model(model)
        rule_check_store.record_usage(job_id, [{"signal_id": None, "model": model, "input_tokens": tokens_in,
                                                "output_tokens": tokens_out, "cost_usd": round(
                                                    (tokens_in * price_in + tokens_out * price_out) / 1_000_000, 6)}],
                                      stage=stage, recipe={"part": "rules", "prompt_version": PROMPT_VERSION})
    if rules.get("error"):
        return {"rule_proposals": 0, "rule_errors": [f"правила: модель: {str(rules['error'])[:300]}"]}
    return apply(rules.get("answer"), payload(job_payload) or {}, slice_id=slice_id,
                 model=str(rules.get("model") or "")[:100] or None)


def apply(answer: Any, item: dict[str, Any], *, slice_id: int | None, model: str | None) -> dict[str, Any]:
    """Проверить предложенные правила кодом и записать прошедшие предложениями в журнал ОС; поставить их автопроверку.
    item: вход вызова, каким он ушёл воркеру (отметки окна и действующие правила)."""
    from oiltech_digest import rule_check

    proposals, problems = checked(answer, item)
    written = []
    with connection.get_connection() as conn:
        for proposal in proposals:
            written.append(feedback_rules.create_proposal(
                proposal["rule"], rule_kind=proposal["kind"], replaces=proposal["replaces"],
                event_ids=proposal["marks"], quote=proposal["quote"], slice_id=slice_id,
                author="Разбор ОС", details={"model": model, "prompt_version": PROMPT_VERSION}, conn=conn))
        conn.commit()
    for action_id in written:
        try:
            rule_check.check_proposal(action_id)
        except Exception:  # noqa: BLE001 - проверку возьмёт шаг планировщика
            logger.exception("rule_check_enqueue_failed action=%s", action_id)
    return {"rule_proposals": len(written), "rule_errors": problems}


def checked(answer: Any, item: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Правила ответа, прошедшие проверку кодом, и почему отброшены остальные."""
    if not isinstance(answer, dict) or not isinstance(answer.get("rules"), list):
        return [], ["правила: ответ не по схеме"]
    marks = {int(mark["id"]): mark for mark in item.get("marks") or []}
    current = {agent["agent"]: {int(rule["id"]): rule for rule in agent.get("rules") or []}
               for agent in item.get("agents") or []}
    taken = _taken_texts()
    threshold, explicit = int(item.get("threshold") or 2), bool(item.get("explicit"))
    found, problems, seen = [], [], set()
    for one in answer["rules"]:
        if not isinstance(one, dict):
            problems.append("правило не по схеме")
            continue
        try:
            proposal = _proposal(one, marks, current, threshold=threshold, explicit=explicit,
                                 topics=item.get("topics") or [])
        except expert_rules.RuleProblem as exc:
            problems.append(f"правило «{str(one.get('text') or '')[:60]}»: {exc}")
            continue
        key = (proposal["rule"]["agent"], plain(proposal["rule"]["text"]))
        if key in seen or key in taken:
            problems.append(f"правило «{proposal['rule']['text'][:60]}»: уже действует или уже предложено")
            continue
        seen.add(key)
        found.append(proposal)
    return found, problems


def _proposal(one: dict[str, Any], marks: dict[int, dict[str, Any]], current: dict[str, dict[int, Any]], *,
              threshold: int, explicit: bool, topics: list[str]) -> dict[str, Any]:
    rule = expert_rules.checked_rule(one.get("agent"), one.get("step"), one.get("category"), one.get("topic"),
                                     one.get("text"), topics=topics)
    cited = sorted({int(number) for number in one.get("marks") or [] if isinstance(number, int)})
    if not cited or any(number not in marks for number in cited):
        raise expert_rules.RuleProblem("номера отметок не из окна среза")
    if not any(marks[number].get("new") for number in cited):
        raise expert_rules.RuleProblem("ни одной новой отметки с прошлого среза")
    quote = " ".join(str(one.get("quote") or "").split())
    source = next((marks[number] for number in cited if _quoted(quote, marks[number], rule["agent"])), None)
    if source is None:
        raise expert_rules.RuleProblem("цитаты нет в тексте отметок")
    if len(cited) < threshold and not (explicit and _EXPLICIT.search(quote)):
        raise expert_rules.RuleProblem(f"отметок меньше порога ({threshold}) и нет явной просьбы")
    kind = str(one.get("kind") or "")
    if kind not in feedback_rules.RULE_KINDS:
        raise expert_rules.RuleProblem(f"вид правила «{kind[:20]}» не из списка: новое, уточнение или сведение")
    replaces = sorted({int(number) for number in one.get("replaces") or [] if isinstance(number, int)})
    agent_rules = current.get(rule["agent"]) or {}
    if any(number not in agent_rules for number in replaces):
        raise expert_rules.RuleProblem("уточнять и сводить можно только действующие правила этого агента")
    if (kind == "new" and replaces) or (kind == "refine" and len(replaces) != 1) or (kind == "merge"
                                                                                     and len(replaces) < 2):
        raise expert_rules.RuleProblem("вид правила не сходится с номерами: новое без номеров, уточнение одного, "
                                       "сведение нескольких")
    if kind == "new" and len(agent_rules) >= expert_rules.LIMIT:
        raise expert_rules.RuleProblem(f"у агента уже {expert_rules.LIMIT} правил: нужно свести похожие")
    return {"rule": rule, "kind": kind, "replaces": replaces, "marks": cited, "quote": quote[:500]}


def _quoted(quote: str, mark: dict[str, Any], agent: str) -> bool:
    wanted = plain(quote).strip(" .,;:!?\"'")
    if len(wanted) < 4:
        return False
    parts = [mark.get("reason"), mark.get("comment"), mark.get("proposed_title"), mark.get("proposed_summary")]
    if agent == "scores":
        # Правило для баллов из расхождения оценки с баллом: цитатой служит и сама оценка.
        parts.append(mark.get("verdict"))
    return any(wanted in plain(part) for part in parts if part)


def _taken_texts() -> set[tuple[str, str]]:
    """Правила, которые уже действуют или уже предложены (ждут решения, убраны): их не предлагают снова."""
    return {(agent, plain(text)) for agent, text in expert_rules_store.taken_rule_texts(RULE_PROPOSAL)}


def plain(value: Any) -> str:
    """Текст для сравнения: без регистра, «ё» как «е», кавычки-ёлочки как прямые, пробелы схлопнуты (и у разбора ОС
    по сигналам)."""
    return " ".join(str(value or "").replace("ё", "е").replace("Ё", "Е").replace("«", "\"").replace("»", "\"")
                    .casefold().split())
