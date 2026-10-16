"""Агент «Карточка сигнала» (#96, спека #93), шов 2 — воркер карточки на подставной модели и
контракт ядра с воркером: справочники в каждом вызове, брак ответа не роняет пачку, старая сборка
отказывает явно, полосы дня и пересчётов, остановка воркера (tests/signal_card_support.py).
"""

import pytest

from oiltech_digest import api, background_jobs, cli, contract, external_worker, signal_card, signal_card_pack
from oiltech_digest.db import repository
from oiltech_digest.processing import external_ai
from oiltech_digest.processing.openai_client import AIClientError
from tests.signal_card_support import MODEL, FakeModel, answer, api_get, business, feed_signal, run_worker, worker
from tests.signal_card_support import admin, contour, user  # noqa: F401 — фикстуры


def test_old_worker_build_refuses_the_card_job_and_check_lanes_shows_the_mismatch(user, contour, monkeypatch, capsys):
    feed_signal("Контракт SLB", "SLB подписала контракт с Saudi Aramco.")
    cli.main(["enqueue-signal-cards"])
    # Сборка NL до #96 (на проде — контракт 1): обработчика карточки у неё нет.
    old_build = {kind: target for kind, target in external_worker._HANDLERS.items() if kind != "signal_card"}
    monkeypatch.setattr(external_worker, "_HANDLERS", old_build)
    client = worker("external-ai", contract_number=1)

    job = client.claim()
    external_worker._run_job(client, job, lambda done=None: None)

    assert "Unsupported external job kind: signal_card" in repository.get_background_job(job["id"])["error_message"]
    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli.cmd_check_lanes(type("Args", (), {})())
    out = capsys.readouterr().out
    assert f"контракт 1, у ядра контракт {contract.CONTRACT}" in out
    assert "NL надо пересобрать" in out


def test_dictionaries_go_to_the_model_in_every_call(user, contour, monkeypatch):
    feed_signal("Контракт SLB", "SLB подписала контракт с Saudi Aramco.")
    feed_signal("Пилот робота", "Equinor испытывает робота на платформе.")
    model = FakeModel({"Контракт SLB": answer(), "Пилот робота": answer(companies=[])})
    monkeypatch.setattr(signal_card, "make_client", lambda: model)

    cli.main(["enqueue-signal-cards"])
    run_worker("external-ai")

    dictionaries = signal_card_pack.load_dictionaries()
    assert len(model.calls) == 2
    for call in model.calls:
        assert call["model"] == MODEL
        for field in ("event_type", "region", "technology_class", "maturity"):
            for value in dictionaries[field]["values"]:
                assert value in call["instructions"]
            assert set(dictionaries[field]["values"]) <= set(call["schema"]["schema"]["properties"][field]["enum"])


def test_answer_off_schema_is_not_written_and_does_not_drop_the_batch(user, admin, contour, monkeypatch):
    for title in ("Контракт SLB", "Модель молчит", "Не по схеме"):
        feed_signal(title, "SLB подписала контракт с Saudi Aramco.")
    model = FakeModel({
        "Контракт SLB": answer(),
        "Модель молчит": AIClientError("OpenAI returned non-JSON output"),
        "Не по схеме": {"event_type": "контракт", "companies": "SLB"},
    })
    monkeypatch.setattr(signal_card, "make_client", lambda: model)

    cli.main(["enqueue-signal-cards"])
    [job] = run_worker("external-ai")

    cards = business(user)
    assert cards["Контракт SLB"]["card_recipe"]
    for title in ("Модель молчит", "Не по схеме"):
        assert (cards[title]["card_recipe"], cards[title]["event_type"]) == (None, None)
    done = api_get(f"/api/jobs/{job['id']}", admin)
    assert done["status"] == "ok"
    errors = {item["signal_id"]: item["error"] for item in done["result"]["applied"]["errors"]}
    assert sorted(errors) == sorted(cards[title]["id"] for title in ("Модель молчит", "Не по схеме"))
    assert "не по схеме" in errors[cards["Не по схеме"]["id"]]


def test_no_model_no_fallback_to_the_feed_model(user, contour, monkeypatch, capsys):
    feed_signal("Контракт SLB", "SLB подписала контракт с Saudi Aramco.")
    monkeypatch.setattr(api.config, "SIGNAL_CARD_MODEL", "")

    with pytest.raises(SystemExit, match="SIGNAL_CARD_MODEL"):
        cli.main(["enqueue-signal-cards"])

    # Задача без модели (поставлена в обход шага) — явная ошибка воркера, а не модель ленты.
    repository.create_background_job("signal_card", {"signal_ids": [1]}, queue_name="external-ai",
                                     execution_region="external", capability="openai")
    model = FakeModel({"Контракт SLB": answer()})
    monkeypatch.setattr(signal_card, "make_client", lambda: model)
    [job] = run_worker("external-ai")

    assert model.calls == []
    assert "SIGNAL_CARD_MODEL" in repository.get_background_job(job["id"])["error_message"]


