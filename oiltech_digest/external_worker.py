"""HTTP-pull worker for the non-RU execution contour."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import json
import logging
import os
import threading
import time
from typing import Any, Callable

import requests

from oiltech_digest import config
from oiltech_digest.ingestion import external_fetch
from oiltech_digest.documents import external as documents_external
from oiltech_digest.processing import external_ai

logger = logging.getLogger(__name__)


def run_loop(
    *,
    core_api_url: str | None = None,
    token: str | None = None,
    worker_id: str | None = None,
    queues: list[str] | None = None,
    capabilities: list[str] | None = None,
    poll_seconds: float | None = None,
    once: bool = False,
    concurrency: int | None = None,
) -> None:
    settings = {
        "core_api_url": core_api_url or config.CORE_API_URL,
        "token": token or config.EXTERNAL_WORKER_TOKEN,
        "queues": queues or config.EXTERNAL_WORKER_QUEUES,
        "capabilities": capabilities or config.EXTERNAL_WORKER_CAPABILITIES,
    }
    base_id = worker_id or config.EXTERNAL_WORKER_ID
    sleep_seconds = config.EXTERNAL_WORKER_POLL_SECONDS if poll_seconds is None else poll_seconds
    slots = max(1, int(config.EXTERNAL_WORKER_CONCURRENCY if concurrency is None else concurrency))
    if slots == 1 or once:
        _claim_loop(ExternalWorkerClient(worker_id=base_id, **settings), sleep_seconds, once=once)
        return
    # Потоки полосы: у каждого свой клиент (своя сессия requests) и своё имя в claimed_by —
    # по нему видно, какой поток держит задачу. Выдача под SKIP LOCKED: одну задачу
    # два потока не получат.
    threads = [
        threading.Thread(
            target=_claim_loop,
            args=(ExternalWorkerClient(worker_id=f"{base_id}#{slot}", **settings), sleep_seconds),
            name=f"{base_id}#{slot}",
            daemon=True,
        )
        for slot in range(1, slots + 1)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def _claim_loop(client: "ExternalWorkerClient", sleep_seconds: float, *, once: bool = False) -> None:
    failures = 0
    while True:
        try:
            job = client.claim()
            failures = 0
        except Exception:  # noqa: BLE001 - ядро недоступно (выкат, сеть): ждём, а не умираем
            if once:
                raise
            failures += 1
            logger.warning("external_claim_failed worker=%s attempt=%s", client.worker_id, failures)
            time.sleep(min(sleep_seconds * (2 ** min(failures, 5)), 60.0))
            continue
        if job is None:
            if once:
                return
            time.sleep(sleep_seconds)
            continue
        _handle_job(client, job)


class ExternalWorkerClient:
    def __init__(
        self,
        *,
        core_api_url: str,
        token: str,
        worker_id: str,
        queues: list[str],
        capabilities: list[str],
    ) -> None:
        if not core_api_url:
            raise ValueError("CORE_API_URL is required for external-worker")
        if not token:
            raise ValueError("EXTERNAL_WORKER_TOKEN is required for external-worker")
        self.core_api_url = core_api_url.rstrip("/")
        self.worker_id = worker_id
        self.queues = queues
        self.capabilities = capabilities
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def claim(self) -> dict[str, Any] | None:
        response = self.session.post(
            f"{self.core_api_url}/api/external-worker/claim",
            json={
                "worker_id": self.worker_id,
                "queues": self.queues,
                "capabilities": self.capabilities,
                "max_lease_seconds": config.EXTERNAL_WORKER_DEFAULT_LEASE_SECONDS,
            },
            timeout=30,
        )
        response.raise_for_status()
        return (response.json() or {}).get("job")

    def progress(self, job: dict[str, Any], progress: float) -> None:
        response = self.session.post(
            f"{self.core_api_url}/api/external-worker/jobs/{job['id']}/progress",
            json={"lease_token": job["lease_token"], "progress": progress},
            timeout=30,
        )
        response.raise_for_status()

    def heartbeat(self, job: dict[str, Any]) -> None:
        """Продлить lease задачи (без lease_seconds core берёт дефолт 600с)."""
        response = self.session.post(
            f"{self.core_api_url}/api/external-worker/jobs/{job['id']}/heartbeat",
            json={"lease_token": job["lease_token"]},
            timeout=30,
        )
        response.raise_for_status()

    def fork(self) -> "ExternalWorkerClient":
        """Тот же воркер со своей сессией — для фонового продления аренды из другого потока."""
        clone = ExternalWorkerClient.__new__(ExternalWorkerClient)
        clone.core_api_url = self.core_api_url
        clone.worker_id = self.worker_id
        clone.queues = self.queues
        clone.capabilities = self.capabilities
        clone.session = requests.Session()
        clone.session.headers.update(self.session.headers)
        return clone

    def complete(self, job: dict[str, Any], result: dict[str, Any]) -> None:
        response = self.session.post(
            f"{self.core_api_url}/api/external-worker/jobs/{job['id']}/complete",
            json={"lease_token": job["lease_token"], "result": json_ready(result)},
            timeout=60,
        )
        response.raise_for_status()

    def fail(self, job: dict[str, Any], error: str, *, retryable: bool = True, retry_after_seconds: int = 300) -> None:
        response = self.session.post(
            f"{self.core_api_url}/api/external-worker/jobs/{job['id']}/fail",
            json={
                "lease_token": job["lease_token"],
                "error": error[:1000],
                "retryable": retryable,
                "retry_after_seconds": retry_after_seconds,
            },
            timeout=30,
        )
        response.raise_for_status()


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def json_ready(result: Any) -> Any:
    """Итог уходит ядру JSON-ом: дата — строкой ISO, число из базы — float.

    Без этого дата в любом поле итога роняла отправку уже сделанной (и оплаченной)
    работы: 18.09 — сбор (c858539), 21.09 — радар агентов; задача уходила в повтор и
    падала снова. Прочее непривычное по-прежнему — громкая ошибка, а не молчаливая строка."""
    return json.loads(json.dumps(result, default=_json_default))


def _safe_heartbeat(client: "ExternalWorkerClient", job: dict[str, Any]) -> None:
    try:
        client.heartbeat(job)
    except requests.HTTPError as exc:
        # 409 = core отозвал lease (задача возвращена в очередь). Это НЕ временный сбой:
        # результат уже не примут, поэтому батч прерываем немедленно, а не платим OpenAI
        # за мусор. Прочие HTTP-ошибки по-прежнему считаем временными.
        if exc.response is not None and exc.response.status_code == 409:
            logger.warning("external_lease_lost job_id=%s — прерываю обработку", job.get("id"))
            raise external_ai.LeaseLost(f"lease lost for job {job.get('id')}") from exc
        logger.warning("external_heartbeat_failed job_id=%s", job.get("id"))
    except Exception:  # noqa: BLE001 - сбой heartbeat не должен прерывать обработку
        logger.warning("external_heartbeat_failed job_id=%s", job.get("id"))


# Потолок времени задачи по виду, с. Замер 18–21.09: ИИ-пакет — до 19 мин, сбор — до
# 1,5 мин; потолок в 3–10 раз выше, он ловит зависание, а не медленный сайт.
_JOB_MAX_SECONDS = {
    "process_articles": 3600,
    "recheck_relevance": 3600,
    "translate_titles": 3600,
    "process_document": 3600,
    "reprint_review": 1800,
    "scrape_source": 900,
    "refetch_text": 900,
}


def job_deadline_seconds(kind: str | None) -> int:
    return _JOB_MAX_SECONDS.get(str(kind or ""), config.EXTERNAL_JOB_MAX_SECONDS)


def _exit_for_restart() -> None:
    # Зависший поток обработчика не прервать изнутри: выходим, Docker перезапускает
    # контейнер (restart: unless-stopped), задача возвращается в очередь по аренде.
    os._exit(70)


class LeaseKeeper:
    """Продлевает аренду задачи, пока та выполняется, — фоновым потоком.

    Раньше аренду продлевал только код обработчика, и новый код его забывал: 24.07
    (петля переотдачи и двойная оплата), 17.09 (загрузчики), 21.09 (докачка радара).
    Здесь это не зависит от обработчика. 409 от ядра — аренда отозвана: помечаем, и
    ближайший heartbeat обработчика прерывает работу (LeaseLost), чтобы не платить за
    выброшенный результат. По потолку времени задача возвращается в очередь, а процесс
    перезапускается — зависание лечится само, а не держит полосу."""

    def __init__(
        self,
        client: "ExternalWorkerClient",
        job: dict[str, Any],
        *,
        interval: float | None = None,
        max_seconds: float | None = None,
        on_deadline: Callable[[], None] = _exit_for_restart,
    ) -> None:
        self.client = client
        self.job = job
        self.interval = config.EXTERNAL_WORKER_HEARTBEAT_SECONDS if interval is None else interval
        self.max_seconds = job_deadline_seconds(job.get("kind")) if max_seconds is None else max_seconds
        self.on_deadline = on_deadline
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"lease-{job.get('id')}", daemon=True)

    def start(self) -> "LeaseKeeper":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        started = time.monotonic()
        while not self._stop.wait(self.interval):
            if time.monotonic() - started > self.max_seconds:
                logger.error(
                    "external_job_deadline job_id=%s kind=%s limit=%ss — задача в очередь, процесс на перезапуск",
                    self.job.get("id"), self.job.get("kind"), self.max_seconds,
                )
                try:
                    self.client.fail(self.job, f"потолок времени {int(self.max_seconds)} с", retryable=True,
                                     retry_after_seconds=60)
                except Exception:  # noqa: BLE001 - задача вернётся по истечении аренды
                    logger.warning("external_job_deadline_fail_report_failed job_id=%s", self.job.get("id"))
                self.on_deadline()
                return
            try:
                self.client.heartbeat(self.job)
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 409:
                    logger.warning("external_lease_lost job_id=%s — отмечено фоновым продлением", self.job.get("id"))
                    self.lost.set()
                    return
                logger.warning("external_heartbeat_failed job_id=%s", self.job.get("id"))
            except Exception:  # noqa: BLE001 - временный сбой: следующая попытка через interval
                logger.warning("external_heartbeat_failed job_id=%s", self.job.get("id"))


def _handle_job(client: ExternalWorkerClient, job: dict[str, Any]) -> None:
    fork = getattr(client, "fork", None)
    keeper = LeaseKeeper(fork() if callable(fork) else client, job).start()

    def beat() -> None:
        if keeper.lost.is_set():
            raise external_ai.LeaseLost(f"lease lost for job {job.get('id')}")
        _safe_heartbeat(client, job)

    try:
        _run_job(client, job, beat)
    finally:
        keeper.stop()


def _run_job(client: ExternalWorkerClient, job: dict[str, Any], beat: Callable[[], None]) -> None:
    logger.info("external_job_started job_id=%s kind=%s queue=%s", job["id"], job.get("kind"), job.get("queue"))
    try:
        if job.get("kind") == "process_articles":
            client.progress(job, 20)
            # Heartbeat по каждой статье продлевает lease — большой батч на медленной
            # модели (gpt-5.5) больше не истекает по lease и не уходит в ретрай-петлю.
            result = external_ai.process_payload(
                job.get("payload") or {},
                heartbeat=beat,
            )
            client.progress(job, 90)
            client.complete(job, result)
        elif job.get("kind") == "recheck_relevance":
            client.progress(job, 20)
            result = external_ai.process_recheck_payload(
                job.get("payload") or {},
                heartbeat=beat,
            )
            client.progress(job, 90)
            client.complete(job, result)
        elif job.get("kind") == "translate_titles":
            client.progress(job, 20)
            result = external_ai.process_translate_payload(
                job.get("payload") or {},
                heartbeat=beat,
            )
            client.progress(job, 90)
            client.complete(job, result)
        elif job.get("kind") == "process_document":
            client.progress(job, 20)
            result = documents_external.process_document_payload(
                job.get("payload") or {},
                heartbeat=beat,
            )
            client.progress(job, 90)
            client.complete(job, result)
        elif job.get("kind") == "scrape_source":
            client.progress(job, 20)
            result = external_fetch.process_payload(
                job.get("payload") or {},
                heartbeat=beat,
            )
            client.progress(job, 90)
            client.complete(job, result)
        elif job.get("kind") == "reprint_review":
            client.progress(job, 20)
            result = external_ai.process_reprint_review_payload(
                job.get("payload") or {},
                heartbeat=beat,
            )
            client.progress(job, 90)
            client.complete(job, result)
        elif job.get("kind") == "refetch_text":
            client.progress(job, 20)
            result = external_fetch.process_refetch_text_payload(
                job.get("payload") or {},
                heartbeat=beat,
            )
            client.progress(job, 90)
            client.complete(job, result)
        else:
            raise ValueError(f"Unsupported external job kind: {job.get('kind')}")
        logger.info("external_job_finished job_id=%s kind=%s", job["id"], job.get("kind"))
    except external_ai.LeaseLost:
        # Ни complete, ни fail слать нельзя — оба вернут 409. Просто выходим: задача уже
        # в очереди у core и будет выдана заново (возможно, этому же воркеру).
        logger.warning("external_job_abandoned job_id=%s — lease отозван, беру следующую", job.get("id"))
        return
    except Exception as exc:  # noqa: BLE001 - external failures must be returned to core
        logger.exception("external_job_failed job_id=%s kind=%s", job.get("id"), job.get("kind"))
        try:
            client.fail(job, str(exc), retryable=True)
        except Exception:
            logger.exception("external_job_fail_report_failed job_id=%s", job.get("id"))
