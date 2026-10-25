"""Агент «Отбор» для ленты (#130, спека #128), шов 1 — воркер «Отбора» на подставной модели и контракт ядра с
воркером: старая сборка отказывает явно, модель без отката на модель ленты, шаг планировщика за флагом, сбой по
причине (технический повторяется до трёх задач, ответ не по правилам — нет), один повтор при конфликте шагов,
расход под именем агента, остановка воркера (tests/otbor_support.py).
"""

from pathlib import Path

import pytest

from oiltech_digest import api, background_jobs, cli, contract, external_worker, otbor
from oiltech_digest.db import connection, repository
from oiltech_digest.processing import external_ai
from oiltech_digest.processing.openai_client import AIClientError
from tests.otbor_support import MODEL, OtborModel, answer, article, decide, reject, showcase
from tests.otbor_support import contour  # noqa: F401 — фикстура
from tests.signal_card_support import admin, api_get, run_worker, user, worker  # noqa: F401 — фикстуры


def _jobs(admin):
    return api_get("/api/jobs", admin, kind="otbor")


def test_old_worker_build_refuses_the_otbor_job_and_check_lanes_shows_the_mismatch(user, contour, monkeypatch, capsys):
    article("Программа бурения Equinor", "Equinor начнёт бурение 12 скважин на шельфе Норвегии в 2027 году.")
    cli.main(["enqueue-otbor"])
    # Сборка NL до #130 (контракт 3): обработчика «Отбора» у неё нет.
    old_build = {kind: target for kind, target in external_worker._HANDLERS.items() if kind != "otbor"}
    monkeypatch.setattr(external_worker, "_HANDLERS", old_build)
    client = worker("external-ai", contract_number=3)

    job = client.claim()
    external_worker._run_job(client, job, lambda done=None: None)

    assert "Unsupported external job kind: otbor" in repository.get_background_job(job["id"])["error_message"]
    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli.cmd_check_lanes(type("Args", (), {})())
    out = capsys.readouterr().out
    assert f"контракт 3, у ядра контракт {contract.CONTRACT}" in out
    assert contract.CONTRACT == 5


def test_build_before_131_refuses_the_signal_summary_job(user, contour, monkeypatch):
    # Сборка NL контракта 4: «Суть и перевод» сигналов у неё нет — отказ явной ошибкой, а не тихий пропуск.
    repository.create_background_job("signal_summary", {"signal_ids": [1]}, queue_name="external-ai",
                                     execution_region="external", capability="openai")
    old_build = {kind: target for kind, target in external_worker._HANDLERS.items() if kind != "signal_summary"}
    monkeypatch.setattr(external_worker, "_HANDLERS", old_build)
    client = worker("external-ai", contract_number=4)

    job = client.claim()
    external_worker._run_job(client, job, lambda done=None: None)

    assert "Unsupported external job kind: signal_summary" in repository.get_background_job(job["id"])["error_message"]


def test_no_model_no_fallback_to_the_feed_model(user, contour, monkeypatch):
    article("Программа бурения Equinor", "Equinor начнёт бурение 12 скважин на шельфе Норвегии в 2027 году.")
    monkeypatch.setattr(api.config, "OTBOR_MODEL", "")

    with pytest.raises(SystemExit, match="OTBOR_MODEL"):
        cli.main(["enqueue-otbor"])

    # Задача без модели (поставлена в обход шага) — явная ошибка воркера, а не модель ленты.
    repository.create_background_job("otbor", {"article_ids": [1]}, queue_name="external-ai",
                                     execution_region="external", capability="openai")
    model = OtborModel({})
    monkeypatch.setattr(otbor, "make_client", lambda: model)
    [job] = run_worker("external-ai")

    assert model.calls == []
    assert "OTBOR_MODEL" in repository.get_background_job(job["id"])["error_message"]


