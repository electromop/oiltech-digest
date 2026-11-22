"""Готовность материала выпуска (#199, спека #197), шов 1: платформа на тестовой базе, HTTP API и шаги планировщика.

Отметка «в дайджест» сама ставит материалу недостающие шаги конвейера (суть и перевод, «Карточка», баллы) по полосе
пересчётов и для сигнала старше окна шагов планировщика; без отметки шаги ведут себя как раньше. Шаг, задача которого
уже идёт, второй раз не ставится. Перепроверка готовности в последний день перед закрытием месяца (на проде 10-е, здесь
день смены окна 5: 4-е) с часа из настройки ставит заново шаги неготовых материалов не чаще суточного предела шагов.
Пакет выпуска ждёт готовности всех материалов, «Отправить как есть» собирает его сразу и пишет в журнал обмена, что было
не готово. Экран «Дайджест» и вкладка «Журнал обмена» показывают, чего не хватает материалам."""

from __future__ import annotations

from pathlib import Path
import re

import pytest
from psycopg.rows import dict_row

from oiltech_digest import cli, config, signal_card, signal_scores
from oiltech_digest.db import connection
from oiltech_digest.processing import external_ai
from tests.card_pipeline_support import ScoresModel, accepted, answer, card_answer, even, pipeline, scores  # noqa: F401
from tests.digest_user_path_support import SEPT, feed_signal, msk
from tests.digest_user_path_support import issue  # noqa: F401: фикстура выпуска
from tests.exchange_support import exchange  # noqa: F401: фикстура обмена
from tests.exchange_support import freeze, packages, ready
from tests.otbor_support import api_request, contour, window_of_published  # noqa: F401: фикстуры
from tests.signal_card_support import MODEL, FakeModel, run_worker, user  # noqa: F401: фикстуры

CONTRACT = "Контракт Weatherford в Омане"
CONTRACT_TEXT = "Weatherford получила контракт на бурение 274 скважин в Омане. Контракт подписан 24 сентября 2026 года."
PIPELINE_KINDS = ("process_articles", "signal_summary", "signal_card", "signal_scores")
BULK = "external-ai-bulk"


def _jobs(since: int = 0) -> list[dict]:
    with connection.get_connection() as conn:
        return conn.cursor(row_factory=dict_row).execute(
            "SELECT id, kind, queue_name, status, payload_json FROM background_jobs WHERE kind = ANY(%s) AND id > %s"
            " ORDER BY id", (list(PIPELINE_KINDS), since)).fetchall()


def _steps(since: int = 0) -> list[tuple]:
    """Задачи шагов конвейера по порядку постановки: вид, полоса, номера, стадии обработки ленты, повод."""
    return [(job["kind"], job["queue_name"], job["payload_json"].get("signal_ids") or job["payload_json"].get("article_ids"),
             job["payload_json"].get("only"), job["payload_json"].get("trigger")) for job in _jobs(since)]


def _last_job() -> int:
    with connection.get_connection() as conn:
        return int(conn.execute("SELECT COALESCE(max(id), 0) FROM background_jobs").fetchone()[0])


def _fail_all() -> None:
    """Задачи шагов упали: сбой модели."""
    with connection.get_connection() as conn:
        conn.execute("UPDATE background_jobs SET status = 'failed', finished_at = now() WHERE kind = ANY(%s)",
                     (list(PIPELINE_KINDS),))
        conn.commit()


def _feed_signal_of(article_id: int) -> int:
    with connection.get_connection() as conn:
        return int(conn.execute("SELECT id FROM signals WHERE origin = 'feed' AND origin_article_id = %s",
                                (article_id,)).fetchone()[0])


def _age(signal_id: int, days: int) -> None:
    """Сигнал принят давно: старше окна шагов планировщика."""
    with connection.get_connection() as conn:
        conn.execute("UPDATE signals SET accepted_at = now() - make_interval(days => %s),"
                     " created_at = now() - make_interval(days => %s) WHERE id = %s", (days, days, signal_id))
        conn.commit()


