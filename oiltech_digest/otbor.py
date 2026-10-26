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

С #131 «Отбор» решает и находки поиска радара, и материалы коллег — сигналы с меткой пути (signals.selector): вход у
них тот же, что строка радара и материала в пакете эксперту; у находки с несколькими ссылками — «Ссылки находки»,
и только у неё «разные события в одной находке» не конфликт шагов.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import partial
from typing import Any, Callable

from oiltech_digest import config, contract
from oiltech_digest.db import otbor_inputs, otbor_store, repository
from oiltech_digest.otbor_answer import NO_REASON, answer_problem, answer_schema, conflicts, outcome, technical_failure
from oiltech_digest.otbor_pack_format import load_dictionaries
from oiltech_digest.processing.external_ai import StopRequested
from oiltech_digest.processing.openai_client import output_budget
from oiltech_digest.processing.pipeline import make_client
from oiltech_digest.signal_card_pack import _publisher_label, publication_day, publication_text

KIND = otbor_store.KIND
# Версия инструкции (и таблицы конфликтов, otbor_answer) — в рецепте решения: другая инструкция — другие решения.
PROMPT_VERSION = "2026-10-26.2"
_OUTPUT_TOKENS = 600
# Справочники, из которых модель выбирает коды, — в порядке её рассуждения (поля ответа).
CHOICES = ("industry", "occasion", "oilfield_relevance", "decision", "reject_reason", "category")


class OtborSetupError(ValueError):
    """Агента нельзя поставить в работу: выключен флаг или не задана модель."""


# --- Постановка (ядро) ---------------------------------------------------------------------------


def scheduler_step() -> dict[str, Any]:
    """Шаг планировщика «Отбор», за флагом OTBOR_ENABLED: статьи, находки и материалы, у которых кончились попытки,
    а итога нет (задачи падали целиком), — в раздел «„Отбор“ не ответил»; новые, которые решает он, — в задачи по
    полосе ежедневной обработки."""
    if not config.OTBOR_ENABLED:
        return {"articles": 0, "signals": 0, "jobs": [], "reason": "disabled"}
    # Без модели шаг отказывает в каждом цикле, даже когда новых статей нет: настройка видна сразу.
    configured_model()
    window = {"days": config.OTBOR_NEW_DAYS, "attempts": config.OTBOR_ATTEMPTS, "limit": config.OTBOR_STEP_LIMIT}
    # Попытки кончились, а итога нет (задачи падали целиком): публикация — в раздел «„Отбор“ не ответил».
    unanswered = otbor_store.unanswered_articles(**window)
    for article_id, reason in unanswered:
        failed = technical_failure(reason)
        otbor_store.write_failure(article_id, failed.failure, failed.failure_reason, log=failed.log)
    unanswered_signals = otbor_store.unanswered_signals(**window)
    for signal_id, reason in unanswered_signals:
        failed = technical_failure(reason)
        otbor_store.write_signal_failure(signal_id, failed.failure, failed.failure_reason, log=failed.log)
    ids = otbor_store.new_article_ids(**window)
    signal_ids = otbor_store.new_signal_ids(**window)
    return {"articles": len(ids), "signals": len(signal_ids),
            "jobs": enqueue(ids, signal_ids=signal_ids, trigger="daily"),
            "unanswered": len(unanswered) + len(unanswered_signals)}


def enqueue(article_ids: list[int], *, signal_ids: list[int] | tuple[int, ...] = (), trigger: str,
            bulk: bool = False) -> list[dict[str, Any]]:
    """Задачи агента пачками по OTBOR_BATCH: статьи и сигналы (находки и материалы, #131) — своими задачами. bulk —
    названные публикации (повтор после разбора сбоя): по полосе пересчётов, чтобы не стоять перед потоком дня.
    Только при включённом флаге: до экзамена (#133) агент на проде не работает."""
    from oiltech_digest import network_policy

    if not config.OTBOR_ENABLED:
        raise OtborSetupError("OTBOR_ENABLED выключен: «Отбор» выходит в работу после экзамена (#133)")
    model = configured_model()
    route = network_policy.route_ai_bulk() if bulk else network_policy.route_ai_processing()
    batch = config.OTBOR_BATCH
    return [
        repository.create_background_job(
            KIND,
            {key: ids[start:start + batch], "model": model, "reasoning": config.OTBOR_REASONING, "trigger": trigger},
            queue_name=route.queue_name, execution_region=route.execution_region, capability=route.capability,
        )
        for key, ids in (("article_ids", list(article_ids)), ("signal_ids", list(signal_ids)))
        for start in range(0, len(ids), batch)
    ]


