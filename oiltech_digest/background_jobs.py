"""Small persistent background-job runner for heavy API operations.

This is intentionally lightweight: one process-local executor plus database
state. It gives the API stable job contracts now and leaves room to swap the
executor for Redis/Celery later without changing frontend-facing endpoints.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import logging
import time
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from oiltech_digest import config
from oiltech_digest.db import repository
from oiltech_digest.ingestion import playwright_parser, request_parser, rss_parser, telegram_parser
from oiltech_digest.ingestion.source_diagnostics import diagnose_source
from oiltech_digest.processing.digest import write_digest_export
from oiltech_digest.processing.pipeline import (
    make_client,
    process_pipeline_articles,
)

_executor = ThreadPoolExecutor(max_workers=max(1, config.BACKGROUND_JOB_WORKERS))
logger = logging.getLogger(__name__)
DAILY_SIGNAL_DISCOVERY_MARKER = "daily_signal_discovery"
# «Ежедневный» это календарные сутки заказчика, а не «24 часа назад».
DAILY_SIGNAL_DISCOVERY_TZ = ZoneInfo("Europe/Moscow")


def daily_signal_discovery_payload() -> dict[str, Any]:
    """Задача ежедневного прогона радара. Модель и прочие настройки поиска GPT (#138), решение ядра при постановке:
    без годной модели поиска задача не ставится (signal_discovery.SearchSetupError)."""
    from oiltech_digest import signal_discovery

    return {
        **signal_discovery.research_settings(days=config.SIGNAL_DISCOVERY_DAYS),
        "topic": None,
        "days": config.SIGNAL_DISCOVERY_DAYS,
        "offline": False,
        "dry_run": False,
        "trigger": "scheduler_daily",
        "schedule": DAILY_SIGNAL_DISCOVERY_MARKER,
    }


def _hours_since_local_midnight(now: datetime | None = None) -> float:
    local = (now or datetime.now(timezone.utc)).astimezone(DAILY_SIGNAL_DISCOVERY_TZ)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return round(max((local - midnight).total_seconds() / 3600, 0.01), 4)


def enqueue_daily_signal_discovery(*, force: bool = False) -> dict[str, Any]:
    if not config.SIGNAL_DISCOVERY_DAILY_ENABLED and not force:
        return {"enqueued": False, "reason": "disabled"}

    marker = {"schedule": DAILY_SIGNAL_DISCOVERY_MARKER}
    # С полуночи по Москве, а не «за 24 часа»: 18.09 запуск в 17:15 МСК заблокировал
    # утренний крон 19.09 (прошло 14 ч из 24), и радар в тот день не отработал.
    # При фиксированном часе крона окно ровно в 24 ч: ещё и гонка секунд.
    lookback_hours = _hours_since_local_midnight()
    # 'failed': тоже попытка этого дня. Без него упавшая задача ставилась заново на
    # каждом цикле планировщика (30 мин): 134 запуска за 5 дней вместо 5. Пока падение
    # было мгновенным (403 на первом вызове), это ничего не стоило; падение в конце
    # прогона сжигало бы почти весь его бюджет каждые полчаса.
    if not force and repository.has_recent_background_job(
        kind="signal_discovery",
        payload_subset=marker,
        lookback_hours=lookback_hours,
        statuses=("queued", "running", "finalizing", "ok", "failed"),
    ):
        return {
            "enqueued": False,
            "reason": "already_scheduled",
            "lookback_hours": lookback_hours,
        }
    # Раз в N суток (решение владельца 04.11: радар раз в 3 дня с окном 3 дня): прогон прошлых суток держит
    # следующий до N-х суток. Упавший не держит (задача failed или поиск не ответил ни в одной теме): его окно никто
    # не обыскал, следующая попытка в следующие сутки.
    every_days = config.SIGNAL_DISCOVERY_DAILY_EVERY_DAYS
    if not force and every_days > 1:
        cadence_hours = round(lookback_hours + 24 * (every_days - 1), 4)
        if repository.has_recent_signal_discovery_search(payload_subset=marker, lookback_hours=cadence_hours):
            return {
                "enqueued": False,
                "reason": "not_due",
                "lookback_hours": cadence_hours,
                "every_days": every_days,
            }

    # Маршрут спрашиваем у политики, а не прибиваем к 'ru'. Радар ищет через
    # OpenAI (поиск GPT), а OpenAI отвечает РФ-адресам 403
    # unsupported_country_region_territory: поэтому жёсткий регион 'ru' ронял
    # ежедневную задачу КАЖДЫЙ раз, молча: сигналы стояли с 13.09.
    # Остальные ИИ-стадии давно ходят этим же маршрутом, через внешний воркер.
    from oiltech_digest import network_policy

    decision = network_policy.route_ai_processing()
    job = enqueue(
        "signal_discovery",
        daily_signal_discovery_payload(),
        queue_name=decision.queue_name,
        execution_region=decision.execution_region,
        capability=decision.capability,
        max_attempts=1,
    )
    return {"enqueued": True, "job": job, "lookback_hours": lookback_hours}


def enqueue(
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    user_id: int | None = None,
    queue_name: str = "default",
    execution_region: str = "ru",
    capability: str | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Create a persistent job and submit it to the local executor."""
    if kind not in _HANDLERS:
        raise ValueError(f"Unknown background job kind: {kind}")
    job = repository.create_background_job(
        kind,
        payload or {},
        user_id=user_id,
        queue_name=queue_name,
        execution_region=execution_region,
        capability=capability,
        max_attempts=max_attempts,
    )
    if config.BACKGROUND_JOB_INLINE and runs_inline(queue_name):
        _executor.submit(run, int(job["id"]))
    return job


def runs_inline(queue_name: str) -> bool:
    """Можно ли исполнить задачу в этом же процессе.

    Внешняя очередь, решение маршрута: задачу должен взять воркер за рубежом. У агентов
    20.09 планировщик без BACKGROUND_JOB_INLINE=0 выполнил радар из external-ai прямо на
    РФ-ядре → OpenAI 403, задача дня потеряна. Здесь маршрут не обходит ни один процесс,
    какой бы флаг ни забыли в compose."""
    return not str(queue_name or "").startswith("external")


def run(job_id: int) -> None:
    """Execute a queued job and persist terminal state."""
    job = repository.get_background_job(job_id)
    if job is None:
        return
    _execute(job, mark_running=True)


def run_claimed(job: dict[str, Any]) -> None:
    """Execute a job already claimed by an external worker."""
    _execute(job, mark_running=False)


def worker_loop(
    *,
    poll_seconds: float | None = None,
    once: bool = False,
    stale_minutes: int | None = None,
    queue_names: list[str] | None = None,
) -> None:
    """Poll the DB queue and execute jobs in the current process."""
    poll_seconds = config.BACKGROUND_JOB_POLL_SECONDS if poll_seconds is None else poll_seconds
    stale_minutes = config.BACKGROUND_JOB_STALE_MINUTES if stale_minutes is None else stale_minutes
    queue_names = queue_names or config.BACKGROUND_JOB_QUEUES
    finalize_minutes = config.FINALIZE_STALE_MINUTES
    sweep_interval = max(poll_seconds, 30.0)  # переочередь зависших: не чаще раза в ~30с

    def _sweep_stale() -> None:
        local = repository.requeue_stale_background_jobs(stale_minutes, finalize_minutes)
        if local.requeued or local.exhausted:
            logger.warning(
                "jobs_requeued_stale requeued=%s exhausted=%s stale_minutes=%s "
                "finalize_minutes=%s queues=%s",
                local.requeued,
                local.exhausted,
                stale_minutes,
                finalize_minutes,
                ",".join(queue_names),
            )
        # Внешний контур убирается ПО LEASE, а не по настенным часам, и делать это надо
        # по расписанию. Раньше реапер жил только внутри /api/external-worker/claim (T21):
        # пока воркер занят длинной задачей, он за новой не приходит, значит и протухшие
        # lease никто не разбирает. Умри воркер совсем: задачи висели бы 'running' вечно,
        # и интерфейс врал бы про идущую работу. Здесь этот сигнал наконец опрашивается
        # регулярно, независимо от того, приходит воркер за работой или нет.
        external = repository.requeue_expired_external_leases()
        if external.requeued or external.exhausted:
            logger.warning(
                "external_leases_reaped requeued=%s exhausted=%s",
                external.requeued,
                external.exhausted,
            )
        # Отправка кода допуска, упавшая окончательно по истёкшей аренде, без текста с кодом.
        try:
            from oiltech_digest import admission

            admission.settle_notifications()
        except Exception:  # noqa: BLE001 - уборка не должна останавливать очередь
            logger.exception("admission_settle_failed")

    last_sweep = 0.0
    while True:
        # Периодически вытаскиваем зависшие running/finalizing (раньше: только разово на старте,
        # из-за чего застрявший после краша 'finalizing' ждал рестарта воркача; баг T2/H2).
        now_mono = time.monotonic()
        if now_mono - last_sweep >= sweep_interval:
            _sweep_stale()
            last_sweep = now_mono
        job = repository.claim_next_background_job(queue_names=queue_names)
        if job is None:
            if once:
                return
            time.sleep(poll_seconds)
            continue
        logger.info(
            "background_job_started job_id=%s kind=%s queue=%s attempts=%s",
            job["id"],
            job["kind"],
            job.get("queue_name"),
            job.get("attempts"),
        )
        run_claimed(job)


def _execute(job: dict[str, Any], *, mark_running: bool) -> None:
    job_id = int(job["id"])
    handler = _HANDLERS.get(job["kind"])
    if handler is None:
        repository.fail_background_job(job_id, f"Unknown background job kind: {job['kind']}")
        return

    try:
        if mark_running:
            repository.mark_background_job_running(job_id)
            job = repository.get_background_job(job_id) or job
        result = handler(dict(job.get("payload_json") or {}), job_id)
        repository.finish_background_job(job_id, result)
        logger.info(
            "background_job_finished job_id=%s kind=%s queue=%s",
            job["id"],
            job["kind"],
            job.get("queue_name"),
        )
    except Exception as exc:  # noqa: BLE001 - terminal job errors must be recorded
        retry_delay = _retry_delay_seconds(job)
        repository.fail_background_job(int(job["id"]), str(exc), retry_delay_seconds=retry_delay)
        logger.exception(
            "background_job_failed job_id=%s kind=%s queue=%s retry_delay_seconds=%s",
            job["id"],
            job["kind"],
            job.get("queue_name"),
            retry_delay,
        )


def _retry_delay_seconds(job: dict[str, Any]) -> int | None:
    attempts = int(job.get("attempts") or 0)
    max_attempts = int(job.get("max_attempts") or 0)
    if attempts >= max_attempts:
        return None
    return min(config.BACKGROUND_JOB_RETRY_BASE_SECONDS * (2 ** max(0, attempts - 1)), 1800)


def _run_digest_export(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    repository.update_background_job_progress(job_id, 25)
    result = write_digest_export(
        month=str(payload.get("month") or ""),
        export_format=str(payload.get("export_format") or "pdf"),
        limit=int(payload.get("limit") or 100),
        min_score=float(payload.get("min_score") or 0),
        max_score=float(payload["max_score"]) if payload.get("max_score") is not None else None,
        search=str(payload.get("search") or "") or None,
        top_tag=str(payload.get("top_tag") or "") or None,
        # user_id в задачах до #111 не читаем: выпуск общий.
    )
    repository.update_background_job_progress(job_id, 90)
    return result


def _run_process_articles(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    client = make_client(bool(payload.get("offline", False)))
    article_ids = payload.get("article_ids") or []
    has_article_ids = bool(article_ids)
    limit = int(payload.get("limit") or 5)

    if has_article_ids:
        ids = [int(article_id) for article_id in article_ids]
        articles = repository.get_articles_by_ids(ids, include_summary=True)
    else:
        articles = repository.get_articles_needing_pipeline(limit)

    # Отметка «эта попытка НАЧАЛА жечь OpenAI»: строго ДО первого обращения к модели.
    # По ней requeue_stale_background_jobs отличает задачу, которую нельзя перезапускать
    # автоматически (повторный прогон = повторный реальный расход), от упавшей до AI.
    # Именно отдельная колонка, а не progress: claim/mark-running форсят progress в 10
    # ещё до тела обработчика, поэтому по progress «до/после AI» не различить.
    repository.mark_background_job_ai_started(job_id)
    repository.update_background_job_progress(job_id, 20)
    stats = process_pipeline_articles(articles, client, fetch_full=True)
    repository.update_background_job_progress(job_id, 95)

    return {"pipeline": stats}


def _run_scrape_source(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    source_id = int(payload["source_id"])
    source = repository.get_source(source_id)
    if source is None:
        raise ValueError("Source not found")

    strategy = source.get("parse_strategy")
    if strategy not in {"request", "playwright"}:
        raise ValueError("Скраппер доступен только для request/playwright-источников")

    repository.update_background_job_progress(job_id, 25)
    stats = playwright_parser.parse_source(source) if strategy == "playwright" else request_parser.parse_source(source)
    repository.update_background_job_progress(job_id, 90)
    return {"source_id": source_id, "stats": stats}


def _run_parse_source_once(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    source_id = int(payload["source_id"])
    source = repository.get_source(source_id)
    if source is None:
        raise ValueError("Source not found")
    strategy = source.get("parse_strategy")
    repository.update_background_job_progress(job_id, 20)
    if strategy == "rss":
        stats = rss_parser.parse_source(source)
    elif strategy == "request":
        stats = request_parser.parse_source(source)
    elif strategy == "telegram":
        stats = telegram_parser.parse_source(source)
    elif strategy == "playwright":
        stats = playwright_parser.parse_source(source)
    else:
        raise ValueError("parse_source_once supports rss/request/telegram/playwright sources")
    repository.update_background_job_progress(job_id, 90)
    return {"source_id": source_id, "strategy": strategy, "stats": stats}


def _run_diagnose_source(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    source_id = int(payload["source_id"])
    source = repository.get_source(source_id)
    if source is None:
        raise ValueError("Source not found")
    overrides = payload.get("overrides") or {}
    limit = int(payload.get("limit") or 5)

    repository.update_background_job_progress(job_id, 20)
    result = diagnose_source({**source, **overrides}, limit=limit)
    repository.update_background_job_progress(job_id, 90)
    return result


def _run_signal_discovery(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    from oiltech_digest.signal_discovery import config_from_payload, discover_signals

    repository.update_background_job_progress(job_id, 15)
    result = discover_signals(config_from_payload(payload, background_job_id=job_id))
    repository.update_background_job_progress(job_id, 95)
    return result


def job_download_path(job: dict[str, Any]) -> Path | None:
    result = job.get("result_json") or {}
    path = result.get("path")
    return Path(path) if path else None


def _run_reprint_review(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    """Судья перепечаток. Регистрируется и здесь, а не только в external_worker:
    задачу может исполнить как внешний воркер, так и локальный путь, и без этой
    записи она отвечает «Unsupported job kind» и висит в очереди."""
    from oiltech_digest.processing import external_ai

    repository.update_background_job_progress(job_id, 20)
    result = external_ai.process_reprint_review_payload(
        external_ai.build_reprint_review_payload(payload)
    )
    applied = external_ai.apply_reprint_review_result(result, job_id=job_id)
    repository.update_background_job_progress(job_id, 95)
    return {**result, "applied": applied}


def _run_refetch_text(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    """Дозаполнение тела статей-обрывков: та же причина регистрации."""
    from oiltech_digest.ingestion import external_fetch

    repository.update_background_job_progress(job_id, 20)
    result = external_fetch.process_refetch_text_payload(
        external_fetch.build_refetch_text_payload(payload)
    )
    applied = external_fetch.apply_refetch_text_result(result)
    repository.update_background_job_progress(job_id, 95)
    return {**result, "applied": applied}


def _run_signal_card(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    """Агент «Карточка сигнала» в местном контуре (#96): та же причина регистрации, что у судьи
    перепечаток: без внешних воркеров маршрут ИИ ставит задачу в местную очередь ai."""
    from oiltech_digest import signal_card

    repository.update_background_job_progress(job_id, 20)
    result = signal_card.process_payload(signal_card.build_payload(payload))
    applied = signal_card.apply_result(result, job_id=job_id, signal_ids=payload.get("signal_ids") or [])
    repository.update_background_job_progress(job_id, 95)
    return {"signal_card": True, "applied": applied}


def _run_otbor(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    """Агент «Отбор» в местном контуре (#130), как карточка сигнала: без внешних воркеров маршрут ИИ ставит
    задачу в местную очередь ai."""
    from oiltech_digest import otbor

    repository.update_background_job_progress(job_id, 20)
    result = otbor.process_payload(otbor.build_payload(payload))
    applied = otbor.apply_result(result, job_id=job_id, article_ids=payload.get("article_ids") or [],
                                 signal_ids=payload.get("signal_ids") or [], shadow=otbor.is_shadow(payload))
    repository.update_background_job_progress(job_id, 95)
    return {"otbor": True, "applied": applied}


def _run_signal_summary(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    """«Суть и перевод» сигналов в местном контуре (#131): как «Отбор»."""
    from oiltech_digest import signal_summary

    repository.update_background_job_progress(job_id, 20)
    result = signal_summary.process_payload(signal_summary.build_payload(payload))
    applied = signal_summary.apply_result(result, job_id=job_id, signal_ids=payload.get("signal_ids") or [])
    repository.update_background_job_progress(job_id, 95)
    return {"signal_summary": True, "applied": applied}


def _run_signal_scores(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    """Баллы «Карточки» (второй вызов, #132) в местном контуре: как «Суть и перевод»."""
    from oiltech_digest import signal_scores

    repository.update_background_job_progress(job_id, 20)
    result = signal_scores.process_payload(signal_scores.build_payload(payload))
    applied = signal_scores.apply_result(result, job_id=job_id, signal_ids=payload.get("signal_ids") or [])
    repository.update_background_job_progress(job_id, 95)
    return {"signal_scores": True, "applied": applied}


def _run_feedback_review(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    """«Разбор ОС» недельного среза (#189) в местном контуре: как баллы «Карточки»."""
    from oiltech_digest import feedback_review

    repository.update_background_job_progress(job_id, 20)
    result = feedback_review.process_payload(feedback_review.build_payload(payload))
    applied = feedback_review.apply_result(result, job_id=job_id, job_payload=payload)
    repository.update_background_job_progress(job_id, 95)
    return {"feedback_review": True, "applied": applied}


def _run_rule_check(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    """Автопроверка правила эксперта (#190) в местном контуре: как разбор ОС."""
    from oiltech_digest import rule_check

    repository.update_background_job_progress(job_id, 20)
    result = rule_check.process_payload(rule_check.build_payload(payload))
    applied = rule_check.apply_result(result, job_id=job_id, job_payload=payload)
    repository.update_background_job_progress(job_id, 95)
    return {"rule_check": True, "applied": applied}


def _run_admission_notify(payload: dict[str, Any], job_id: int) -> dict[str, Any]:
    """Код допуска отправляет только воркер NL (#113): с РФ-ядра Telegram недоступен. Вид
    зарегистрирован, чтобы ядро могло поставить задачу; очередь external-notify здесь не исполняется."""
    raise RuntimeError("admission_notify исполняется только внешним воркером (очередь external-notify)")


_HANDLERS: dict[str, Callable[[dict[str, Any], int], dict[str, Any]]] = {
    "digest_export": _run_digest_export,
    "process_articles": _run_process_articles,
    "parse_source_once": _run_parse_source_once,
    "scrape_source": _run_scrape_source,
    "diagnose_source": _run_diagnose_source,
    "reprint_review": _run_reprint_review,
    "refetch_text": _run_refetch_text,
    "signal_discovery": _run_signal_discovery,
    "signal_card": _run_signal_card,
    "otbor": _run_otbor,
    "signal_summary": _run_signal_summary,
    "signal_scores": _run_signal_scores,
    "feedback_review": _run_feedback_review,
    "rule_check": _run_rule_check,
    "admission_notify": _run_admission_notify,
}