def _finding(key: str, *, category: str = "technology", done: bool = False) -> int:
    """Находка поиска, принятая «Отбором»: суть, русский заголовок, карточка и баллы есть только у готовой."""
    from oiltech_digest.db import repository

    signal_id = repository.upsert_signal({"signal_key": key, "title": f"Находка {key}", "theme": "не классифицировано",
                                          "maturity": "watch", "score": 0})
    repository.upsert_signal_evidence(signal_id, {"source_url": f"https://radar.example/{key}", "title": f"Находка {key}",
                                                  "publisher": "Offshore Weekly", "extracted_fact": "Текст находки."})
    with connection.get_connection() as conn:
        conn.execute(
            "UPDATE signals SET selector = 'otbor', otbor_decision = 'accept', signal_category = %s, summary = %s,"
            " title_ru = %s, card_recipe = %s::jsonb, scores_recipe = %s::jsonb WHERE id = %s",
            (category, "Суть находки." if done else None, f"Находка {key}" if done else None,
             '{"prompt_version": "t"}' if done else None, '{"prompt_version": "t"}' if done else None, signal_id))
        conn.commit()
    _age(signal_id, 30)
    return signal_id


def _write(*signal_ids: int, **fields: str) -> None:
    """Поля сигналов, как их записал шаг конвейера."""
    with connection.get_connection() as conn:
        conn.execute(f"UPDATE signals SET {', '.join(f'{name} = %s' for name in fields)} WHERE id = ANY(%s)",
                     (*fields.values(), list(signal_ids)))
        conn.commit()


def _spend(job_ids: list[int]) -> list[tuple[str, int]]:
    with connection.get_connection() as conn:
        return [tuple(row) for row in conn.execute(
            "SELECT stage, count(*)::int FROM ai_processing_runs WHERE job_id = ANY(%s) GROUP BY stage ORDER BY stage",
            (job_ids,)).fetchall()]


def test_screen_names_the_steps_as_the_server_does():
    """Экран «Дайджест» и вкладка «Журнал обмена» пишут шаги готовности теми же словами, что команды и журнал обмена."""
    from oiltech_digest import material_readiness

    source = (Path(__file__).resolve().parents[1] / "frontend/src/features/digest/DigestMaterialEditor.tsx").read_text(
        encoding="utf-8")
    block = source.split("const READINESS_LABELS", 1)[1].split("\n};", 1)[0]

    assert dict(re.findall(r'(\w+): "([^"]+)"', block)) == material_readiness.LABELS
    assert tuple(material_readiness.LABELS) == material_readiness.STEPS


