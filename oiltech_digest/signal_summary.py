"""«Суть и перевод» сигналов (#131, спека #128): находки поиска и материалы коллег, принятые «Отбором», получают
суть и русский заголовок тем же этапом, что статьи ленты (pipeline.summarize_article, title_ru_for_article). Раньше
находке их давал судья радара, а при «Отборе» судьи нет.

Вход — публикация (та же, что видел «Отбор») и поля «Отбора» по договорённости: категория, связь с нефтесервисом и
применение; решения и обоснования «Отбора» на входе нет. Дальше «Отбора» идут только принятые. Заполняется только
недостающее: описание автора материала — его суть, правку человека этап не трогает. Модели — те же, что у сути и
перевода ленты (OPENAI_SUMMARY_MODEL, OPENAI_TRANSLATE_MODEL на воркере; по проверке на эталоне #150 — gpt-6.1-terra,
low, вместо снимаемой 11.12.2026 gpt-5-mini). Ядро собирает пакет при выдаче, воркер без базы зовёт модель, ядро пишет:
ответ воркера — недоверенный ввод.
"""

from __future__ import annotations

from typing import Any, Callable

from oiltech_digest import config
from oiltech_digest.db import repository, signal_summary_store
from oiltech_digest.processing.external_ai import StopRequested, _core_glossary
from oiltech_digest.processing.pipeline import make_client, summarize_article, title_ru_for_article
from oiltech_digest.signal_card_pack import _publisher_label, publication_day, publication_text

KIND = signal_summary_store.KIND
# Шаг этапа в «Ходе решения» сигнала.
STAGE = "Суть и перевод"
# Потолки ответа воркера (недоверенный ввод): суть — несколько предложений, заголовок — как у перевода ленты.
_SUMMARY_CHARS, _TITLE_CHARS = 1500, 200


class SummarySetupError(ValueError):
    """Этап нельзя поставить в работу: выключен флаг «Отбора»."""


# --- Постановка (ядро) ---------------------------------------------------------------------------


def scheduler_step() -> dict[str, Any]:
    """Шаг планировщика «Суть и перевод» сигналов, за флагом OTBOR_ENABLED (этап нового конвейера): принятые
    «Отбором» находки и материалы без сути или русского заголовка — в задачи по полосе ежедневной обработки."""
    if not config.OTBOR_ENABLED:
        return {"signals": 0, "jobs": [], "reason": "disabled"}
    ids = signal_summary_store.new_signal_ids(days=config.OTBOR_NEW_DAYS, limit=config.OTBOR_STEP_LIMIT)
    return {"signals": len(ids), "jobs": enqueue(ids, trigger="daily")}


def enqueue_named(signal_ids: list[int]) -> dict[str, Any]:
    """Названные сигналы (повтор после сбоя модели) — по полосе пересчётов. Дальше «Отбора» идут только принятые:
    остальные — мимо (skipped), за них не платим."""
    accepted = signal_summary_store.accepted_ids(signal_ids)
    return {"signals": len(accepted), "skipped": [one for one in signal_ids if one not in accepted],
            "jobs": enqueue(accepted, trigger="manual", bulk=True)}


def enqueue(signal_ids: list[int], *, trigger: str, bulk: bool = False) -> list[dict[str, Any]]:
    """Задачи этапа пачками по OTBOR_BATCH; bulk — названные сигналы, по полосе пересчётов."""
    from oiltech_digest import network_policy

    if not config.OTBOR_ENABLED:
        raise SummarySetupError("OTBOR_ENABLED выключен: «Суть и перевод» сигналов — этап конвейера «Отбора»")
    route = network_policy.route_ai_bulk() if bulk else network_policy.route_ai_processing()
    batch = config.OTBOR_BATCH
    return [
        repository.create_background_job(
            KIND, {"signal_ids": signal_ids[start:start + batch], "trigger": trigger},
            queue_name=route.queue_name, execution_region=route.execution_region, capability=route.capability,
        )
        for start in range(0, len(signal_ids), batch)
    ]


# --- Пакет (ядро, при выдаче) --------------------------------------------------------------------


def build_payload(job_payload: dict[str, Any]) -> dict[str, Any]:
    """Самодостаточный пакет для воркера без базы: публикации сигналов, поля «Отбора», чего не хватает."""
    items = [_item(row) for row in signal_summary_store.summary_inputs(job_payload.get("signal_ids") or [])]
    return {"kind": KIND, "items": [item for item in items if item["needs"]]}


def _item(row: dict[str, Any]) -> dict[str, Any]:
    """Публикация сигнала так, как её видит этап, — поля статьи ленты (pipeline._article_prompt), публикация та же, что
    у «Отбора» (у материала — colleague_materials.as_publication); поля «Отбора» — ключами сигнала
    (pipeline.OTBOR_FIELDS)."""
    return {
        "signal_id": int(row["signal_id"]),
        "needs": signal_summary_store.missing(row),
        "title": publication_text(row.get("title"))[:500],
        "source_name": _publisher_label(row),
        "url": row.get("url") or "",
        "language": row.get("language") or "",
        "published_at": publication_day(row.get("published")) or "",
        "summary": None,
        "raw_text": publication_text(row.get("text")),
        "signal_category": row.get("signal_category"),
        "oilfield_relevance": row.get("oilfield_relevance"),
        "oilfield_application": row.get("oilfield_application"),
    }


