"""Зависание планировщика 24.09: потолок шага и уборка зомби.

24.09 08:32 шаг parse не вернулся 20 ч 45 мин: page.content() у листинга JPT ждал без
срока, шаги идут друг за другом — стоял весь цикл, ни сбора, ни постановки ИИ. Под
PID 1 контейнера к тому часу было около 2250 зомби chrome (по 2 на рендер; 25.09 в 05:31 —
2259) при pids.max 2315: браузеру не на чем было заводить потоки. run_step ведёт шаг под сроком
(oiltech_digest/step_timeout.py), а сервисы с Chromium идут с init, который их подбирает.

Процессы настоящие: срок — это сигналы и группы процессов, подделка их не проверит.
Тесты скрипта целиком падают на коде до правки: зависший шаг держал цикл.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from oiltech_digest import singleton

ROOT = Path(__file__).resolve().parents[1]

# Подставной python для docker-scheduler.sh: шаги cli отмечаются в файле («имя pid»), шаг
# HANG_STEP висит, а сторож шага и всё прочее идут настоящим интерпретатором.
FAKE_PYTHON = """#!/bin/sh
if [ "$1" = "-m" ] && [ "$2" = "oiltech_digest.cli" ]; then
  echo "$3 $$" >> "$STEPS_FILE"
  if [ "$3" = "$HANG_STEP" ]; then
    exec sleep 300
  fi
  exit 0
