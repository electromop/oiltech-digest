"""Сбой поиска радара виден админу.

23–27.09 поиск (тогда Brave, 402: исчерпан месячный лимит) не отвечал во всех 13 темах: сигналов 0,
а задача и прогон — ok, и пять дней этого никто не заметил. В итог темы шёл только
web_status, причина терялась.

Теперь итог темы хранит первую ошибку поиска GPT коротко, прогон — здоровье поиска, и это
одинаково на пути на месте и через воркер NL. Статус ЗАДАЧИ не меняется: его читает
правило «ежедневный прогон уже ставили в эти сутки». Здоровье отдаётся только админу."""

import json

import pytest
from fastapi.testclient import TestClient

from oiltech_digest import api, background_jobs, config, contract, network_policy, signal_discovery, signal_research
from oiltech_digest.db import repository
from oiltech_digest.processing.openai_client import AIClientError, AIResponse

TOPICS = ["Бурение", "Экология", "Цифровизация"]
WORKER_HEADERS = {"Authorization": "Bearer worker-secret", contract.HEADER_CONTRACT: str(contract.CONTRACT)}
ADMIN = {"id": 1, "email": "admin@example.com", "role": "admin"}
USER = {"id": 2, "email": "user@example.com", "role": "user"}
# Так клиент OpenAI называет отказ по лимиту, так его кладёт в research.error поиск (signal_research.research_topic).
RATE_LIMIT = 'OpenAI API error 429: {"error": {"message": "Rate limit reached for gpt-6.1-sol"}}'
FIRST_ERROR = f"AIClientError: {RATE_LIMIT}"


@pytest.mark.parametrize(
    ("web_search", "expected"),
    [
        ({"status": "error", "research": {"status": "error", "error": FIRST_ERROR}}, FIRST_ERROR),
        # Длинный текст ошибки на экране админа — коротко.
        ({"status": "error", "research": {"status": "error", "error": "AIClientError: " + "x" * 400}},
         ("AIClientError: " + "x" * 400)[:157] + "..."),
        # Поиск не отвечал, а текста нет: статус темы.
        ({"status": "error", "research": {}}, "error"),
        ({"status": "ok", "research": {"status": "ok", "events": 0}}, None),
        ({"status": "empty"}, None),
        (None, None),
    ],
)
def test_topic_summary_keeps_first_search_error_short(web_search, expected):
    assert signal_discovery._search_error_summary(web_search) == expected


class _Search:
    """Подставной поиск GPT: answer(тема) — список событий или исключение."""

    def __init__(self, answer):
        self.answer = answer
        self.topics: list[str] = []

    def research_json(self, instructions, user_input, schema, **kwargs):
        topic = user_input.splitlines()[0].removeprefix("Тема: ")
        self.topics.append(topic)
        events = self.answer(topic)
        if isinstance(events, Exception):
            raise events
        return AIResponse(data={"events": events, "_cited_urls": [], "_web_search_calls": 1},
                          model="gpt-6.1-sol", input_tokens=100, output_tokens=10)


def _search_answers(monkeypatch, answer) -> _Search:
    search = _Search(answer)
    monkeypatch.setattr(signal_research, "make_client", lambda offline=False: search)
    # Повтор поиска после сбоя без паузы: в проде пауза — от обрыва сети на секунду.
    monkeypatch.setattr(signal_research, "RESEARCH_RETRY_PAUSE_SECONDS", 0)
    return search


@pytest.fixture
def radar(isolated_db, monkeypatch):
    """Три корневые тематики заказчика = три темы радара; модель «Дедупа» не зовётся (находок нет)."""
    with repository.get_connection() as conn:
        for order, name in enumerate(TOPICS, start=1):
            conn.execute("INSERT INTO tags (name, enabled, sort_order) VALUES (%s, TRUE, %s)", (name, order))
        conn.commit()
    monkeypatch.setattr(config, "SIGNAL_RADAR_TOPIC_SOURCE", "tags")
    monkeypatch.setattr(config, "SIGNAL_DISCOVERY_DAILY_ENABLED", True)
    monkeypatch.setattr(config, "BACKGROUND_JOB_INLINE", False)
    monkeypatch.setattr(config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("worker-secret"))
    return isolated_db


def _route(monkeypatch, *, external: bool) -> None:
    decision = (
        network_policy.ExecutionDecision("external-ai", "external", "openai", "ai_external_enabled")
        if external else network_policy.ExecutionDecision("ai", "ru", "openai", "ai_local")
    )
    monkeypatch.setattr(network_policy, "route_ai_processing", lambda: decision)


