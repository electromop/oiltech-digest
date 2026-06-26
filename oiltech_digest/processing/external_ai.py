"""External-worker AI payloads and result application."""

from __future__ import annotations

from typing import Any, Callable

from oiltech_digest.db import repository
from oiltech_digest.processing.openai_client import AIResponse
from oiltech_digest.processing.pipeline import (
    _negative_keyword_block,
    keyword_tag,
    make_client,
    normalize_score_payload,
    relevance_article,
    score_article,
    summarize_article,
    tag_article,
    title_ru_for_article,
)

RECHECK_BATCH_DEFAULT = 100
TRANSLATE_BATCH_DEFAULT = 100


def build_process_articles_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Expand a DB-backed process_articles job into a self-contained external payload."""
    article_ids = [int(item) for item in payload.get("article_ids") or []]
    limit = int(payload.get("limit") or 5)
    if article_ids:
        articles = repository.get_articles_by_ids(article_ids, include_summary=True)
    else:
        articles = repository.get_articles_needing_summary(limit)
    return {
        "kind": "process_articles",
        "offline": bool(payload.get("offline", False)),
        "limit": limit,
        "article_ids": article_ids,
        "articles": [_jsonable_dict(article) for article in articles],
        "tags": [_jsonable_dict(tag) for tag in repository.list_enabled_tags()],
        "criteria": [_jsonable_dict(item) for item in repository.list_enabled_scoring_criteria()],
    }


def process_payload(payload: dict[str, Any], heartbeat: Callable[[], None] | None = None) -> dict[str, Any]:
    """Run the AI pipeline without direct database access.

    ``heartbeat`` (если передан) вызывается перед обработкой КАЖДОЙ статьи — это
    продлевает lease задачи у core. Без него длинный батч на медленной модели
    (gpt-5.5) истекает по lease (600с) ещё до завершения, и задача бесконечно
    переотдаётся/ретраится, не закоммитив ничего. Колбэк не должен ронять обработку.
    """
    client = make_client(bool(payload.get("offline", False)))
    tags = payload.get("tags") or []
    criteria = payload.get("criteria") or []
    if not tags:
        raise ValueError("No tags supplied in external AI payload")
    if not criteria:
        raise ValueError("No scoring criteria supplied in external AI payload")

    result: dict[str, Any] = {
        "external_ai": True,
        "kind": "process_articles",
        "stats": {"processed": 0, "summary": 0, "relevant": 0, "rejected": 0,
                  "tagged": 0, "translated": 0, "scored": 0, "errors": 0},
        "articles": [],
    }
    for article in payload.get("articles") or []:
        if heartbeat is not None:
            try:
                heartbeat()
            except Exception:  # noqa: BLE001 - heartbeat не должен ломать обработку батча
                pass
        item: dict[str, Any] = {"article_id": int(article["id"]), "errors": []}
        result["stats"]["processed"] += 1
        try:
            # Стоп-слова родительских тегов: отсекаем статью ДО любых AI-вызовов (бэклог #6).
            blocked_reason = _negative_keyword_block(article, tags)
            if blocked_reason:
                item["relevance"] = {
                    "relevant": False,
                    "reason": blocked_reason,
                    "model": "negative-keyword",
                    "provider": "offline",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "cost_usd": 0.0,
                }
                result["stats"]["rejected"] += 1
                result["articles"].append(item)
                continue
            # Гейт релевантности ПЕРВЫМ — на сыром тексте, до суммаризации.
            # Нерелевантное дальше не суммируем/не тегируем/не скорим (чистота + экономия).
            relevance_resp = relevance_article(article, client)
            relevant = bool(relevance_resp.data.get("relevant"))
            item["relevance"] = _response_payload(
                relevance_resp,
                {"relevant": relevant, "reason": relevance_resp.data.get("reason")},
            )
            result["stats"]["relevant" if relevant else "rejected"] += 1
            if not relevant:
                result["articles"].append(item)
                continue

            summary_resp = summarize_article(article, client)
            item["summary"] = _response_payload(
                summary_resp,
                {"summary": summary_resp.data["summary"]},
            )
            article["summary"] = summary_resp.data["summary"]
            result["stats"]["summary"] += 1

            # Перевод заголовка — отдельная стадия (AI только для иностранных заголовков).
            title_ru, translate_resp = title_ru_for_article(article, client)
            if title_ru is not None:
                if translate_resp is not None:
                    item["translation"] = _response_payload(translate_resp, {"title_ru": title_ru})
                    result["stats"]["translated"] += 1
                else:
                    item["translation"] = {"title_ru": title_ru, "model": None, "provider": "offline",
                                           "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0}

            tag_resp = tag_article(article, tags, client)
            tag_id = _valid_tag_id(tag_resp.data.get("tag_id"), tags)
            if tag_id == 0:
                tag_id = int(keyword_tag(article, tags)["tag_id"])
            item["tagging"] = _response_payload(
                tag_resp,
                {
                    "tag_id": tag_id,
                    "confidence": _clamp(float(tag_resp.data.get("confidence") or 0), 0, 1),
                    "rationale": tag_resp.data.get("rationale"),
                },
            )
            result["stats"]["tagged"] += 1

            score_resp = score_article(article, criteria, client)
            score_payload = normalize_score_payload(article, criteria, score_resp.data)
            item["scoring"] = _response_payload(score_resp, score_payload)
            result["stats"]["scored"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad article must not kill the whole batch
            result["stats"]["errors"] += 1
            item["errors"].append(str(exc)[:1000])
        result["articles"].append(item)
    return result


# ---------------------------------------------------------------------------
# Перепрогон релевантности (gate-only) для чистки базы новым сильным гейтом.
# Нерелевантные статьи удаляются физически на стороне core (apply_recheck_result).
# ---------------------------------------------------------------------------

def build_recheck_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Развернуть DB-задачу recheck_relevance в самодостаточный внешний payload.
    Гейт судит по сырому тексту; теги нужны для блокировки по стоп-словам (паритет с продом)."""
    article_ids = [int(item) for item in payload.get("article_ids") or []]
    articles = repository.get_articles_by_ids(article_ids) if article_ids else []
    return {
        "kind": "recheck_relevance",
        "article_ids": article_ids,
        "articles": [_jsonable_dict(article) for article in articles],
        "tags": [_jsonable_dict(tag) for tag in repository.list_enabled_tags()],
    }