def test_scheduler_step_is_behind_the_flag_and_takes_new_articles_once(user, admin, contour, monkeypatch, capsys):
    first = article("Программа бурения Equinor", "Equinor начнёт бурение 12 скважин на шельфе Норвегии в 2027 году.")
    monkeypatch.setattr(api.config, "OTBOR_ENABLED", False)
    cli.main(["enqueue-otbor"])
    assert "skipped reason=disabled" in capsys.readouterr().out
    assert _jobs(admin) == []

    monkeypatch.setattr(api.config, "OTBOR_ENABLED", True)
    cli.main(["enqueue-otbor"])
    cli.main(["enqueue-otbor"])  # следующий цикл: статья уже в задаче

    assert [(job["queue"], job["payload"]["article_ids"], job["payload"]["model"]) for job in _jobs(admin)] == [
        ("external-ai", [first], MODEL)]


def test_technical_failure_is_retried_up_to_three_tasks(user, admin, contour, monkeypatch):
    article("Модель молчит", "Equinor испытает робота-бурильщика на шельфе в 2027 году.")
    answers = {"Модель молчит": AIClientError("timeout")}
    monkeypatch.setattr(otbor, "make_client", lambda: OtborModel(answers))

    for _ in range(4):  # четыре цикла планировщика: модель молчит в каждом
        cli.main(["enqueue-otbor"])
        run_worker("external-ai")

    # API не ответил — до трёх вызовов (решение владельца 23.10), дальше — только командой с номером.
    assert len(_jobs(admin)) == 3
    answers["Модель молчит"] = answer()
    cli.main(["enqueue-otbor"])
    assert len(_jobs(admin)) == 3
    assert showcase(user) == {}


def test_whole_task_failure_leaves_the_article_in_the_no_answer_section_once_the_tries_are_over(
        user, admin, contour, monkeypatch, capsys):
    # Задача падает целиком (сборка воркера, настройка, сбой воркера): итога нет, ядро ничего не пишет. После
    # попыток статья не пропадает, а лежит в разделе «„Отбор“ не ответил» (решение владельца 23.10).
    article("Задача падает", "Equinor испытает робота-бурильщика на шельфе в 2027 году.")
    client = worker("external-ai")
    for _ in range(3):
        cli.main(["enqueue-otbor"])
        client.fail(client.claim(), "воркер упал", retryable=False)
    assert api_get("/api/signals", admin, section="otbor_failed") == []

    for _ in range(2):  # следующие циклы: задач больше нет, пометка одна
        cli.main(["enqueue-otbor"])
    [card] = api_get("/api/signals", admin, section="otbor_failed")
    assert card["title"] == "Задача падает"
    assert "задачи «Отбора» с ней падали целиком: 3 из 3" in card["section_reason"]
    assert [step["stage"] for step in card["decision_log"]] == ["«Отбор»"]
    assert len(_jobs(admin)) == 3


def test_answer_off_rules_is_not_written_not_retried_and_does_not_drop_the_batch(user, admin, contour, monkeypatch):
    monkeypatch.setattr(api.config, "OTBOR_ENABLED", False)
    gate_article = article("Статья гейта", "Weatherford получила контракт на бурение 274 скважин в Омане.")
    monkeypatch.setattr(api.config, "OTBOR_ENABLED", True)
    article("Программа бурения Equinor", "Equinor начнёт бурение 12 скважин на шельфе Норвегии в 2027 году.")
    off_schema = article("Не по схеме", "SLB подписала контракт с Saudi Aramco на бурение 40 скважин.")
    off_dictionary = article("Не из справочника", "Нефть дешевеет после роста запасов нефти в США.")
    answers = {"Программа бурения Equinor": answer(), "Не по схеме": {"decision": "accept"},
               "Не из справочника": answer(category="рынок")}

    model = decide(monkeypatch, answers)
    cli.main(["enqueue-otbor"])  # следующий цикл: ответ не по правилам шаг не повторяет
    run_worker("external-ai")

    assert set(showcase(user)) == {"Программа бурения Equinor"}
    assert len(model.calls) == 3
    [job] = _jobs(admin)
    errors = {item["article_id"]: item["error"]
              for item in api_get(f"/api/jobs/{job['id']}", admin)["result"]["applied"]["errors"]}
    assert errors == {off_schema: "ответ не по правилам: нет строки industry",
                      off_dictionary: "ответ не по правилам: значение не из справочника: category = «рынок»"}
    # Повтор — только командой с номером, после разбора; статью гейта команда не трогает.
    monkeypatch.setattr(api.config, "AI_BULK_LANE_ENABLED", True)
    cli.main(["enqueue-otbor", "--ids", f"{off_schema},{gate_article}"])
    assert [(job["queue"], job["payload"]["article_ids"]) for job in _jobs(admin)
            if job["payload"]["trigger"] == "manual"] == [("external-ai-bulk", [off_schema])]


