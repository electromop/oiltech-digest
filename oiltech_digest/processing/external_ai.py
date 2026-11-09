"""External-worker AI payloads and result application."""

from __future__ import annotations

import logging
from typing import Any, Callable

from oiltech_digest import contract
from oiltech_digest.db import repository
from oiltech_digest.processing import fulltext_recompute
from oiltech_digest.processing.domain_glossary import enforce_glossary_text
from oiltech_digest.processing.openai_client import AIResponse
from oiltech_digest.processing.pipeline import (
    make_client,
    summarize_article,
    title_ru_for_article,
)

logger = logging.getLogger(__name__)

TRANSLATE_BATCH_DEFAULT = 100


def build_process_articles_payload(payload: dict[str, Any], *, job_id: int | None = None) -> dict[str, Any]:
    """Expand a DB-backed process_articles job into a self-contained external payload.

    С job_id (выдача воркеру) статьи резервируются за задачей: соседняя ИИ-полоса не
    возьмёт те же и не оплатит их второй раз (repository.reserve_process_articles).
    Пометка only проверяется здесь, до выдачи: при записи ответ модели уже оплачен. Она же
    уходит воркеру: не запрошенное он не зовёт (process_payload). Стадии пакета: суть и
    перевод заголовка (тег и балл статьи сняты 08.11, их критериев и тегов в пакете нет)."""
    stages = stages_to_write(payload.get("only"))
    article_ids = [int(item) for item in payload.get("article_ids") or []]
    limit = int(payload.get("limit") or contract.PROCESS_LIMIT_DEFAULT)
    if job_id is not None:
        reserved = repository.reserve_process_articles(job_id, limit=limit, article_ids=article_ids or None)
        articles = repository.get_articles_by_ids(reserved, include_summary=True)
    elif article_ids:
        articles = repository.get_articles_by_ids(article_ids, include_summary=True)
    else:
        articles = repository.get_articles_needing_pipeline(limit)
    # Явный список не обходит «Отбор» (#130): отклонённую им и ждущую его решения воркер не получает.
    articles = [article for article in articles if not repository.held_by_otbor(article)]
    if payload.get(fulltext_recompute.PAYLOAD_FLAG) and articles:
        # Пересчёт после полного текста ждал пакет, выданный с обрывком: статью могли отклонить,
        # скрыть или отметить в выпуск, а месяц закрыть. За таких модель не платится.
        keep = fulltext_recompute.still_applicable([int(article["id"]) for article in articles])
        articles = [article for article in articles if int(article["id"]) in keep]
    if payload.get("only") and "summary" in stages:
        # Перегенерация сути: старая суть с браком («электроэнergyю») ушла бы в промпт
        # (_article_prompt кладёт summary), и модель повторила бы её слово в слово.
        articles = [{**article, "summary": None} for article in articles]
    worker_payload = {
        "kind": "process_articles",
        "offline": bool(payload.get("offline", False)),
        "limit": limit,
        "article_ids": article_ids,
        "articles": [_jsonable_dict(article) for article in articles],
    }
    if payload.get("only"):
        worker_payload["only"] = sorted(stages)
    return worker_payload


class LeaseLost(RuntimeError):
    """Core отозвал lease задачи (heartbeat вернул 409).

    Принципиально отличается от временного сбоя сети: при 409 задача уже возвращена
    в очередь и core НИКОГДА не примет её результат. Продолжать обработку: значит
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
    раньше LeaseLost и отдают сделанное с пометкой partial: оплаченное не пропадает.
    """


def _stages_left(article: dict[str, Any], stages: frozenset[str], *, missing_only: bool) -> frozenset[str]:
    """Стадии, которые воркер зовёт у статьи.

    У отклонённой («Отбором», а без его решения гейтом ленты до «Отбора») ни одной, в любом пути: гейта,
    который мог передумать, с 08.11 нет, и платить за суть отклонённой незачем. Пакет планировщика
    (missing_only, #53) зовёт только недостающие: записанные суть и перевод заново не зовутся. Явный список
    и пометка only пересчитывают запрошенное целиком. Поля кладёт ядро (repository.get_articles_by_ids с
    include_summary)."""
    if repository.article_rejected(article):
        return frozenset()
    if not missing_only:
        return stages
    done = {"summary": bool(article.get("summary")), "translation": bool(article.get("title_ru"))}
    return frozenset(stage for stage in stages if not done[stage])