def process_recheck_payload(payload: dict[str, Any], heartbeat: Callable[[], None] | None = None) -> dict[str, Any]:
    """Только гейт релевантности по сырому тексту (без summary/tag/score). Без доступа к БД."""
    client = make_client(bool(payload.get("offline", False)))
    tags = payload.get("tags") or []
    result: dict[str, Any] = {
        "recheck_relevance": True,
        "kind": "recheck_relevance",
        "stats": {"processed": 0, "relevant": 0, "rejected": 0, "errors": 0},
        "articles": [],
    }
    for article in payload.get("articles") or []:
        if heartbeat is not None:
            try:
                heartbeat()
            except Exception:  # noqa: BLE001
                pass
        item: dict[str, Any] = {"article_id": int(article["id"]), "errors": []}
        result["stats"]["processed"] += 1
        try:
            blocked_reason = _negative_keyword_block(article, tags)
            if blocked_reason:
                item["relevance"] = {"relevant": False, "reason": blocked_reason, "model": "negative-keyword"}
                result["stats"]["rejected"] += 1
            else:
                resp = relevance_article(article, client)
                relevant = bool(resp.data.get("relevant"))
                item["relevance"] = _response_payload(resp, {"relevant": relevant, "reason": resp.data.get("reason")})
                result["stats"]["relevant" if relevant else "rejected"] += 1
        except Exception as exc:  # noqa: BLE001 - одна плохая статья не валит батч
            result["stats"]["errors"] += 1
            item["errors"].append(str(exc)[:1000])
        result["articles"].append(item)
    return result


