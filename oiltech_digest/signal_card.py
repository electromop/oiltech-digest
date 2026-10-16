"""Агент «Карточка сигнала» (#96, спека #93, ADR 0003): атрибуты карточки по тексту главной публикации.

Один вызов модели на сигнал, без инструментов и без выхода в сеть, — не автономный агент: путь
известен, обязательное проверяет код. Ядро собирает пакет при выдаче задачи (тексты, тема,
категория, справочники), воркер без базы зовёт модель со справочниками в каждом вызове и
возвращает ответы как есть, ядро проверяет их кодом и записывает: ответ воркера — недоверенный
ввод. Модель — явная настройка агента (SIGNAL_CARD_MODEL), без отката на модель ленты: едет в
задаче и попадает в рецепт карточки вместе с версией инструкции и справочников.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import re
from typing import Any, Callable

from oiltech_digest import config
from oiltech_digest.db import repository, signal_cards
from oiltech_digest.processing.external_ai import StopRequested
from oiltech_digest.processing.openai_client import output_budget
from oiltech_digest.processing.pipeline import make_client
from oiltech_digest.signal_card_pack import UNDEFINED, load_dictionaries, publication_text

AGENT = signal_cards.AGENT_KIND
# Версия инструкции — в рецепте карточки: другая инструкция — другие карточки.
PROMPT_VERSION = "2026-10-16.1"
_OUTPUT_TOKENS = 1200
_CATEGORY_RU = {"technology": "технология", "business": "бизнес", "other": "другое"}
_RELEVANCE_CODES = {"прямая": "direct", "переносимая": "transferable", "нет связи": "none"}
_DICTIONARY_FIELDS = ("event_type", "region", "technology_class", "maturity")
_EARLIEST_EVENT = date(2000, 1, 1)


class CardSetupError(ValueError):
    """Агента нельзя поставить в работу: не задана модель."""


# --- Постановка (ядро) ---------------------------------------------------------------------------


def enqueue_new_signals() -> dict[str, Any]:
    """Шаг планировщика «заполнить карточки»: новые сигналы без карточки — по полосе ежедневной
    обработки. За флагом SIGNAL_CARD_ENABLED."""
    if not config.SIGNAL_CARD_ENABLED:
        return {"signals": 0, "jobs": [], "reason": "disabled"}
    _model()
    ids = signal_cards.new_signal_ids(days=config.SIGNAL_CARD_NEW_DAYS, limit=config.SIGNAL_CARD_STEP_LIMIT)
    return {"signals": len(ids), "jobs": enqueue_cards(ids, trigger="daily")}


def enqueue_cards(signal_ids: list[int], *, trigger: str, bulk: bool = False) -> list[dict[str, Any]]:
    """Задачи агента пачками по SIGNAL_CARD_BATCH. bulk — пересчёт (названные сигналы, история): по
    полосе пересчётов, чтобы не стоять перед потоком дня."""
    from oiltech_digest import network_policy

    model = _model()
    decision = network_policy.route_ai_bulk() if bulk else network_policy.route_ai_processing()
    batch = config.SIGNAL_CARD_BATCH
    return [
        repository.create_background_job(
            AGENT,
            {"signal_ids": signal_ids[start:start + batch], "model": model,
             "reasoning": config.SIGNAL_CARD_REASONING, "trigger": trigger},
            queue_name=decision.queue_name, execution_region=decision.execution_region,
            capability=decision.capability,
        )
        for start in range(0, len(signal_ids), batch)
    ]


def _model() -> str:
    if not config.SIGNAL_CARD_MODEL:
        raise CardSetupError("SIGNAL_CARD_MODEL не задан: модель ленты агент карточки не берёт")
    return config.SIGNAL_CARD_MODEL


# --- Пакет (ядро, при выдаче) --------------------------------------------------------------------


def build_payload(job_payload: dict[str, Any]) -> dict[str, Any]:
    """Самодостаточный пакет для воркера без базы: публикации, тема, категория, справочники."""
    signals = [
        {
            "signal_id": int(row["signal_id"]),
            "publication": row["publication"],
            "title": publication_text(row.get("title"))[:500],
            "text": publication_text(row.get("text")),
            "published": _day(row.get("published")),
            "publisher": row.get("publisher") or "",
            "theme": row.get("theme") or "",
            "category": _CATEGORY_RU.get(row.get("category") or "technology", UNDEFINED),
        }
        for row in signal_cards.card_inputs(job_payload.get("signal_ids") or [])
    ]
    return {
        "kind": AGENT, "model": job_payload.get("model"), "reasoning": job_payload.get("reasoning"),
        "dictionaries": load_dictionaries(), "signals": signals,
    }


def _day(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date().isoformat()
    return str(value)[:10]


# --- Вызов модели (воркер, без базы) -------------------------------------------------------------


def process_payload(payload: dict[str, Any], heartbeat: Callable[..., None] | None = None) -> dict[str, Any]:
    """Сторона воркера: один вызов модели на сигнал, справочники — в каждом. Ответ модели — как есть."""
    model = str(payload.get("model") or "").strip()
    if not model:
        # Клиент без модели взял бы OPENAI_MODEL ленты — тихий откат, которого быть не должно.
        raise ValueError("signal_card: в задаче нет модели (SIGNAL_CARD_MODEL на ядре) — модель ленты агент не берёт")
    dictionaries = payload.get("dictionaries") or {}
    instructions = _instructions(dictionaries)
    schema = _answer_schema(dictionaries)
    client = make_client()
    cards: list[dict[str, Any]] = []
    result = {"signal_card": True, "kind": AGENT, "prompt_version": PROMPT_VERSION,
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
- event_date и event_date_source — дата самого события (подписание, запуск, испытание, объявление).
  Названа в тексте или однозначно из него следует — event_date = ГГГГ-ММ-ДД, event_date_source =
  «из текста». В тексте даты нет — event_date пусто, event_date_source = «по дате публикации».
  Понять нельзя — event_date пусто, event_date_source = «не определено».
- companies — компании-участники события: кто внедряет, поставляет, разрабатывает, покупает,
  продаёт, инвестирует. Издатель публикации и компании, упомянутые мимоходом, — не участники. Для
  каждой: name — название компании; quote — ДОСЛОВНЫЙ фрагмент текста (без изменений, до 30 слов),
  где она названа участником. Участников нет — пустой список.
- event_type, region, technology_class, maturity — строго одно значение из справочника ниже,
  буквально. Не подходит ни одно — «не определено».
- oilfield_relevance — связь с нефтесервисом: «прямая»; «переносимая» — технология из другой
  отрасли (горнодобыча, производство, транспорт), применимая в нефтесервисе; «нет связи». Понять
  нельзя — «не определено».
- oilfield_application — при прямой и переносимой связи: одной фразой по-русски, где и как это
  применяется в нефтесервисе; иначе пусто.
- why_important — одной фразой по-русски, почему сигнал важен нефтесервису; при «нет связи» — пусто."""