def _external(monkeypatch) -> None:
    """Прод-маршрут ИИ: внешний воркер, своя полоса пересчётов, «Карточка» включена."""
    monkeypatch.setattr(config, "EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr(config, "AI_EXECUTION_REGION", "external")
    monkeypatch.setattr(config, "AI_BULK_LANE_ENABLED", True)
    monkeypatch.setattr(config, "SIGNAL_CARD_ENABLED", True)
    monkeypatch.setattr(config, "SIGNAL_CARD_MODEL", MODEL)


# --- Отметка «в дайджест» -------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("window_of_published")
def test_digest_mark_puts_the_missing_steps_of_an_old_material_in_pipeline_order_and_their_spend_is_by_stage(
        user, pipeline, monkeypatch):
    monkeypatch.setattr(config, "AI_BULK_LANE_ENABLED", True)
    ids, _ = accepted(monkeypatch, {CONTRACT: answer()}, {CONTRACT: CONTRACT_TEXT})
    article_id = ids[CONTRACT]
    signal_id = _feed_signal_of(article_id)
    _age(signal_id, 30)
    start = _last_job()

    # Без отметки старый сигнал шаги планировщика не берут: их окно прежнее.
    cli.main(["enqueue-signal-cards"])
    cli.main(["enqueue-signal-scores"])
    assert _steps(start) == []

    def mark(status: str = "digest") -> None:
        response = api_request("PATCH", f"/api/articles/{article_id}", user, {"status": status})
        assert response.status_code == 200, response.text

    mark()
    # Баллам считать не из чего, пока нет «Карточки»: отметка ставит только её.
    expected = [("signal_card", BULK, [signal_id], None, "digest_mark")]
    assert _steps(start) == expected
    # Повторная отметка, снятие и новая отметка задач не двоят: шаг, задача которого идёт, второй раз не ставится.
    mark()
    mark("new")
    mark()
    assert _steps(start) == expected

    card, rated = FakeModel({CONTRACT: card_answer()}), ScoresModel({CONTRACT: scores(even("business", 70))})
    monkeypatch.setattr(signal_card, "make_client", lambda: card)
    monkeypatch.setattr(signal_scores, "make_client", lambda: rated)
    run_worker(BULK)

    # Баллы поставило ядро, когда записало итог «Карточки»: они увидели готовую карточку.
    assert _steps(start) == [*expected, ("signal_scores", BULK, [signal_id], None, "digest_mark")]
    assert (len(card.calls), len(rated.calls)) == (1, 1)
    assert _spend([job["id"] for job in _jobs(start)]) == [("signal_card", 1), ("signal_scores", 1)]
    october = api_request("GET", "/api/digest-content?month=2026-10&limit=500&min_score=0", user, None).json()
    [material] = [one for section in october["sections"] for one in section["materials"]]
    assert (material["article_id"], material["readiness"]) == (article_id, [])
    assert october["readiness"] == {"materials": 1, "not_ready": 0}
    # Готовому материалу отметка задач не ставит.
    mark("new")
    mark()
    assert len(_steps(start)) == 2


@pytest.mark.usefixtures("window_of_published")
def test_old_finding_goes_summary_then_card_then_scores_other_gets_no_scores_ready_gets_nothing(
        user, pipeline, monkeypatch):
    from oiltech_digest import material_readiness

    monkeypatch.setattr(config, "AI_BULK_LANE_ENABLED", True)
    finding, other, done = _finding("finding"), _finding("other", category="other"), _finding("done", done=True)

    for signal_id in (finding, other, done):
        response = api_request("PATCH", f"/api/signals/{signal_id}", user, {"selected_for_digest": True})
        assert response.status_code == 200, response.text

    # «Карточка» после «Отбора» ждёт суть (порядок конвейера, #132): отметка ставит только суть.
    summaries = [("signal_summary", BULK, [finding], None, "digest_mark"),
                 ("signal_summary", BULK, [other], None, "digest_mark")]
    assert _steps() == summaries
    # Суть записана (ядро зовёт after_step при записи итога задачи): «Карточка».
    _write(finding, other, summary="Суть находки.", title_ru="Находка")
    for job in _jobs():
        material_readiness.after_step(job)
    cards = [("signal_card", BULK, [finding], None, "digest_mark"), ("signal_card", BULK, [other], None, "digest_mark")]
    assert _steps() == summaries + cards
    # «Карточка» записана: баллы, но только технологии; «другому» баллов не положено.
    _write(finding, other, card_recipe='{"prompt_version": "t"}')
    for job in _jobs()[2:]:
        material_readiness.after_step(job)
    assert _steps() == summaries + cards + [("signal_scores", BULK, [finding], None, "digest_mark")]


def test_mark_of_a_feed_article_puts_only_its_missing_stage_and_the_job_drops_what_came_meanwhile(
        exchange, monkeypatch):
    _external(monkeypatch)
    hi = feed_signal(exchange.a["hi"])
    exchange.analyst.mark(exchange.a["hi"])

    # Суть у статьи есть, русского заголовка нет: обработка ленты по номеру только его; «Карточка» сигнала ленты.
    assert _steps() == [("process_articles", BULK, [exchange.a["hi"]], ["translation"], "digest_mark"),
                        ("signal_card", BULK, [hi], None, "digest_mark")]
    # Пока задача ждала, заголовок пришёл обработкой ленты дня: статья из пакета выпадает, модель за неё не платится.
    with connection.get_connection() as conn:
        conn.execute("UPDATE article_cards SET title_ru = 'Перевод' WHERE article_id = %s", (exchange.a["hi"],))
        conn.commit()
    [job] = [one for one in _jobs() if one["kind"] == "process_articles"]
    assert external_ai.build_process_articles_payload(job["payload_json"], job_id=job["id"])["articles"] == []


def test_next_step_follows_any_written_step_while_the_mark_stays(exchange, monkeypatch):
    """Суть статьи пишет поток дня (статья в его пакете): «Карточку» ставит запись этого итога, а после снятия отметки
    цепочка останавливается."""
    from oiltech_digest import material_readiness

    _external(monkeypatch)
    article = exchange.a["hi"]
    hi = feed_signal(article)
    with connection.get_connection() as conn:
        conn.execute("UPDATE signals SET otbor_decision = 'accept', signal_category = 'business', summary = NULL"
                     " WHERE id = %s", (hi,))
        conn.execute("UPDATE article_cards SET summary = NULL WHERE article_id = %s", (article,))
        conn.commit()

    exchange.analyst.mark(article)
    assert _steps() == [("process_articles", BULK, [article], ["summary", "translation"], "digest_mark")]
    # Пакет потока дня (без повода готовности) записал суть и перевод статьи; мост перенёс их в сигнал ленты.
    with connection.get_connection() as conn:
        conn.execute("UPDATE article_cards SET summary = 'Суть.', title_ru = 'Заголовок' WHERE article_id = %s",
                     (article,))
        conn.commit()
    feed_signal(article)
    material_readiness.after_step({"id": 0, "kind": "process_articles", "payload_json": {"reserved_article_ids": [article]}})
    assert _steps()[1:] == [("signal_card", BULK, [hi], None, "digest_mark")]
    # Отметку сняли: «Карточка» записана, но баллы уже не ставятся.
    exchange.analyst.mark(article, "new")
    _write(hi, card_recipe='{"prompt_version": "t"}')
    material_readiness.after_step(_jobs()[-1])
    assert len(_steps()) == 2


def test_mark_resyncs_a_feed_signal_that_lost_the_text_of_its_article_without_a_paid_call(exchange, monkeypatch):
    _external(monkeypatch)
    hi = feed_signal(exchange.a["hi"])
    ready(hi)
    with connection.get_connection() as conn:
        conn.execute("UPDATE signals SET summary = NULL, card_recipe = NULL, otbor_decision = 'accept',"
                     " signal_category = 'business' WHERE id = %s", (hi,))
        conn.commit()

    exchange.analyst.mark(exchange.a["hi"])

    # Суть у статьи есть: её возвращает мост «лента → сигнал» без задачи, и «Карточке» уже есть из чего считать.
    assert _steps() == [("signal_card", BULK, [hi], None, "digest_mark")]
    with connection.get_connection() as conn:
        assert conn.execute("SELECT summary FROM signals WHERE id = %s", (hi,)).fetchone()[0] == "Суть материала hi."


def test_two_marks_at_once_put_each_step_once(exchange, monkeypatch):
    """Двойное нажатие «В дайджест»: второй запрос ждёт, пока первый поставит задачи, и видит их как идущие."""
    import threading
    import time

    from oiltech_digest import material_readiness

    _external(monkeypatch)
    hi = feed_signal(exchange.a["hi"])
    inside, release = threading.Event(), threading.Event()
    create = material_readiness._create
    calls: list[int] = []

    def slow_create(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            inside.set()
            assert release.wait(10)
        return create(*args, **kwargs)

    monkeypatch.setattr(material_readiness, "_create", slow_create)
    first = threading.Thread(target=material_readiness.after_digest_mark, kwargs={"article_id": exchange.a["hi"]})
    first.start()
    assert inside.wait(10)
    second = threading.Thread(target=material_readiness.after_digest_mark, kwargs={"article_id": exchange.a["hi"]})
    second.start()
    time.sleep(0.5)
    # Первый держит замок и ещё не поставил задачи: второй ждёт замка, задач нет.
    assert _steps() == []
    release.set()
    first.join(10)
    second.join(10)

    assert _steps() == [("process_articles", BULK, [exchange.a["hi"]], ["translation"], "digest_mark"),
                        ("signal_card", BULK, [hi], None, "digest_mark")]


def test_failed_enqueue_does_not_fail_the_mark(exchange, monkeypatch):
    from oiltech_digest import material_readiness

    def broken(*args, **kwargs):
        raise RuntimeError("база шагов недоступна")

    monkeypatch.setattr(material_readiness, "enqueue_missing", broken)
    exchange.analyst.mark(exchange.a["hi"])
    exchange.analyst.mark_signal(exchange.s["sep"])

    assert [item["item_type"] for item in exchange.analyst.issue_items(SEPT)] == ["article", "signal"]


# --- Перепроверка готовности ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("rollover, moment, month", [
    # Прод: месяц закрывается 11-го, перепроверка 10-го с 09:00 МСК.
    (11, msk(2026, 12, 10, 9, 0), "2026-11"),
    (11, msk(2026, 12, 10, 23, 59), "2026-11"),
    (11, msk(2026, 12, 10, 8, 59), None),
    (11, msk(2026, 12, 9, 12, 0), None),
    (11, msk(2026, 12, 11, 9, 0), None),
    # Окно без прошлого месяца: месяц закрывается в конце, перепроверка в его последний день.
    (1, msk(2026, 12, 31, 9, 0), "2026-12"),
])
def test_recheck_day_is_the_last_day_before_the_close_from_the_hour(monkeypatch, rollover, moment, month):
    from oiltech_digest import material_readiness

    monkeypatch.setattr(config, "FEED_ROLLOVER_DAY", rollover)
    monkeypatch.setattr(config, "READINESS_RECHECK_HOUR", 9)

    assert material_readiness.closing_month(moment) == month


