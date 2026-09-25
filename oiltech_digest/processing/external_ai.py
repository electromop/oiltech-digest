"""External-worker AI payloads and result application."""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Callable

from oiltech_digest import contract
from oiltech_digest.db import repository
from oiltech_digest.processing.domain_glossary import enforce_glossary_text
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

logger = logging.getLogger(__name__)

RECHECK_BATCH_DEFAULT = 100
TRANSLATE_BATCH_DEFAULT = 100


def build_process_articles_payload(payload: dict[str, Any], *, job_id: int | None = None) -> dict[str, Any]:
    """Expand a DB-backed process_articles job into a self-contained external payload.

    С job_id (выдача воркеру) статьи резервируются за задачей: соседняя ИИ-полоса не
    возьмёт те же и не оплатит их второй раз (repository.reserve_process_articles)."""
    article_ids = [int(item) for item in payload.get("article_ids") or []]
    limit = int(payload.get("limit") or contract.PROCESS_LIMIT_DEFAULT)
    if job_id is not None:
        reserved = repository.reserve_process_articles(job_id, limit=limit, article_ids=article_ids or None)
        articles = repository.get_articles_by_ids(reserved, include_summary=True)
    elif article_ids:
        articles = repository.get_articles_by_ids(article_ids, include_summary=True)
    else:
        articles = repository.get_articles_needing_pipeline(limit)
    return {
        "kind": "process_articles",
        "offline": bool(payload.get("offline", False)),
        "limit": limit,
        "article_ids": article_ids,
        "articles": [_jsonable_dict(article) for article in articles],
        "tags": [_jsonable_dict(tag) for tag in repository.list_enabled_tags()],
        "criteria": [_jsonable_dict(item) for item in repository.list_enabled_scoring_criteria()],
    }


class LeaseLost(RuntimeError):
    """Core отозвал lease задачи (heartbeat вернул 409).

    Принципиально отличается от временного сбоя сети: при 409 задача уже возвращена
    в очередь и core НИКОГДА не примет её результат. Продолжать обработку — значит
    платить OpenAI за работу, которая гарантированно будет выброшена.
    Инцидент 24.07: воркер ~80 минут крутил такой цикл (heartbeat 409 → вызов OpenAI
    200 → heartbeat 409 …) со скоростью ~$11/час в мусор, и это не попадало даже
    в ai_processing_runs, потому что complete тоже отвергался.
    """


class StopRequested(LeaseLost):
    """Воркер останавливается (SIGTERM при выкате NL) и возвращает задачу ядру сам.

    Подкласс LeaseLost намеренно: каждый цикл обработчика уже выпускает LeaseLost наружу,
    а прочие сбои heartbeat глотает. Цикл, который про остановку не знает, так всё равно
    прервётся, а не продолжит работу, которую никто не примет. Циклы ИИ-пакетов ловят его
    раньше LeaseLost и отдают сделанное с пометкой partial — оплаченное не пропадает.
    """


