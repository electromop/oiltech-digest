"""HTTP-pull worker for the non-RU execution contour."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import importlib
import json
import logging
import os
import signal
import threading
import time
from typing import Any, Callable

import requests

from oiltech_digest import config, contract
# Основные обработчики грузятся при старте, а не лениво по таблице _HANDLERS: сломанный
# импорт должен уронить запуск контейнера, а не первую задачу своего вида.
from oiltech_digest.ingestion import external_fetch  # noqa: F401
from oiltech_digest.documents import external as documents_external  # noqa: F401
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
    if once:
        _claim_loop(ExternalWorkerClient(worker_id=base_id, **settings), sleep_seconds, once=True)
        return
    # Потоки полосы: у каждого свой клиент (своя сессия requests) и своё имя в claimed_by —
    # по нему видно, какой поток держит задачу. Выдача под SKIP LOCKED: одну задачу
    # два потока не получат. Главный поток задач не берёт: он ждёт сигнала остановки и
    # возвращает ядру то, что не успело закончиться (_supervise).
    names = [base_id] if slots == 1 else [f"{base_id}#{slot}" for slot in range(1, slots + 1)]
    threads = [
        threading.Thread(
            target=_claim_loop,
            args=(ExternalWorkerClient(worker_id=name, **settings), sleep_seconds),
            name=name,
            daemon=True,
        )
        for name in names
    ]
    previous = _install_stop_signals()
    try:
        for thread in threads:
            thread.start()
        _supervise(threads)
    finally:
        _restore_signals(previous)


def _claim_loop(client: "ExternalWorkerClient", sleep_seconds: float, *, once: bool = False) -> None:
    failures = 0
    idle = sleep_seconds
    while not _halted():
        try:
            job = client.claim()
            failures = 0
        except Exception:  # noqa: BLE001 - ядро недоступно (выкат, сеть): ждём, а не умираем
            if once:
                raise
            failures += 1
            logger.warning("external_claim_failed worker=%s attempt=%s", client.worker_id, failures)
            _pause(min(sleep_seconds * (2 ** min(failures, 5)), 60.0))
            continue
        if job is None:
            if once:
                return
            _pause(idle)
            # Пусто — следующий вопрос реже: 3 → 6 → … → 30 с, на первой задаче снова 3.
            # 21.09 постоянные 3 с на шести потоках NL давали 582 claim за 5 мин простоя.
            idle = min(idle * 2, max(sleep_seconds, config.EXTERNAL_WORKER_POLL_MAX_SECONDS))
            continue
        idle = sleep_seconds
        if _STOPPING.is_set():
            # Задачу выдали в тот момент, когда пришёл сигнал: не начинаем, а сразу отдаём.
            _release(client, job, "остановка воркера: задача выдана в момент остановки")
            return
        _handle_job(client, job)
        if _halted():
            # Процесс уходит (остановка или перезапуск из-за зависшей соседки): новых
            # задач не берём, чтобы не оборвать их выходом.
            return


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
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            # Кто пришёл: ядро помнит сборку и контракт каждого контейнера NL (contract.py).
            contract.HEADER_BUILD: config.OILTECH_BUILD,
            contract.HEADER_CONTRACT: str(contract.CONTRACT),
        })

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

    def release(self, job: dict[str, Any], *, reason: str, result: dict[str, Any] | None = None) -> None:
        """Вернуть задачу ядру на остановке: сразу в очередь, попытка не списывается.

        result — сделанная часть пакета (partial): ядро запишет её и вычтет из задачи,
        чтобы при следующей выдаче модель не звалась за уже оплаченное."""
        response = self.session.post(
            f"{self.core_api_url}/api/external-worker/jobs/{job['id']}/release",
            json={
                "lease_token": job["lease_token"],
                "reason": reason[:500],
                "result": json_ready(result) if result else None,
            },
            timeout=60,
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


# Сколько задача может не подавать признаков продвижения (heartbeat обработчика —
# по статье, кандидату, куску документа), прежде чем её сочтут зависшей, с. Не общее
# время: пачка в 500 статей с шагом ~20 с идёт часами и должна дойти (ревью 21.09 —
# потолок на всё время трижды выбросил бы её оплаченную работу). Шаг ИИ — до нескольких
# минут на длинном рассуждении модели, шаг сбора — до ~1,5 мин на браузерной странице.
_JOB_STALL_SECONDS = {
    "process_articles": 1200,
    "recheck_relevance": 1200,
    "translate_titles": 1200,
    "process_document": 1800,
    "reprint_review": 1200,
    "scrape_source": 600,
    "refetch_text": 600,
}
# Процесс перед перезапуском перестаёт брать задачи и ждёт соседние потоки полосы: выход
# рвал бы их здоровые задачи (ждали бы истечения аренды и теряли попытку).
_DRAIN_SECONDS = 300
_DRAINING = threading.Event()
_INFLIGHT = 0
_INFLIGHT_LOCK = threading.Lock()


def _inflight(delta: int = 0) -> int:
    global _INFLIGHT
    with _INFLIGHT_LOCK:
        _INFLIGHT += delta
        return _INFLIGHT


def job_stall_seconds(kind: str | None) -> int:
    return _JOB_STALL_SECONDS.get(str(kind or ""), config.EXTERNAL_JOB_MAX_SECONDS)


def _exit_for_restart() -> None:
    # Зависший поток обработчика не прервать изнутри: выходим, Docker перезапускает
    # контейнер (restart: unless-stopped), задача уже возвращена в очередь.
    os._exit(70)


# --- Мягкая остановка (SIGTERM при выкате NL) --------------------------------------------
#
# До 23.09 обработчика SIGTERM не было, а stop_grace_period — 10 с по умолчанию: каждая
# пересборка NL обрывала задачи в работе, они ждали конца аренды (600 с), а оплаченная часть
# ИИ-пакета оплачивалась заново. Теперь по сигналу процесс перестаёт брать задачи, текущим
# даёт EXTERNAL_WORKER_STOP_GRACE_SECONDS на то, чтобы закончить, затем обработчик
# останавливается на ближайшем шаге и отдаёт ядру сделанное (release). Шаг, который так и
# не дошёл до границы за EXTERNAL_WORKER_STOP_STEP_SECONDS, главный поток возвращает сам.
_STOPPING = threading.Event()
_STOP_AT: float | None = None
# Сколько главный поток ждёт отчёта, который уже в пути (complete/release/fail).
_REPORT_SECONDS = 15.0


def request_stop(reason: str = "") -> None:
    """Остановить воркер мягко. Вызывается из обработчика сигнала — только флаг и время."""
    global _STOP_AT
    if _STOPPING.is_set():
        return
    _STOP_AT = time.monotonic()
    _STOPPING.set()


def _on_stop_signal(signum: int, frame: Any) -> None:
    request_stop(f"signal {signum}")


def _install_stop_signals() -> dict[int, Any]:
    if threading.current_thread() is not threading.main_thread():
        return {}
    return {signum: signal.signal(signum, _on_stop_signal) for signum in (signal.SIGTERM, signal.SIGINT)}


def _restore_signals(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        if handler is not None:
            signal.signal(signum, handler)


def _halted() -> bool:
    return _DRAINING.is_set() or _STOPPING.is_set()


def _pause(seconds: float) -> None:
    """Пауза, которую прерывает остановка: простаивающий воркер уходит сразу, а не через 30 с."""
    _STOPPING.wait(seconds)


def _stop_due() -> bool:
    """Срок на завершение вышел — обработчик останавливается на ближайшем шаге."""
    return _STOPPING.is_set() and time.monotonic() >= (_STOP_AT or 0.0) + config.EXTERNAL_WORKER_STOP_GRACE_SECONDS


class _InFlight:
    """Задача в работе у процесса. Отчитаться о ней должен ровно один: поток обработчика
    (complete/release/fail) или главный поток на остановке — иначе release главного потока
    мог бы опередить уже готовый complete и выбросить оплаченный итог."""

    __slots__ = ("client", "job", "state")

    def __init__(self, client: "ExternalWorkerClient", job: dict[str, Any]) -> None:
        self.client = client
        self.job = job
        self.state = "working"  # working → reporting (обработчик) | returned (главный поток)


_JOBS: dict[int, _InFlight] = {}
_JOBS_LOCK = threading.Lock()


def _track(client: "ExternalWorkerClient", job: dict[str, Any]) -> None:
    with _JOBS_LOCK:
        _JOBS[int(job["id"])] = _InFlight(client, job)


def _untrack(job: dict[str, Any]) -> None:
    with _JOBS_LOCK:
        _JOBS.pop(int(job["id"]), None)


def _begin_report(job: dict[str, Any]) -> bool:
    """Поток обработчика отчитывается сам — если задачу ещё не вернул главный поток."""
    with _JOBS_LOCK:
        entry = _JOBS.get(int(job["id"]))
        if entry is None:
            return True
        if entry.state == "returned":
            return False
        entry.state = "reporting"
        return True


def _take_unfinished() -> list[_InFlight]:
    with _JOBS_LOCK:
        taken = [entry for entry in _JOBS.values() if entry.state == "working"]
        for entry in taken:
            entry.state = "returned"
        return taken


def _reporting() -> bool:
    with _JOBS_LOCK:
        return any(entry.state == "reporting" for entry in _JOBS.values())


def _report(job: dict[str, Any], send: Callable[[], None]) -> bool:
    if not _begin_report(job):
        logger.warning("external_job_report_skipped job_id=%s — задачу уже вернули ядру на остановке", job.get("id"))
        return False
    send()
    return True


def _release(client: "ExternalWorkerClient", job: dict[str, Any], reason: str,
             *, result: dict[str, Any] | None = None) -> None:
    try:
        client.release(job, reason=reason, result=result)
        logger.warning("external_job_released job_id=%s kind=%s partial=%s — %s",
                       job.get("id"), job.get("kind"), bool(result), reason)
    except Exception:  # noqa: BLE001 - ядро недоступно или аренду уже сняли: вернётся по аренде
        logger.exception("external_job_release_failed job_id=%s — вернётся по истечении аренды", job.get("id"))


def _supervise(threads: list[threading.Thread]) -> None:
    """Главный поток: ждёт сигнала, а по нему возвращает ядру то, что не успело закончиться."""
    while any(thread.is_alive() for thread in threads):
        if _STOPPING.wait(1.0):
            break
    if not _STOPPING.is_set():
        return
    grace = config.EXTERNAL_WORKER_STOP_GRACE_SECONDS
    with _JOBS_LOCK:
        busy = sorted(_JOBS)
    logger.warning("external_worker_stopping — новых задач не беру; в работе %s, на завершение %s с",
                   busy or "нет", int(grace))
    deadline = (_STOP_AT if _STOP_AT is not None else time.monotonic()) + grace + config.EXTERNAL_WORKER_STOP_STEP_SECONDS
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    for entry in _take_unfinished():
        fork = getattr(entry.client, "fork", None)
        _release(fork() if callable(fork) else entry.client, entry.job, "остановка воркера: шаг не завершился в срок")
    report_deadline = time.monotonic() + _REPORT_SECONDS
    while _reporting() and time.monotonic() < report_deadline:
        time.sleep(0.1)
    logger.warning("external_worker_stopped")


class LeaseKeeper:
    """Продлевает аренду задачи, пока та выполняется, — фоновым потоком.

    Раньше аренду продлевал только код обработчика, и новый код его забывал: 24.07
    (петля переотдачи и двойная оплата), 17.09 (загрузчики), 21.09 (докачка радара).
    Здесь это не зависит от обработчика. 409 от ядра — аренда отозвана: помечаем, и
    ближайший heartbeat обработчика прерывает работу (LeaseLost), чтобы не платить за
    выброшенный результат. Если обработчик перестал подавать признаки продвижения
    (touch), задача возвращается в очередь, процесс перестаёт брать новые, ждёт
    соседние потоки и перезапускается — зависание лечится само, а не держит полосу."""

    def __init__(
        self,
        client: "ExternalWorkerClient",
        job: dict[str, Any],
        *,
        interval: float | None = None,
        stall_seconds: float | None = None,
        drain_seconds: float = _DRAIN_SECONDS,
        on_deadline: Callable[[], None] = _exit_for_restart,
    ) -> None:
        self.client = client
        self.job = job
        self.interval = config.EXTERNAL_WORKER_HEARTBEAT_SECONDS if interval is None else interval
        self.stall_seconds = job_stall_seconds(job.get("kind")) if stall_seconds is None else stall_seconds
        self.drain_seconds = drain_seconds
        self.on_deadline = on_deadline
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._last_progress = time.monotonic()
        self._thread = threading.Thread(target=self._run, name=f"lease-{job.get('id')}", daemon=True)

    def start(self) -> "LeaseKeeper":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def touch(self) -> None:
        """Обработчик продвинулся (прошёл статью, кандидата, кусок документа)."""
        self._last_progress = time.monotonic()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            if time.monotonic() - self._last_progress > self.stall_seconds:
                self._restart_stalled()
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

    def _restart_stalled(self) -> None:
        logger.error(
            "external_job_stalled job_id=%s kind=%s — нет продвижения %ss: в очередь, процесс на перезапуск",
            self.job.get("id"), self.job.get("kind"), int(self.stall_seconds),
        )
        try:
            self.client.fail(self.job, f"нет продвижения {int(self.stall_seconds)} с", retryable=True,
                             retry_after_seconds=60)
        except Exception:  # noqa: BLE001 - задача вернётся по истечении аренды
            logger.warning("external_job_stall_fail_report_failed job_id=%s", self.job.get("id"))
        _DRAINING.set()
        deadline = time.monotonic() + self.drain_seconds
        while _inflight() > 1 and time.monotonic() < deadline:
            time.sleep(1.0)
        self.on_deadline()


def _handle_job(client: ExternalWorkerClient, job: dict[str, Any]) -> None:
    fork = getattr(client, "fork", None)
    keeper = LeaseKeeper(fork() if callable(fork) else client, job).start()

    def beat() -> None:
        if keeper.lost.is_set():
            raise external_ai.LeaseLost(f"lease lost for job {job.get('id')}")
        if _stop_due():
            raise external_ai.StopRequested(f"worker stopping, job {job.get('id')}")
        keeper.touch()
        _safe_heartbeat(client, job)

    _inflight(+1)
    _track(client, job)
    try:
        _run_job(client, job, beat)
    finally:
        keeper.stop()
        _untrack(job)
        _inflight(-1)


# Вид задачи → обработчик на воркере: модуль по имени и функция. По имени, а не объектом:
# тесты подменяют атрибут модуля, и таблица обязана видеть подмену, а обработчик с тяжёлым
# импортом (радар агентов — при слиянии контуров) грузится только к первой своей задаче.
_HANDLERS: dict[str, tuple[str, str]] = {
    "process_articles": ("oiltech_digest.processing.external_ai", "process_payload"),
    "recheck_relevance": ("oiltech_digest.processing.external_ai", "process_recheck_payload"),
    "translate_titles": ("oiltech_digest.processing.external_ai", "process_translate_payload"),
    "process_document": ("oiltech_digest.documents.external", "process_document_payload"),
    "scrape_source": ("oiltech_digest.ingestion.external_fetch", "process_payload"),
    "reprint_review": ("oiltech_digest.processing.external_ai", "process_reprint_review_payload"),
    "refetch_text": ("oiltech_digest.ingestion.external_fetch", "process_refetch_text_payload"),
}


def _handler(kind: str) -> Callable[..., dict[str, Any]]:
    target = _HANDLERS.get(kind)
    if target is None:
        raise ValueError(f"Unsupported external job kind: {kind}")
    module_name, name = target
    return getattr(importlib.import_module(module_name), name)


def _run_job(client: ExternalWorkerClient, job: dict[str, Any], beat: Callable[[], None]) -> None:
    kind = str(job.get("kind") or "")
    logger.info("external_job_started job_id=%s kind=%s queue=%s", job["id"], kind, job.get("queue"))
    try:
        handler = _handler(kind)
        client.progress(job, 20)
        # Heartbeat по каждому шагу (статья, страница, кусок документа) продлевает lease —
        # большой батч на медленной модели не истекает по аренде и не уходит в ретрай-петлю.
        result = handler(job.get("payload") or {}, heartbeat=beat)
        if isinstance(result, dict) and result.get("partial"):
            _report(job, lambda: _release(client, job, "остановка воркера: возвращаю сделанное", result=result))
            return
        client.progress(job, 90)
        if _report(job, lambda: client.complete(job, result)):
            logger.info("external_job_finished job_id=%s kind=%s", job["id"], kind)
    except external_ai.StopRequested:
        # Вид без частичного итога (сбор, документ): задача уходит в очередь целиком.
        _report(job, lambda: _release(client, job, "остановка воркера до конца задачи"))
    except external_ai.LeaseLost:
        # Ни complete, ни fail слать нельзя — оба вернут 409. Просто выходим: задача уже
        # в очереди у core и будет выдана заново (возможно, этому же воркеру).
        logger.warning("external_job_abandoned job_id=%s — lease отозван, беру следующую", job.get("id"))
        return
    except Exception as exc:  # noqa: BLE001 - external failures must be returned to core
        logger.exception("external_job_failed job_id=%s kind=%s", job.get("id"), job.get("kind"))
        try:
            _report(job, lambda: client.fail(job, str(exc), retryable=True))
        except Exception:
            logger.exception("external_job_fail_report_failed job_id=%s", job.get("id"))
