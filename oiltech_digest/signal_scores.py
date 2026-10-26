"""Второй вызов агента «Карточка» (#132, спека #128): баллы сигнала по критериям профиля его категории.

Один вызов модели на сигнал, без инструментов и без выхода в сеть. Ядро собирает пакет при выдаче задачи: публикация,
поля по договорённости (категория, отрасль, связь и применение, дата события и стадия зрелости из карточки, суть) и
критерии профиля с линейками; воркер без базы зовёт модель и возвращает ответы как есть; ядро проверяет их кодом
(score_values) и пишет: ответ воркера — недоверенный ввод. Модель ставит баллы от 0 до 100 по линейкам; «Свежесть»
считает код, «Зрелость / доказательность» код держит в ступени стадии карточки, итог по весам экрана «Скоринг»,
ступень и «Решение платформы» — тоже код (signal_scoring, repository.platform_decision_sql). Весов модель не видит:
правка весов не должна менять её оценки (ADR 0002, п. 7). Профиль — по категории: бизнес оценивается набором
business, технология — tech_radar; «другое» не оценивается. Модель — явная настройка SIGNAL_SCORES_MODEL, без отката
на модель ленты и основного вызова: едет в задаче и попадает в рецепт баллов.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable

from oiltech_digest import config, event_dates, signal_scoring
from oiltech_digest.db import repository, signal_scores_store
from oiltech_digest.otbor_answer import code_label
from oiltech_digest.otbor_pack_format import load_dictionaries as otbor_dictionaries
from oiltech_digest.otbor_pack_format import load_rubrics
from oiltech_digest.processing.external_ai import InvalidJobPayload, StopRequested
from oiltech_digest.processing.openai_client import output_budget
from oiltech_digest.processing.pipeline import keyword_score_for_criterion, make_client
from oiltech_digest.scoring_profiles import criteria_snapshot, snapshot_texts
from oiltech_digest.signal_card import EXPERT_RULES
from oiltech_digest.signal_card_pack import CATEGORY_RU, UNDEFINED, publication_day, publication_text

KIND = signal_scores_store.KIND
# Версия инструкции — в рецепте баллов: другая инструкция — другие баллы.
PROMPT_VERSION = "2026-10-25.1"
# Шаг этапа в «Ходе решения» сигнала.
STAGE = "«Карточка», баллы"
_OUTPUT_TOKENS = 900
# Потолок длины «почему такой балл» (недоверенный ввод).
_WHY_CHARS = 300
_SCALE_LABELS = (("strong", "80 и выше"), ("useful", "65-79"), ("watch", "40-64"), ("weak", "ниже 40"))


class ScoresSetupError(ValueError):
    """Этап нельзя поставить в работу: выключен флаг «Отбора», не задана модель или границы ступеней неверны."""


# --- Постановка (ядро) ---------------------------------------------------------------------------


def scheduler_step() -> dict[str, Any]:
    """Шаг планировщика «баллы», за флагом OTBOR_ENABLED (этап конвейера «Отбора»): сигналы после основного вызова
    «Карточки» без баллов и с баллами, которые «пересчитываются», — в задачи по полосе ежедневной обработки."""
    if not config.OTBOR_ENABLED:
        return {"signals": 0, "jobs": [], "reason": "disabled"}
    # Без модели шаг отказывает в каждом цикле, даже когда сигналов нет: настройка видна сразу.
    configured_model()
    ids = signal_scores_store.new_signal_ids(days=config.OTBOR_NEW_DAYS, limit=config.OTBOR_STEP_LIMIT)
    return {"signals": len(ids), "jobs": enqueue(ids, trigger="daily")}


def enqueue_named(signal_ids: list[int]) -> dict[str, Any]:
    """Названные сигналы (повтор после сбоя модели) — по полосе пересчётов. Неоцениваемые (до «Отбора», отклонённые,
    «другое», без карточки) — мимо (skipped)."""
    scorable = signal_scores_store.scorable_ids(signal_ids)
    return {"signals": len(scorable), "skipped": [one for one in signal_ids if one not in scorable],
            "jobs": enqueue(scorable, trigger="manual", bulk=True)}


def enqueue(signal_ids: list[int], *, trigger: str, bulk: bool = False) -> list[dict[str, Any]]:
    """Задачи этапа пачками по OTBOR_BATCH; bulk — названные сигналы, по полосе пересчётов."""
    from oiltech_digest import network_policy

    if not config.OTBOR_ENABLED:
        raise ScoresSetupError("OTBOR_ENABLED выключен, а баллы «Карточки» идут этапом конвейера «Отбора»")
    model = configured_model()
    route = network_policy.route_ai_bulk() if bulk else network_policy.route_ai_processing()
    batch = config.OTBOR_BATCH
    return [
        repository.create_background_job(
            KIND, {"signal_ids": signal_ids[start:start + batch], "model": model,
                   "reasoning": config.SIGNAL_SCORES_REASONING, "trigger": trigger},
            queue_name=route.queue_name, execution_region=route.execution_region, capability=route.capability,
        )
        for start in range(0, len(signal_ids), batch)
    ]


def configured_model() -> str:
    """Модель этапа. Заодно проверяет границы ступеней: неверные исказили бы ступени или уронили бы запись после
    оплаченного вызова, поэтому этап с ними в работу не ставится."""
    if not config.SIGNAL_SCORES_MODEL:
        raise ScoresSetupError("SIGNAL_SCORES_MODEL не задан: модель ленты и основного вызова баллы не берут")
    problem = signal_scoring.bands_problem(config.SIGNAL_SCORE_BANDS)
    if problem:
        raise ScoresSetupError(f"SIGNAL_SCORE_BANDS {problem}")
    return config.SIGNAL_SCORES_MODEL


def wrong_block(signal_id: int, author: str | None) -> str | None:
    """«Не тот блок» (спека #128): категория сигнала после «Отбора» меняется сразу, баллы «пересчитываются», и
    перезапускается только второй вызов — потоком дня, чтобы человек дождался. Новая категория; None — сигнал этот
    этап не оценивает. Этап выключен — баллы пересчитает его шаг, когда его включат."""
    step = {"stage": "Человек", "text": f"«Не тот блок» ({author or 'без имени'}): категория сменилась, баллы "
                                        "считаются по профилю новой категории"}
    changed = signal_scores_store.wrong_block(signal_id, log=[step])
    if changed is None:
        return None
    category, card_done = changed
    if card_done:
        # Карточки ещё нет — баллы поставит шаг дня, когда она будет: на входе второго вызова её поля.
        try:
            enqueue([int(signal_id)], trigger="wrong_block")
        except ScoresSetupError:
            pass
    return category


# --- Пакет (ядро, при выдаче) --------------------------------------------------------------------


def build_payload(job_payload: dict[str, Any]) -> dict[str, Any]:
    """Самодостаточный пакет для воркера без базы: публикации с полями по договорённости и критерии профилей с
    линейками (без весов). Сумма весов профиля не 100 — задачу не выдаём: итог исказился бы (ADR 0002, п. 3). Снимок
    критериев (criteria_snapshot) воркер вернёт как есть: по нему ядро при записи узнает, что модель оценивала те же
    критерии, что стоят сейчас."""
    rubrics = load_rubrics()
    contexts = [context(row) for row in signal_scores_store.score_inputs(job_payload.get("signal_ids") or [])]
    profiles, snapshots = {}, {}
    for profile in sorted({one["profile"] for one in contexts}):
        criteria = repository.list_enabled_scoring_criteria(profile)
        total = sum(float(criterion["weight"]) for criterion in criteria)
        if round(total, 6) != 100:
            raise InvalidJobPayload(f"сумма весов профиля {profile} равна {total:g}, а не 100: баллы не считаются")
        profiles[profile] = model_criteria(criteria, rubrics, profile)
        snapshots[profile] = criteria_snapshot(criteria)
    return {"kind": KIND, "model": job_payload.get("model"), "reasoning": job_payload.get("reasoning"),
            "rubrics_version": rubrics["version"], "profiles": profiles, "criteria_snapshots": snapshots,
            "items": [payload_item(one) for one in contexts]}


def context(row: dict[str, Any]) -> dict[str, Any]:
    """Сигнал так, как его оценивает второй вызов и проверяет ядро (score_values): публикация, как у основного вызова
    (тот же текст, даты днём по UTC), и поля по договорённости; профиль — по категории."""
    published, received = publication_day(row.get("published")), publication_day(row.get("received"))
    return {
        "signal_id": int(row["signal_id"]), "publication": row["publication"],
        "profile": signal_scores_store.PROFILES[row["signal_category"]], "category": row["signal_category"],
        "title": publication_text(row.get("title"))[:500], "text": publication_text(row.get("text")),
        "publisher": row.get("publisher") or "", "summary": publication_text(row.get("summary")),
        "published": date.fromisoformat(published) if published else None,
        "received": date.fromisoformat(received) if received else None,
        "industry": row.get("industry"), "oilfield_relevance": row.get("oilfield_relevance"),
        "oilfield_application": row.get("oilfield_application"),
        "event_date": row.get("event_date"), "event_date_source": row.get("event_date_source"),
        "event_date_precision": row.get("event_date_precision"), "stage": row.get("technology_maturity"),
    }


def payload_item(item: dict[str, Any]) -> dict[str, Any]:
    """Сигнал во входе модели (context → пакет воркера): подписи вместо кодов, дата события — только из текста (дата
    публикации вместо неё не подставляется). Той же функцией вход строит экзамен (#133): мерится тот же вход."""
    labels = otbor_dictionaries()
    from_text = item["event_date_source"] == event_dates.FROM_TEXT and item["event_date"] is not None
    return {
        "signal_id": item["signal_id"], "publication": item["publication"], "profile": item["profile"],
        "title": item["title"], "text": item["text"], "publisher": item["publisher"], "summary": item["summary"],
        "published": item["published"].isoformat() if item["published"] else None,
        "category": CATEGORY_RU.get(item["category"], UNDEFINED),
        "industry": code_label(labels, "industry", item["industry"]) or UNDEFINED,
        "oilfield_relevance": code_label(labels, "oilfield_relevance", item["oilfield_relevance"]) or UNDEFINED,
        "oilfield_application": item["oilfield_application"] or "",
        "event_date": event_dates.label(item["event_date"], item["event_date_precision"]) if from_text else None,
        "stage": item["stage"] or UNDEFINED,
    }


def model_criteria(criteria: list[dict[str, Any]], rubrics: dict[str, Any], profile: str) -> list[dict[str, Any]]:
    """Критерии, которые оценивает модель: имя и линейка (без веса). Описание с экрана «Скоринг» — только у критерия
    без линейки: у набора заказчика оно черновое и может ей противоречить («Достоверность и актуальность» там с
    «свежестью события», а линейка возраст оценивать запрещает). «Свежести» здесь нет — её считает код; у «Зрелости» —
    как её ступень задаёт стадия карточки."""
    by_name = {one["name"]: one for one in rubrics["profiles"][profile]}
    found = []
    for criterion in criteria:
        rubric = by_name.get(criterion["name"]) or {}
        if signal_scoring.computed(rubric):
            continue
        found.append({"name": criterion["name"], "description": "" if rubric else criterion.get("description") or "",
                      "note": rubric.get("note") or "", "bands": rubric.get("bands") or {},
                      "stage_bands": rubric.get("stage_bands") if signal_scoring.bounded(rubric) else None})
    return found


# --- Вызов модели (воркер, без базы) -------------------------------------------------------------


def process_payload(payload: dict[str, Any], heartbeat: Callable[..., None] | None = None) -> dict[str, Any]:
    """Сторона воркера: вызов модели на сигнал по критериям его профиля; ответ модели — как есть."""
    model = str(payload.get("model") or "").strip()
    if not model:
        # Клиент без модели взял бы OPENAI_MODEL ленты — тихий откат, которого быть не должно.
        raise ValueError("signal_scores: в задаче нет модели (SIGNAL_SCORES_MODEL на ядре), а модель ленты этап "
                         "не берёт")
    profiles = payload.get("profiles") or {}
    client = make_client()
    rows: list[dict[str, Any]] = []
    result = {"signal_scores": True, "kind": KIND, "prompt_version": PROMPT_VERSION,
              "rubrics_version": payload.get("rubrics_version"),
              "criteria_snapshots": payload.get("criteria_snapshots"), "scores": rows}
    for item in payload.get("items") or []:
        if heartbeat:
            try:
                heartbeat(result)
            except StopRequested:
                # Остановка воркера: сделанное уходит ядру частичным итогом, остальное — в очередь.
                result["partial"] = True
                break
        criteria = profiles.get(item.get("profile")) or []
        row = {"signal_id": item.get("signal_id"), "profile": item.get("profile"), "publication": item.get("publication")}
        try:
            response = client.complete_json(
                instructions(criteria), model_input(item), answer_schema(criteria),
                max_output_tokens=output_budget(_OUTPUT_TOKENS, payload.get("reasoning")),
                model=model, reasoning_effort=payload.get("reasoning"),
            )
        except Exception as exc:  # noqa: BLE001 - один сигнал не роняет пачку
            rows.append({**row, "error": str(exc)[:300]})
            continue
        rows.append({**row, "answer": response.data, "model": response.model,
                     "input_tokens": response.input_tokens, "output_tokens": response.output_tokens})
    return result


_TASK = """Ты оцениваешь сигнал рынка нефтесервиса по критериям ниже — по тексту ОДНОЙ публикации и полям её
карточки. Опирайся только на вход, не выдумывай фактов.

Каждому критерию — балл от 0 до 100 по его линейке: ступень (80 и выше, 65-79, 40-64, ниже 40) — по правилу ступени,
внутри ступени выше то, что подтверждено цифрами, названными участниками и независимыми источниками. why — коротко
по-русски, почему такой балл; что линейка велит писать в «почему» раздельно (масштаб и эффект), пиши раздельно."""


def instructions(criteria: list[dict[str, Any]]) -> str:
    lines = [_TASK, "", EXPERT_RULES, "", "Критерии:"]
    for number, criterion in enumerate(criteria, start=1):
        lines.append(f"{number}. {criterion['name']}. {criterion['description']} {criterion['note']}".rstrip())
        for code, scale in _SCALE_LABELS:
            band = criterion["bands"].get(code) or {}
            example = f" Пример: {band['example']}." if band.get("example") else ""
            lines.append(f"   {scale}: {band.get('rule', '')}{example}")
        if criterion.get("stage_bands"):
            stages = "; ".join(f"{stage} — {dict(_SCALE_LABELS)[band]}" for stage, band in criterion["stage_bands"].items())
            lines.append(f"   Ступень задаёт «Стадия зрелости» во входе: {stages}. Внедрение — 80 и выше, только если "
                         "в тексте назван заказчик, у которого технология внедрена; иначе это запуск продукта: 40-64. "
                         "named_customer — назван ли такой заказчик.")
    return "\n".join(lines)


def answer_schema(criteria: list[dict[str, Any]]) -> dict[str, Any]:
    """Схема ответа модели (строгий JSON): балл и «почему» по каждому критерию; при «Зрелости» — назван ли заказчик
    внедрения. По ней же ядро проверяет ответ (score_values)."""
    properties: dict[str, Any] = {"scores": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["criterion", "score", "why"],
        "properties": {"criterion": {"type": "string", "enum": [criterion["name"] for criterion in criteria]},
                       "score": {"type": "integer", "minimum": 0, "maximum": 100}, "why": {"type": "string"}},
    }}}
    if any(criterion.get("stage_bands") for criterion in criteria):
        properties["named_customer"] = {"type": "boolean"}
    return {"name": "signal_scores", "schema": {
        "type": "object", "additionalProperties": False, "required": list(properties), "properties": properties,
    }}


