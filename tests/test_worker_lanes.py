"""Воркер полос (21.09): аренда продлевается фоном, зависание лечится перезапуском,
полоса может держать несколько потоков, каждая очередь в раскладке NL имеет воркера.

Каждый тест падает на коде до правки."""

import threading
import time
from pathlib import Path

import pytest
import requests
import yaml

from oiltech_digest import external_worker, lanes
from oiltech_digest.processing import external_ai

JOB = {"id": 7, "kind": "process_articles", "lease_token": "t", "payload": {}}


def _http_409() -> requests.HTTPError:
    response = requests.Response()
    response.status_code = 409
    return requests.HTTPError("409", response=response)


class _Client:
    def __init__(self, *, heartbeat_error: Exception | None = None, forked: "_Client | None" = None):
        self.heartbeats = 0
        self.completed: list = []
        self.failed: list = []
        self.heartbeat_error = heartbeat_error
        self.forked = forked
        self.worker_id = "nl-test"

    def fork(self):
        return self.forked or self

    def heartbeat(self, job):
        self.heartbeats += 1
        if self.heartbeat_error:
            raise self.heartbeat_error

    def progress(self, job, progress):
        pass

    def complete(self, job, result):
        self.completed.append(result)

    def fail(self, job, error, *, retryable=True, retry_after_seconds=300):
        self.failed.append((error, retryable))


def _wait_until(condition, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_lease_is_extended_even_when_handler_never_calls_heartbeat(monkeypatch):
    """Класс 24.07 / 17.09 / 21.09: новый код забывал beat() — аренда уходила."""
    monkeypatch.setattr(external_worker.config, "EXTERNAL_WORKER_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(external_ai, "process_payload", lambda payload, heartbeat=None: time.sleep(0.2) or {"ok": 1})
    keeper_client = _Client()
    client = _Client(forked=keeper_client)

    external_worker._handle_job(client, dict(JOB))

    assert keeper_client.heartbeats >= 5
    assert client.completed == [{"ok": 1}]


def test_revoked_lease_stops_paid_work_at_next_step(monkeypatch):
    """Ядро отозвало аренду (409) — фон отмечает, и следующий шаг обработчика прерывается:
    за выброшенный результат не платим (24.07: ~$11/ч в петле)."""
    monkeypatch.setattr(external_worker.config, "EXTERNAL_WORKER_HEARTBEAT_SECONDS", 0.01)
    steps = []

    def handler(payload, heartbeat=None):
        for _ in range(200):
            heartbeat()
            steps.append(1)
            time.sleep(0.01)
        return {"ok": 1}

    monkeypatch.setattr(external_ai, "process_payload", handler)
    monkeypatch.setattr(external_worker, "_safe_heartbeat", lambda client, job: None)  # сам шаг сеть не трогает
    client = _Client(forked=_Client(heartbeat_error=_http_409()))

    external_worker._handle_job(client, dict(JOB))

    assert 0 < len(steps) < 200
    assert client.completed == [] and client.failed == []


def test_hung_job_goes_back_to_queue_and_process_restarts():
    restarted = threading.Event()
    client = _Client()

    keeper = external_worker.LeaseKeeper(client, dict(JOB), interval=0.01, max_seconds=0.05, on_deadline=restarted.set)
    keeper.start()

    assert restarted.wait(2.0)
    assert client.failed and client.failed[0][1] is True  # retryable: задача вернётся в очередь
    keeper.stop()


def test_deadlines_catch_hangs_not_slow_but_normal_jobs():
    # Замер 18–21.09: ИИ-пакет ≤ 19 мин, сбор ≤ 1,5 мин.
    assert external_worker.job_deadline_seconds("process_articles") >= 3 * 19 * 60
    assert external_worker.job_deadline_seconds("scrape_source") >= 10 * 90
    assert external_worker.job_deadline_seconds("unknown_kind") == external_worker.config.EXTERNAL_JOB_MAX_SECONDS


def test_lane_runs_several_claim_threads_with_distinct_names(monkeypatch):
    seen = []
    lock = threading.Lock()

    def fake_loop(client, sleep_seconds, *, once=False):
        with lock:
            seen.append(client.worker_id)

    monkeypatch.setattr(external_worker, "_claim_loop", fake_loop)

    external_worker.run_loop(core_api_url="https://core.example", token="t", worker_id="nl-fetch-1",
                             queues=["external-fetch"], capabilities=["http_fetch"], concurrency=3)

    assert sorted(seen) == ["nl-fetch-1#1", "nl-fetch-1#2", "nl-fetch-1#3"]


def test_claim_loop_survives_core_outage(monkeypatch):
    """Раньше сбой выдачи ронял процесс; в полосе из трёх потоков так тихо умирал бы поток."""

    class Stop(BaseException):
        pass

    answers = [requests.ConnectionError("core restarting"), {"id": 1, "kind": "scrape_source"}, Stop()]
    handled = []

    class Client:
        worker_id = "nl-fetch-1#1"

        def claim(self):
            answer = answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer

    monkeypatch.setattr(external_worker.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(external_worker, "_handle_job", lambda client, job: handled.append(job["id"]))

    with pytest.raises(Stop):
        external_worker._claim_loop(Client(), 0.0)

    assert handled == [1]


def test_every_lane_has_its_own_nl_worker():
    """Раскладка NL: у каждой внешней очереди есть воркер, полосы не делят контейнер
    (иначе пересчёт снова встанет перед потоком дня, а браузер — перед RSS)."""
    compose = Path(__file__).resolve().parents[1] / "docker-compose.external-worker.yml"
    services = yaml.safe_load(compose.read_text())["services"]
    served: dict[str, list[str]] = {}
    for name, service in services.items():
        env = service["environment"]
        queues = [item.strip() for item in env["EXTERNAL_WORKER_QUEUES"].split(",")]
        assert len(queues) == 1, f"{name} слушает несколько полос: {queues}"
        served.setdefault(queues[0], []).append(name)

    assert set(served) == set(lanes.EXTERNAL_LANES)
    assert all(len(names) == 1 for names in served.values())
    by_queue = {queue: services[names[0]]["environment"] for queue, names in served.items()}
    assert by_queue[lanes.FETCH].get("EXTERNAL_WORKER_CONCURRENCY") == "3"
    assert by_queue[lanes.BROWSER].get("EXTERNAL_WORKER_CONCURRENCY", "1") == "1"
