"""Ровно один планировщик (сессия C, п. 2).

21.09 лишний `compose up` поднял второй планировщик: 8,5 ч дублей сбора и ИИ ($4,48),
радар дня потерян. Теперь процесс планировщика держит advisory lock в Postgres всё время
жизни; второй пишет в лог и ждёт, не делая ни одного шага.

Процессы настоящие: замок — это сессия базы, подделка в памяти его не проверит.
Каждый тест падает на коде до правки."""

import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

import psycopg
import pytest
import yaml

from oiltech_digest.config import DATABASE_URL

ROOT = Path(__file__).resolve().parents[1]
# Шаг «планировщика»: отмечается в файле и живёт, пока его не остановят.
STEP = ("import os, sys, time\n"
        "open(sys.argv[1], 'a').write(f\"{os.getpid()} {os.environ.get('SCHEDULER_LOCK_HELD')}\\n\")\n"
        "time.sleep(120)\n")


def _wrapper(key: int, marker: Path, *extra: str) -> subprocess.Popen:
    # Вывод — в файл, а не в pipe: шаг живёт в своей группе процессов, и оставшись сиротой,
    # он держал бы pipe открытым — чтение вывода висело бы до его конца.
    out = marker.with_name(f"{marker.name}.{time.monotonic_ns()}.out")
    proc = subprocess.Popen(
        [sys.executable, "-m", "oiltech_digest.cli", "scheduler-lock", "--key", str(key),
         "--poll-seconds", "0.2", *extra, "--", sys.executable, "-c", STEP, str(marker)],
        cwd=str(ROOT), env={**os.environ, "PYTHONUNBUFFERED": "1"},
        stdout=out.open("w"), stderr=subprocess.STDOUT,
    )
    proc.out_path = out  # type: ignore[attr-defined]
    proc.marker = marker  # type: ignore[attr-defined]
    return proc


def _output(proc: subprocess.Popen) -> str:
    return proc.out_path.read_text()  # type: ignore[attr-defined]


def _steps(marker: Path) -> list[list[str]]:
    return [line.split() for line in marker.read_text().splitlines()] if marker.exists() else []


def _wait(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _stop(*procs: subprocess.Popen) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)  # обёртка гасит свои шаги сама
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        for pid, _held in _steps(proc.marker):  # type: ignore[attr-defined]
            if _alive(int(pid)):
                os.kill(int(pid), signal.SIGKILL)


@pytest.fixture()
def key() -> int:
    # Замок общий на всю базу, а не на схему теста: у каждого теста свой ключ.
    return random.randint(10**9, 2 * 10**9)


def test_second_scheduler_waits_and_runs_no_steps(tmp_path, key):
    marker = tmp_path / "steps"
    first = _wrapper(key, marker)
    second = None
    try:
        assert _wait(lambda: len(_steps(marker)) == 1)
        assert _steps(marker)[0][1] == "1"  # шаги знают, что замок у их процесса

        second = _wrapper(key, marker)
        time.sleep(2.0)
        assert len(_steps(marker)) == 1  # второй не сделал ни одного шага
        assert second.poll() is None  # и не упал — ждёт

        first.send_signal(signal.SIGTERM)
        first.wait(timeout=15)
        assert _wait(lambda: len(_steps(marker)) == 2)  # замок перешёл к ждущему
    finally:
        _stop(*(proc for proc in (first, second) if proc is not None))


def test_waiting_scheduler_says_who_holds_the_lock(tmp_path, key):
    marker = tmp_path / "steps"
    first = _wrapper(key, marker)
    second = None
    try:
        assert _wait(lambda: len(_steps(marker)) == 1)
        second = _wrapper(key, marker)
        time.sleep(1.5)
        second.send_signal(signal.SIGTERM)
        second.wait(timeout=15)
    finally:
        _stop(*(proc for proc in (first, second) if proc is not None))
    output = _output(second)
    assert "жду" in output and f"scheduler-lock:{key}" in output
    assert second.returncode != 0


def test_steps_stop_when_lock_connection_is_lost(tmp_path, key):
    """Замок — сессия базы. Порвалась (перезапуск Postgres) — второй экземпляр может его
    взять, поэтому шаги этого процесса обязаны остановиться, а не идти без замка."""
    marker = tmp_path / "steps"
    first = _wrapper(key, marker, "--check-seconds", "0.3")
    try:
        assert _wait(lambda: len(_steps(marker)) == 1)
        step_pid = int(_steps(marker)[0][0])
        with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
            killed = admin.execute(
                "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE application_name LIKE %s",
                (f"oiltech-scheduler-lock:{key}%",),
            ).fetchone()[0]
        assert killed == 1
        first.wait(timeout=15)
        assert first.returncode == 75  # Docker поднимет заново, и замок будет взят честно
        assert _wait(lambda: not _alive(step_pid), 5)
    finally:
        _stop(first)


def test_sigterm_stops_scheduler_steps_quickly(tmp_path, key):
    marker = tmp_path / "steps"
    first = _wrapper(key, marker)
    try:
        assert _wait(lambda: len(_steps(marker)) == 1)
        step_pid = int(_steps(marker)[0][0])
        started = time.monotonic()
        first.send_signal(signal.SIGTERM)
        first.wait(timeout=15)
        assert time.monotonic() - started < 5  # раньше PID 1-shell игнорировал SIGTERM, Docker ждал 10 с
        assert _wait(lambda: not _alive(step_pid), 5)
    finally:
        _stop(first)


def test_scheduler_script_takes_the_lock_before_any_step():
    script = (ROOT / "scripts" / "docker-scheduler.sh").read_text().splitlines()
    lock_line = next(index for index, line in enumerate(script) if "scheduler-lock" in line and "exec " in line)
    first_step = next(index for index, line in enumerate(script)
                      if line.strip().startswith(("run_step ", "run_required_step ")))
    assert lock_line < first_step


def test_bare_compose_up_cannot_start_pipeline_services():
    """Голый `up -d` 21.09 поднял всё. Конвейер — только по профилю или по имени сервиса."""
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    for name in ("worker", "playwright-worker", "scheduler"):
        assert "pipeline" in (services[name].get("profiles") or []), name
    # tasks — архивный модуль (сессия B): его голый `up` тоже не поднимает, но другим профилем.
    assert services["tasks"].get("profiles"), "tasks"
    for name in ("db", "app", "caddy"):
        assert not services[name].get("profiles"), name