# --- Вызов модели (воркер, без базы) -------------------------------------------------------------


def process_payload(payload: dict[str, Any], heartbeat: Callable[..., None] | None = None) -> dict[str, Any]:
    """Сторона воркера: суть и перевод заголовка — что нужно сигналу; ответы модели — как есть."""
    client = make_client()
    summaries: list[dict[str, Any]] = []
    result = {"signal_summary": True, "kind": KIND, "summaries": summaries}
    for item in payload.get("items") or []:
        if heartbeat:
            try:
                heartbeat(result)
            except StopRequested:
                # Остановка воркера: сделанное уходит ядру частичным итогом, остальное — в очередь.
                result["partial"] = True
                break
        needs = set(item.get("needs") or [])
        row: dict[str, Any] = {"signal_id": item.get("signal_id")}
        try:
            if "summary" in needs:
                row["summary"] = _answer(summarize_article(item, client), "summary")
            if "translation" in needs:
                title_ru, response = title_ru_for_article(item, client)
                if title_ru is not None:
                    row["translation"] = (_answer(response, "title_ru", title_ru) if response is not None
                                          else {"title_ru": title_ru, "model": None})
        except Exception as exc:  # noqa: BLE001 - один сигнал не роняет пачку
            row["error"] = str(exc)[:300]
        summaries.append(row)
    return result


def _answer(response: Any, field: str, value: Any = None) -> dict[str, Any]:
    return {field: response.data.get(field) if value is None else value, "model": response.model,
            "input_tokens": response.input_tokens, "output_tokens": response.output_tokens}


# --- Применение (ядро) ---------------------------------------------------------------------------


def apply_result(result: dict[str, Any], *, job_id: int | None = None,
                 signal_ids: list[Any] | None = None) -> dict[str, Any]:
    """Сторона ядра: суть и русский заголовок — в сигнал, только в пустые поля (signal_summary_store.write_summary),
    словарь терминов — версией ядра. Строка чужого сигнала не пишется; расход — по ставкам ядра."""
    rows = [row for row in result.get("summaries") or [] if isinstance(row, dict)]
    expected = None if signal_ids is None else {str(value) for value in signal_ids}
    ids = [signal_id for row in rows if (signal_id := _signal_id(row, expected)) is not None]
    contexts = {int(row["signal_id"]): row for row in signal_summary_store.summary_inputs(ids)}
    signal_summary_store.record_usage(job_id, [usage for row in rows for usage in _usage(row, expected)])
    applied: dict[str, Any] = {"written": 0, "errors": []}
    for row in rows:
        signal_id = _signal_id(row, expected)
        if signal_id is None:
            applied["errors"].append({"signal_id": str(row.get("signal_id"))[:30], "error": "сигнала нет в этой задаче"})
            continue
        if row.get("error"):
            applied["errors"].append({"signal_id": signal_id, "error": f"модель: {str(row['error'])[:300]}"})
            continue
        context = _context(contexts.get(signal_id))
        values = {
            "summary": _core_glossary(_text(row.get("summary"), "summary", _SUMMARY_CHARS), context),
            "title_ru": _core_glossary(_text(row.get("translation"), "title_ru", _TITLE_CHARS), context),
        }
        written = signal_summary_store.write_summary(signal_id, values, log=[_step(values)])
        applied["written"] += bool(written)
    return applied


def _signal_id(row: dict[str, Any], expected: set[str] | None) -> int | None:
    try:
        signal_id = int(row.get("signal_id"))
    except (TypeError, ValueError):
        return None
    return signal_id if expected is None or str(signal_id) in expected else None


def _text(stage: Any, field: str, limit: int) -> str:
    value = stage.get(field) if isinstance(stage, dict) else None
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _context(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Публикация для словаря терминов на ядре: какие термины в ней есть."""
    if row is None:
        return None
    return {"id": row["signal_id"], "title": row.get("title") or "", "raw_text": row.get("text") or "",
            "language": row.get("language") or ""}


def _step(values: dict[str, str]) -> dict[str, str]:
    done = [label for column, label in (("title_ru", "русский заголовок"), ("summary", "суть")) if values.get(column)]
    return {"stage": STAGE, "text": ("Готово: " + " и ".join(done)) if done else "Модель не дала ни сути, ни заголовка"}


def _usage(row: dict[str, Any], expected: set[str] | None) -> list[dict[str, Any]]:
    """Оплаченные вызовы модели — по стадиям сути и перевода; строка чужого сигнала — не расход этой задачи."""
    signal_id = _signal_id(row, expected)
    if signal_id is None:
        return []
    found = []
    for stage in signal_summary_store.STAGES:
        payload = row.get(stage) if isinstance(row.get(stage), dict) else {}
        try:
            tokens_in = max(0, int(payload.get("input_tokens") or 0))
            tokens_out = max(0, int(payload.get("output_tokens") or 0))
        except (TypeError, ValueError):
            continue
        if not tokens_in + tokens_out:
            continue
        model = str(payload.get("model") or "")[:100] or None
        price_in, price_out = config.price_for_model(model)
        found.append({"signal_id": signal_id, "stage": stage, "model": model, "input_tokens": tokens_in,
                      "output_tokens": tokens_out,
                      "cost_usd": round((tokens_in * price_in + tokens_out * price_out) / 1_000_000, 6)})
    return found