def _instructions(dictionaries: dict[str, Any]) -> str:
    lines = [_TASK, "", f"Справочники (версия {dictionaries.get('version')}):"]
    for field in _DICTIONARY_FIELDS:
        block = dictionaries.get(field) or {}
        rule = f" ({block['rule']})" if block.get("rule") else ""
        lines.append(f"- {field} — {block.get('title', field)}{rule}: " + "; ".join(block.get("values") or []))
    return "\n".join(lines)


def _answer_schema(dictionaries: dict[str, Any]) -> dict[str, Any]:
    def choice(values: list[str]) -> dict[str, Any]:
        return {"type": "string", "enum": [*values, UNDEFINED]}

    properties: dict[str, Any] = {
        "event_date": {"type": "string"},
        "event_date_source": choice(list((dictionaries.get("date_source") or {}).get("values") or [])),
        "companies": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "quote"],
            "properties": {"name": {"type": "string"}, "quote": {"type": "string"}},
        }},
        **{field: choice(list((dictionaries.get(field) or {}).get("values") or [])) for field in _DICTIONARY_FIELDS},
        "oilfield_relevance": choice(list(_RELEVANCE_CODES)),
        "oilfield_application": {"type": "string"},
        "why_important": {"type": "string"},
    }
    return {"name": "signal_card", "schema": {
        "type": "object", "additionalProperties": False, "required": list(properties), "properties": properties,
    }}


def _model_input(item: dict[str, Any]) -> str:
    return "\n".join([
        f"Заголовок: {item.get('title') or ''}",
        f"Издатель: {item.get('publisher') or 'не указан'}",
        f"Дата публикации: {item.get('published') or 'не указана'}",
        f"Тема: {item.get('theme') or 'не указана'}",
        f"Категория: {item.get('category') or UNDEFINED}",
        "Текст публикации:",
        item.get("text") or "",
    ])


# --- Применение (ядро) ---------------------------------------------------------------------------