def configured_model() -> str:
    if not config.OTBOR_MODEL:
        raise OtborSetupError("OTBOR_MODEL не задан: модель ленты «Отбор» не берёт")
    return config.OTBOR_MODEL


# --- Пакет (ядро, при выдаче) --------------------------------------------------------------------


def build_payload(job_payload: dict[str, Any]) -> dict[str, Any]:
    """Самодостаточный пакет для воркера без базы: публикации (статьи, находки, материалы), справочники, приёмщик
    ОС."""
    return {
        "kind": KIND, "model": job_payload.get("model"), "reasoning": job_payload.get("reasoning"),
        "dictionaries": load_dictionaries(),
        "items": [*(_item(row) for row in otbor_inputs.article_inputs(job_payload.get("article_ids") or [])),
                  *(_signal_item(row) for row in otbor_inputs.signal_inputs(job_payload.get("signal_ids") or []))],
    }


def _item(row: dict[str, Any]) -> dict[str, Any]:
    """Публикация так, как её видит модель: поля как у входа пакета эксперту (otbor_pack), тот же текст
    (publication_text) и даты днём по UTC. Приёмщик ОС (решение владельца 21.10) — фиксированная форма: похожие
    сигналы, оценённые людьми (заголовок, решение, код причины, категория, тема), и текст оценок, если правила
    сервиса ОС его пропустят. «Отбор» сам ОС не собирает; до сервиса ОС приёмщик пуст."""
    return {
        "ref": contract.article_ref(row["article_id"]),
        "title": publication_text(row.get("title"))[:500],
        "text": publication_text(row.get("text")),
        "publisher": row.get("publisher") or "",
        "language": row.get("language") or "",
        "published": publication_day(row.get("published")),
        "received": publication_day(row.get("received")),
        "links": [],
        "feedback": {"similar_signals": [], "text": ""},
        **_card_fact(row),
    }


def _signal_item(row: dict[str, Any]) -> dict[str, Any]:
    """Находка или материал (#131) так, как их видит модель, — как строка радара и материала в пакете эксперту
    (otbor_pack): издатель — источник статьи корпуса или сайт из ссылки (имя издателя у ссылки поиска записала модель
    поиска; у материала без ссылки издателя нет); текст материала — colleague_materials.as_publication. Ссылки
    находки — заголовок, издатель и дата каждой, до 8."""
    return {
        "ref": contract.signal_ref(row["signal_id"]),
        "title": publication_text(row.get("title"))[:500],
        "text": publication_text(row.get("text")),
        "publisher": _publisher_label(row),
        "language": row.get("language") or "",
        "published": publication_day(row.get("published")),
        "received": publication_day(row.get("received")),
        "links": [{"title": publication_text(link.get("title"))[:300] or None, "publisher": _publisher_label(link),
                   "published": publication_day(link.get("published"))} for link in row.get("links") or []],
        "feedback": {"similar_signals": [], "text": ""},
        **_card_fact(row),
    }


def _card_fact(row: dict[str, Any]) -> dict[str, str]:
    """Факт «Карточки» у публикации, которую она вернула (#132): дата события из текста старше срока. Только у
    возвращённой — у остальных входа не меняет (экзамен #133 подаёт манифест без него)."""
    return {"card_fact": row["card_fact"]} if row.get("card_fact") else {}


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
    schema = answer_schema(dictionaries)
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
    links = len(item.get("links") or [])
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
        found = [] if answer_problem(response.data, dictionaries) else conflicts(response.data, dictionaries,
                                                                                    links=links)
        if not found:
            break
    return decision