def test_recheck_on_the_last_day_before_the_close_from_the_hour_puts_steps_again_within_the_daily_limit(
        exchange, monkeypatch, capsys):
    hi = feed_signal(exchange.a["hi"])
    # Отметки поставлены, когда ИИ был в местном контуре и «Карточка» выключена: задач нет.
    exchange.analyst.mark(exchange.a["hi"])
    exchange.analyst.mark_signal(exchange.s["sep"])
    assert _steps() == []
    _external(monkeypatch)
    monkeypatch.setattr(config, "READINESS_RECHECK_HOUR", 9)
    expected = [("process_articles", BULK, [exchange.a["hi"]], ["translation"], "readiness"),
                ("signal_card", BULK, sorted([hi, exchange.s["sep"]]), None, "readiness")]

    # Сентябрь закрывается 05.10 (день смены окна 5): перепроверка 04.10 с 09:00 МСК.
    freeze(monkeypatch, msk(2026, 10, 3, 9, 30))
    cli.main(["digest-readiness"])
    freeze(monkeypatch, msk(2026, 10, 4, 8, 59))
    cli.main(["digest-readiness"])
    assert _steps() == []
    freeze(monkeypatch, msk(2026, 10, 4, 9, 0))
    cli.main(["digest-readiness"])
    assert _steps() == expected
    assert "digest-readiness: выпуск за сентябрь 2026: не готовы 2 из 2" in capsys.readouterr().out
    # Следующий цикл: задачи идут, второй раз не ставятся.
    freeze(monkeypatch, msk(2026, 10, 4, 9, 40))
    cli.main(["digest-readiness"])
    assert _steps() == expected
    # Сбой модели: вторая попытка за сутки, дальше предел шагов конвейера (две задачи шага на материал).
    _fail_all()
    cli.main(["digest-readiness"])
    assert len(_steps()) == 4
    _fail_all()
    cli.main(["digest-readiness"])
    assert len(_steps()) == 4