def process_payload(payload: dict[str, Any], heartbeat: Callable[..., None] | None = None) -> dict[str, Any]:
    """Run the AI pipeline without direct database access: суть и перевод заголовка принятых статей.

    ``heartbeat`` (если передан) вызывается перед обработкой КАЖДОЙ статьи это     продлевает lease задачи у core. Без него длинный батч на медленной модели
    истекает по lease (600с) ещё до завершения, и задача бесконечно
    переотдаётся/ретраится, не закоммитив ничего. Колбэк не должен ронять обработку.
    Ему передаётся итог на этот момент: если следующая статья зависнет на остановке
    воркера, сделанное уйдёт ядру и не оплатится второй раз (worker_shutdown).

    ``only`` в payload: какие стадии нужны (пересчёт сути после полного текста: только суть).
    Остальные не зовутся: ядро их всё равно не запишет (apply_process_result). Гейт, тег и
    балл статьи сняты 08.11 (решает «Отбор», тему и баллы ставит «Карточка»): ядро выдаёт
    задачу только сборке контракта 11 (contract.required_contract).
    """
    stages = stages_to_write(payload.get("only"))  # граница: битая пометка: до любого вызова модели
    client = make_client(bool(payload.get("offline", False)))
    result: dict[str, Any] = {
        "external_ai": True,
        "kind": "process_articles",
        "stats": {"processed": 0, "summary": 0, "translated": 0, "errors": 0},
        "articles": [],
    }
    # Пакет планировщика, ни списка статей, ни пометки only (#53): только недостающие стадии.
    # Статья без перевода получала бы новую суть на каждом проходе. Явный список (перекачка
    # тела, импорт статьи) и only пересчитывают запрошенное целиком, как раньше.
    scheduler_batch = not payload.get("article_ids") and payload.get("only") is None
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
                # Колбэк не принял итог: ошибка кода, а не сети. Проглоченная, она тихо
                # отключила бы и остановку, и отзыв аренды (класс 24.07).
                raise
            except Exception:  # noqa: BLE001 - heartbeat не должен ломать обработку батча
                pass
        item: dict[str, Any] = {"article_id": int(article["id"]), "errors": []}
        result["stats"]["processed"] += 1
        todo = _stages_left(article, stages, missing_only=scheduler_batch)
        try:
            if "summary" in todo:
                summary_resp = summarize_article(article, client)
                item["summary"] = _response_payload(
                    summary_resp,
                    {"summary": summary_resp.data["summary"]},
                )
                article["summary"] = summary_resp.data["summary"]
                result["stats"]["summary"] += 1

            if "translation" in todo:
                # Перевод заголовка, отдельная стадия: иностранный переводится, русский не по правилам эксперта
                # правится. Статья здесь принята: отклонённую конвейер сюда не пускает (_stages_left), а явные
                # списки с пометкой only ставятся по принятым (перегенерация сути, правка заголовков месяца).
                title_ru, translate_resp = title_ru_for_article(article, client, edit_russian=True)
                if title_ru is not None:
                    if translate_resp is not None:
                        item["translation"] = _response_payload(translate_resp, {"title_ru": title_ru})
                        result["stats"]["translated"] += 1
                    else:
                        item["translation"] = {"title_ru": title_ru, "model": None, "provider": "offline",
                                               "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                                               "cost_usd": 0.0}
        except Exception as exc:  # noqa: BLE001 - one bad article must not kill the whole batch
            result["stats"]["errors"] += 1
            item["errors"].append(str(exc)[:1000])
        result["articles"].append(item)
    return result


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
            except TypeError:  # колбэк не принял итог: ошибка кода, не сети (см. process_payload)
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
    """Статьи итога одним запросом, контекст словаря: какие термины в статье есть."""
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
    """Словарь на ядре: при записи итога внешнего воркера.

    Воркер уже прогнал текст через словарь, но своей версией кода: NL пересобирает
    владелец, и между выкатами ядро и воркер расходятся: правка словаря 25.09 («спудил»,
    «granularными») без пересборки NL не дошла бы до новых карточек. Повтор безопасен:
    замены идемпотентны. Сбой, пишем как пришло: оплаченный итог дороже терминологии.
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


PROCESS_STAGES = ("summary", "translation")
# Стадии, снятые 08.11: гейт ленты, тег и балл статьи. Задача, поставленная до выката (пересчёт после полного
# текста: суть, тег и балл), выдаётся с тем, что от неё осталось, а не падает. Итог сборки NL, выданный до выката,
# может нести их ответы: они не пишутся, но расход записывается, вызов оплачен.
RETIRED_STAGES = ("relevance", "tagging", "scoring")


def _write_summary(article_id: int, payload: dict[str, Any], context: dict[str, Any] | None) -> None:
    repository.upsert_article_card(article_id, _core_glossary(payload["summary"], context), payload.get("model"))


def _write_translation(article_id: int, payload: dict[str, Any], context: dict[str, Any] | None) -> None:
    if payload.get("title_ru"):
        repository.set_article_title_ru(article_id, _core_glossary(payload["title_ru"], context))


_STAGE_WRITERS = {
    "summary": _write_summary,
    "translation": _write_translation,
}


class InvalidJobPayload(ValueError):
    """Payload задачи не исполнить ни с какой попытки: выдача воркеру её проваливает."""


def stages_to_write(only: Any) -> frozenset[str]:
    """Какие стадии пишет ядро. None: все; иначе непустой список известных стадий.

    Пометку ставят пересчёт после полного текста, перегенерация сути и правка заголовков, но читается
    она из payload_json задачи, это граница: строка «summary» дала бы множество букв (ничего не записать),
    пустой список дал бы все стадии. Снятые стадии (RETIRED_STAGES) из пометки выпадают; пометка из одних
    снятых тоже ошибка. Ошибка здесь значит ошибку кода: падаем при выдаче и ещё раз до любой записи."""
    if only is None:
        return frozenset(PROCESS_STAGES)
    known = (*PROCESS_STAGES, *RETIRED_STAGES)
    if not isinstance(only, (list, tuple)) or not only or not all(stage in known for stage in only):
        raise InvalidJobPayload(f"only: непустой список стадий из {PROCESS_STAGES}, получено {only!r}")
    kept = frozenset(stage for stage in only if stage in PROCESS_STAGES)
    if not kept:
        raise InvalidJobPayload(f"only: стадии {only!r} сняты 08.11, писать нечего")
    return kept


def apply_process_result(
    result: dict[str, Any], *, job_id: int | None = None, only: Any = None
) -> dict[str, Any]:
    """Apply an external AI result to the core database.

    job_id, id задачи-источника: уходит в ai_processing_runs для идемпотентности биллинга
    (баг H1/T2). Повторное применение того же результата (ретрай/переотдача) не двоит счёт.

    only: какие стадии записать (пересчёт после полного текста, перегенерация сути, правка
    заголовков): воркер не запрошенное не зовёт (process_payload), а фильтр остаётся здесь, чтобы
    запись не зависела от сборки NL. Расход по всем стадиям учитывается: он оплачен, в том числе
    снятых (RETIRED_STAGES) из итога, выданного сборке до выката: их ответы не пишутся."""
    write = stages_to_write(only)
    stats = {"articles": 0, "summary": 0, "translation": 0, "errors": 0}
    contexts = _glossary_contexts(result)
    for item in result.get("articles") or []:
        article_id = int(item["article_id"])
        stats["articles"] += 1
        for stage in (*PROCESS_STAGES, *RETIRED_STAGES):
            payload = item.get(stage)
            if not payload:
                continue
            if stage in write:
                _STAGE_WRITERS[stage](article_id, payload, contexts.get(article_id))
                stats[stage] += 1
            # Перевод русского заголовка, копия без модели: такой вызов не оплачен.
            if stage != "translation" or payload.get("provider") != "offline" or payload.get("model"):
                _insert_run(article_id, stage, payload, job_id=job_id)
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


def _jsonable_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in dict(row).items()}


def build_reprint_review_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Пары-кандидаты в перепечатки: на зарубежный воркер.

    Судья зовёт OpenAI, а с РФ-адреса OpenAI отвечает 403
    unsupported_country_region_territory. Ровно на этом 17.09 стоял радар сигналов,
    и первый прогон судьи дал 12 ошибок из 12 по той же причине. Все ИИ-стадии
    ходят через внешний контур, эта не исключение.

    Тексты кладём в payload: у воркера нет базы.
    """
    from oiltech_digest.db import repository

    pairs = payload.get("pairs") or []
    if not pairs:
        # Пустой список это ошибка постановки, а не «нечего делать»: молча вернув
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
    """Сторона воркера: рассудить пары. В базу не ходит. Токены каждой пары едут в вердикте: расход ядро пишет в
    ai_processing_runs (до 03.11 судья в учёт не попадал)."""
    from oiltech_digest.processing.reprints import configured_model, judge_pair

    configured_model()  # без модели: сбой задачи с причиной, а не «разные события» у каждой пары
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
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
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
    """Сторона ядра: записать пометки. Удаления нет намеренно: запись обратима."""
    from oiltech_digest.db import repository
    from oiltech_digest.processing.reprints import resolve_primary as reprints_resolve_primary

    if result.get("dry_run"):
        # Сухой прогон ничего не пишет это умолчание CLI и главный сценарий
        # показа заказчику перед тем, как что-то схлопывать. Расход тоже, как у радара
        # (signal_discovery.apply_discovery): токены остаются в итоге задачи.
        return {"applied": 0, "dry_run": True}
    repository.record_reprint_usage(job_id, result.get("verdicts") or [])

    applied = 0
    skipped = 0
    for verdict in result.get("verdicts") or []:
        if verdict.get("error") or not verdict.get("same_event"):
            continue
        try:
            a_id, b_id = int(verdict["a_id"]), int(verdict["b_id"])
        except (KeyError, TypeError, ValueError):
            # Ответ воркера, недоверенный вход: битую запись пропускаем, а не падаем
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