def _daily_on_site(monkeypatch, *, force: bool = False) -> dict:
    """Ежедневный прогон, исполненный на ядре (CLI, локальная очередь)."""
    _route(monkeypatch, external=False)
    job_id = int(background_jobs.enqueue_daily_signal_discovery(force=force)["job"]["id"])
    background_jobs.run(job_id)
    return repository.get_background_job(job_id)


def _daily_through_worker(monkeypatch, *, force: bool = False) -> dict:
    """Ежедневный прогон через NL: выдача со снимком → прогон без базы → complete."""
    _route(monkeypatch, external=True)
    job_id = int(background_jobs.enqueue_daily_signal_discovery(force=force)["job"]["id"])
    # Часы базы в ВМ colima спешат и откатываются рывками (28.09: до 1,1 с назад за 20 с):
    # задача с run_after = now() на миг «из будущего», и выдача её не видит — тест плавал.
    with repository.get_connection() as conn:
        conn.execute("UPDATE background_jobs SET run_after = now() - interval '1 minute' WHERE id = %s", (job_id,))
        conn.commit()
    client = TestClient(api.app)
    claimed = client.post(
        "/api/external-worker/claim",
        headers=WORKER_HEADERS,
        json={"worker_id": "nl-agents-1", "queues": ["external-agents"], "capabilities": ["openai"]},
    ).json()["job"]
    with monkeypatch.context() as worker:
        def no_database(*args, **kwargs):
            raise RuntimeError("у воркера NL нет базы")

        for name in ("get_connection", "list_enabled_tags", "list_signal_agent_memory", "list_agent_memory"):
            worker.setattr(repository, name, no_database)
        result = signal_discovery.process_external_payload(json.loads(json.dumps(claimed["payload"])))
    completed = client.post(
        f"/api/external-worker/jobs/{claimed['id']}/complete",
        headers=WORKER_HEADERS,
        json={"lease_token": claimed["lease_token"], "result": json.loads(json.dumps(result))},
    )
    assert completed.status_code == 200
    return repository.get_background_job(int(claimed["id"]))


def _generation_run(job_id: int) -> dict:
    with repository.get_connection() as conn:
        row = conn.execute(
            "SELECT status, result_json, error_message FROM signal_generation_runs WHERE background_job_id = %s",
            (job_id,),
        ).fetchone()
    return {"status": row[0], "result": row[1], "error_message": row[2]}


def test_search_failure_gives_the_same_topic_summary_and_run_on_both_paths(radar, monkeypatch):
    search = _search_answers(monkeypatch, lambda topic: AIClientError(RATE_LIMIT))

    on_site = _daily_on_site(monkeypatch)
    through_worker = _daily_through_worker(monkeypatch, force=True)

    # Каждая тема, на каждом пути: попытка и повтор.
    assert sorted(search.topics) == sorted(TOPICS * 4)
    on_site_topics = on_site["result_json"]["topic_results"]
    worker_topics = through_worker["result_json"]["applied"]["topics"]
    assert [row["web_error"] for row in on_site_topics] == [FIRST_ERROR] * 3
    assert [(row["topic"], row["web_error"]) for row in worker_topics] == [
        (row["topic"], row["web_error"]) for row in on_site_topics
    ]
    assert [row["web_status"] for row in worker_topics] == ["error"] * 3

    run = _generation_run(int(on_site["id"]))
    assert run == _generation_run(int(through_worker["id"]))
    assert run["status"] == "failed"
    assert run["result"]["search_health"] == {"topics": 3, "failed": 3, "first_error": FIRST_ERROR}
    assert run["result"]["signals"] == 0
    assert run["error_message"] == f"Поиск не ответил во всех темах прогона (3 из 3): {FIRST_ERROR}"


@pytest.mark.parametrize("path", ["on_site", "through_worker"])
def test_search_failure_keeps_job_ok_and_the_daily_rule(radar, monkeypatch, path):
    """Прогон failed, а задача ok: правило «уже ставили в эти сутки» её видит — радар не
    переставляется каждые полчаса (134 запуска за 5 дней в сентябре)."""
    _search_answers(monkeypatch, lambda topic: AIClientError(RATE_LIMIT))
    enqueued: list = []

    job = (_daily_on_site if path == "on_site" else _daily_through_worker)(monkeypatch)

    assert job["status"] == "ok"
    assert _generation_run(int(job["id"]))["status"] == "failed"
    monkeypatch.setattr(background_jobs, "enqueue", lambda *args, **kwargs: enqueued.append(args))
    assert background_jobs.enqueue_daily_signal_discovery()["reason"] == "already_scheduled"
    assert enqueued == []


