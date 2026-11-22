"""Готовность материала выпуска (#199, спека #197), шов 1 глазами администратора и потребителя: пакет выпуска ждёт
готовности всех материалов и собирается в первом цикле, когда готово всё; «Отправить как есть» собирает его сразу и
пишет в журнал обмена, что было не готово; вкладка «Журнал обмена» и экран «Дайджест» показывают, чего не хватает
материалам. Отметка «в дайджест», цепочка шагов и перепроверка: test_material_readiness.py."""

from __future__ import annotations

import pytest

from oiltech_digest import cli, config
from oiltech_digest.db import connection
from tests.digest_user_path_support import SEPT, feed_signal
from tests.digest_user_path_support import issue  # noqa: F401: фикстура выпуска
from tests.exchange_support import exchange  # noqa: F401: фикстура обмена
from tests.exchange_support import freeze, packages, ready
from tests.readiness_support import external, fail_all, steps


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
    external(monkeypatch)
    cli.main(["digest-readiness"])
    assert [step[0] for step in steps()] == ["process_articles", "signal_card"]

    ready(hi)
    cli.main(["exchange-release"])
    assert "не готовы 1 из 2" in capsys.readouterr().out
    assert packages() == []
    # Первый цикл, когда готово всё: пакет выпуска.
    ready(exchange.s["sep"])
    cli.main(["exchange-release"])
    assert "exchange-release: пакет №1: выпуск за сентябрь 2026, карточек 2" in capsys.readouterr().out
    assert [card["signal_id"] for card in exchange.take()[0]["cards"]] == sorted([hi, exchange.s["sep"]])
    fail_all()
    cli.main(["digest-readiness"])
    assert len(steps()) == 2


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