def test_step_conflict_gets_one_retry_with_the_conflict_then_the_decision_stays_disputed(user, contour, monkeypatch):
    article("Исправился", "Equinor начнёт бурение 12 скважин на шельфе Норвегии в 2027 году.")
    article("Упорствует", "Weatherford получила контракт на бурение 274 скважин в Омане.")
    conflict = answer(industry="other_industry")  # прямая связь у другой отрасли — шаги не сошлись
    model = decide(monkeypatch, {"Исправился": [conflict, answer()], "Упорствует": [conflict, conflict]})

    cards = showcase(user)
    by_title = {}
    for call in model.calls:
        by_title.setdefault(call["input"].splitlines()[0], []).append(call["input"])
    # Ровно один повтор, и в нём указан конфликт.
    assert {title: len(inputs) for title, inputs in by_title.items()} == {
        "Заголовок: Исправился": 2, "Заголовок: Упорствует": 2}
    assert "прямая связь у другой отрасли" in by_title["Заголовок: Исправился"][1]
    assert "шаги не сошлись" not in by_title["Заголовок: Исправился"][0]
    assert (cards["Исправился"]["industry"], cards["Исправился"]["otbor_disputed"]) == ("oil_gas", None)
    assert cards["Упорствует"]["industry"] == "other_industry"
    assert "прямая связь у другой отрасли" in cards["Упорствует"]["otbor_disputed"]
    assert [step["stage"] for step in cards["Упорствует"]["decision_log"]] == ["«Отбор»"] * 3


