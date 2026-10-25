"""Агент «Карточка сигнала» (#96, спека #93, ADR 0003): атрибуты карточки по тексту главной публикации.

Один вызов модели на сигнал, без инструментов и без выхода в сеть, — не автономный агент: путь
известен, обязательное проверяет код. Ядро собирает пакет при выдаче задачи (тексты, суть,
категория, справочники, тематики), воркер без базы зовёт модель со справочниками в каждом вызове и
возвращает ответы как есть, ядро проверяет их кодом и записывает: ответ воркера — недоверенный
ввод. Модель — явная настройка агента (SIGNAL_CARD_MODEL), без отката на модель ленты: едет в
задаче и попадает в рецепт карточки вместе с версией инструкции и справочников.

С #132 (спека #128) — основной вызов «Карточки» в конвейере «Отбора»: тема (одна из 13 тематик заказчика, три
кандидата по ключам — signal_themes), дата события с точностью (день, месяц или год). Связь с нефтесервисом и
применение ставит «Отбор», смешанность — тоже не «Карточка». На входе суть и категория, решения, причины и
обоснования «Отбора» нет. Дата события из текста старше OTBOR_OLD_EVENT_DAYS у принятой «Отбором» публикации
возвращает её в «Отбор» с этим фактом, один раз. Баллы — второй вызов (signal_scores).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable

from oiltech_digest import config, event_dates, signal_themes, word_forms
from oiltech_digest.db import repository, signal_card_store
from oiltech_digest.processing.external_ai import StopRequested
from oiltech_digest.processing.openai_client import output_budget
from oiltech_digest.processing.pipeline import make_client
from oiltech_digest.signal_card_pack import (CATEGORY_RU, CHOICE_DICTIONARIES, UNDEFINED, load_dictionaries,
                                             publication_day, publication_text)

KIND = signal_card_store.KIND
# Версия инструкции — в рецепте карточки: другая инструкция — другие карточки.
PROMPT_VERSION = "2026-10-25.1"
_OUTPUT_TOKENS = 1200
# Источник даты события — значения справочника date_source, по ним код решает, откуда дата (event_dates).
FROM_TEXT, BY_PUBLICATION = event_dates.FROM_TEXT, event_dates.BY_PUBLICATION
# Справочники, которые модель видит в каждом вызове: выбор значений и смысл даты.
_MODEL_DICTIONARIES = (*CHOICE_DICTIONARIES, "date_source")
# Правила из комментариев эксперта к разметке (решение 29 журнала гриля, 23.10) — в инструкции обоих вызовов
# «Карточки» (signal_scores).
EXPERT_RULES = """Правила эксперта:
- дата события — дата главного события публикации, а не упомянутых в ней прошлых этапов;
- «сегодня», «вчера», «на этой неделе» отсчитываются от даты публикации;
- дата из адреса ссылки и из метаданных страницы — не дата события;
- план, цель, прогноз, опцион и рамочное соглашение — не факт;
- проектный (расчётный) ресурс и заявление поставщика — не доказательство;
- рекорд компании — не новизна для отрасли;
- сумма договора — масштаб события, а не эффект для заказчика."""
# Поле карточки с выбором из справочника: колонка signals (она же параметр фильтра экрана, #97) → справочник,
# по нему же назван ключ в ответе модели. Зрелость — technology_maturity: maturity — решение радара.
CHOICE_COLUMNS = {"event_type": "event_type", "region": "region", "technology_class": "technology_class",
                  "technology_maturity": "maturity"}
# Оценка цены до вызова — для сухих прогонов экзамена и истории (#98): ответ карточки с рассуждением low
# — около 600 токенов, русский текст — около трёх знаков на токен (с запасом).
ESTIMATED_OUTPUT_TOKENS = 600
CHARS_PER_TOKEN = 3
# Типы источника-СМИ (по справочнику источников прода 18.10): издатель такого типа о событии только
# сообщает, и код вычёркивает его из участников. Остальные решает модель по инструкции: пустой тип — у
# находки поиска без статьи корпуса (часто это пресс-релиз самой компании), RSS — канал, а не вид
# издателя (компании и СМИ вперемешку), аналитики, госорганы и вузы публикуют и собственные события (#98).
_REPORTER_TYPES = frozenset({"media", "news", "journal", "telegram", "tech"})
# Потолки длины ответа воркера (недоверенный ввод); фраза — как у судьи радара.
_PHRASE_CHARS, _NAME_CHARS, _QUOTE_CHARS = 400, 200, 600


class CardSetupError(ValueError):
    """Агента нельзя поставить в работу: выключен флаг, не задана модель, у истории нет полосы пересчётов."""


# --- Постановка (ядро) ---------------------------------------------------------------------------


def enqueue_new_signals() -> dict[str, Any]:
    """Шаг планировщика «заполнить карточки»: новые сигналы без карточки — по полосе ежедневной
    обработки. За флагом SIGNAL_CARD_ENABLED."""
    if not config.SIGNAL_CARD_ENABLED:
        return {"signals": 0, "jobs": [], "reason": "disabled"}
    # Без модели шаг отказывает в каждом цикле, даже когда новых сигналов нет: настройка видна сразу.
    configured_model()
    ids = signal_card_store.new_signal_ids(days=config.SIGNAL_CARD_NEW_DAYS, limit=config.SIGNAL_CARD_STEP_LIMIT)
    return {"signals": len(ids), "jobs": enqueue_cards(ids, trigger="daily")}


def enqueue_cards(signal_ids: list[int], *, trigger: str, bulk: bool = False) -> list[dict[str, Any]]:
    """Задачи агента пачками по SIGNAL_CARD_BATCH. bulk — пересчёт (названные сигналы, история): по
    полосе пересчётов, чтобы не стоять перед потоком дня. Только при включённом флаге: до экзамена
    (#98) агент на проде не работает."""
    from oiltech_digest import network_policy

    if not config.SIGNAL_CARD_ENABLED:
        raise CardSetupError("SIGNAL_CARD_ENABLED выключен: агент карточки выходит в работу после экзамена (#98)")
    model = configured_model()
    decision = network_policy.route_ai_bulk() if bulk else network_policy.route_ai_processing()
    batch = config.SIGNAL_CARD_BATCH
    return [
        repository.create_background_job(
            KIND,
            {"signal_ids": signal_ids[start:start + batch], "model": model,
             "reasoning": config.SIGNAL_CARD_REASONING, "trigger": trigger},
            queue_name=decision.queue_name, execution_region=decision.execution_region,
            capability=decision.capability,
        )
        for start in range(0, len(signal_ids), batch)
    ]


def configured_model() -> str:
    if not config.SIGNAL_CARD_MODEL:
        raise CardSetupError("SIGNAL_CARD_MODEL не задан: модель ленты агент карточки не берёт")
    return config.SIGNAL_CARD_MODEL


def estimate(chars: int, calls: int, model: str) -> dict[str, Any]:
    """Цена вызовов до оплаты — для сухих прогонов экзамена и истории (#98): вход — по длине запросов в
    знаках (request_chars), ответ — ESTIMATED_OUTPUT_TOKENS на вызов, ставки ядра (config.price_for_model)."""
    tokens_in = -(-chars // CHARS_PER_TOKEN)
    tokens_out = ESTIMATED_OUTPUT_TOKENS * calls
    price_in, price_out = config.price_for_model(model)
    return {"model": model, "calls": calls, "input_tokens": tokens_in, "output_tokens": tokens_out,
            "cost_usd": (tokens_in * price_in + tokens_out * price_out) / 1_000_000}


# --- Пакет (ядро, при выдаче) --------------------------------------------------------------------


def build_payload(job_payload: dict[str, Any]) -> dict[str, Any]:
    """Самодостаточный пакет для воркера без базы: публикации с сутью и категорией, справочники, имена 13 тематик и
    у каждой публикации — темы-кандидаты по ключам (#132)."""
    tags = repository.list_enabled_tags()
    signals = []
    for row in signal_card_store.card_inputs(job_payload.get("signal_ids") or []):
        publication = _publication(row)
        text = " ".join([publication["title"], publication["summary"], publication["text"]])
        signals.append({**{key: value for key, value in publication.items() if key != "received"},
                        "published": publication["published"].isoformat() if publication["published"] else None,
                        "theme_candidates": signal_themes.candidates(text, tags)})
    return {
        "kind": KIND, "model": job_payload.get("model"), "reasoning": job_payload.get("reasoning"),
        "dictionaries": load_dictionaries(), "themes": signal_themes.names(tags), "signals": signals,
    }


def _publication(row: dict[str, Any]) -> dict[str, Any]:
    """Главная публикация сигнала так, как её видит модель, — по ней же ядро сверяет ответ: тот же
    текст (publication_text), издатель, дата публикации днём по UTC, суть сигнала. Дата поступления — для
    возраста события, когда даты публикации нет (#132); модель её не видит."""
    published, received = publication_day(row.get("published")), publication_day(row.get("received"))
    return {
        "signal_id": int(row["signal_id"]),
        "publication": row["publication"],
        "title": publication_text(row.get("title"))[:500],
        "text": publication_text(row.get("text")),
        "published": date.fromisoformat(published) if published else None,
        "received": date.fromisoformat(received) if received else None,
        "publisher": row.get("publisher") or "",
        "publisher_type": row.get("publisher_type") or "",
        "summary": publication_text(row.get("summary")),
        # Без категории — находка радара до 29.09: радар показывает её как технологию.
        "category": CATEGORY_RU.get(row.get("category") or "technology", UNDEFINED),
    }


# --- Вызов модели (воркер, без базы) -------------------------------------------------------------


def process_payload(payload: dict[str, Any], heartbeat: Callable[..., None] | None = None) -> dict[str, Any]:
    """Сторона воркера: один вызов модели на сигнал, справочники — в каждом. Ответ модели — как есть."""
    model = str(payload.get("model") or "").strip()
    if not model:
        # Клиент без модели взял бы OPENAI_MODEL ленты — тихий откат, которого быть не должно.
        raise ValueError("signal_card: в задаче нет модели (SIGNAL_CARD_MODEL на ядре) — модель ленты агент не берёт")
    dictionaries = payload.get("dictionaries") or {}
    themes = list(payload.get("themes") or [])
    instructions = _instructions(dictionaries, themes)
    schema = _answer_schema(dictionaries, themes)
    client = make_client()
    cards: list[dict[str, Any]] = []
    result = {"signal_card": True, "kind": KIND, "prompt_version": PROMPT_VERSION,
              "dictionaries_version": dictionaries.get("version"), "cards": cards}
    for item in payload.get("signals") or []:
        if heartbeat:
            try:
                heartbeat(result)
            except StopRequested:
                # Остановка воркера: сделанное уходит ядру частичным итогом, остальное — в очередь.
                result["partial"] = True
                break
        card = {"signal_id": item["signal_id"], "publication": item.get("publication")}
        try:
            response = client.complete_json(
                instructions, _model_input(item), schema,
                max_output_tokens=output_budget(_OUTPUT_TOKENS, payload.get("reasoning")),
                model=model, reasoning_effort=payload.get("reasoning"),
            )
        except Exception as exc:  # noqa: BLE001 - один сигнал не роняет пачку
            cards.append({**card, "error": str(exc)[:300]})
            continue
        cards.append({**card, "answer": response.data, "model": response.model,
                      "input_tokens": response.input_tokens, "output_tokens": response.output_tokens})
    return result


_TASK = """Ты заполняешь карточку сигнала — события на рынке нефтесервиса или смежных отраслей — по
тексту ОДНОЙ публикации. Опирайся только на вход; чего в нём нет — «не определено». Не выдумывай
компаний, дат и фактов.

Поля ответа:
- event_date и event_date_source — дата главного события публикации (подписание, запуск, испытание,
  объявление). Названа в тексте или однозначно из него следует — event_date_source = «из текста», а
  event_date — с той точностью, что в тексте: ГГГГ-ММ-ДД; известен только месяц — ГГГГ-ММ; только
  год — ГГГГ. День и месяц, которых в тексте нет, не подставляй. В тексте даты нет — event_date пусто,
  event_date_source = «по дате публикации». Понять нельзя — event_date пусто, event_date_source =
  «не определено».
- companies — компании-участники события: кто внедряет, поставляет, разрабатывает, покупает,
  продаёт, инвестирует. Издание, которое о событии сообщает, и компании, упомянутые мимоходом, — не
  участники; компания, которая сама сообщает о своём событии, — участник. Для каждой: name —
  название компании; quote — ДОСЛОВНЫЙ фрагмент текста (без изменений, до 30 слов), где она названа
  участником. Участников нет — пустой список.
- event_type, region, technology_class, maturity — строго одно значение из справочника ниже,
  буквально. Не подходит ни одно — «не определено».
- theme — тематика заказчика, к которой событие относится по содержанию: строго одна из списка ниже,
  буквально. «Темы-кандидаты по ключам» во входе — совпадения слов, подсказка, а не ответ. Не подходит
  ни одна — «не определено».
- why_important — одной фразой по-русски, почему сигнал важен нефтесервису."""


def _instructions(dictionaries: dict[str, Any], themes: list[str]) -> str:
    lines = [_TASK, "", EXPERT_RULES, "", f"Справочники (версия {dictionaries.get('version')}):"]
    for field in _MODEL_DICTIONARIES:
        block = dictionaries.get(field) or {}
        rule = f" ({block['rule']})" if block.get("rule") else ""
        lines.append(f"- {field} — {block.get('title', field)}{rule}: " + "; ".join(_values(dictionaries, field)))
    lines.append("- theme — тематики заказчика: " + "; ".join(themes))
    return "\n".join(lines)


def _answer_schema(dictionaries: dict[str, Any], themes: list[str]) -> dict[str, Any]:
    """Схема ответа модели (строгий JSON) — по ней же ядро проверяет ответ (_shape_problem)."""
    def enum(field: str) -> dict[str, Any]:
        return {"type": "string", "enum": [*_values(dictionaries, field), UNDEFINED]}

    properties: dict[str, Any] = {
        "event_date": {"type": "string"},
        "event_date_source": enum("date_source"),
        "companies": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "quote"],
            "properties": {"name": {"type": "string"}, "quote": {"type": "string"}},
        }},
        **{field: enum(field) for field in CHOICE_DICTIONARIES},
        "theme": {"type": "string", "enum": [*themes, UNDEFINED]},
        "why_important": {"type": "string"},
    }
    return {"name": "signal_card", "schema": {
        "type": "object", "additionalProperties": False, "required": list(properties), "properties": properties,
    }}