def apply_result(result: dict[str, Any], *, job_id: int | None = None) -> dict[str, Any]:
    """Сторона ядра: проверить ответы кодом и записать карточки с рецептом. Ответ не по схеме не
    пишется: ошибка — в итоге задачи, остальные карточки пачки пишутся."""
    dictionaries = load_dictionaries()
    cards = [card for card in result.get("cards") or [] if isinstance(card, dict)]
    ids = [signal_id for signal_id in map(_signal_id, cards) if signal_id is not None]
    publications = {int(row["signal_id"]): _publication(row) for row in signal_cards.card_inputs(ids)}
    applied = 0
    errors: list[dict[str, Any]] = []
    signal_cards.record_usage(job_id, [usage for card in cards if (usage := _usage(card))])
    if result.get("dictionaries_version") != dictionaries.get("version"):
        # Модель выбирала из других справочников: рецепт солгал бы, а значения сверены не с теми.
        stale = (f"справочники сменились после выдачи задачи ({result.get('dictionaries_version')} → "
                 f"{dictionaries.get('version')}): карточка не записана")
        return {"applied": 0, "errors": [{"signal_id": _signal_id(card), "error": stale} for card in cards]}
    for card in cards:
        signal_id = _signal_id(card)
        if card.get("error"):
            errors.append({"signal_id": signal_id, "error": f"модель: {str(card['error'])[:300]}"})
            continue
        problem = _shape_problem(card.get("answer"))
        if problem:
            errors.append({"signal_id": signal_id, "error": f"ответ не по схеме: {problem}"})
            continue
        publication = publications.get(signal_id)
        if publication is None:
            errors.append({"signal_id": signal_id, "error": "нет сигнала или его главной публикации"})
            continue
        if card.get("publication") != publication["key"]:
            # Цитаты сверяются с текстом, который видела модель, — иначе компании отсеялись бы зря.
            errors.append({"signal_id": signal_id, "error": "главная публикация сменилась после выдачи задачи"})
            continue
        values = check_answer(card["answer"], publication, dictionaries)
        recipe = {
            "agent": AGENT, "prompt_version": result.get("prompt_version"), "model": card.get("model"),
            "dictionaries_version": result.get("dictionaries_version"),
            "filled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "job_id": job_id,
        }
        if signal_cards.write_card(signal_id, values, recipe=recipe):
            applied += 1
    return {"applied": applied, "errors": errors}


def _usage(card: dict[str, Any]) -> dict[str, Any] | None:
    """Оплаченный вызов модели — и с ответом не по схеме: модель за него уже взяла деньги. Цена — по
    ставкам ядра, как у радара (repository.record_radar_ai_usage)."""
    signal_id = _signal_id(card)
    try:
        tokens_in, tokens_out = int(card.get("input_tokens") or 0), int(card.get("output_tokens") or 0)
    except (TypeError, ValueError):
        return None
    if signal_id is None or not tokens_in + tokens_out:
        return None
    model = str(card.get("model") or "")[:100] or None
    price_in, price_out = config.price_for_model(model)
    return {"signal_id": signal_id, "model": model, "input_tokens": tokens_in, "output_tokens": tokens_out,
            "cost_usd": round((tokens_in * price_in + tokens_out * price_out) / 1_000_000, 6)}


def _signal_id(card: dict[str, Any]) -> int | None:
    try:
        return int(card.get("signal_id"))
    except (TypeError, ValueError):
        return None


def _publication(row: dict[str, Any]) -> dict[str, Any]:
    """Что ядро сверяет с ответом: тот же текст, что ушёл модели (publication_text), издатель, дата."""
    published = _day(row.get("published"))
    return {"key": row["publication"], "text": publication_text(row.get("text")),
            "publisher": row.get("publisher") or "", "published": date.fromisoformat(published) if published else None}


_TEXT_FIELDS = ("event_date", "event_date_source", *_DICTIONARY_FIELDS, "oilfield_relevance",
                "oilfield_application", "why_important")


def _shape_problem(answer: Any) -> str | None:
    """Чем ответ не совпал со схемой агента; None — совпал. Значения справочников здесь не
    проверяются: чужое значение — «не определено», а не брак ответа."""
    if not isinstance(answer, dict):
        return "не объект"
    for field in _TEXT_FIELDS:
        if not isinstance(answer.get(field), str):
            return f"нет строки {field}"
    companies = answer.get("companies")
    if not isinstance(companies, list) or not all(
        isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("quote"), str)
        for item in companies
    ):
        return "companies — не список компаний с цитатами"
    return None