def model_input(item: dict[str, Any]) -> str:
    """Вход модели о сигнале: публикация и поля по договорённости; рассуждений других агентов в нём нет."""
    application = item.get("oilfield_application")
    return "\n".join([
        f"Заголовок: {item.get('title') or ''}",
        f"Издатель: {item.get('publisher') or 'не указан'}",
        f"Дата публикации: {item.get('published') or 'не указана'}",
        f"Категория: {item.get('category') or UNDEFINED}",
        f"Отрасль: {item.get('industry') or UNDEFINED}",
        f"Связь с нефтесервисом: {item.get('oilfield_relevance') or UNDEFINED}",
        *([f"Применение в нефтесервисе: {application}"] if application else []),
        f"Дата события из текста: {item.get('event_date') or 'не названа'}",
        f"Стадия зрелости: {item.get('stage') or UNDEFINED}",
        f"Суть: {item.get('summary') or 'не указана'}",
        "Текст публикации:",
        item.get("text") or "",
    ])


# --- Применение (ядро) ---------------------------------------------------------------------------


def apply_result(result: dict[str, Any], *, job_id: int | None = None,
                 signal_ids: list[Any] | None = None) -> dict[str, Any]:
    """Сторона ядра: проверить ответы кодом (score_values) и записать баллы с рецептом. Ответ не по правилам не
    пишется: ошибка — в итоге задачи, остальные сигналы пачки пишутся. Строка чужого сигнала не пишется; сигнал,
    который этап больше не оценивает или у которого сменились категория или главная публикация, — тоже: баллы
    пересчитает следующая задача. Так же с критериями профиля, которые сменились после выдачи (имя, описание, ключевые
    слова, набор): модель оценивала прежние. Правка одних весов запись не держит: итог считает ядро по нынешним весам
    и пишет их в снимок (ADR 0002, п. 7)."""
    rubrics = load_rubrics()
    rows = [row for row in result.get("scores") or [] if isinstance(row, dict)]
    expected = None if signal_ids is None else {str(value) for value in signal_ids}
    ids = [signal_id for row in rows if (signal_id := _signal_id(row, expected)) is not None]
    contexts = {one["signal_id"]: one for one in map(context, signal_scores_store.score_inputs(ids))}
    signal_scores_store.record_usage(job_id, [usage for row in rows if (usage := _usage(row, expected))])
    if result.get("rubrics_version") != rubrics["version"]:
        # Модель ставила баллы по другим линейкам: рецепт солгал бы.
        stale = (f"линейки сменились после выдачи задачи ({str(result.get('rubrics_version'))[:50]} → "
                 f"{rubrics['version']}): баллы не записаны")
        return {"written": 0, "errors": [{"signal_id": row.get("signal_id"), "error": stale} for row in rows]}
    criteria = {profile: repository.list_enabled_scoring_criteria(profile)
                for profile in signal_scores_store.PROFILES.values()}
    sent = result.get("criteria_snapshots") if isinstance(result.get("criteria_snapshots"), dict) else {}
    changed = {profile for profile, current in criteria.items()
               if snapshot_texts(sent.get(profile)) != snapshot_texts(criteria_snapshot(current))}
    applied: dict[str, Any] = {"written": 0, "errors": []}
    for row in rows:
        signal_id = _signal_id(row, expected)
        item = contexts.get(signal_id) if signal_id is not None else None
        problem = _context_problem(row, signal_id, item)
        if problem is None and item["profile"] in changed:
            problem = ("критерии профиля сменились после выдачи задачи (экран «Скоринг»): баллы поставит следующая "
                       "задача")
        values = None
        if problem is None:
            values, problem = score_values(row, item, criteria[item["profile"]])
        if values is None:
            applied["errors"].append({"signal_id": signal_id, "error": problem})
            continue
        recipe = {"agent": KIND, "prompt_version": str(result.get("prompt_version") or "")[:50] or None,
                  "model": _model_name(row), "rubrics_version": rubrics["version"], "totals": values["totals"],
                  "keywords": bool(config.SIGNAL_SCORES_KEYWORDS), "named_customer": values["named_customer"],
                  "filled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "job_id": job_id}
        if signal_scores_store.write_scores(signal_id, values, category=item["category"], recipe=recipe,
                                            log=[_step(values)]):
            applied["written"] += 1
    return applied


def _context_problem(row: dict[str, Any], signal_id: int | None, item: dict[str, Any] | None) -> str | None:
    if signal_id is None:
        return "сигнала нет в этой задаче"
    if item is None:
        return "сигнал больше не оценивается: решение, категория или дубль сменились после выдачи задачи"
    if row.get("profile") != item["profile"]:
        return "категория сменилась после выдачи задачи («не тот блок»): баллы пересчитает следующая задача"
    if row.get("publication") != item["publication"]:
        return "главная публикация сменилась после выдачи задачи"
    return None


def score_values(row: dict[str, Any], item: dict[str, Any],
                 criteria: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
    """Строка итога воркера → баллы сигнала или почему их не будет (ошибка модели, ответ не по правилам). Через неё
    пишет ядро (apply_result) и мерит экзамен (#133): мерится то, что дойдёт до сигнала. item — сигнал (context),
    criteria — критерии его профиля с весами и ключевыми словами (экран «Скоринг»). Линейки — из файла
    scoring_rubrics.json; вариант итога и границы ступеней — настройки SIGNAL_SCORES_KEYWORDS и SIGNAL_SCORE_BANDS."""
    if row.get("error"):
        return None, f"модель: {str(row['error'])[:300]}"
    rubrics = load_rubrics()
    scale = rubrics["bands"]
    by_name = {one["name"]: one for one in rubrics["profiles"][item["profile"]]}
    asked = model_criteria(criteria, rubrics, item["profile"])
    answer = row.get("answer")
    problem = _answer_problem(answer, asked)
    if problem:
        return None, f"ответ не по правилам: {problem}"
    given = {entry["criterion"]: entry for entry in answer["scores"]}
    article = {"title": item["title"], "summary": item["summary"], "raw_text": item["text"]}
    items = []
    for criterion in criteria:
        rubric = by_name.get(criterion["name"]) or {}
        excluded = None
        if signal_scoring.computed(rubric):
            score, excluded = signal_scoring.freshness(rubric, scale, item, item["published"], item["received"])
            keyword, why = None, excluded or "считает код по дате события из текста"
        else:
            entry = given[criterion["name"]]
            score = float(entry["score"])
            keyword, why = keyword_score_for_criterion(article, criterion), " ".join(entry["why"].split())[:_WHY_CHARS]
        # «Зрелость» — в ступени стадии карточки, и балл модели, и итог с ключевыми словами (signal_scoring.final_score).
        bounds = (signal_scoring.maturity_range(rubric, scale, item["stage"], answer.get("named_customer"))
                  if signal_scoring.bounded(rubric) else None)
        if bounds is not None:
            score = float(min(max(score, bounds[0]), bounds[1]))
        items.append({"criterion_id": int(criterion["id"]), "name": criterion["name"],
                      "weight": float(criterion["weight"]), "ai_score": score, "keyword_score": keyword,
                      "rationale": why, "excluded": excluded, "bounds": list(bounds) if bounds else None})
    for one in items:
        one["final_score"] = signal_scoring.final_score(one, keywords=bool(config.SIGNAL_SCORES_KEYWORDS))
    totals = signal_scoring.totals(items)
    score = signal_scoring.chosen(totals)
    return {"profile": item["profile"], "items": items, "totals": totals, "score": score,
            "band": signal_scoring.band(score), "criteria_snapshot": criteria_snapshot(criteria),
            "named_customer": answer.get("named_customer") if "named_customer" in answer else None}, None


def _answer_problem(answer: Any, asked: list[dict[str, Any]]) -> str | None:
    """Чем ответ не по правилам; None — по правилам: у каждого критерия ровно один балл (целое от 0 до 100) и
    «почему»; при «Зрелости» — назван ли заказчик (да или нет)."""
    if not isinstance(answer, dict) or not isinstance(answer.get("scores"), list):
        return "нет списка scores"
    names = [criterion["name"] for criterion in asked]
    seen: list[str] = []
    for entry in answer["scores"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("criterion"), str):
            return "строка scores без критерия"
        name, score = entry["criterion"], entry.get("score")
        if name not in names:
            return f"критерия «{name[:80]}» нет в профиле"
        if name in seen:
            return f"критерий «{name}» дважды"
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
            return f"балл критерия «{name}» не целое число от 0 до 100"
        if not isinstance(entry.get("why"), str):
            return f"у критерия «{name}» нет «почему»"
        seen.append(name)
    missing = [name for name in names if name not in seen]
    if missing:
        return "нет балла критериев " + ", ".join(f"«{name}»" for name in missing)
    if any(criterion["stage_bands"] for criterion in asked) and not isinstance(answer.get("named_customer"), bool):
        return "нет named_customer (назван ли заказчик внедрения)"
    return None


def _step(values: dict[str, Any]) -> dict[str, str]:
    """Шаг этапа в «Ходе решения»: итог, ступень и что не вошло в итог."""
    profile = {"business": "бизнес-сигналы", "tech_radar": "технологический радар"}[values["profile"]]
    left_out = [f"«{item['name']}» не входит: {item['excluded']}" for item in values["items"] if item["excluded"]]
    score = "нет" if values["score"] is None else f"{values['score']:g}"
    band = signal_scoring.BAND_LABELS.get(values["band"]) or "без ступени"
    return {"stage": STAGE, "text": f"Профиль «{profile}». Итог {score} ({band})."
                                    + (" " + "; ".join(left_out) + "." if left_out else "")}


def _signal_id(row: dict[str, Any], expected: set[str] | None) -> int | None:
    try:
        signal_id = int(row.get("signal_id"))
    except (TypeError, ValueError):
        return None
    return signal_id if expected is None or str(signal_id) in expected else None


def _model_name(row: dict[str, Any]) -> str | None:
    return str(row.get("model") or "")[:100] or None


def _usage(row: dict[str, Any], expected: set[str] | None) -> dict[str, Any] | None:
    """Оплаченный вызов модели — и с ответом не по правилам: модель за него уже взяла деньги. Цена — по ставкам ядра.
    Строка чужого сигнала — не расход этой задачи."""
    signal_id = _signal_id(row, expected)
    try:
        tokens_in = max(0, int(row.get("input_tokens") or 0))
        tokens_out = max(0, int(row.get("output_tokens") or 0))
    except (TypeError, ValueError):
        return None
    if signal_id is None or not tokens_in + tokens_out:
        return None
    model = _model_name(row)
    price_in, price_out = config.price_for_model(model)
    return {"signal_id": signal_id, "model": model, "input_tokens": tokens_in, "output_tokens": tokens_out,
            "cost_usd": round((tokens_in * price_in + tokens_out * price_out) / 1_000_000, 6)}