def request_chars(item: dict[str, Any], dictionaries: dict[str, Any], themes: list[str]) -> int:
    """Длина запроса модели в знаках — инструкция со справочниками и вход сигнала: для оценки цены до вызова."""
    return len(_instructions(dictionaries, themes)) + len(_model_input(item))


def _model_input(item: dict[str, Any]) -> str:
    return "\n".join([
        f"Заголовок: {item.get('title') or ''}",
        f"Издатель: {item.get('publisher') or 'не указан'}",
        f"Дата публикации: {item.get('published') or 'не указана'}",
        f"Категория: {item.get('category') or UNDEFINED}",
        f"Суть: {item.get('summary') or 'не указана'}",
        "Темы-кандидаты по ключам: " + ("; ".join(item.get("theme_candidates") or []) or "нет"),
        "Текст публикации:",
        item.get("text") or "",
    ])


# --- Применение (ядро) ---------------------------------------------------------------------------


def apply_result(result: dict[str, Any], *, job_id: int | None = None,
                 signal_ids: list[Any] | None = None) -> dict[str, Any]:
    """Сторона ядра: проверить ответы кодом и записать карточки с рецептом. Ответ не по схеме не
    пишется: ошибка — в итоге задачи, остальные карточки пачки пишутся. signal_ids — сигналы задачи:
    карточку другого сигнала итог воркера не пишет. Дата события из текста старше OTBOR_OLD_EVENT_DAYS
    возвращает принятую «Отбором» публикацию в «Отбор» (#132)."""
    dictionaries = load_dictionaries()
    themes = signal_themes.names(repository.list_enabled_tags())
    cards = [card for card in result.get("cards") or [] if isinstance(card, dict)]
    ids = [signal_id for signal_id in map(_signal_id, cards) if signal_id is not None]
    publications = {int(row["signal_id"]): _publication(row) for row in signal_card_store.card_inputs(ids)}
    expected = None if signal_ids is None else {str(value) for value in signal_ids}
    signal_card_store.record_usage(job_id, [usage for card in cards if (usage := _usage(card, expected))])
    if result.get("dictionaries_version") != dictionaries.get("version"):
        # Модель выбирала из других справочников: рецепт солгал бы, а значения сверены не с теми.
        stale = (f"справочники сменились после выдачи задачи ({result.get('dictionaries_version')} → "
                 f"{dictionaries.get('version')}): карточка не записана")
        return {"applied": 0, "errors": [{"signal_id": _signal_id(card), "error": stale} for card in cards]}
    applied = 0
    errors: list[dict[str, Any]] = []
    returned: list[tuple[str, int]] = []
    for card in cards:
        signal_id = _signal_id(card)
        problem = _publication_problem(card, signal_id, expected, publications)
        values = None
        if problem is None:
            values, problem = card_values(card, publications[signal_id], dictionaries, themes)
        if values is None:
            errors.append({"signal_id": signal_id, "error": problem})
            continue
        recipe = {
            "agent": KIND, "prompt_version": str(result.get("prompt_version") or "")[:50] or None,
            "model": _model_name(card), "dictionaries_version": dictionaries.get("version"),
            "filled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "job_id": job_id,
        }
        if signal_card_store.write_card(signal_id, values, recipe=recipe):
            applied += 1
            if (back := _return_old_event(signal_id, values, publications[signal_id])) is not None:
                returned.append(back)
    if returned:
        _enqueue_returned(returned)
    return {"applied": applied, "errors": errors, "returned_to_otbor": len(returned)}