def test_after_the_close_recheck_goes_every_cycle_until_the_release_and_the_release_waits_for_all_materials(
        exchange, monkeypatch, capsys):
    hi = feed_signal(exchange.a["hi"])
    exchange.analyst.mark(exchange.a["hi"])
    exchange.analyst.mark_signal(exchange.s["sep"])
    monkeypatch.setattr(config, "EXCHANGE_ENABLED", True)
    monkeypatch.setattr(config, "EXCHANGE_FIRST_MONTH", SEPT)
    freeze(monkeypatch, exchange.after_close)

    cli.main(["exchange-release"])
    out = capsys.readouterr().out
    assert (f"exchange-release: выпуск за сентябрь 2026 ждёт готовности материалов, не готовы 2 из 2: №{hi} (нет "
            f"перевода заголовка, нет карточки), №{exchange.s['sep']} (нет карточки)") in out
    assert packages() == []
    # После закрытия перепроверка идёт каждый цикл, пока выпуск не собран.
    _external(monkeypatch)
    cli.main(["digest-readiness"])
    assert [step[0] for step in _steps()] == ["process_articles", "signal_card"]

    ready(hi)
    cli.main(["exchange-release"])
    assert "не готовы 1 из 2" in capsys.readouterr().out
    assert packages() == []
    # Первый цикл, когда готово всё: пакет выпуска.
    ready(exchange.s["sep"])
    cli.main(["exchange-release"])
    assert "exchange-release: пакет №1: выпуск за сентябрь 2026, карточек 2" in capsys.readouterr().out
    assert [card["signal_id"] for card in exchange.take()[0]["cards"]] == sorted([hi, exchange.s["sep"]])
    _fail_all()
    cli.main(["digest-readiness"])
    assert len(_steps()) == 2