def test_partial_search_failure_keeps_run_ok_with_health(radar, monkeypatch):
    _search_answers(monkeypatch, lambda topic: [] if topic == "Цифровизация" else AIClientError(RATE_LIMIT))

    job = _daily_through_worker(monkeypatch)

    run = _generation_run(int(job["id"]))
    assert run["status"] == "ok"
    assert run["error_message"] is None
    assert run["result"]["search_health"] == {"topics": 3, "failed": 2, "first_error": FIRST_ERROR}
    # Тема, где поиск ответил пусто, — не сбой.
    assert [row["web_error"] for row in job["result_json"]["applied"]["topics"]] == [FIRST_ERROR, FIRST_ERROR, None]


def _get(path: str, user: dict):
    api.app.dependency_overrides[api.require_user] = lambda: user
    try:
        return TestClient(api.app).get(path)
    finally:
        api.app.dependency_overrides.pop(api.require_user, None)


def test_search_health_goes_to_admin_only(radar, monkeypatch):
    _search_answers(monkeypatch, lambda topic: AIClientError(RATE_LIMIT))
    job = _daily_through_worker(monkeypatch)
    old = repository.upsert_signal({"signal_key": "k-old", "title": "Прошлый сигнал", "theme": "Бурение", "score": 60})
    repository.upsert_signal_evidence(old, {"source_url": "https://example.com/old", "title": "Прошлый сигнал"})

    admin = _get("/api/signals/search-health", ADMIN)

    assert admin.status_code == 200
    health = admin.json()["search_health"]
    assert health["run_at"] == job["started_at"].isoformat()
    assert {key: health[key] for key in ("status", "signals", "topics", "failed", "first_error", "job_id")} == {
        "status": "failed", "signals": 0, "topics": 3, "failed": 3, "first_error": FIRST_ERROR, "job_id": job["id"],
    }

    read: list = []
    monkeypatch.setattr(repository, "latest_signal_generation_run", lambda **kwargs: read.append(kwargs))
    denied = _get("/api/signals/search-health", USER)
    assert denied.status_code == 403
    assert read == []  # до базы запрос не дошёл
    # Лента радара обычного пользователя — прежняя: список карточек, здоровья поиска в ней нет.
    signals = _get("/api/signals", USER).json()
    assert [row["title"] for row in signals] == ["Прошлый сигнал"]
    assert not any("search_health" in row or "web_error" in row for row in signals)


def _job_with_run(*, daily: bool, run_status: str, health: dict | None, minutes_ago: int) -> int:
    payload = background_jobs.daily_signal_discovery_payload() if daily else {"topic": "Бурение"}
    result = {"topics": 3, "signals": 1}
    if health is not None:
        result["search_health"] = health
    with repository.get_connection() as conn:
        job_id = conn.execute(
            """
            INSERT INTO background_jobs (kind, queue_name, status, payload_json, max_attempts, created_at)
            VALUES ('signal_discovery', 'external-agents', 'ok', %s::jsonb, 1, now() - make_interval(mins => %s))
            RETURNING id
            """,
            (json.dumps(payload), minutes_ago),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO signal_generation_runs (background_job_id, trigger, status, result_json)
            VALUES (%s, 'signal_discovery_external', %s, %s::jsonb)
            """,
            (job_id, run_status, json.dumps(result)),
        )
        conn.commit()
    return job_id


def test_search_health_is_of_the_latest_finished_daily_run(radar):
    failed = {"topics": 3, "failed": 3, "first_error": FIRST_ERROR}
    healthy = {"topics": 3, "failed": 0, "first_error": None}
    _job_with_run(daily=True, run_status="failed", health=failed, minutes_ago=3 * 24 * 60)
    latest_daily = _job_with_run(daily=True, run_status="ok", health=healthy, minutes_ago=24 * 60)
    _job_with_run(daily=False, run_status="failed", health=failed, minutes_ago=60)  # ручной прогон — не ежедневный
    _job_with_run(daily=True, run_status="running", health=None, minutes_ago=5)  # ещё идёт

    health = _get("/api/signals/search-health", ADMIN).json()["search_health"]

    # После успешного прогона сбоя нет — экран плашку не покажет.
    assert (health["job_id"], health["failed"], health["topics"]) == (latest_daily, 0, 3)


def test_search_health_is_empty_without_daily_runs(radar):
    assert _get("/api/signals/search-health", ADMIN).json() == {"search_health": None}