def apply_recheck_result(result: dict[str, Any], *, force: bool = False, dry_run: bool = False,
                         mark: bool = False) -> dict[str, Any]:
    """Применить вердикты к core: релевантные — персист, нерелевантные — УДАЛИТЬ.
    Статьи в сохранённом дайджесте по умолчанию пропускаются (force=False).

    dry_run=True — НИЧЕГО не менять/не удалять: только посчитать и собрать превью.
    mark=True — нерелевантные НЕ удалять физически, а ПОМЕТИТЬ на удаление
    (pending_deletion): исчезают из ленты, но в БД (восстановимы recheck-unmark,
    физически удаляются разом recheck-purge). Безопасный режим по умолчанию для чистки."""
    stats = {"checked": 0, "kept": 0, "deleted": 0, "marked": 0, "skipped_in_digest": 0, "errors": 0}
    rejected_ids: list[int] = []
    reason_by_id: dict[int, str | None] = {}
    for item in result.get("articles") or []:
        article_id = int(item["article_id"])
        relevance = item.get("relevance")
        if item.get("errors") or not relevance:
            stats["errors"] += len(item.get("errors") or []) or 1
            continue
        stats["checked"] += 1
        if bool(relevance.get("relevant")):
            if not dry_run:
                repository.set_article_relevance(article_id, True, relevance.get("reason"), relevance.get("model"))
                if relevance.get("model") and relevance.get("model") != "negative-keyword":
                    _insert_run(article_id, "relevance", {**relevance, "provider": "openai"})
            stats["kept"] += 1
        elif dry_run:
            stats["deleted"] += 1  # сколько БЫ удалили
            rejected_ids.append(article_id)
            reason_by_id[article_id] = relevance.get("reason")
        elif mark:
            outcome = repository.mark_article_for_deletion(article_id, relevance.get("reason"), force=force)
            stats["marked" if outcome == "marked" else "skipped_in_digest"] += 1
        else:
            deleted = repository.delete_article(article_id, force=force)
            stats["deleted" if deleted else "skipped_in_digest"] += 1
    if dry_run and rejected_ids:
        # подтянуть заголовок/источник для просмотра (статьи ещё на месте)
        rows = repository.get_articles_by_ids(rejected_ids)
        stats["rejected_preview"] = [
            {"id": int(r["id"]), "title": r.get("title"), "source": r.get("source_name"),
             "reason": reason_by_id.get(int(r["id"]))}
            for r in rows
        ]
    return stats


# ---------------------------------------------------------------------------
# Бэкфилл перевода заголовков по всей базе (отдельная стадия) через воркер.
# ---------------------------------------------------------------------------

def build_translate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    article_ids = [int(item) for item in payload.get("article_ids") or []]
    articles = repository.get_articles_by_ids(article_ids) if article_ids else []
    return {
        "kind": "translate_titles",
        "article_ids": article_ids,
        "articles": [_jsonable_dict(article) for article in articles],
    }


