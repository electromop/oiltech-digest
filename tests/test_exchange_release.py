"""Пакет выпуска для потребителя (#198, спека #197): состав выпуска платформы на закрытии месяца, глазами потребителя.

Платформа на тестовой базе (окно месяца с днём смены 5, conftest), двойник потребителя забирает пакеты из точки обмена,
проверяет подпись ключом платформы и расшифровывает своим ключом (tests/exchange_consumer.py). Выпуск: тот же состав,
что у PDF и выгрузки (repository.issue_composition): статья идёт своим сигналом ленты, дубль корнем склейки; пустой
месяц даёт пустой пакет; выпуск у месяца один. Шаг планировщика за флагом собирает выпуск в первом цикле после
закрытия, не раньше первого месяца из настройки."""

from __future__ import annotations

import pytest

from oiltech_digest import cli, config, exchange_packages
from oiltech_digest.db import connection, repository
from tests.digest_user_path_support import SEPT, add_signal, feed_signal, msk
from tests.digest_user_path_support import issue  # noqa: F401: фикстура выпуска
from tests.exchange_consumer import same_fields_as_sample
from tests.exchange_support import exchange  # noqa: F401: фикстура обмена
from tests.exchange_support import freeze, packages, sent_signals


def _merge(duplicate: int, root: int) -> None:
    with connection.get_connection() as conn:
        conn.execute("UPDATE signals SET merged_into_signal_id = %s WHERE id = %s", (root, duplicate))
        conn.commit()


def _september(exchange) -> dict[str, int]:
    """Выпуск сентября: две статьи (у второй сигнал ленты склеен с карточкой радара, которой нет в выпуске) и
    карточка радара. Отмечают 29.09, пока сентябрь открыт."""
    hi, mid = feed_signal(exchange.a["hi"]), feed_signal(exchange.a["mid"])
    other = add_signal("other", first_seen=msk(2026, 9, 14, 10, 0))
    _merge(mid, other)
    exchange.analyst.mark(exchange.a["hi"])
    exchange.analyst.mark(exchange.a["mid"])
    exchange.analyst.mark_signal(exchange.s["sep"])
    return {"hi": hi, "mid": mid, "other": other, "sep": exchange.s["sep"]}


def test_consumer_takes_the_release_of_the_closed_month_as_the_platform_issue(exchange, monkeypatch):
    ids = _september(exchange)
    freeze(monkeypatch, exchange.after_close)

    built = exchange_packages.build_release(SEPT, by="test")
    [package] = exchange.take()

    composition = repository.issue_composition(SEPT)
    assert composition == {"article_ids": sorted([exchange.a["hi"], exchange.a["mid"]]), "signal_ids": [ids["sep"]]}
    # Статья своим сигналом ленты, дубль корнем склейки.
    expected = sorted([ids["hi"], ids["other"], ids["sep"]])
    assert (package["number"], package["kind"], package["month"]) == (built.number, "release", SEPT)
    assert [card["signal_id"] for card in package["cards"]] == expected
    same_fields_as_sample(package)
    assert built.details["moved"] == [{"requested": ids["mid"], "root": ids["other"]}]
    # Журнал отправленного: сигнал, первый пакет и месяц выпуска, состояние «действует».
    journal = sent_signals()
    assert sorted(journal) == expected
    assert {(row["first_package"], row["month"], row["state"]) for row in journal.values()} == {
        (built.number, SEPT, "active")}


def test_article_of_the_issue_without_a_feed_signal_is_named_in_the_package_details(exchange, monkeypatch):
    """У статьи без сигнала ленты (собрана до 01.08.2026 или сигнал не заведён) номера карточки нет: она не уходит, а
    сборка называет её в журнале обмена."""
    hi = feed_signal(exchange.a["hi"])
    exchange.analyst.mark(exchange.a["hi"])
    exchange.analyst.mark(exchange.a["low"])
    with connection.get_connection() as conn:
        conn.execute("DELETE FROM signals WHERE origin = 'feed' AND origin_article_id = %s", (exchange.a["low"],))
        conn.commit()
    freeze(monkeypatch, exchange.after_close)

    built = exchange_packages.build_release(SEPT, by="test")

    assert built.signal_ids == [hi]
    assert built.details["without_signal"] == [exchange.a["low"]]
    assert [card["signal_id"] for card in exchange.take()[0]["cards"]] == [hi]