def test_step_table_approved_23_10_allows_transfer_from_oil_gas_any_category_and_reject_without_one(
        user, admin, contour, monkeypatch):
    # Таблица конфликтов шагов утверждена владельцем 23.10 (решение 29 журнала гриля, раздел 5 разбора разметки):
    # переносимая — и у нефтегаза, при любой категории; категория у «не брать» не обязательна.
    transfer = {"oilfield_relevance": "transferable", "oilfield_application": "Задача: осмотр без людей на высоте"}
    article("Экзоскелеты Башнефти", "Башнефть перенесла экзоскелеты с НПЗ на ремонт скважин.")
    article("Двойники верфей", "Akselos и UPTEC переносят цифровые двойники верфей на морские платформы.")
    article("Перенос у нефтесервиса", "Weatherford применила на буровой роботов с автозавода.")
    article("Пожар на буровой", "На буровой в Омане случился пожар, работы остановлены.")
    article("Старая новость", "Котировки нефти в 2019 году росли после решения ОПЕК.")
    article("Отказ без категории", "Нефть дешевеет после роста запасов нефти в США.")
    model = decide(monkeypatch, {
        "Экзоскелеты Башнефти": answer(category="technology", **transfer),
        "Двойники верфей": answer(industry="other_industry", category="business", **transfer),
        "Перенос у нефтесервиса": [answer(industry="oilfield_services", category="technology", **transfer),
                                   answer(industry="oilfield_services", category="technology")],
        "Пожар на буровой": [reject("incident_war_politics", industry="oilfield_services",
                                    oilfield_relevance="direct"),
                             reject("incident_war_politics", industry="oilfield_services")],
        "Старая новость": [reject("old_event"), reject("old_event", oilfield_relevance="direct")],
        "Отказ без категории": reject("oil_gas_no_link", category=""),
    })

    calls = {}
    for call in model.calls:
        title = call["input"].splitlines()[0].removeprefix("Заголовок: ")
        calls.setdefault(title, []).append(call["input"])
    # Повтор — только там, где таблица говорит «нет»: перенос у нефтесервиса, происшествие при прямой связи,
    # старое событие без связи.
    assert {title: len(inputs) for title, inputs in calls.items()} == {
        "Экзоскелеты Башнефти": 1, "Двойники верфей": 1, "Перенос у нефтесервиса": 2, "Пожар на буровой": 2,
        "Старая новость": 2, "Отказ без категории": 1}
    assert "переносимая связь у нефтесервиса" in calls["Перенос у нефтесервиса"][1]
    assert "причина «Происшествия, война, политика» при отрасли «нефтесервис» и связи «прямая»" in (
        calls["Пожар на буровой"][1])
    technology, business = showcase(user, "technology"), showcase(user)
    assert (technology["Экзоскелеты Башнефти"]["oilfield_relevance"],
            technology["Экзоскелеты Башнефти"]["otbor_disputed"]) == ("transferable", None)
    assert business["Двойники верфей"]["otbor_disputed"] is None
    assert technology["Перенос у нефтесервиса"]["oilfield_relevance"] == "direct"
    # Отказ без категории — решение, а не «ответ не по правилам».
    [no_category] = api_get("/api/signals", admin, section="otbor:oil_gas_no_link")
    assert (no_category["title"], no_category["signal_category"]) == ("Отказ без категории", None)
    assert api_get("/api/signals", admin, section="otbor_invalid") == []


def test_otbor_spend_is_accounted_under_its_name(user, admin, contour, monkeypatch):
    article("Программа бурения Equinor", "Equinor начнёт бурение 12 скважин на шельфе Норвегии в 2027 году.")
    article("Котировки нефти", "Нефть дешевеет после роста запасов нефти в США.")
    decide(monkeypatch, {"Программа бурения Equinor": answer(), "Котировки нефти": reject("oil_gas_no_link")})

    report = {row["stage"]: row for row in api_get("/api/reports/ai-cost", admin)}
    assert report["otbor"]["runs"] == 2
    # gpt-5.4-mini: 2 000 входных × $0,75 + 300 выходных × $4,5 за 1 млн = $0,00285 на статью.
    assert float(report["otbor"]["cost_usd"]) == pytest.approx(0.0057)
    monthly = {row["area"]: (row["model"], row["runs"]) for row in api_get("/api/stats/monthly", admin, months=1)["ai_cost"]}
    assert monthly == {"otbor": (f"{MODEL}-2026-03-17", 2)}


def test_worker_stop_keeps_the_paid_part_and_requeues_the_rest(user, admin, contour, monkeypatch):
    for title in ("Первая", "Вторая", "Третья"):
        article(title, f"{title}: Equinor начнёт бурение скважин на шельфе Норвегии.")
    model = OtborModel({title: answer() for title in ("Первая", "Вторая", "Третья")})
    monkeypatch.setattr(otbor, "make_client", lambda: model)
    cli.main(["enqueue-otbor"])
    client = worker("external-ai")
    job = client.claim()

    def beat(done=None):
        # Выкат NL (SIGTERM) приходит, когда первое решение уже оплачено.
        if done and done.get("decisions"):
            raise external_ai.StopRequested("worker stopping")

    external_worker._run_job(client, job, beat)

    assert set(showcase(user)) == {"Первая"}
    requeued = api_get(f"/api/jobs/{job['id']}", admin)
    assert requeued["status"] == "queued" and len(requeued["payload"]["article_ids"]) == 2
    run_worker("external-ai")
    assert set(showcase(user)) == {"Первая", "Вторая", "Третья"}
    assert len(model.calls) == 3  # первая статья второй раз не оплачена


