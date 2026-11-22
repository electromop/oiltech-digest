"""Готовность материала выпуска (#199, спека #197), шов 1: платформа на тестовой базе, HTTP API и шаги планировщика.

Отметка «в дайджест» сама ставит материалу недостающие шаги конвейера (суть и перевод, «Карточка», баллы) по полосе
пересчётов и для сигнала старше окна шагов планировщика; без отметки шаги ведут себя как раньше. Ставится шаг, которому
уже есть из чего считать, следующий ставит запись итога предыдущего, пока отметка стоит; шаг, задача которого уже идёт,
второй раз не ставится. Перепроверка готовности в последний день перед закрытием месяца (на проде 10-е, здесь день
смены окна 5: 4-е) с часа из настройки ставит заново шаги неготовых материалов не чаще суточного предела шагов. Выпуск,
«Отправить как есть», журнал обмена и экран «Дайджест»: test_exchange_readiness.py."""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from oiltech_digest import cli, config, signal_card, signal_scores
from oiltech_digest.db import connection
from oiltech_digest.processing import external_ai
from tests.card_pipeline_support import ScoresModel, accepted, answer, card_answer, even, pipeline, scores  # noqa: F401
from tests.digest_user_path_support import SEPT, feed_signal, msk
from tests.digest_user_path_support import issue  # noqa: F401: фикстура выпуска
from tests.exchange_support import exchange  # noqa: F401: фикстура обмена
from tests.exchange_support import freeze, ready
from tests.otbor_support import api_request, contour, window_of_published  # noqa: F401: фикстуры
from tests.readiness_support import BULK, external, fail_all, jobs, last_job, steps, write
from tests.signal_card_support import FakeModel, run_worker, user  # noqa: F401: фикстуры

CONTRACT = "Контракт Weatherford в Омане"
CONTRACT_TEXT = "Weatherford получила контракт на бурение 274 скважин в Омане. Контракт подписан 24 сентября 2026 года."


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


# --- Отметка «в дайджест» -------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("window_of_published")
def test_digest_mark_puts_the_missing_steps_of_an_old_material_in_pipeline_order_and_their_spend_is_by_stage(
        user, pipeline, monkeypatch):
    monkeypatch.setattr(config, "AI_BULK_LANE_ENABLED", True)
    ids, _ = accepted(monkeypatch, {CONTRACT: answer()}, {CONTRACT: CONTRACT_TEXT})
    article_id = ids[CONTRACT]
    signal_id = _feed_signal_of(article_id)
    _age(signal_id, 30)
    start = last_job()

    # Без отметки старый сигнал шаги планировщика не берут: их окно прежнее.
    cli.main(["enqueue-signal-cards"])
    cli.main(["enqueue-signal-scores"])
    assert steps(start) == []

    def mark(status: str = "digest") -> None:
        response = api_request("PATCH", f"/api/articles/{article_id}", user, {"status": status})
        assert response.status_code == 200, response.text

    mark()
    # Баллам считать не из чего, пока нет «Карточки»: отметка ставит только её.
    expected = [("signal_card", BULK, [signal_id], None, "digest_mark")]
    assert steps(start) == expected
    # Повторная отметка, снятие и новая отметка задач не двоят: шаг, задача которого идёт, второй раз не ставится.
    mark()
    mark("new")
    mark()
    assert steps(start) == expected

    card, rated = FakeModel({CONTRACT: card_answer()}), ScoresModel({CONTRACT: scores(even("business", 70))})
    monkeypatch.setattr(signal_card, "make_client", lambda: card)
    monkeypatch.setattr(signal_scores, "make_client", lambda: rated)
    run_worker(BULK)

    # Баллы поставило ядро, когда записало итог «Карточки»: они увидели готовую карточку.
    assert steps(start) == [*expected, ("signal_scores", BULK, [signal_id], None, "digest_mark")]
    assert (len(card.calls), len(rated.calls)) == (1, 1)
    assert _spend([job["id"] for job in jobs(start)]) == [("signal_card", 1), ("signal_scores", 1)]
    october = api_request("GET", "/api/digest-content?month=2026-10&limit=500&min_score=0", user, None).json()
    [material] = [one for section in october["sections"] for one in section["materials"]]
    assert (material["article_id"], material["readiness"]) == (article_id, [])
    assert october["readiness"] == {"materials": 1, "not_ready": 0}
    # Готовому материалу отметка задач не ставит.
    mark("new")
    mark()
    assert len(steps(start)) == 2


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
    assert steps() == summaries
    # Суть записана (ядро зовёт after_step при записи итога задачи): «Карточка».
    write(finding, other, summary="Суть находки.", title_ru="Находка")
    for job in jobs():
        material_readiness.after_step(job)
    cards = [("signal_card", BULK, [finding], None, "digest_mark"), ("signal_card", BULK, [other], None, "digest_mark")]
    assert steps() == summaries + cards
    # «Карточка» записана: баллы, но только технологии; «другому» баллов не положено.
    write(finding, other, card_recipe='{"prompt_version": "t"}')
    for job in jobs()[2:]:
        material_readiness.after_step(job)
    assert steps() == summaries + cards + [("signal_scores", BULK, [finding], None, "digest_mark")]