# --- «Отправить как есть», журнал обмена и экран «Дайджест» ---------------------------------------------------------


def test_send_as_is_builds_the_release_at_once_journals_what_was_not_ready_and_no_second_release_follows(
        exchange, monkeypatch, capsys):
    hi = feed_signal(exchange.a["hi"])
    exchange.analyst.mark(exchange.a["hi"])
    exchange.analyst.mark_signal(exchange.s["sep"])
    ready(exchange.s["sep"])

    refused = exchange.admin.post("/api/exchange-journal/releases", json={"month": SEPT})
    assert refused.status_code == 409 and "ещё открыт" in refused.json()["detail"]
    freeze(monkeypatch, exchange.after_close)
    assert exchange.analyst.post("/api/exchange-journal/releases", json={"month": SEPT}).status_code == 403
    with pytest.raises(SystemExit) as waiting:
        cli.main(["exchange-release", "--month", SEPT])
    assert "ждёт готовности материалов, не готовы 1 из 2" in str(waiting.value)
    assert "exchange-release --month 2026-09 --as-is" in str(waiting.value)

    answer_ = exchange.admin.post("/api/exchange-journal/releases", json={"month": SEPT})
    assert answer_.status_code == 200, answer_.text
    body = answer_.json()
    assert (body["package"], body["sent"]) == (1, sorted([hi, exchange.s["sep"]]))
    assert body["not_ready"] == [{"signal_id": hi, "missing": ["translation", "card"]}]
    assert body["message"] == (f"Пакет №1: выпуск за сентябрь 2026, карточек 2; отправлен как есть, не готовы 1 из 2: "
                               f"№{hi} (нет перевода заголовка, нет карточки).")
    assert [card["signal_id"] for card in exchange.take()[0]["cards"]] == sorted([hi, exchange.s["sep"]])
    [package] = exchange.admin.get("/api/exchange-journal").json()["packages"]
    assert package["notes"] == [f"Отправлен как есть: №{hi} не был готов (нет перевода заголовка, нет карточки)"]
    # Выпуск у месяца один: ни действие, ни шаг второго не собирают.
    again = exchange.admin.post("/api/exchange-journal/releases", json={"month": SEPT})
    assert again.status_code == 409 and "уже собран: пакет №1" in again.json()["detail"]
    monkeypatch.setattr(config, "EXCHANGE_ENABLED", True)
    monkeypatch.setattr(config, "EXCHANGE_FIRST_MONTH", SEPT)
    cli.main(["exchange-release"])
    assert "собирать нечего" in capsys.readouterr().out
    assert len(packages()) == 1