def test_month_without_marks_gives_an_empty_release_and_a_release_is_built_once(exchange, monkeypatch):
    freeze(monkeypatch, exchange.after_close)

    empty = exchange_packages.build_release("2026-08", by="test")
    with pytest.raises(exchange_packages.Refused, match=r"выпуск за август 2026 уже собран: пакет №"):
        exchange_packages.build_release("2026-08", by="test")

    [package] = exchange.take()
    assert (package["number"], package["kind"], package["month"], package["cards"]) == (
        empty.number, "release", "2026-08", [])
    assert len(packages()) == 1


def test_open_month_and_missing_keys_are_refused_with_a_reason(exchange, monkeypatch):
    with pytest.raises(exchange_packages.Refused, match="выпуск за сентябрь 2026 ещё открыт"):
        exchange_packages.build_release(SEPT, by="test")
    freeze(monkeypatch, exchange.after_close)
    monkeypatch.setattr(config, "EXCHANGE_SIGNING_KEYS", "")
    with pytest.raises(exchange_packages.Refused, match=r"нет ключа подписи платформы \(EXCHANGE_SIGNING_KEYS\)"):
        exchange_packages.build_release(SEPT, by="test")
    assert packages() == []


def test_step_does_nothing_with_the_flag_off(exchange, monkeypatch):
    _september(exchange)
    freeze(monkeypatch, exchange.after_close)
    monkeypatch.setattr(config, "EXCHANGE_FIRST_MONTH", SEPT)

    assert exchange_packages.release_step() is None
    assert packages() == []


def test_step_builds_the_release_in_the_first_cycle_after_the_close_from_the_first_month(exchange, monkeypatch):
    ids = _september(exchange)
    monkeypatch.setattr(config, "EXCHANGE_ENABLED", True)
    monkeypatch.setattr(config, "EXCHANGE_FIRST_MONTH", SEPT)

    # 04.10 23:59 МСК сентябрь ещё открыт (смена окна 5-го): собирать нечего.
    freeze(monkeypatch, msk(2026, 10, 4, 23, 59))
    assert exchange_packages.release_step() == []
    # Первый цикл после закрытия: выпуск сентября; август раньше первого месяца шага, его шаг не трогает.
    freeze(monkeypatch, msk(2026, 10, 5, 0, 30))
    [built] = exchange_packages.release_step()
    assert (built.kind, built.month, built.signal_ids) == ("release", SEPT, sorted([ids["hi"], ids["other"], ids["sep"]]))
    # Следующий цикл: выпуск у месяца уже есть.
    assert exchange_packages.release_step() == []
    assert [(row["kind"], row["month"]) for row in packages()] == [("release", SEPT)]


def test_step_without_the_first_month_or_keys_fails_with_a_reason(exchange, monkeypatch, capsys):
    freeze(monkeypatch, exchange.after_close)
    monkeypatch.setattr(config, "EXCHANGE_ENABLED", True)

    with pytest.raises(SystemExit) as failed:
        cli.main(["exchange-release"])
    assert "не задан первый месяц шага (EXCHANGE_FIRST_MONTH, ГГГГ-ММ)" in str(failed.value)

    monkeypatch.setattr(config, "EXCHANGE_FIRST_MONTH", SEPT)
    monkeypatch.setattr(config, "EXCHANGE_CONSUMER_KEYS", "")
    with pytest.raises(SystemExit) as failed:
        cli.main(["exchange-release"])
    assert "нет открытого ключа потребителя (EXCHANGE_CONSUMER_KEYS)" in str(failed.value)
    assert packages() == []


def test_commands_build_a_named_closed_month_and_report_the_step(exchange, monkeypatch, capsys):
    ids = _september(exchange)
    freeze(monkeypatch, exchange.after_close)

    cli.main(["exchange-release"])
    assert "exchange-release: шаг выключен (EXCHANGE_ENABLED)" in capsys.readouterr().out
    # Стартовая загрузка: выпуск названного закрытого месяца командой, флаг для неё не нужен.
    cli.main(["exchange-release", "--month", SEPT])
    out = capsys.readouterr().out
    assert "пакет №1: выпуск за сентябрь 2026, карточек 3" in out
    with pytest.raises(SystemExit) as again:
        cli.main(["exchange-release", "--month", SEPT])
    assert "выпуск за сентябрь 2026 уже собран: пакет №1" in str(again.value)

    cli.main(["exchange-status"])
    status = capsys.readouterr().out
    assert "№1 выпуск за сентябрь 2026: карточек 3, собран" in status and "не забран" in status
    assert all(str(one) in status for one in (ids["hi"], ids["other"], ids["sep"]))
