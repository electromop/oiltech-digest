"""Агент источников выключен в коде (решение владельца 08.11, config.SOURCE_AGENT_DISABLED).

Планировщик не ставит его шаги, ядро не ставит и не исполняет его задачи, команды и эндпоинты запуска отказывают;
чтение истории агента остаётся. Логику самого агента проверяют прежние тесты, включая его на время теста.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from oiltech_digest import api, background_jobs, cli, config

ADMIN = {"id": 5, "email": "admin@example.com", "role": "admin"}
SCHEDULER = Path(__file__).resolve().parents[1] / "scripts" / "docker-scheduler.sh"


def test_agent_is_off_by_code_default():
    assert config.SOURCE_AGENT_DISABLED is True


def test_scheduler_ignores_the_env_flag():
    text = SCHEDULER.read_text(encoding="utf-8")
    assert "\nSOURCE_DISCOVERY_ENABLED=0\n" in text
    assert '"${SOURCE_DISCOVERY_ENABLED' not in text


@pytest.mark.parametrize("kind", sorted(background_jobs.SOURCE_AGENT_KINDS))
def test_enqueue_refuses_agent_jobs(monkeypatch, kind):
    created = []
    monkeypatch.setattr(background_jobs.repository, "create_background_job", lambda *a, **k: created.append(a) or {})

    with pytest.raises(ValueError, match="агент источников выключен"):
        background_jobs.enqueue(kind, {})

    assert created == []


@pytest.mark.parametrize("kind", sorted(background_jobs.SOURCE_AGENT_KINDS))
def test_queued_agent_job_fails_without_retry_and_does_not_run(monkeypatch, kind):
    ran, failed = [], []
    monkeypatch.setitem(background_jobs._HANDLERS, kind, lambda payload, job_id: ran.append(job_id) or {})
    monkeypatch.setattr(background_jobs.repository, "fail_background_job",
                        lambda job_id, message, **kwargs: failed.append((job_id, message, kwargs)))
    monkeypatch.setattr(background_jobs.repository, "finish_background_job", lambda *a, **k: None)

    background_jobs.run_claimed({"id": 41, "kind": kind, "queue_name": "default", "payload_json": {}})

    assert ran == []
    assert failed == [(41, config.SOURCE_AGENT_DISABLED_MESSAGE, {})]


def test_other_jobs_still_run(monkeypatch):
    ran, finished = [], []
    monkeypatch.setitem(background_jobs._HANDLERS, "digest_export", lambda payload, job_id: ran.append(job_id) or {"ok": 1})
    monkeypatch.setattr(background_jobs.repository, "finish_background_job",
                        lambda job_id, result: finished.append((job_id, result)))

    background_jobs.run_claimed({"id": 42, "kind": "digest_export", "queue_name": "default", "payload_json": {}})

    assert ran == [42]
    assert finished == [(42, {"ok": 1})]


@pytest.mark.parametrize("argv", [
    ["discover-sources", "--topic", "бурение"],
    ["enqueue-source-discovery"],
    ["agent-plan"],
    ["enqueue-agent-plan"],
    ["agent-loop"],
    ["enqueue-agent-loop"],
    ["source-candidate-test", "7"],
    ["source-candidate-evaluate", "7"],
    ["source-candidate-approve", "7"],
])
def test_cli_run_commands_refuse(monkeypatch, argv):
    called = []
    monkeypatch.setattr(cli, "_setup_logging", lambda verbose: None)
    parser = cli.build_parser()
    monkeypatch.setattr(cli, "build_parser", lambda: parser)
    # Если отказ не сработает, команда не пойдёт в базу и сеть: вызов записывается.
    for action in parser._subparsers._group_actions[0].choices.values():
        action.set_defaults(func=lambda args: called.append(args.command))

    with pytest.raises(SystemExit, match="агент источников выключен"):
        cli.main(argv)

    assert called == []


@pytest.mark.parametrize("argv", [
    ["source-candidates"],
    # У scheduler-lock свой аргумент command (список) перекрывает имя подкоманды: проверка не должна на нём падать,
    # иначе встал бы весь планировщик (он идёт через scheduler-lock).
    ["scheduler-lock", "--key", "5", "--", "echo", "шаг"],
])
def test_cli_other_commands_still_run(monkeypatch, argv):
    called = []
    monkeypatch.setattr(cli, "_setup_logging", lambda verbose: None)
    parser = cli.build_parser()
    monkeypatch.setattr(cli, "build_parser", lambda: parser)
    parser._subparsers._group_actions[0].choices[argv[0]].set_defaults(func=lambda args: called.append(argv[0]))

    cli.main(argv)

    assert called == [argv[0]]


@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/source-discovery/discover", {"topic": "бурение", "seed_url": "https://example.com"}),
    ("get", "/api/source-discovery/plan", None),
    ("post", "/api/source-discovery/plan/enqueue", {}),
    ("post", "/api/source-discovery/loop/enqueue", {}),
    ("post", "/api/source-discovery/loop/dry-run", {}),
    ("post", "/api/source-candidates/7/evaluate", {}),
    ("post", "/api/source-candidates/7/approve", {}),
])
def test_api_run_endpoints_answer_410(method, path, body):
    api.app.dependency_overrides[api.require_user] = lambda: ADMIN
    try:
        client = TestClient(api.app)
        response = client.get(path) if method == "get" else client.post(path, json=body)
    finally:
        api.app.dependency_overrides.clear()

    assert response.status_code == 410
    assert response.json()["detail"] == config.SOURCE_AGENT_DISABLED_MESSAGE


def test_api_run_endpoint_checks_admin_first():
    api.app.dependency_overrides[api.require_user] = lambda: {**ADMIN, "role": "user"}
    try:
        response = TestClient(api.app).post("/api/source-discovery/loop/enqueue", json={})
    finally:
        api.app.dependency_overrides.clear()

    assert response.status_code == 403


def test_api_history_endpoints_still_answer(monkeypatch):
    monkeypatch.setattr(api.repository, "list_source_candidates", lambda **kwargs: [{"id": 7, "status": "rejected"}])
    api.app.dependency_overrides[api.require_user] = lambda: ADMIN
    try:
        response = TestClient(api.app).get("/api/source-candidates?limit=5")
    finally:
        api.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == [{"id": 7, "status": "rejected"}]