# --- Возврат в «Отбор» (#132) --------------------------------------------------------------------


def _return_old_event(signal_id: int, values: dict[str, Any], publication: dict[str, Any]) -> tuple[str, int] | None:
    """Вернуть принятую «Отбором» публикацию в «Отбор», если «Карточка» нашла в тексте старое событие: решение
    снимается, факт ложится во вход «Отбора». Один раз; решение человека и выбор «в дайджест» не трогаются
    (signal_card_store.return_to_otbor). Без «Отбора» (флаг, модель) возврат не делается: решать было бы некому."""
    fact = event_dates.old_event_fact(values, publication["published"], publication["received"],
                                      config.OTBOR_OLD_EVENT_DAYS)
    if fact is None or not (config.OTBOR_ENABLED and config.OTBOR_MODEL):
        return None
    step = {"stage": "«Карточка»",
            "text": f"Старое событие: {fact}, старше {config.OTBOR_OLD_EVENT_DAYS} дней: возврат в «Отбор»"}
    return signal_card_store.return_to_otbor(signal_id, fact, log=[step])


def _enqueue_returned(returned: list[tuple[str, int]]) -> None:
    """Задачи «Отбора» о возвращённых публикациях — сразу, по полосе пересчётов: окно шага планировщика старую статью
    уже не берёт."""
    from oiltech_digest import otbor

    otbor.enqueue([ref for kind, ref in returned if kind == "article"],
                  signal_ids=[ref for kind, ref in returned if kind == "signal"], trigger="card_return", bulk=True)