def test_mark_of_a_feed_article_puts_only_its_missing_stage_and_the_job_drops_what_came_meanwhile(
        exchange, monkeypatch):
    external(monkeypatch)
    hi = feed_signal(exchange.a["hi"])
    exchange.analyst.mark(exchange.a["hi"])

    # Суть у статьи есть, русского заголовка нет: обработка ленты по номеру только его; «Карточка» сигнала ленты.
    assert steps() == [("process_articles", BULK, [exchange.a["hi"]], ["translation"], "digest_mark"),
                        ("signal_card", BULK, [hi], None, "digest_mark")]
    # Пока задача ждала, заголовок пришёл обработкой ленты дня: статья из пакета выпадает, модель за неё не платится.
    with connection.get_connection() as conn:
        conn.execute("UPDATE article_cards SET title_ru = 'Перевод' WHERE article_id = %s", (exchange.a["hi"],))
        conn.commit()
    [job] = [one for one in jobs() if one["kind"] == "process_articles"]
    assert external_ai.build_process_articles_payload(job["payload_json"], job_id=job["id"])["articles"] == []


def test_next_step_follows_any_written_step_while_the_mark_stays(exchange, monkeypatch):
    """Суть статьи пишет поток дня (статья в его пакете): «Карточку» ставит запись этого итога, а после снятия отметки
    цепочка останавливается."""
    from oiltech_digest import material_readiness

    external(monkeypatch)
    article = exchange.a["hi"]
    hi = feed_signal(article)
    with connection.get_connection() as conn:
        conn.execute("UPDATE signals SET otbor_decision = 'accept', signal_category = 'business', summary = NULL"
                     " WHERE id = %s", (hi,))
        conn.execute("UPDATE article_cards SET summary = NULL WHERE article_id = %s", (article,))
        conn.commit()

    exchange.analyst.mark(article)
    assert steps() == [("process_articles", BULK, [article], ["summary", "translation"], "digest_mark")]
    # Пакет потока дня (без повода готовности) записал суть и перевод статьи; мост перенёс их в сигнал ленты.
    with connection.get_connection() as conn:
        conn.execute("UPDATE article_cards SET summary = 'Суть.', title_ru = 'Заголовок' WHERE article_id = %s",
                     (article,))
        conn.commit()
    feed_signal(article)
    material_readiness.after_step({"id": 0, "kind": "process_articles", "payload_json": {"reserved_article_ids": [article]}})
    assert steps()[1:] == [("signal_card", BULK, [hi], None, "digest_mark")]
    # Отметку сняли: «Карточка» записана, но баллы уже не ставятся.
    exchange.analyst.mark(article, "new")
    write(hi, card_recipe='{"prompt_version": "t"}')
    material_readiness.after_step(jobs()[-1])
    assert len(steps()) == 2


def test_mark_resyncs_a_feed_signal_that_lost_the_text_of_its_article_without_a_paid_call(exchange, monkeypatch):
    external(monkeypatch)
    hi = feed_signal(exchange.a["hi"])
    ready(hi)
    with connection.get_connection() as conn:
        conn.execute("UPDATE signals SET summary = NULL, card_recipe = NULL, otbor_decision = 'accept',"
                     " signal_category = 'business' WHERE id = %s", (hi,))
        conn.commit()

    exchange.analyst.mark(exchange.a["hi"])

    # Суть у статьи есть: её возвращает мост «лента → сигнал» без задачи, и «Карточке» уже есть из чего считать.
    assert steps() == [("signal_card", BULK, [hi], None, "digest_mark")]
    with connection.get_connection() as conn:
        assert conn.execute("SELECT summary FROM signals WHERE id = %s", (hi,)).fetchone()[0] == "Суть материала hi."


def test_two_marks_at_once_put_each_step_once(exchange, monkeypatch):
    """Двойное нажатие «В дайджест»: второй запрос ждёт, пока первый поставит задачи, и видит их как идущие."""
    import threading
    import time

    from oiltech_digest import material_readiness

    external(monkeypatch)
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
    assert steps() == []
    release.set()
    first.join(10)
    second.join(10)

    assert steps() == [("process_articles", BULK, [exchange.a["hi"]], ["translation"], "digest_mark"),
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
    assert steps() == []
    external(monkeypatch)
    monkeypatch.setattr(config, "READINESS_RECHECK_HOUR", 9)
    expected = [("process_articles", BULK, [exchange.a["hi"]], ["translation"], "readiness"),
                ("signal_card", BULK, sorted([hi, exchange.s["sep"]]), None, "readiness")]

    # Сентябрь закрывается 05.10 (день смены окна 5): перепроверка 04.10 с 09:00 МСК.
    freeze(monkeypatch, msk(2026, 10, 3, 9, 30))
    cli.main(["digest-readiness"])
    freeze(monkeypatch, msk(2026, 10, 4, 8, 59))
    cli.main(["digest-readiness"])
    assert steps() == []
    freeze(monkeypatch, msk(2026, 10, 4, 9, 0))
    cli.main(["digest-readiness"])
    assert steps() == expected
    assert "digest-readiness: выпуск за сентябрь 2026: не готовы 2 из 2" in capsys.readouterr().out
    # Следующий цикл: задачи идут, второй раз не ставятся.
    freeze(monkeypatch, msk(2026, 10, 4, 9, 40))
    cli.main(["digest-readiness"])
    assert steps() == expected
    # Сбой модели: вторая попытка за сутки, дальше предел шагов конвейера (две задачи шага на материал).
    fail_all()
    cli.main(["digest-readiness"])
    assert len(steps()) == 4
    fail_all()
    cli.main(["digest-readiness"])
    assert len(steps()) == 4