def check_answer(answer: dict[str, Any], publication: dict[str, Any], dictionaries: dict[str, Any]) -> dict[str, Any]:
    """Ответ модели → значения карточки. Значение не из справочника — «не определено»; компания без
    цитаты из текста публикации и издатель публикации отбрасываются, алиасы сводятся к одному имени."""
    def choice(field: str, value: str) -> str:
        return _choice(value, (dictionaries.get(field) or {}).get("values") or [])

    event_date, source = _event_date(answer["event_date"], choice("date_source", answer["event_date_source"]),
                                     publication["published"])
    relevance = _RELEVANCE_CODES.get(_choice(answer["oilfield_relevance"], list(_RELEVANCE_CODES)))
    return {
        "event_date": event_date,
        "event_date_source": source,
        "participants_json": _participants(answer["companies"], publication, dictionaries),
        "event_type": choice("event_type", answer["event_type"]),
        "region": choice("region", answer["region"]),
        "technology_class": choice("technology_class", answer["technology_class"]),
        "technology_maturity": choice("maturity", answer["maturity"]),
        "oilfield_relevance": relevance,
        # Как у судьи радара: применение — только при прямой или переносимой связи.
        "oilfield_application": (answer["oilfield_application"].strip() or None
                                 if relevance in ("direct", "transferable") else None),
        "why_interesting": answer["why_important"].strip() or None,
    }


def _event_date(value: str, source: str, published: date | None) -> tuple[date | None, str]:
    """Дата события и откуда она: «по дате публикации» — дата публикации; «из текста» — дата модели,
    если она не позже публикации и не раньше 2000 года, иначе «не определено». Дата публикации — по
    UTC, поэтому сутки допуска: событие дня публикации по Москве бывает на день позже её даты по UTC.
    Даты публикации нет (находка «режима ChatGPT») — событие не позже сегодняшнего дня."""
    if source == "по дате публикации":
        return (published, source) if published else (None, UNDEFINED)
    if source != "из текста":
        return None, UNDEFINED
    try:
        day = date.fromisoformat(value.strip())
    except ValueError:
        return None, UNDEFINED
    latest = (published or datetime.now(timezone.utc).date()) + timedelta(days=1)
    if not _EARLIEST_EVENT <= day <= latest:
        return None, UNDEFINED
    return day, source


def _participants(companies: list[dict[str, str]], publication: dict[str, Any],
                  dictionaries: dict[str, Any]) -> list[dict[str, Any]]:
    """Участник — только с дословной цитатой из текста публикации, в которой он назван (verified —
    проверено кодом): цитата, где компании нет, — догадка модели, а не участник. Издатель участником
    не считается; алиасы — одно имя, повтор под другим именем отбрасывается."""
    entries = dictionaries.get("companies") or []
    canonical = {_plain(name): entry["name"] for entry in entries for name in (entry["name"], *entry["aliases"])}
    variants = {entry["name"]: (entry["name"], *entry["aliases"]) for entry in entries}
    text = _quotable(publication["text"])
    publisher = _plain(publication["publisher"])
    publisher = canonical.get(publisher, publisher)
    kept: dict[str, dict[str, Any]] = {}
    for company in companies:
        given = " ".join(company["name"].split())
        name = canonical.get(_plain(given), given)
        quote = " ".join(company["quote"].split())
        if not name or not quote or _quotable(quote) not in text \
                or not _named_in(quote, (given, *variants.get(name, (name,)))):
            continue
        key = _plain(name)
        if key in kept or key == _plain(publisher):
            continue
        kept[key] = {"name": name, "quote": quote, "verified": True}
    return list(kept.values())


def _named_in(quote: str, names: tuple[str, ...]) -> bool:
    """Названа ли компания в цитате — под любым из имён. Слово имени сверяется по основе (без двух
    последних букв, не короче трёх): «Роснефтью» — это «Роснефть». Имя без слов длиннее двух букв
    (BP, H&P) — целиком."""
    words = re.findall(r"\w+", _plain(quote))
    for name in names:
        tokens = [token for token in re.findall(r"\w+", _plain(name)) if len(token) >= 3]
        if not tokens and _plain(name) in _plain(quote):
            return True
        if any(word.startswith(token[:max(3, len(token) - 2)]) for token in tokens for word in words):
            return True
    return False


# Кавычки и тире модель переписывает на свой лад — для сверки цитаты они одно и то же.
_QUOTE_MARKS = str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "’": "'", "‘": "'",
                              "—": "-", "–": "-"})


def _quotable(text: str) -> str:
    return _plain(str(text or "").translate(_QUOTE_MARKS))


def _choice(value: str, allowed: list[str]) -> str:
    """Значение справочника, как оно записано в справочнике; регистр, «ё» и пробелы — не расхождение."""
    wanted = _plain(value)
    return next((item for item in allowed if _plain(item) == wanted), UNDEFINED)


def _plain(value: str) -> str:
    return " ".join(str(value or "").replace("ё", "е").replace("Ё", "Е").casefold().split())