def test_otbor_job_runs_in_the_local_contour_too(user, monkeypatch):
    # Без внешних воркеров (местный стек) ИИ-задачи идут в местную очередь ai.
    monkeypatch.setattr(api.config, "EXTERNAL_WORKERS_ENABLED", False)
    monkeypatch.setattr(api.config, "OTBOR_ENABLED", True)
    monkeypatch.setattr(api.config, "OTBOR_MODEL", MODEL)
    monkeypatch.setattr(api.config, "BACKGROUND_JOB_INLINE", False)
    article("Программа бурения Equinor", "Equinor начнёт бурение 12 скважин на шельфе Норвегии в 2027 году.")
    monkeypatch.setattr(otbor, "make_client", lambda: OtborModel({"Программа бурения Equinor": answer()}))

    cli.main(["enqueue-otbor"])
    background_jobs.worker_loop(once=True, queue_names=["ai"], poll_seconds=0)

    assert showcase(user)["Программа бурения Equinor"]["otbor_recipe"]["model"] == f"{MODEL}-2026-03-17"


def test_core_writes_decisions_only_for_the_articles_of_the_job(user, admin, contour, monkeypatch):
    article("Программа бурения Equinor", "Equinor начнёт бурение 12 скважин на шельфе Норвегии в 2027 году.")
    monkeypatch.setattr(api.config, "OTBOR_ENABLED", False)
    foreign = article("Чужая статья", "Weatherford получила контракт на бурение 274 скважин в Омане.")
    monkeypatch.setattr(api.config, "OTBOR_ENABLED", True)
    monkeypatch.setattr(otbor, "make_client", lambda: OtborModel({"Программа бурения Equinor": answer()}))
    cli.main(["enqueue-otbor"])
    client = worker("external-ai")
    job = client.claim()

    # Итог воркера — недоверенный ввод: в нём решение о статье, которой в задаче не было.
    result = otbor.process_payload(job["payload"])
    result["decisions"].append({"ref": f"article:{foreign}", "calls": [{"answer": reject("oil_gas_no_link")}],
                                "model": MODEL, "input_tokens": 1, "output_tokens": 1})
    client.complete(job, result)

    assert set(showcase(user)) == {"Программа бурения Equinor"}
    [error] = api_get(f"/api/jobs/{job['id']}", admin)["result"]["applied"]["errors"]
    assert error["error"] == "публикации нет в этой задаче"


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "signals" / "otbor-before-deploy.sql"
SIGNAL_COLUMNS = {"industry", "otbor_decision", "reject_reason", "otbor_disputed", "otbor_failure",
                  "otbor_failure_reason", "otbor_recipe"}


def _columns(table):
    with connection.get_connection() as conn:
        return {row[0] for row in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = %s", (table,))}


def test_before_deploy_sql_adds_otbor_to_the_old_schema_and_repeats_safely(isolated_db):
    with connection.get_connection() as conn:
        for column in SIGNAL_COLUMNS:
            conn.execute(f"ALTER TABLE signals DROP COLUMN {column}")
        conn.execute("ALTER TABLE articles DROP COLUMN selector")
        conn.commit()

    for _ in range(2):  # повтор безопасен
        with connection.get_connection() as conn:
            conn.commit()  # SET search_path открыл транзакцию — у скрипта своя
            conn.autocommit = True
            conn.execute(SCRIPT.read_text(encoding="utf-8"))

    assert SIGNAL_COLUMNS <= _columns("signals") and "selector" in _columns("articles")
