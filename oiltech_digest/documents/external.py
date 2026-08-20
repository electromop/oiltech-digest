"""Разбор документа через внешний контур: build → process → apply.

Форма скопирована с существующих стадий (перевод заголовков, перепроверка релевантности),
чтобы воркер и core-эндпоинты не пришлось учить новому протоколу. Отличия от статьи —
там, где документ принципиально другой:

1. Вход НЕ обрезается 6000 знаками. `_compact(raw_text, 6000)` для отчёта на 60 страниц
   означает разбор первых 2-4 страниц молча — здесь вместо этого нарезка на фрагменты
   и свёртка (map-reduce).
2. В нагрузку кладётся текст фрагментов, но НЕ кладётся ничего лишнего, а обратно
   возвращается конверт, который core чистит перед сохранением: `payload_json` и
   `result_json` отдаются клиенту целиком, а админ видит задачи любого пользователя —
   текст личного документа в них означал бы утечку из режима «личное по умолчанию».
3. Числа проверяются кодом при применении результата, а не принимаются на слово.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from oiltech_digest import config
from oiltech_digest.db import documents_repo
from oiltech_digest.documents import prompts
from oiltech_digest.documents.chunking import chunk_anchors
from oiltech_digest.documents.model import Anchor
from oiltech_digest.documents.verification import verify_value
from oiltech_digest.processing.external_ai import LeaseLost, _response_payload
from oiltech_digest.processing.pipeline import make_client

logger = logging.getLogger(__name__)

# Потолок на объём, уезжающий за одну выдачу задачи. Гидрация идёт в веб-процессе и обязана
# уложиться в 30-секундный таймаут claim и в запас памяти сервера. Превышение НЕ режется
# молча: сколько фрагментов ушло и сколько всего — в статистике и в ответе пользователю.
MAX_CHUNKS_PER_JOB = 40


def build_document_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Гидрация в core: по id документа собрать фрагменты текста для воркера."""
    document_id = int(payload["document_id"])
    document = documents_repo.get_document(document_id)
    if document is None:
        raise ValueError(f"document {document_id} not found")

    anchors = [Anchor(number=int(r["number"]), text=r["text"] or "") for r in documents_repo.get_anchors(document_id)]
    chunks = chunk_anchors(anchors)
    sent = chunks[:MAX_CHUNKS_PER_JOB]

    return {
        "kind": "process_document",
        "document_id": document_id,
        "anchor_unit": document.get("anchor_unit") or "блок",
        "chunks_total": len(chunks),
        "chunks": [
            {"index": c.index, "text": c.text, "anchor_from": c.anchor_from, "anchor_to": c.anchor_to}
            for c in sent
        ],
        "offline": bool(payload.get("offline", False)),
    }


def process_document_payload(
    payload: dict[str, Any], heartbeat: Callable[[], None] | None = None
) -> dict[str, Any]:
    """Исполняется ВО ВНЕШНЕМ ВОРКЕРЕ. Без базы и без сети к core — только модель."""
    client = make_client(bool(payload.get("offline", False)))
    model = config.OPENAI_DOC_MODEL
    reasoning = config.OPENAI_DOC_REASONING
    anchor_unit = payload.get("anchor_unit") or "блок"

    chunk_notes: list[str] = []
    facts: list[dict[str, Any]] = []
    usage: list[dict[str, Any]] = []
    errors: list[str] = []

    for chunk in payload.get("chunks") or []:
        if heartbeat is not None:
            try:
                heartbeat()
            except LeaseLost:
                # Единственный сбой, обязанный прервать разбор: работать дальше — значит
                # платить за результат, который core уже не примет (инцидент 24.07).
                raise
            except Exception:  # noqa: BLE001
                pass
        user_input = (
            f"единица привязки: {anchor_unit}\n"
            f"якоря во фрагменте: с {chunk['anchor_from']} по {chunk['anchor_to']}\n"
            f"текст:\n{chunk['text']}"
        )
        try:
            resp = client.complete_json(
                prompts.DOC_CHUNK_INSTRUCTIONS, user_input, prompts.DOC_CHUNK_SCHEMA,
                max_output_tokens=2500, model=model, reasoning_effort=reasoning,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"фрагмент {chunk['index']}: {str(exc)[:300]}")
            continue
        usage.append(_response_payload(resp, {}))
        data = resp.data or {}
        if data.get("about"):
            chunk_notes.append(f"[{anchor_unit} {chunk['anchor_from']}–{chunk['anchor_to']}] {data['about']}")
        for fact in data.get("facts") or []:
            anchor = fact.get("anchor")
            # Модель обязана ссылаться на якорь ИЗ ЭТОГО фрагмента. Ссылку за его пределы
            # не чиним и не угадываем — обнуляем, и факт уйдёт в неподтверждённые.
            if not isinstance(anchor, int) or not (chunk["anchor_from"] <= anchor <= chunk["anchor_to"]):
                anchor = None
            facts.append({
                "value": str(fact.get("value") or "")[:120],
                "unit": (fact.get("unit") or None),
                "context": str(fact.get("context") or "")[:500],
                "anchor": anchor,
            })

    card: dict[str, Any] = {}
    if chunk_notes:
        try:
            resp = client.complete_json(
                prompts.DOC_CARD_INSTRUCTIONS, "\n".join(chunk_notes)[:60000],
                prompts.DOC_CARD_SCHEMA, max_output_tokens=3000,
                model=model, reasoning_effort=reasoning,
            )
            usage.append(_response_payload(resp, {}))
            data = resp.data or {}
            passport = data.get("passport") or {}
            card = {
                "doc_type": passport.get("doc_type"),
                "publisher": passport.get("publisher"),
                "doc_date": passport.get("date"),
                "language": passport.get("language"),
                "essence": data.get("essence"),
                "summary": data.get("summary") or [],
                "claims": data.get("claims") or [],
            }
        except Exception as exc:  # noqa: BLE001
            errors.append(f"свёртка: {str(exc)[:300]}")

    return {
        "process_document": True,          # маркерный флаг: без него apply не сработает
        "kind": "process_document",
        "document_id": int(payload["document_id"]),
        "card": card,
        "facts": facts,
        "usage": usage,
        "stats": {
            "chunks_sent": len(payload.get("chunks") or []),
            "chunks_total": int(payload.get("chunks_total") or 0),
            "facts": len(facts),
            "errors": len(errors),
        },
        "errors": errors,
    }