def process_payload(payload: dict[str, Any], heartbeat: Callable[..., None] | None = None) -> dict[str, Any]:
    """Run the AI pipeline without direct database access.

    ``heartbeat`` (если передан) вызывается перед обработкой КАЖДОЙ статьи — это
    продлевает lease задачи у core. Без него длинный батч на медленной модели
    (gpt-5.5) истекает по lease (600с) ещё до завершения, и задача бесконечно
    переотдаётся/ретраится, не закоммитив ничего. Колбэк не должен ронять обработку.
    Ему передаётся итог на этот момент: если следующая статья зависнет на остановке
    воркера, сделанное уйдёт ядру и не оплатится второй раз (worker_shutdown).
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
                heartbeat(result)
            except StopRequested:
                # Воркер останавливается: сделанное уходит ядру, остальное вернётся в очередь.
                result["partial"] = True
                break
            except LeaseLost:
                # Единственный сбой heartbeat, который ОБЯЗАН прервать батч:
                # работать дальше = платить за результат, который core не примет.
                raise
            except TypeError:
                # Колбэк не принял итог — ошибка кода, а не сети. Проглоченная, она тихо
                # отключила бы и остановку, и отзыв аренды (класс 24.07).
                raise
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
            relevance_resp = relevance_article(article, client, tags=tags)
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


def process_recheck_payload(payload: dict[str, Any], heartbeat: Callable[..., None] | None = None) -> dict[str, Any]:
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
                heartbeat(result)
            except StopRequested:
                result["partial"] = True
                break
            except LeaseLost:
                # Единственный сбой heartbeat, который ОБЯЗАН прервать батч:
                # работать дальше = платить за результат, который core не примет.
                raise
            except TypeError:  # колбэк не принял итог — ошибка кода, не сети (см. process_payload)
                raise
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
                resp = relevance_article(article, client, tags=tags)
                relevant = bool(resp.data.get("relevant"))
                item["relevance"] = _response_payload(resp, {"relevant": relevant, "reason": resp.data.get("reason")})
                result["stats"]["relevant" if relevant else "rejected"] += 1
        except Exception as exc:  # noqa: BLE001 - одна плохая статья не валит батч
            result["stats"]["errors"] += 1
            item["errors"].append(str(exc)[:1000])
        result["articles"].append(item)
    return result


def apply_recheck_result(result: dict[str, Any], *, force: bool = False, dry_run: bool = False,
                         mark: bool = False, job_id: int | None = None) -> dict[str, Any]:
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
        # Прогон пишем ДО ветвления по вердикту: вызов к OpenAI оплачен независимо от того,
        # релевантна статья или нет. Раньше _insert_run стоял ТОЛЬКО в ветке relevant=True →
        # гейт-вызовы по ОТКЛОНЁННЫМ статьям (~40% базы) и весь dry_run не попадали в
        # ai_processing_runs, и экран «AI-затраты» систематически занижал счёт (сверка с
        # дашбордом OpenAI 2026-07-23 дала разрыв до 4.5× на шумных батчах перепроверки).
        run_model = relevance.get("model")
        if run_model and run_model != "negative-keyword":
            _insert_run(article_id, "relevance", {**relevance, "provider": "openai"}, job_id=job_id)
        if bool(relevance.get("relevant")):
            if not dry_run:
                repository.set_article_relevance(article_id, True, relevance.get("reason"), relevance.get("model"))
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


def process_translate_payload(payload: dict[str, Any], heartbeat: Callable[..., None] | None = None) -> dict[str, Any]:
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
                heartbeat(result)
            except StopRequested:
                result["partial"] = True
                break
            except LeaseLost:
                # Единственный сбой heartbeat, который ОБЯЗАН прервать батч:
                # работать дальше = платить за результат, который core не примет.
                raise
            except TypeError:  # колбэк не принял итог — ошибка кода, не сети (см. process_payload)
                raise
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


def _glossary_contexts(result: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Статьи итога одним запросом — контекст словаря: какие термины в статье есть."""
    ids = [
        int(item["article_id"])
        for item in result.get("articles") or []
        if item.get("summary") or (item.get("translation") or {}).get("title_ru")
    ]
    if not ids:
        return {}
    try:
        return {int(row["id"]): row for row in repository.get_articles_by_ids(ids)}
    except Exception:  # noqa: BLE001 - словарь не стоит записи оплаченного итога
        logger.warning("словарь на ядре: статьи итога не прочитаны, пишу итог как есть", exc_info=True)
        return {}


def _core_glossary(text: str, article: dict[str, Any] | None) -> str:
    """Словарь на ядре — при записи итога внешнего воркера.

    Воркер уже прогнал текст через словарь, но своей версией кода: NL пересобирает
    владелец, и между выкатами ядро и воркер расходятся — правка словаря 25.09 («спудил»,
    «granularными») без пересборки NL не дошла бы до новых карточек. Повтор безопасен:
    замены идемпотентны. Сбой — пишем как пришло: оплаченный итог дороже терминологии.
    """
    if not text or not article:
        return text
    try:
        return enforce_glossary_text(text, article)
    except Exception:  # noqa: BLE001
        logger.warning("словарь на ядре: сбой на статье %s, пишу как есть", article.get("id"), exc_info=True)
        return text


def apply_translate_result(result: dict[str, Any], *, job_id: int | None = None) -> dict[str, Any]:
    stats = {"articles": 0, "translation": 0, "errors": 0}
    contexts = _glossary_contexts(result)
    for item in result.get("articles") or []:
        article_id = int(item["article_id"])
        stats["articles"] += 1
        translation = item.get("translation")
        if translation and translation.get("title_ru"):
            repository.set_article_title_ru(article_id, _core_glossary(translation["title_ru"], contexts.get(article_id)))
            if translation.get("provider") != "offline" or translation.get("model"):
                _insert_run(article_id, "translation", translation, job_id=job_id)
            stats["translation"] += 1
        if item.get("errors"):
            stats["errors"] += len(item["errors"])
    return stats