_TASK = """Ты решаешь, брать ли публикацию на платформу мониторинга рынка нефтесервиса, по тексту ОДНОЙ
публикации. Опирайся только на вход; не выдумывай фактов и дат.

Иди по шагам в порядке полей ответа, каждый следующий шаг опирается на предыдущие:
1. industry — отрасль, где произошло событие (где работает его главный участник).
2. occasion — главный повод публикации по её тексту, а не по заголовку.
3. oilfield_relevance — связь с нефтесервисом, только из связей повода. oilfield_application — при переносимой
   связи по-русски: какую задачу нефтесервиса решит технология, какие нужны доработки и ограничения; при прямой
   связи и без связи — пусто.
4. decision — брать (accept) или не брать (reject).
5. reject_reason — у reject один код причины отказа; у accept — none.
6. justification — одной-двумя фразами по-русски, до 200 знаков, почему такое решение.
7. category — категория публикации.

Коды — строго из справочников ниже, буквально. Дата публикации и дата поступления — когда о событии написали и
когда публикация пришла на платформу; дата самого события — только та, что названа в тексте или из него
следует. «Ссылки находки» — публикации, собранные в эту находку (заголовок, издатель, дата); «нет» — публикация
одна. Похожие сигналы, оценённые людьми, и оценки людей — справка о том, как люди оценивали похожее, а не
правило."""


def _instructions(dictionaries: dict[str, Any]) -> str:
    lines = [_TASK, "", f"Справочники (версия {dictionaries.get('version')}):"]
    for name in CHOICES:
        block = dictionaries.get(name) or {}
        lines.append(f"{name} — {block.get('title', name)}. {block.get('rule', '')}".rstrip())
        for value in block.get("values") or []:
            examples = "; ".join(value.get("examples") or [])
            relevances = " или ".join(value.get("relevances") or [])  # связи повода публикации
            lines.append(f"- {value['code']} — {value['label']}: {value.get('rule', '')}"
                         + (f" Связь: {relevances}." if relevances else "")
                         + (f" Примеры: {examples}." if examples else ""))
        if name == "reject_reason":
            lines.append(f"- {NO_REASON} — причины нет (решение accept).")
    return "\n".join(lines)


def model_input(item: dict[str, Any], *, conflict: list[str] | None = None) -> str:
    """Вход модели о публикации: она сама, приёмщик ОС и, при повторе, конфликт прошлого ответа. Происхождения
    нет (решение владельца 22.10): «радар — значит технология» тянуло бы к старой мерке."""
    feedback = item.get("feedback") or {}
    similar = feedback.get("similar_signals") or []
    links = item.get("links") or []
    lines = [
        f"Заголовок: {item.get('title') or ''}",
        f"Источник: {item.get('publisher') or 'не указан'}",
        f"Язык: {item.get('language') or 'не указан'}",
        f"Дата публикации: {item.get('published') or 'не указана'}",
        f"Дата поступления: {item.get('received') or 'не указана'}",
        # Вернула «Карточка» (#132): что она нашла в тексте, — решение остаётся за «Отбором».
        *([f"Факт «Карточки»: {item['card_fact']}"] if item.get("card_fact") else []),
        # Как колонка таблицы эксперта (otbor_pack_format.GIVEN_COLUMNS): по ней справочник объясняет причину
        # «разные события в одной находке».
        "Ссылки находки:" + (" нет" if not links else ""),
        *[f"- {link.get('title') or 'без заголовка'}; {link.get('publisher') or 'издатель не указан'};"
          f" {link.get('published') or 'дата не указана'}" for link in links],
        "Похожие сигналы, оценённые людьми: " + ("нет" if not similar else ""),
        *[f"- {one.get('title') or ''}; решение: {one.get('decision') or ''}; причина: {one.get('reason') or ''};"
          f" категория: {one.get('category') or ''}; тема: {one.get('theme') or ''}" for one in similar],
        f"Оценки людей текстом: {feedback.get('text') or 'нет'}",
    ]
    if conflict:
        lines.append("Прошлый ответ: шаги не сошлись (" + "; ".join(conflict) + "). Проверь шаги и ответь заново.")
    return "\n".join([*lines, "Текст публикации:", item.get("text") or ""])


# --- Применение (ядро) ---------------------------------------------------------------------------