def _in_job(signal_id: int | None, expected: set[str] | None) -> bool:
    return signal_id is not None and (expected is None or str(signal_id) in expected)


def _publication_problem(card: dict[str, Any], signal_id: int | None, expected: set[str] | None,
                         publications: dict[int, dict[str, Any]]) -> str | None:
    """Почему строку итога воркера не с чем сверить: сигнал чужой, главной публикации нет или она сменилась;
    None — можно сверять ответ (card_values)."""
    if not _in_job(signal_id, expected):
        return "сигнала нет в этой задаче"
    publication = publications.get(signal_id)
    if publication is None:
        return "нет сигнала или его главной публикации"
    if card.get("publication") != publication["publication"]:
        # Цитаты сверяются с текстом, который видела модель, — иначе компании отсеялись бы зря.
        return "главная публикация сменилась после выдачи задачи"
    return None


def card_values(card: dict[str, Any], publication: dict[str, Any], dictionaries: dict[str, Any],
                themes: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    """Строка итога воркера → значения карточки или почему карточки не будет (ошибка модели, ответ не по
    схеме). Через неё пишет ядро (apply_result) и мерит экзамен (#98): мерится то, что дойдёт до карточки."""
    problem = _answer_problem(card, _answer_schema(dictionaries, themes))
    if problem:
        return None, problem
    return check_answer(card["answer"], publication, dictionaries, themes), None


def _answer_problem(card: dict[str, Any], schema: dict[str, Any]) -> str | None:
    if card.get("error"):
        return f"модель: {str(card['error'])[:300]}"
    problem = _shape_problem(card.get("answer"), schema)
    return f"ответ не по схеме: {problem}" if problem else None


def _shape_problem(answer: Any, schema: dict[str, Any]) -> str | None:
    """Чем ответ не совпал со схемой агента; None — совпал. Значения справочников здесь не
    проверяются: чужое значение — «не определено», а не брак ответа."""
    if not isinstance(answer, dict):
        return "не объект"
    for field, spec in schema["schema"]["properties"].items():
        value = answer.get(field)
        if spec["type"] == "string" and not isinstance(value, str):
            return f"нет строки {field}"
        if spec["type"] == "array":
            required = spec["items"]["required"]
            if not isinstance(value, list) or not all(
                isinstance(item, dict) and all(isinstance(item.get(key), str) for key in required) for item in value
            ):
                return f"{field} — не список объектов с полями {', '.join(required)}"
    return None


def _usage(card: dict[str, Any], expected: set[str] | None) -> dict[str, Any] | None:
    """Оплаченный вызов модели — и с ответом не по схеме: модель за него уже взяла деньги. Цена — по
    ставкам ядра, как у радара (repository.record_radar_ai_usage). Строка чужого сигнала — не расход
    этой задачи."""
    signal_id = _signal_id(card)
    try:
        tokens_in = max(0, int(card.get("input_tokens") or 0))
        tokens_out = max(0, int(card.get("output_tokens") or 0))
    except (TypeError, ValueError):
        return None
    if not _in_job(signal_id, expected) or not tokens_in + tokens_out:
        return None
    model = _model_name(card)
    price_in, price_out = config.price_for_model(model)
    return {"signal_id": signal_id, "model": model, "input_tokens": tokens_in, "output_tokens": tokens_out,
            "cost_usd": round((tokens_in * price_in + tokens_out * price_out) / 1_000_000, 6)}


def _signal_id(card: dict[str, Any]) -> int | None:
    try:
        return int(card.get("signal_id"))
    except (TypeError, ValueError):
        return None


def _model_name(card: dict[str, Any]) -> str | None:
    return str(card.get("model") or "")[:100] or None


def check_answer(answer: dict[str, Any], publication: dict[str, Any], dictionaries: dict[str, Any],
                 themes: list[str]) -> dict[str, Any]:
    """Ответ модели → значения карточки. Значение не из справочника — «не определено»; компания без
    цитаты из текста публикации и издание-издатель отбрасываются, написания одной компании (word_forms.Companies)
    сводятся к одному участнику. Тема не из 13 тематик — не тема: None, карточка её не пишет."""
    source = _listed(dictionaries, "date_source", answer["event_date_source"])
    event_date, source, precision = event_dates.from_answer(answer["event_date"], source, publication["published"],
                                                            UNDEFINED)
    return {
        "event_date": event_date,
        "event_date_source": source,
        "event_date_precision": precision,
        "participants_json": _participants(answer["companies"], publication, dictionaries),
        **{column: _listed(dictionaries, key, answer[key]) for column, key in CHOICE_COLUMNS.items()},
        "theme": next((name for name in themes if _plain(name) == _plain(answer["theme"])), None),
        # «Почему важно нефтесервису» — в прежнем поле судьи «почему интересно».
        "why_interesting": _phrase(answer["why_important"]),
    }


def _participants(companies: list[dict[str, str]], publication: dict[str, Any],
                  dictionaries: dict[str, Any]) -> list[dict[str, Any]]:
    """Участник — только с дословной цитатой из текста публикации, в которой он назван под любым своим
    написанием (verified — проверено кодом): цитата, где компании нет, — догадка модели, а не участник, как и
    цитата, где её имя стоит только внутри имени другой компании справочника («Газпром» в «Газпром нефть»), и
    обрывок слова или имени текста («Газпром» из «Газпромнефть-Хантос» или из «Газпром нефть»).
    Издатель-СМИ (_REPORTER_TYPES) о событии сообщает — участником не считается; компания, которая
    сама публикует свою новость, — считается: в её пресс-релизе она главный участник. Одна ли это компания —
    по правилу word_forms.Companies: «PDO signed» называет «Petroleum Development Oman (PDO)», пояснение в скобках
    в имя не входит, «Equinor ASA» — это «Equinor» справочника; повтор под другим написанием отбрасывается, и
    издатель — под любым написанием. Имя участника — имя записи справочника, вне справочника — как дала модель."""
    entries = dictionaries.get("companies") or []
    rule = word_forms.companies(entries)
    text = word_forms.quote_form(publication["text"])
    reporter = (publication["publisher"] or "") if _plain(publication["publisher_type"]) in _REPORTER_TYPES else ""
    kept: list[dict[str, Any]] = []
    for company in companies:
        given = " ".join(company["name"].split())[:_NAME_CHARS]
        name = rule.canonical(given) or given
        quote = " ".join(company["quote"].split())[:_QUOTE_CHARS]
        spelled = [" ".join(form) for form in rule.forms(given)]
        if not name or not quote or not word_forms.named_in_quote(quote, text, spelled, _other_names(entries, name)):
            continue
        if rule.same(name, reporter) or any(rule.same(name, other["name"]) for other in kept):
            continue
        kept.append({"name": name, "quote": quote, "verified": True})
    return kept


def _other_names(entries: list[dict[str, Any]], name: str) -> list[str]:
    """Имена всех компаний справочника, кроме этой (name — её имя в справочнике или имя модели вне его)."""
    return [other for entry in entries if entry["name"] != name for other in (entry["name"], *entry["aliases"])]


def _phrase(value: str) -> str | None:
    return " ".join(value.split())[:_PHRASE_CHARS] or None


def _values(dictionaries: dict[str, Any], field: str) -> list[str]:
    return list((dictionaries.get(field) or {}).get("values") or [])


def _listed(dictionaries: dict[str, Any], field: str, value: str) -> str:
    """Значение справочника, как оно записано в справочнике; регистр, «ё» и пробелы — не расхождение.
    Не нашлось — «не определено»."""
    wanted = _plain(value)
    return next((item for item in _values(dictionaries, field) if _plain(item) == wanted), UNDEFINED)


def _plain(value: str) -> str:
    return " ".join(str(value or "").replace("ё", "е").replace("Ё", "Е").casefold().split())