def process_translate_payload(payload: dict[str, Any], heartbeat: Callable[[], None] | None = None) -> dict[str, Any]:
    client = make_client(bool(payload.get("offline", False)))
    result: dict[str, Any] = {
        "translate_titles": True,
        "kind": "translate_titles",
        "stats": {"processed": 0, "translated": 0, "errors": 0},
        "articles": [],
    }
    for article in payload.get("articles") or []:
        if heartbeat is not None:
            try:
                heartbeat()
            except Exception:  # noqa: BLE001
                pass
        item: dict[str, Any] = {"article_id": int(article["id"]), "errors": []}
        result["stats"]["processed"] += 1
        try:
            title_ru, resp = title_ru_for_article(article, client)
            if title_ru is not None:
                if resp is not None:
                    item["translation"] = _response_payload(resp, {"title_ru": title_ru})
                    result["stats"]["translated"] += 1
                else:
                    item["translation"] = {"title_ru": title_ru, "model": None, "provider": "offline",
                                           "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0}
        except Exception as exc:  # noqa: BLE001
            result["stats"]["errors"] += 1
            item["errors"].append(str(exc)[:1000])
        result["articles"].append(item)
    return result


def apply_translate_result(result: dict[str, Any]) -> dict[str, Any]:
    stats = {"articles": 0, "translation": 0, "errors": 0}
    for item in result.get("articles") or []:
        article_id = int(item["article_id"])
        stats["articles"] += 1
        translation = item.get("translation")
        if translation and translation.get("title_ru"):
            repository.set_article_title_ru(article_id, translation["title_ru"])
            if translation.get("provider") != "offline" or translation.get("model"):
                _insert_run(article_id, "translation", translation)
            stats["translation"] += 1
        if item.get("errors"):
            stats["errors"] += len(item["errors"])
    return stats


def apply_process_result(result: dict[str, Any]) -> dict[str, Any]:
    """Apply an external AI result to the core database."""
    stats = {"articles": 0, "summary": 0, "relevance": 0, "translation": 0, "tagging": 0, "scoring": 0, "errors": 0}
    for item in result.get("articles") or []:
        article_id = int(item["article_id"])
        stats["articles"] += 1
        if item.get("summary"):
            summary = item["summary"]
            repository.upsert_article_card(article_id, summary["summary"], summary.get("model"))
            _insert_run(article_id, "summary", summary)
            stats["summary"] += 1
        if item.get("translation"):
            translation = item["translation"]
            if translation.get("title_ru"):
                repository.set_article_title_ru(article_id, translation["title_ru"])
            if translation.get("provider") != "offline" or translation.get("model"):
                _insert_run(article_id, "translation", translation)
            stats["translation"] += 1
        if item.get("relevance"):
            relevance = item["relevance"]
            repository.set_article_relevance(
                article_id,
                bool(relevance.get("relevant")),
                relevance.get("reason"),
                relevance.get("model"),
            )
            _insert_run(article_id, "relevance", relevance)
            stats["relevance"] += 1
        if item.get("tagging"):
            tagging = item["tagging"]
            repository.upsert_article_tag(
                article_id,
                int(tagging["tag_id"]),
                float(tagging.get("confidence") or 0),
                tagging.get("rationale"),
                tagging.get("model"),
            )
            _insert_run(article_id, "tagging", tagging)
            stats["tagging"] += 1
        if item.get("scoring"):
            scoring = item["scoring"]
            repository.replace_article_score(
                article_id,
                float(scoring["total_score"]),
                str(scoring["score_label"]),
                str(scoring.get("explanation") or ""),
                scoring.get("items") or [],
                scoring.get("model"),
            )
            _insert_run(article_id, "scoring", scoring)
            stats["scoring"] += 1
        if item.get("errors"):
            stats["errors"] += len(item["errors"])
    return stats


def _response_payload(response: AIResponse, data: dict[str, Any]) -> dict[str, Any]:
    return {
        **data,
        "model": response.model,
        "provider": "openai" if response.model != "offline-deterministic" else "offline",
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "total_tokens": response.total_tokens,
        "cost_usd": response.cost_usd,
    }


def _insert_run(article_id: int, stage: str, payload: dict[str, Any]) -> None:
    repository.insert_ai_run(
        {
            "article_id": article_id,
            "stage": stage,
            "provider": payload.get("provider") or "openai",
            "model": payload.get("model"),
            "language": None,
            "input_tokens": int(payload.get("input_tokens") or 0),
            "output_tokens": int(payload.get("output_tokens") or 0),
            "total_tokens": int(payload.get("total_tokens") or 0),
            "cost_usd": float(payload.get("cost_usd") or 0),
            "status": "ok",
            "error_message": None,
        }
    )


def _valid_tag_id(value: Any, tags: list[dict]) -> int:
    try:
        tag_id = int(value)
    except (TypeError, ValueError):
        return 0
    return tag_id if any(int(tag["id"]) == tag_id for tag in tags) else 0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _jsonable_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in dict(row).items()}