fi
exec "$REAL_PYTHON" "$@"
"""


def _wait(predicate, timeout: float) -> bool:
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
    except PermissionError:  # macOS так отвечает про зомби, которого ещё не подобрали
        return True
    return True


def _steps(path: Path) -> list[tuple[str, int]]:
    if not path.exists():
        return []
    return [(name, int(pid)) for name, pid in (line.split() for line in path.read_text().splitlines())]


def _fake_python_env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "python"
    fake.write_text(FAKE_PYTHON)
    fake.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "REAL_PYTHON": sys.executable,
        "STEPS_FILE": str(tmp_path / "steps"),
    }


def _scheduler(tmp_path: Path, **env: str) -> subprocess.Popen:
    """docker-scheduler.sh без замка и бутстрапа: первый шаг цикла — maintenance-cleanup."""
    out = tmp_path / "scheduler.out"
    proc = subprocess.Popen(
        ["sh", "scripts/docker-scheduler.sh"],
        cwd=str(ROOT),
        env={
            **_fake_python_env(tmp_path),
            "HANG_STEP": "maintenance-cleanup",
            "SCHEDULER_LOCK_HELD": "1",
            "SKIP_BOOTSTRAP": "1",
            "RUN_MAINTENANCE_ON_START": "1",
            "RUN_DISCOVER_ON_START": "1",
            "AI_PROCESS_LIMIT": "0",
            "FETCH_EXTERNAL_ENABLED": "0",
            "REPRINTS_INTERVAL_HOURS": "0",
            "CYCLE_INTERVAL_SECONDS": "300",
            "STEP_TIMEOUT_SECONDS": "1",
            "STEP_KILL_AFTER_SECONDS": "1",
            **env,
        },
        # Как у scheduler-lock: скрипт — лидер своей группы, остановка бьёт в группу.
        start_new_session=True,
        stdout=out.open("w"),
        stderr=subprocess.STDOUT,
    )
    proc.out_path = out  # type: ignore[attr-defined]
    proc.steps_path = tmp_path / "steps"  # type: ignore[attr-defined]
    return proc


def _cleanup(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):  # EPERM на macOS: в группе один зомби скрипта
        pass
    proc.wait(timeout=5)
    for name, pid in _steps(proc.steps_path):  # type: ignore[attr-defined]
        if name != "maintenance-cleanup":  # прочие подставные шаги выходят сами
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def test_hung_step_is_cut_and_the_cycle_goes_on(tmp_path):
    proc = _scheduler(tmp_path)
    try:
        # До правки maintenance-cleanup висел бы 300 с, и discover-rss не начался бы.
        assert _wait(lambda: "Cycle finished" in proc.out_path.read_text(), 20), proc.out_path.read_text()
        names = [name for name, _pid in _steps(proc.steps_path)]
        assert names[:3] == ["maintenance-cleanup", "discover-rss", "parse"]
        assert names[-2:] == ["check-lanes", "stats"]
        hung_pid = _steps(proc.steps_path)[0][1]
        assert not _alive(hung_pid)
        output = proc.out_path.read_text()
        assert "TIMEOUT maintenance-cleanup" in output
        assert "FAIL maintenance-cleanup exit=124" in output
        assert "OK discover-rss" in output
    finally:
        _cleanup(proc)


def test_failed_step_reports_its_real_exit_code(tmp_path):
    """`code="$?"` сразу после `if ...; fi` давал 0: по POSIX так выходит `if` без ветки.
    Лог писал «FAIL … exit=0», и run_required_step не останавливал скрипт никогда."""
    script = (ROOT / "scripts" / "docker-scheduler.sh").read_text()
    defs = script[script.index("log() {"):script.index("CYCLE_INTERVAL_SECONDS=")]
    result = subprocess.run(
        ["sh", "-c", defs + '\nrun_step fake sh -c "exit 3"; echo "returned=$?"'],
        cwd=str(ROOT),
        env={**_fake_python_env(tmp_path), "STEP_TIMEOUT_SECONDS": "30", "STEP_KILL_AFTER_SECONDS": "1"},
        capture_output=True, text=True, timeout=30,
    )
    assert "FAIL fake exit=3" in result.stdout, result.stdout + result.stderr
    assert "returned=3" in result.stdout


def test_container_stop_still_reaches_the_step(tmp_path):
    """scheduler-lock гасит скрипт сигналом всей его группе процессов (singleton._signal_child).
    coreutils `timeout` без --foreground увёл бы шаг в свою группу — и остановка контейнера
    до шага не дошла бы. Сторож оставляет шаг в группе скрипта."""
    proc = _scheduler(tmp_path, STEP_TIMEOUT_SECONDS="300")
    try:
        assert _wait(lambda: len(_steps(proc.steps_path)) == 1, 15)
        step_pid = _steps(proc.steps_path)[0][1]
        singleton._signal_child(proc, signal.SIGTERM)
        assert _wait(lambda: not _alive(step_pid), 5)
    finally:
        _cleanup(proc)


def _guard(*args: str, timeout: float = 30) -> tuple[int, float, str]:
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "oiltech_digest.step_timeout", *args],
        cwd=str(ROOT), capture_output=True, text=True, timeout=timeout,
    )
    return result.returncode, time.monotonic() - started, result.stdout + result.stderr


def test_guard_passes_the_step_exit_code_through():
    assert _guard("30", "1", "--", sys.executable, "-c", "raise SystemExit(0)")[0] == 0
    assert _guard("30", "1", "--", sys.executable, "-c", "raise SystemExit(3)")[0] == 3
    killed = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    assert _guard("30", "1", "--", sys.executable, "-c", killed)[0] == 128 + signal.SIGKILL


def test_guard_stops_a_step_past_the_deadline_with_124():
    code, seconds, output = _guard("1", "5", "--", sys.executable, "-c", "import time; time.sleep(60)")
    assert code == 124
    assert seconds < 5  # SIGTERM снял шаг сразу, до SIGKILL дело не дошло
    assert "SIGTERM" in output and "SIGKILL" not in output


def test_guard_kills_a_step_that_ignores_sigterm():
    ignores = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    code, seconds, output = _guard("1", "1", "--", sys.executable, "-c", ignores)
    assert code == 124
    assert 2 <= seconds < 10
    assert "SIGKILL" in output


def test_zero_deadline_runs_the_step_without_a_ceiling():
    code, _seconds, _output = _guard("0", "1", "--", sys.executable, "-c", "import time; time.sleep(1.5); raise SystemExit(5)")
    assert code == 5


def test_services_that_launch_chromium_reap_zombies():
    """25.09 под PID 1 планировщика было 2259 зомби chrome при pids.max 2315 (DefaultTasksMax
    systemd — 15% от threads-max): каждый рендер оставлял ~2 процесса, которые python под
    PID 1 не подбирает. init (tini) подбирает осиротевших, и слоты не кончаются."""
    core = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    for name in ("scheduler", "app", "worker", "playwright-worker"):
        assert core[name].get("init") is True, name
    nl = yaml.safe_load((ROOT / "docker-compose.external-worker.yml").read_text())["services"]
    assert nl["external-worker-browser"].get("init") is True


@pytest.mark.parametrize("value", ["", "abc", "-1"])
def test_guard_rejects_a_bad_deadline(value):
    code, _seconds, output = _guard(value, "1", "--", "true")
    assert code == 2