PROCESS_STAGES = ("summary", "translation", "relevance", "tagging", "scoring")


def apply_process_result(
    result: dict[str, Any], *, job_id: int | None = None, only: list[str] | None = None
) -> dict[str, Any]:
    """Apply an external AI result to the core database.

    job_id — id задачи-источника: уходит в ai_processing_runs для идемпотентности биллинга
    (баг H1/T2). Повторное применение того же результата (ретрай/переотдача) не двоит счёт.

    only — какие стадии записать (перегенерация сути: `enqueue-resummarize`). Воркер
    гоняет весь конвейер, он пометки не знает, — и так задачу исполняет любая сборка NL.
    Остальное не пишется: гейт, передумав, убрал бы статью из ленты, а теги и балл
    сдвинулись бы у отобранного в выпуск. Расход по всем стадиям учитывается — он оплачен."""
    write = set(only or PROCESS_STAGES)
    stats = {"articles": 0, "summary": 0, "relevance": 0, "translation": 0, "tagging": 0, "scoring": 0, "errors": 0}
    contexts = _glossary_contexts(result)
    for item in result.get("articles") or []:
        article_id = int(item["article_id"])
        stats["articles"] += 1
        if item.get("summary"):
            summary = item["summary"]
            if "summary" in write:
                repository.upsert_article_card(
                    article_id, _core_glossary(summary["summary"], contexts.get(article_id)), summary.get("model")
                )
                stats["summary"] += 1
            _insert_run(article_id, "summary", summary, job_id=job_id)
        if item.get("translation"):
            translation = item["translation"]
            if "translation" in write:
                if translation.get("title_ru"):
                    repository.set_article_title_ru(article_id, _core_glossary(translation["title_ru"], contexts.get(article_id)))
                stats["translation"] += 1
            if translation.get("provider") != "offline" or translation.get("model"):
                _insert_run(article_id, "translation", translation, job_id=job_id)
        if item.get("relevance"):
            relevance = item["relevance"]
            if "relevance" in write:
                repository.set_article_relevance(
                    article_id,
                    bool(relevance.get("relevant")),
                    relevance.get("reason"),
                    relevance.get("model"),
                )
                stats["relevance"] += 1
            _insert_run(article_id, "relevance", relevance, job_id=job_id)
        if item.get("tagging"):
            tagging = item["tagging"]
            if "tagging" in write:
                repository.upsert_article_tag(
                    article_id,
                    int(tagging["tag_id"]),
                    float(tagging.get("confidence") or 0),
                    tagging.get("rationale"),
                    tagging.get("model"),
                )
                stats["tagging"] += 1
            _insert_run(article_id, "tagging", tagging, job_id=job_id)
        if item.get("scoring"):
            scoring = item["scoring"]
            if "scoring" in write:
                repository.replace_article_score(
                    article_id,
                    float(scoring["total_score"]),
                    str(scoring["score_label"]),
                    str(scoring.get("explanation") or ""),
                    scoring.get("items") or [],
                    scoring.get("model"),
                )
                stats["scoring"] += 1
            _insert_run(article_id, "scoring", scoring, job_id=job_id)
        if item.get("errors"):
            stats["errors"] += len(item["errors"])
    return stats


def _digest(value: str) -> str:
    return hashlib.sha1(value.strip().lower().encode("utf-8")).hexdigest()[:16]


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