def apply_document_result(result: dict[str, Any], *, job_id: int | None = None) -> dict[str, Any]:
    """Исполняется в core. Пишет карточку и факты, СВЕРЯЯ каждое число с текстом якоря."""
    document_id = int(result["document_id"])
    anchors = {int(r["number"]): (r["text"] or "") for r in documents_repo.get_anchors(document_id)}

    facts = []
    verified_count = 0
    for fact in result.get("facts") or []:
        anchor = fact.get("anchor")
        anchor_text = anchors.get(anchor, "") if isinstance(anchor, int) else ""
        # Проверку делает КОД, а не второй вызов модели: детерминированно и бесплатно.
        ok = verify_value(fact.get("value") or "", fact.get("unit"), anchor_text)
        verified_count += int(ok)
        facts.append({**fact, "verified": ok})

    card = result.get("card") or {}
    model = None
    for entry in result.get("usage") or []:
        model = entry.get("model") or model

    if card:
        documents_repo.save_card(document_id, card, model)
    documents_repo.replace_facts(document_id, facts)

    for index, entry in enumerate(result.get("usage") or []):
        documents_repo.record_ai_run(
            job_id=job_id,
            document_id=document_id,
            # stage несёт номер вызова: иначе частичный уникальный индекс
            # (job_id, document_id, stage) схлопнул бы все фрагменты в одну строку счёта.
            stage=f"document_chunk_{index}" if index < len(result.get("usage") or []) - 1 else "document_card",
            model=entry.get("model"),
            input_tokens=int(entry.get("input_tokens") or 0),
            output_tokens=int(entry.get("output_tokens") or 0),
            cost_usd=float(entry.get("cost_usd") or 0.0),
        )

    errors = result.get("errors") or []
    status = "ready" if card and not errors else ("ready" if card else "failed")
    documents_repo.set_status(document_id, status, "; ".join(errors)[:2000] or None)

    return {
        "document_id": document_id,
        "facts": len(facts),
        "verified": verified_count,
        "unverified": len(facts) - verified_count,
        "errors": len(errors),
    }


def scrub_result(result: dict[str, Any]) -> dict[str, Any]:
    """Что останется в result_json задачи.

    Содержимое документа отсюда ВЫРЕЗАНО намеренно: этот словарь отдаётся клиенту через
    /api/jobs, и админ видит задачи любого пользователя. Карточка, факты и цитаты уже
    применены в таблицы документов, где действует проверка владельца.
    """
    return {
        "process_document": True,
        "kind": "process_document",
        "document_id": result.get("document_id"),
        "stats": result.get("stats") or {},
        "errors": result.get("errors") or [],
        "usage": [
            {k: v for k, v in (entry or {}).items()
             if k in ("model", "provider", "input_tokens", "output_tokens", "total_tokens", "cost_usd")}
            for entry in (result.get("usage") or [])
        ],
    }