def test_scheduler_step_is_behind_the_flag_and_takes_new_signals_once(user, admin, contour, monkeypatch, capsys):
    for title in ("Свежий", "Старый", "Дубль"):
        feed_signal(title, "SLB подписала контракт с Saudi Aramco.")
    ids = {title: card["id"] for title, card in business(user).items()}
    with repository.get_connection() as conn:
        conn.execute("UPDATE signals SET created_at = now() - interval '10 days' WHERE id = %s", (ids["Старый"],))
        conn.execute("UPDATE signals SET merged_into_signal_id = %s WHERE id = %s", (ids["Свежий"], ids["Дубль"]))
        conn.commit()

    monkeypatch.setattr(api.config, "SIGNAL_CARD_ENABLED", False)
    cli.main(["enqueue-signal-cards"])
    assert "skipped reason=disabled" in capsys.readouterr().out
    assert api_get("/api/jobs", admin, kind="signal_card") == []

    monkeypatch.setattr(api.config, "SIGNAL_CARD_ENABLED", True)
    cli.main(["enqueue-signal-cards"])
    cli.main(["enqueue-signal-cards"])  # следующий цикл: сигнал уже в задаче

    jobs = api_get("/api/jobs", admin, kind="signal_card")
    assert [(job["queue"], job["payload"]["signal_ids"], job["payload"]["model"]) for job in jobs] == [
        ("external-ai", [ids["Свежий"]], MODEL)]


def test_named_signals_are_recalculated_in_the_bulk_lane(user, admin, contour, monkeypatch):
    monkeypatch.setattr(api.config, "AI_BULK_LANE_ENABLED", True)
    monkeypatch.setattr(api.config, "SIGNAL_CARD_ENABLED", False)  # пересчёт названных — не шаг планировщика
    for title in ("Контракт SLB", "Пилот робота"):
        feed_signal(title, "SLB подписала контракт с Saudi Aramco.")
    ids = sorted(card["id"] for card in business(user).values())
    model = FakeModel({"Контракт SLB": answer(), "Пилот робота": answer(event_type="пилот")})
    monkeypatch.setattr(signal_card, "make_client", lambda: model)

    cli.main(["enqueue-signal-cards", "--ids", ",".join(map(str, ids))])
    jobs = api_get("/api/jobs", admin, kind="signal_card")
    run_worker("external-ai-bulk")

    assert [(job["queue"], job["payload"]["signal_ids"]) for job in jobs] == [("external-ai-bulk", ids)]
    assert {title: card["event_type"] for title, card in business(user).items()} == {
        "Контракт SLB": "контракт", "Пилот робота": "пилот"}


def test_worker_stop_keeps_the_paid_part_and_requeues_the_rest(user, admin, contour, monkeypatch):
    for title in ("Первый", "Второй", "Третий"):
        feed_signal(title, "SLB подписала контракт с Saudi Aramco.")
    model = FakeModel({title: answer() for title in ("Первый", "Второй", "Третий")})
    monkeypatch.setattr(signal_card, "make_client", lambda: model)
    cli.main(["enqueue-signal-cards"])
    client = worker("external-ai")
    job = client.claim()

    def beat(done=None):
        # Выкат NL (SIGTERM) приходит, когда первая карточка уже оплачена.
        if done and done.get("cards"):
            raise external_ai.StopRequested("worker stopping")

    external_worker._run_job(client, job, beat)

    cards = business(user)
    assert [title for title, card in cards.items() if card["card_recipe"]] == ["Первый"]
    requeued = api_get(f"/api/jobs/{job['id']}", admin)
    assert (requeued["status"], requeued["payload"]["signal_ids"]) == (
        "queued", sorted(cards[title]["id"] for title in ("Второй", "Третий")))

    run_worker("external-ai")
    assert all(card["card_recipe"] for card in business(user).values())
    assert len(model.calls) == 3  # первый сигнал второй раз не оплачен


def test_card_job_runs_in_the_local_contour_too(user, monkeypatch):
    # Без внешних воркеров (местный стек) ИИ-задачи идут в местную очередь ai.
    monkeypatch.setattr(api.config, "EXTERNAL_WORKERS_ENABLED", False)
    monkeypatch.setattr(api.config, "SIGNAL_CARD_ENABLED", True)
    monkeypatch.setattr(api.config, "SIGNAL_CARD_MODEL", MODEL)
    monkeypatch.setattr(api.config, "BACKGROUND_JOB_INLINE", False)
    feed_signal("Контракт SLB", "SLB подписала контракт с Saudi Aramco.")
    monkeypatch.setattr(signal_card, "make_client", lambda: FakeModel({"Контракт SLB": answer()}))

    cli.main(["enqueue-signal-cards"])
    background_jobs.worker_loop(once=True, queue_names=["ai"], poll_seconds=0)

    assert business(user)["Контракт SLB"]["card_recipe"]["model"] == f"{MODEL}-2026-03-17"
