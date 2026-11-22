"""Агент «Разбор ОС» (#189, спека #187): сводит отметки всех участников по каждому сигналу в одно улучшение карточки.

Вид задачи на внешнем воркере (контракт 12; с вызовом о правилах эксперта, #190: 13; с откликами потребителя выпуска,
#200: 14). Ядро собирает пакет при выдаче: сигналы с новыми отметками с прошлого среза, все отметки сигнала за окно
похожих отметок (без имён авторов: только роль), отрицательные отклики потребителя с прошлого среза (роль «сотрудник
потребителя», feedback_review_reactions), поля карточки, текст главной публикации и справочники. Воркер без базы зовёт
модель из настроек контура (gpt-6-sol) по сигналу (промпт: feedback_review_prompt), затем один раз о правилах
(feedback_review_rules). Ядро проверяет ответ кодом: поле из списка, значение из справочника, номера отметок и откликов
из пакета этой карточки, цитата дословно из текста этих отметок или подпись кнопки отклика; не прошедшее не пишется.
Прошедшее ложится в журнал ОС предложением (ждёт решения администратора); новое по тому же сигналу заменяет прежнее
нерешённое. Сам разбор ничего не меняет (решения владельца 11 и 12.11). Расход: своей стадией в учёте расхода ИИ.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Callable

from oiltech_digest import config, feedback_changes, feedback_review_reactions, feedback_review_rules, feedback_settings
from oiltech_digest.db import connection, feedback_store, repository
from oiltech_digest.db.feedback_store import PENDING, PROPOSAL, STALE
from oiltech_digest.feedback_review_prompt import answer_schema, instructions, model_input
from oiltech_digest.processing.external_ai import StopRequested
from oiltech_digest.processing.openai_client import output_budget
from oiltech_digest.processing.pipeline import make_client
from oiltech_digest.signal_card_countries import EVENT_LIMIT, Countries
from oiltech_digest.signal_card_pack import load_dictionaries, publication_text

KIND = "feedback_review"
# Стадия расхода в учёте расхода ИИ: расход разбора ОС отдельной стадией.
STAGE = KIND
PROMPT_VERSION = "2026-11-22.1"
REVIEW_AUTHOR = "Разбор ОС"
_OUTPUT_TOKENS = 1500
_TEXT_CHARS = 4000
_COMPANY_CHARS, _COMPANIES = 120, 10
_DATE_RE = re.compile(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")
# Оценка шкалы коротко, как в списке вердиктов материалов коллег (он сверен с формой ОС тестом).
_VERDICT_RU = feedback_review_rules.VERDICT_RU
# Цитата: не короче стольких знаков. Подпись оценки («Наблюдать») цитатой служит только изменениям, которые от оценки и
# идут: категория («Не тот блок»), «Дубль», уборка («Не релевантно»); прочим полям нужен текст отметки.
_QUOTE_MIN = 4
_VERDICT_KINDS = frozenset({"category", "duplicate", "removal"})
_CATEGORY_CODES = {"бизнес": "business", "технология": "technology", "business": "business",
                   "technology": "technology"}


# --- Постановка (ядро) ---------------------------------------------------------------------------


def enqueue(slice_id: int, signal_ids: list[int], *, since: datetime, until: datetime, model: str,
            window_from: datetime | None = None) -> dict[str, Any]:
    """Одна задача разбора на срез: сигналы с новыми отметками, окно похожих отметок, модель из настроек. window_from:
    начало окна среза: отметки после него новые (правило эксперта стоит хоть на одной новой, #190)."""
    from oiltech_digest import network_policy

    route = network_policy.route_ai_processing()
    return repository.create_background_job(
        KIND, {"slice_id": int(slice_id), "signal_ids": [int(one) for one in signal_ids], "since": since.isoformat(),
               "until": until.isoformat(), "model": model, "reasoning": config.FEEDBACK_REVIEW_REASONING,
               **({"window_from": window_from.isoformat()} if window_from else {})},
        queue_name=route.queue_name, execution_region=route.execution_region, capability=route.capability,
    )


# --- Пакет (ядро, при выдаче) --------------------------------------------------------------------


def build_payload(job_payload: dict[str, Any]) -> dict[str, Any]:
    """Самодостаточный пакет для воркера без базы: карточки, публикации, отметки без имён авторов, справочники."""
    from oiltech_digest.db import signal_card_store

    ids = [int(one) for one in job_payload.get("signal_ids") or []]
    since, until = _moment(job_payload.get("since")), _moment(job_payload.get("until"))
    cards = feedback_store.review_cards(ids)
    marks = feedback_store.signal_marks(ids, since, until)
    reactions = feedback_review_reactions.for_signals(ids, _reactions_since(job_payload), until)
    publications = {int(row["signal_id"]): row for row in signal_card_store.card_inputs(ids)}
    context = choices_context()
    return {
        "kind": KIND, "model": job_payload.get("model"), "reasoning": job_payload.get("reasoning"),
        "prompt_version": PROMPT_VERSION, "choices": context["choices"],
        "items": [_item(cards[one], publications.get(one), marks.get(one) or [], reactions.get(one) or [])
                  for one in ids if one in cards and (marks.get(one) or reactions.get(one))],
        # Правила эксперта (#190): после сигналов один вызов по отметкам окна и действующим правилам агентов.
        "rules_review": feedback_review_rules.payload(job_payload),
    }


def choices_context() -> dict[str, Any]:
    """Справочники значений полей карточки: модели в пакет и коду ядра для проверки ответа."""
    dictionaries = load_dictionaries()
    themes = _themes()
    choices = {field: [str(one) for one in (dictionaries.get(spec.dictionary) or {}).get("values") or []]
               for field, spec in feedback_changes.FIELDS.items() if spec.kind == "choice"}
    choices["theme"] = themes
    choices["signal_category"] = ["бизнес", "технология"]
    return {"choices": choices, "countries": Countries(dictionaries)}


def _themes() -> list[str]:
    from oiltech_digest import signal_themes

    return signal_themes.names(repository.list_enabled_tags())


def _reactions_since(job_payload: dict[str, Any]) -> datetime:
    """Отклики берутся с прошлого среза (начало окна среза); задача без него (до #190): с начала окна похожих отметок."""
    return _moment(job_payload.get("window_from") or job_payload.get("since"))


def _item(card: dict[str, Any], publication: dict[str, Any] | None, marks: list[dict[str, Any]],
          reactions: list[dict[str, Any]]) -> dict[str, Any]:
    item = {
        "signal_id": int(card["id"]),
        "card": card_lines(card),
        "publication": {"title": publication_text((publication or {}).get("title"))[:500],
                        "text": publication_text((publication or {}).get("text"))[:_TEXT_CHARS]},
        "marks": [_mark(mark) for mark in marks],
    }
    # Без откликов пакет прежней формы: его исполнит и сборка воркера до контракта 14.
    if reactions:
        item["reactions"] = feedback_review_reactions.payload(reactions)
    return item


def card_lines(card: dict[str, Any]) -> dict[str, str]:
    """Карточка, как её видят люди: подписи полей и значения."""
    columns = {key: card.get(key) for key in ("event_date", "event_date_precision", "event_countries",
                                               "participants_json")}
    values = {
        "Заголовок": card.get("title_ru") or card.get("title") or "",
        "Суть": card.get("summary") or "",
        "Категория": feedback_changes.CATEGORY_RU.get(card.get("signal_category"), card.get("signal_category") or ""),
    }
    for field in ("theme", "event_type", "technology_maturity", "event_date", "event_countries", "region",
                  "technology_class", "participants"):
        spec = feedback_changes.FIELDS[field]
        values[spec.label] = feedback_changes.display_value(field, {**columns, **{c: card.get(c) for c in spec.columns}})
    return {key: str(value) for key, value in values.items()}


def _mark(mark: dict[str, Any]) -> dict[str, Any]:
    """Отметка без имени автора: роль, день, поля формы и применена ли её правка."""
    role = "администратор" if mark.get("role") == "admin" else "участник" if mark.get("role") else "автор материала"
    undone = mark.get("correction_undone_at") is not None
    return {
        "id": int(mark["id"]), "role": role, "day": str(mark["created_at"])[:10],
        "verdict": _VERDICT_RU.get(mark.get("verdict") or "", mark.get("verdict") or ""),
        "reason": mark.get("reason") or "", "comment": mark.get("comment") or "",
        "proposed_title": "" if undone else (mark.get("corrected_title") or "").strip(),
        "proposed_summary": "" if undone else (mark.get("corrected_thesis") or "").strip(),
        "duplicate_of": mark.get("duplicate_of_signal_id"), "applied": bool(mark.get("applied")),
    }


# --- Вызов модели (воркер, без базы) -------------------------------------------------------------


def process_payload(payload: dict[str, Any], heartbeat: Callable[..., None] | None = None) -> dict[str, Any]:
    """Сторона воркера: вызов модели на сигнал; ответ модели: как есть."""
    model = str(payload.get("model") or "").strip()
    if not model:
        raise ValueError("feedback_review: в задаче нет модели (настройка контура ОС на ядре)")
    client = make_client()
    rows: list[dict[str, Any]] = []
    result = {"feedback_review": True, "kind": KIND, "prompt_version": payload.get("prompt_version"), "reviews": rows}
    choices = payload.get("choices") or {}
    for item in payload.get("items") or []:
        if heartbeat:
            try:
                heartbeat(result)
            except StopRequested:
                result["partial"] = True
                break
        row: dict[str, Any] = {"signal_id": item.get("signal_id")}
        try:
            response = client.complete_json(
                instructions(choices), model_input(item), answer_schema(),
                max_output_tokens=output_budget(_OUTPUT_TOKENS, payload.get("reasoning")),
                model=model, reasoning_effort=payload.get("reasoning"),
            )
        except Exception as exc:  # noqa: BLE001 - один сигнал не роняет пачку
            rows.append({**row, "error": str(exc)[:300]})
            continue
        rows.append({**row, "answer": response.data, "model": response.model,
                     "input_tokens": response.input_tokens, "output_tokens": response.output_tokens})
    feedback_review_rules.run(client, payload, result, heartbeat, model=model)
    return result


# --- Применение (ядро) ---------------------------------------------------------------------------


def apply_result(result: dict[str, Any], *, job_id: int | None, job_payload: dict[str, Any]) -> dict[str, Any]:
    """Сторона ядра: проверить ответы кодом и записать предложения в журнал ОС; расход: стадией разбора ОС. Строка
    чужого сигнала не пишется. Итог задачи целиком (не частичный) закрывает срез."""
    ids = [int(one) for one in job_payload.get("signal_ids") or []]
    expected = {str(one) for one in ids}
    rows = [row for row in result.get("reviews") or [] if isinstance(row, dict)
            and str(row.get("signal_id")) in expected]
    feedback_store.record_usage(job_id, [usage for row in rows if (usage := _usage(row))], stage=STAGE)
    since, until = _moment(job_payload.get("since")), _moment(job_payload.get("until"))
    reviewed = [int(row["signal_id"]) for row in rows]
    marks = feedback_store.signal_marks(reviewed, since, until)
    reactions = feedback_review_reactions.for_signals(reviewed, _reactions_since(job_payload), until)
    cards = feedback_store.review_cards(reviewed)
    context = choices_context()
    threshold = int(feedback_settings.current()["card_threshold_marks"])
    slice_id = job_payload.get("slice_id")
    applied: dict[str, Any] = {"proposals": 0, "errors": []}
    for row in rows:
        signal_id = int(row["signal_id"])
        if row.get("error"):
            applied["errors"].append({"signal_id": signal_id, "error": f"модель: {str(row['error'])[:300]}"})
            continue
        if signal_id not in cards:
            continue
        changes, problems = checked_changes(row.get("answer"), cards[signal_id], marks.get(signal_id) or [], context,
                                            threshold, reactions=reactions.get(signal_id) or [])
        applied["errors"] += [{"signal_id": signal_id, "error": problem} for problem in problems]
        if changes:
            _write_proposal(signal_id, changes, slice_id=slice_id, model=str(row.get("model") or "")[:100] or None)
            applied["proposals"] += 1
    if isinstance(result.get("rules"), dict):
        found = feedback_review_rules.apply_result(result["rules"], job_id=job_id, job_payload=job_payload,
                                                   slice_id=slice_id, stage=STAGE)
        applied["rule_proposals"] = found["rule_proposals"]
        applied["errors"] += [{"signal_id": None, "error": problem} for problem in found["rule_errors"]]
    if slice_id is not None and not result.get("partial"):
        note = f"не прошло проверку кодом: {len(applied['errors'])}" if applied["errors"] else None
        feedback_store.finish_slice(int(slice_id), status="done", note=note)
    return applied


def _numbers(value: Any) -> list[int]:
    return [number for number in value or [] if isinstance(number, int)] if isinstance(value, list) else []


def checked_changes(answer: Any, card: dict[str, Any], marks: list[dict[str, Any]], context: dict[str, Any],
                    threshold: int, *, reactions: list[dict[str, Any]] | None = None
                    ) -> tuple[list[dict[str, Any]], list[str]]:
    """Изменения ответа, прошедшие проверку кодом, и почему отброшены остальные. reactions: отрицательные отклики
    потребителя этой карточки (#200): на них ответ ссылается отдельным списком, порог считает их как отметки."""
    if not isinstance(answer, dict) or not isinstance(answer.get("improvements"), list):
        return [], ["ответ не по схеме"]
    by_id = {int(mark["id"]): mark for mark in marks}
    by_reaction = {int(row["id"]): row for row in reactions or []}
    changes, problems, seen = [], [], set()
    for one in answer["improvements"]:
        if not isinstance(one, dict):
            problems.append("улучшение не по схеме")
            continue
        field, raw = str(one.get("field") or ""), str(one.get("value") or "")
        cited, cited_reactions = _numbers(one.get("marks")), _numbers(one.get("reactions"))
        problem = None
        if field not in feedback_changes.FIELDS:
            problem = f"поле «{field[:40]}» не из списка"
        elif field in seen:
            problem = f"поле «{field}» дважды"
        elif (not cited and not cited_reactions) or any(number not in by_id for number in cited) or any(
                number not in by_reaction for number in cited_reactions):
            problem = f"{field}: номера отметок не из этой карточки"
        elif len(set(cited)) + len(set(cited_reactions)) < threshold:
            problem = f"{field}: отметок меньше порога ({threshold})"
        elif not _quoted(str(one.get("quote") or ""), [by_id[number] for number in cited],
                         verdicts=feedback_changes.FIELDS[field].kind in _VERDICT_KINDS,
                         reactions=[by_reaction[number] for number in cited_reactions]):
            problem = f"{field}: цитаты нет в тексте отметок"
        if problem is None:
            value, problem = parse_value(field, raw, card, context, cited=[by_id[number] for number in cited])
        if problem:
            problems.append(problem)
            continue
        seen.add(field)
        changes.append(_change(field, value, card, quote=" ".join(str(one["quote"]).split())[:500],
                               event_ids=sorted(set(cited)), reaction_ids=sorted(set(cited_reactions))))
    return changes, problems


def parse_value(field: str, raw: str, card: dict[str, Any], context: dict[str, Any], *,
                cited: list[dict[str, Any]] | None = None) -> tuple[Any, str | None]:
    """Значение поля из строки ответа модели или правки администратора. (значение, None) или (None, почему нет).
    cited: отметки, на которых стоит изменение (у правки администратора их нет). Категорию, «Дубль» и уборку
    обосновывают только отметки людей платформы, отклики потребителя нет."""
    spec = feedback_changes.FIELDS[field]
    text = " ".join(raw.split())
    label = spec.label
    if spec.kind == "text":
        value = text if field == "title" else raw.strip()
        if not value or len(value) > spec.limit:
            return None, f"{label}: пусто или длиннее {spec.limit} знаков"
        current = (card.get("title_ru") or card.get("title")) if field == "title" else card.get("summary")
        return (None, f"{label}: уже такой") if value == (current or "").strip() else (value, None)
    if spec.kind in ("theme", "choice"):
        listed = context["choices"].get(field) or []
        value = next((item for item in listed if _plain(item) == _plain(text)), None)
        if value is None:
            return None, f"{label}: «{text[:60]}» не из справочника"
        return (None, f"{label}: уже такое") if value == card.get(spec.columns[0]) else ({spec.columns[0]: value}, None)
    if spec.kind == "date":
        return _date_value(text, card)
    if spec.kind == "countries":
        names = [part.strip() for part in re.split(r"[;,]", text) if part.strip()]
        found = [context["countries"].canonical(name) for name in names]
        if not names or None in found or len(set(found)) > EVENT_LIMIT:
            return None, f"{label}: страна не из справочника или больше {EVENT_LIMIT}"
        value = list(dict.fromkeys(found))
        return (None, f"{label}: уже такие") if value == list(card.get("event_countries") or []) else (
            {"event_countries": value}, None)
    if spec.kind == "companies":
        names = list(dict.fromkeys(" ".join(part.split()) for part in text.split(";") if part.strip()))
        if not names or len(names) > _COMPANIES or any(len(name) > _COMPANY_CHARS for name in names):
            return None, f"{label}: от 1 до {_COMPANIES} компаний через «;»"
        if names == [one.get("name") for one in card.get("participants_json") or []]:
            return None, f"{label}: уже такие"
        return {"participants_json": [{"name": name, "quote": "", "verified": False} for name in names]}, None
    if spec.kind == "category":
        value = _CATEGORY_CODES.get(_plain(text))
        if value is None:
            return None, f"{label}: бизнес или технология"
        if value == card.get("signal_category"):
            return None, f"{label}: уже {feedback_changes.CATEGORY_RU[value]}"
        if cited is not None and not any(mark.get("verdict") == "wrong_block" for mark in cited):
            return None, f"{label}: нет отметки «Не тот блок»"
        return value, None
    if spec.kind == "duplicate":
        numbers = re.findall(r"\d+", text)
        digits = numbers[0] if len(numbers) == 1 else ""
        if not digits or int(digits) == int(card["id"]):
            return None, f"{label}: нужен один номер другой карточки"
        if cited is not None and not any(mark.get("duplicate_of_signal_id") == int(digits) for mark in cited):
            return None, f"{label}: нет отметки «Дубль» с №{digits}"
        return int(digits), None
    if cited is not None and not any(mark.get("verdict") == "wrong_domain" for mark in cited):
        return None, f"{label}: нет отметки «Не релевантно»"
    return True, None


def _date_value(text: str, card: dict[str, Any]) -> tuple[Any, str | None]:
    match = _DATE_RE.fullmatch(text)
    try:
        year, month, day = (int(match.group(1)), int(match.group(2) or 1), int(match.group(3) or 1)) if match else (0, 0, 0)
        moment = date(year, month, day)
    except ValueError:
        moment = None
    if moment is None:
        return None, "Дата события: ГГГГ-ММ-ДД, ГГГГ-ММ или ГГГГ"
    precision = "day" if match.group(3) else "month" if match.group(2) else "year"
    value = {"event_date": moment.isoformat(), "event_date_precision": precision}
    current = {"event_date": str(card.get("event_date") or "")[:10] or None,
               "event_date_precision": card.get("event_date_precision") or "day"}
    return (None, "Дата события: уже такая") if value == current else (value, None)


def _change(field: str, value: Any, card: dict[str, Any], *, quote: str | None, event_ids: list[int],
            reaction_ids: list[int] | None = None) -> dict[str, Any]:
    spec = feedback_changes.FIELDS[field]
    if spec.kind in feedback_changes.CARD_KINDS:
        shown = feedback_changes.display_value(field, value)
        previous = feedback_changes.display_value(field, {column: card.get(column) for column in spec.columns}
                                                  | {"event_date_precision": card.get("event_date_precision")})
    elif spec.kind == "text":
        previous = (card.get("title_ru") or card.get("title")) if field == "title" else card.get("summary")
        shown = value
    elif spec.kind == "category":
        shown = feedback_changes.CATEGORY_RU[value]
        previous = feedback_changes.CATEGORY_RU.get(card.get("signal_category"), card.get("signal_category"))
    elif spec.kind == "duplicate":
        shown, previous = f"склеить с №{value}", "своя карточка"
    else:
        shown, previous = "убрать в «Убрано людьми»", "в витрине"
    return {"field": field, "label": spec.label, "value": value, "shown": str(shown), "previous": previous,
            "quote": quote, "event_ids": event_ids, "reaction_ids": list(reaction_ids or [])}


def admin_changes(signal_id: int, proposed: list[dict[str, Any]], edited: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """«Изменить»: формулировка администратора, проверенная как ответ модели (без цитаты). JournalRefused: не так."""
    from oiltech_digest.feedback_journal import JournalRefused

    by_field = {change["field"]: change for change in proposed}
    card = feedback_store.review_cards([signal_id]).get(int(signal_id))
    if card is None:
        raise JournalRefused(f"карточки №{int(signal_id)} нет")
    context, final = choices_context(), []
    for one in edited:
        field = str(one.get("field") or "")
        if field not in by_field:
            raise JournalRefused(f"поля «{field[:40]}» в предложении нет")
        value, problem = parse_value(field, str(one.get("value") or ""), card, context)
        if problem:
            raise JournalRefused(problem)
        final.append(_change(field, value, card, quote=by_field[field].get("quote"),
                             event_ids=by_field[field].get("event_ids") or [],
                             reaction_ids=by_field[field].get("reaction_ids") or []))
    if not final:
        raise JournalRefused("в новой формулировке нет ни одного поля")
    return final


def _write_proposal(signal_id: int, changes: list[dict[str, Any]], *, slice_id: int | None, model: str | None) -> None:
    """Предложение по сигналу в журнал ОС; прежнее нерешённое по тому же сигналу заменяется им («устарело»)."""
    event_ids = sorted({one for change in changes for one in change["event_ids"]})
    # Отклики потребителя (#200): основание предложения отдельным списком рядом с номерами отметок.
    reaction_ids = sorted({one for change in changes for one in change.get("reaction_ids") or []})
    agents = sorted({feedback_changes.FIELDS[change["field"]].agent for change in changes})
    with connection.get_connection() as conn:
        previous = feedback_store.pending_proposal(signal_id, conn=conn)
        if previous is not None:
            event_ids = sorted(set(event_ids) | {int(one) for one in previous["event_ids"]})
            reaction_ids = sorted(set(reaction_ids) | {int(one) for one in
                                                       (previous["details"] or {}).get("reaction_ids") or []})
        proposal_id = feedback_store.create_action(
            PROPOSAL, PENDING, signal_id=signal_id, slice_id=slice_id, event_ids=event_ids,
            quote=changes[0]["quote"], agents=agents, author=REVIEW_AUTHOR,
            details={"changes": changes, "model": model, "prompt_version": PROMPT_VERSION,
                     "reaction_ids": reaction_ids}, conn=conn)
        if previous is not None:
            feedback_store.set_status(previous["id"], STALE, details={"superseded_by": proposal_id}, conn=conn)
        if slice_id is not None:
            feedback_store.add_proposals(int(slice_id), 1, conn=conn)
        conn.commit()


def _quoted(quote: str, marks: list[dict[str, Any]], *, verdicts: bool,
            reactions: list[dict[str, Any]] | None = None) -> bool:
    """Цитата дословно из текста отметок или подпись кнопки отклика (#200: у отклика другого текста нет)."""
    wanted = _plain(quote).strip(" .,;:!?\"'")
    if len(wanted) < _QUOTE_MIN:
        return False
    texts = [_mark_text(mark, verdict=verdicts) for mark in marks]
    texts += [feedback_review_reactions.label(row) for row in reactions or []]
    return any(wanted in _plain(text) for text in texts)


def _mark_text(mark: dict[str, Any], *, verdict: bool) -> str:
    """Текст отметки, из которого берётся цитата: обоснование, рекомендации, правки (отменённая правка не в счёт);
    подпись оценки: только если изменение от неё и идёт."""
    undone = mark.get("correction_undone_at") is not None
    return "\n".join(str(part) for part in (
        _VERDICT_RU.get(mark.get("verdict") or "", "") if verdict else None, mark.get("reason"), mark.get("comment"),
        None if undone else mark.get("corrected_title"), None if undone else mark.get("corrected_thesis")) if part)


_plain = feedback_review_rules.plain


def _usage(row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        tokens_in = max(0, int(row.get("input_tokens") or 0))
        tokens_out = max(0, int(row.get("output_tokens") or 0))
        signal_id = int(row["signal_id"])
    except (TypeError, ValueError, KeyError):
        return None
    if not tokens_in + tokens_out:
        return None
    model = str(row.get("model") or "")[:100] or None
    price_in, price_out = config.price_for_model(model)
    return {"signal_id": signal_id, "model": model, "input_tokens": tokens_in, "output_tokens": tokens_out,
            "cost_usd": round((tokens_in * price_in + tokens_out * price_out) / 1_000_000, 6)}


_moment = feedback_review_rules._moment