def apply_result(result: dict[str, Any], *, job_id: int | None = None, article_ids: list[Any] | None = None,
                 signal_ids: list[Any] | None = None) -> dict[str, Any]:
    """Сторона ядра: проверить ответы кодом и записать решения или сбои — у статьи в её сигнал ленты, у находки и
    материала (#131) в сам сигнал. Строка не о публикации задачи не пишется; ссылок у находки ядро считает само.
    Справочники сменились после выдачи — модель выбирала из других: технический сбой, шаг повторит."""
    dictionaries = load_dictionaries()
    decisions = [decision for decision in result.get("decisions") or [] if isinstance(decision, dict)]
    expected = None if article_ids is None and signal_ids is None else (
        {contract.article_ref(value) for value in article_ids or []}
        | {contract.signal_ref(value) for value in signal_ids or []})
    otbor_store.record_usage(job_id, [usage for decision in decisions if (usage := _usage(decision, expected))])
    links = otbor_inputs.links_in_input([signal_id for decision in decisions
                                        if (signal_id := contract.ref_signal_id(decision.get("ref"))) is not None])
    stale = result.get("dictionaries_version") != dictionaries.get("version")
    recipe = {"agent": KIND, "prompt_version": str(result.get("prompt_version") or "")[:50] or None,
              "dictionaries_version": dictionaries.get("version"), "job_id": job_id}
    applied = {"decided": 0, "failed": 0, "kept_in_digest": 0, "kept_by_human": 0, "errors": []}
    for decision in decisions:
        article_id, signal_id = _publication(decision, expected)
        if article_id is None and signal_id is None:
            applied["errors"].append({"ref": str(decision.get("ref"))[:60], "error": "публикации нет в этой задаче"})
            continue
        if stale:
            reason = (f"справочники сменились после выдачи задачи ({result.get('dictionaries_version')} → "
                      f"{dictionaries.get('version')})")
            checked = technical_failure(reason)
        else:
            checked = outcome(decision, dictionaries, links=links.get(signal_id, 0))
        if checked.values is None:
            failed = (otbor_store.write_failure(article_id, checked.failure, checked.failure_reason, log=checked.log)
                      if article_id is not None else
                      otbor_store.write_signal_failure(signal_id, checked.failure, checked.failure_reason,
                                                       log=checked.log))
            if failed:
                applied["failed"] += 1
            applied["errors"].append({"article_id": article_id, "signal_id": signal_id,
                                      "error": checked.failure_reason})
            continue
        decided_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        write = (partial(otbor_store.write_decision, article_id) if article_id is not None
                 else partial(otbor_store.write_signal_decision, signal_id))
        written = write(checked.values, disputed=checked.disputed, log=checked.log,
                        recipe={**recipe, "model": _model_name(decision), "decided_at": decided_at})
        if written:
            applied[written] += 1
    return applied


def _publication(decision: dict[str, Any], expected: set[str] | None) -> tuple[int | None, int | None]:
    """(статья, сигнал) строки итога воркера: ровно одно не None, если публикация — из этой задачи."""
    article_id, signal_id = contract.ref_article_id(decision.get("ref")), contract.ref_signal_id(decision.get("ref"))
    ref = (contract.article_ref(article_id) if article_id is not None
           else contract.signal_ref(signal_id) if signal_id is not None else None)
    if ref is None or (expected is not None and ref not in expected):
        return None, None
    return article_id, signal_id


def _usage(decision: dict[str, Any], expected: set[str] | None) -> dict[str, Any] | None:
    """Оплаченные вызовы модели — и с ответом не по правилам: модель за них уже взяла деньги. Цена — по ставкам
    ядра. Строка не о публикации задачи — не расход этой задачи."""
    article_id, signal_id = _publication(decision, expected)
    try:
        tokens_in = max(0, int(decision.get("input_tokens") or 0))
        tokens_out = max(0, int(decision.get("output_tokens") or 0))
    except (TypeError, ValueError):
        return None
    if (article_id is None and signal_id is None) or not tokens_in + tokens_out:
        return None
    model = _model_name(decision)
    price_in, price_out = config.price_for_model(model)
    return {"article_id": article_id, "signal_id": signal_id, "model": model, "input_tokens": tokens_in,
            "output_tokens": tokens_out, "cost_usd": round((tokens_in * price_in + tokens_out * price_out) / 1_000_000, 6)}


def _model_name(decision: dict[str, Any]) -> str | None:
    return str(decision.get("model") or "")[:100] or None