def test_journal_shows_readiness_of_the_month_with_links_and_only_the_administrator_sees_it(exchange, monkeypatch,
                                                                                            capsys):
    hi = feed_signal(exchange.a["hi"])
    exchange.analyst.mark(exchange.a["hi"])
    exchange.analyst.mark_signal(exchange.s["sep"])
    ready(exchange.s["sep"])

    state = exchange.admin.get("/api/exchange-journal").json()["readiness"]
    assert (state["month"], state["closed"], state["release"], state["total"], state["ready"]) == (
        SEPT, False, None, 2, 1)
    assert state["not_ready"] == [{"signal_id": hi, "kind": "article", "id": exchange.a["hi"],
                                   "title": "Материал hi", "category": "business",
                                   "missing": ["translation", "card"]}]
    assert state["as_is"]["allowed"] is False and "ещё открыт" in state["as_is"]["reason"]

    freeze(monkeypatch, exchange.after_close)
    state = exchange.admin.get("/api/exchange-journal/readiness", params={"month": SEPT}).json()
    assert (state["closed"], state["as_is"]) == (True, {"allowed": True, "reason": None})
    assert exchange.admin.get("/api/exchange-journal/readiness", params={"month": "2026-08"}).json()["total"] == 0
    assert exchange.analyst.get("/api/exchange-journal/readiness", params={"month": SEPT}).status_code == 403
    # Команда на сервере показывает то же.
    cli.main(["digest-readiness", "--month", SEPT])
    out = capsys.readouterr().out
    assert "Выпуск за сентябрь 2026: готово 1 из 2" in out
    assert f"№{hi} Материал hi: нет перевода заголовка, нет карточки" in out


def test_send_as_is_is_off_while_an_article_of_the_issue_has_no_feed_signal(exchange, monkeypatch):
    exchange.analyst.mark(exchange.a["hi"])
    freeze(monkeypatch, exchange.after_close)
    with connection.get_connection() as conn:
        conn.execute("DELETE FROM signals WHERE origin = 'feed' AND origin_article_id = %s", (exchange.a["hi"],))
        conn.commit()

    state = exchange.admin.get("/api/exchange-journal/readiness", params={"month": SEPT}).json()

    assert state["as_is"]["allowed"] is False
    assert f"нет сигнала ленты, без него у карточки нет номера: №{exchange.a['hi']}" in state["as_is"]["reason"]
    assert exchange.admin.post("/api/exchange-journal/releases", json={"month": SEPT}).status_code == 409


def test_digest_screen_shows_what_each_material_lacks_and_how_many_materials_of_the_month_are_not_ready(issue):
    feed_signal(issue["a"]["hi"])
    issue["analyst"].mark(issue["a"]["hi"])
    issue["analyst"].mark(issue["a"]["mid"])
    issue["analyst"].mark_signal(issue["s"]["sep"])
    ready(issue["s"]["sep"], feed_signal(issue["a"]["mid"]))

    content = issue["analyst"].issue_content(SEPT)

    lacking = {(one["item_type"], one["article_id"] or one["signal_id"]): one["readiness"]
               for section in content["sections"] for one in section["materials"]}
    assert lacking == {("article", issue["a"]["hi"]): ["translation", "card"], ("article", issue["a"]["mid"]): [],
                       ("signal", issue["s"]["sep"]): []}
    assert content["readiness"] == {"materials": 3, "not_ready": 1}
    # Фильтр экрана сужает список, но не число неготовых материалов месяца.
    narrow = issue["analyst"].get("/api/digest-content", params={"month": SEPT, "limit": 500, "min_score": 80}).json()
    assert len([one for section in narrow["sections"] for one in section["materials"]]) < 3
    assert narrow["readiness"] == {"materials": 3, "not_ready": 1}