def _insert_run(article_id: int, stage: str, payload: dict[str, Any], *, job_id: int | None = None) -> None:
    repository.insert_ai_run(
        {
            "job_id": job_id,
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


def build_reprint_review_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Пары-кандидаты в перепечатки — на зарубежный воркер.

    Судья зовёт OpenAI, а с РФ-адреса OpenAI отвечает 403
    unsupported_country_region_territory. Ровно на этом 17.09 стоял радар сигналов,
    и первый прогон судьи дал 12 ошибок из 12 по той же причине. Все ИИ-стадии
    ходят через внешний контур, эта не исключение.

    Тексты кладём в payload: у воркера нет базы.
    """
    from oiltech_digest.db import repository

    pairs = payload.get("pairs") or []
    if not pairs:
        # Пустой список — это ошибка постановки, а не «нечего делать»: молча вернув
        # пустой пакет, задача завершилась бы «успехом» и скрыла проблему.
        raise ValueError("reprint_review: пустой список pairs")
    packed: list[dict[str, Any]] = []
    for pair in pairs:
        left = repository.get_article(int(pair["a_id"]))
        right = repository.get_article(int(pair["b_id"]))
        if left is None or right is None:
            continue
        packed.append({
            "overlap": pair.get("overlap"),
            "a": _compact_article_for_reprint(left),
            "b": _compact_article_for_reprint(right),
        })
    return {"kind": "reprint_review", "pairs": packed, "dry_run": bool(payload.get("dry_run"))}


def _compact_article_for_reprint(article: dict[str, Any]) -> dict[str, Any]:
    from oiltech_digest.processing.reprints import BODY_LIMIT

    return {
        "id": int(article["id"]),
        "title": article.get("title") or "",
        "source_name": article.get("source_name") or "",
        "published_at": str(article.get("published_at") or ""),
        "raw_text": (article.get("raw_text") or "")[:BODY_LIMIT],
    }


def process_reprint_review_payload(payload: dict[str, Any],
                                   heartbeat: Callable[..., None] | None = None) -> dict[str, Any]:
    """Сторона воркера: рассудить пары. В базу не ходит."""
    from oiltech_digest.processing.reprints import judge_pair

    client = make_client()
    result: dict[str, Any] = {
        "reprint_review": True,
        "kind": "reprint_review",
        "dry_run": bool(payload.get("dry_run")),
        "verdicts": [],
    }
    verdicts: list[dict[str, Any]] = result["verdicts"]
    for pair in payload.get("pairs") or []:
        if heartbeat:
            try:
                heartbeat(result)
            except StopRequested:
                result["partial"] = True
                break
        left, right = pair["a"], pair["b"]
        try:
            response = judge_pair(left, right, client)
            verdicts.append({
                "a_id": left["id"], "b_id": right["id"], "overlap": pair.get("overlap"),
                "same_event": bool(response.data.get("same_event")),
                "primary_id": response.data.get("primary_id"),
                "reason": str(response.data.get("reason") or "")[:500],
                "model": response.model,
                "a_len": len(left.get("raw_text") or ""),
                "b_len": len(right.get("raw_text") or ""),
            })
        except Exception as exc:  # noqa: BLE001 - одна пара не валит батч
            verdicts.append({"a_id": left["id"], "b_id": right["id"],
                             "error": str(exc)[:300]})
    result["stats"] = {
        "checked": len(verdicts),
        "reprints": sum(1 for v in verdicts if v.get("same_event")),
        "errors": sum(1 for v in verdicts if v.get("error")),
    }
    return result


def apply_reprint_review_result(result: dict[str, Any], *, job_id: int | None = None) -> dict[str, Any]:
    """Сторона ядра: записать пометки. Удаления нет намеренно — запись обратима."""
    from oiltech_digest.db import repository
    from oiltech_digest.processing.reprints import resolve_primary as reprints_resolve_primary

    if result.get("dry_run"):
        # Сухой прогон ничего не пишет — это умолчание CLI и главный сценарий
        # показа заказчику перед тем, как что-то схлопывать.
        return {"applied": 0, "dry_run": True}

    applied = 0
    skipped = 0
    for verdict in result.get("verdicts") or []:
        if verdict.get("error") or not verdict.get("same_event"):
            continue
        try:
            a_id, b_id = int(verdict["a_id"]), int(verdict["b_id"])
        except (KeyError, TypeError, ValueError):
            # Ответ воркера — недоверенный вход: битую запись пропускаем, а не падаем
            # на всей пачке.
            continue
        # Правило выбора главной копии живёт в reprints.resolve_primary и только там.
        primary = reprints_resolve_primary(
            verdict.get("primary_id"),
            a_id, int(verdict.get("a_len") or 0),
            b_id, int(verdict.get("b_len") or 0),
        )
        duplicate = b_id if primary == a_id else a_id
        try:
            repository.mark_article_reprint(
                article_id=duplicate, primary_id=primary,
                similarity=verdict.get("overlap"), reason=verdict.get("reason"),
                decided_by="ai", model=verdict.get("model"),
            )
        except ValueError as exc:
            # Инварианты пометки (сам себе перепечатка, невидимая главная копия)
            # отбивают одну пару, а не всю пачку: остальные вердикты годны.
            logger.warning("reprint_mark_skipped a=%s b=%s: %s", a_id, b_id, exc)
            skipped += 1
            continue
        applied += 1
    return {"applied": applied, "skipped": skipped}
