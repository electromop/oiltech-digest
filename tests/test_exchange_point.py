"""Точка обмена с потребителем выпуска и вкладка «Журнал обмена» (#198, спека #197).

Точка /api/exchange/v1 пускает только адрес потребителя (настоящий адрес клиента, как его видит приложение за Caddy) и
только с его токеном; отказ ставится до чтения тела. Номера пакетов идут по порядку: пакет с большим номером не виден
раньше меньшего, повторный запрос отдаёт тот же файл. Вкладка «Журнал обмена» и дозагрузка: только администратору."""

from __future__ import annotations

import threading
import time

import pytest

from oiltech_digest import config, exchange_envelope, exchange_packages
from oiltech_digest.db import connection
from tests.digest_user_path_support import SEPT, add_signal, feed_signal, msk
from tests.digest_user_path_support import issue  # noqa: F401: фикстура выпуска
from tests.exchange_support import exchange  # noqa: F401: фикстура обмена
from tests.exchange_support import CONSUMER_IP, HEADERS, STRANGER_IP, freeze, packages, point, sent_signals


class _Body:
    """Тело кусками без Content-Length; помнит, сколько кусков у него забрали."""

    def __init__(self, chunks: int = 3, size: int = 1_000) -> None:
        self.chunks, self.size, self.taken = chunks, size, 0

    def __iter__(self):
        for _ in range(self.chunks):
            self.taken += 1
            yield b"x" * self.size


def _merge(duplicate: int, root: int) -> None:
    with connection.get_connection() as conn:
        conn.execute("UPDATE signals SET merged_into_signal_id = %s WHERE id = %s", (root, duplicate))
        conn.commit()


def _release(exchange, monkeypatch) -> int:
    exchange.analyst.mark_signal(exchange.s["sep"])
    freeze(monkeypatch, exchange.after_close)
    return exchange_packages.build_release(SEPT, by="test").number


# --- доступ к точке --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("ip, headers", [
    (STRANGER_IP, HEADERS),
    ("10.0.0.5", HEADERS),       # внутренний адрес: заголовку адреса не поверили, все клиенты выглядят адресом Caddy
    ("testclient", HEADERS),     # не адрес
    (CONSUMER_IP, {}),
    (CONSUMER_IP, {"Authorization": "Bearer wrong"}),
    (CONSUMER_IP, {"Authorization": "test-exchange-token"}),
])
def test_foreign_address_and_wrong_token_are_refused_before_the_body_is_read(exchange, ip, headers):
    expected = 403 if ip != CONSUMER_IP else 401
    for method, path in (("GET", "/api/exchange/v1/packages"), ("GET", "/api/exchange/v1/packages/1"),
                         ("PUT", "/api/exchange/v1/reactions/1"), ("POST", "/api/exchange/v1/receipts/1")):
        body = _Body()
        answer = point(ip).request(method, path, headers=headers, content=body if method != "GET" else None)
        assert answer.status_code == expected, (ip, headers, method, path, answer.text)
        assert body.taken == 0


def test_point_is_closed_to_everyone_without_an_address_and_without_a_token_hash(exchange, monkeypatch):
    monkeypatch.setattr(config, "EXCHANGE_CONSUMER_IPS", "")
    assert point().get("/api/exchange/v1/packages", headers=HEADERS).status_code == 403
    # Испорченная запись адреса: закрыто всем, а не «пропустить запись».
    monkeypatch.setattr(config, "EXCHANGE_CONSUMER_IPS", f"{CONSUMER_IP} not-an-address")
    assert point().get("/api/exchange/v1/packages", headers=HEADERS).status_code == 403
    # Через запятую: Caddy с таким значением не запустится, приложение тоже не пускает.
    monkeypatch.setattr(config, "EXCHANGE_CONSUMER_IPS", f"198.51.100.0/24,{CONSUMER_IP}")
    assert point().get("/api/exchange/v1/packages", headers=HEADERS).status_code == 403
    monkeypatch.setattr(config, "EXCHANGE_CONSUMER_IPS", f"198.51.100.0/24  {CONSUMER_IP}")
    assert point().get("/api/exchange/v1/packages", headers=HEADERS).status_code == 200
    assert point(STRANGER_IP).get("/api/exchange/v1/packages", headers=HEADERS).status_code == 200
    monkeypatch.setattr(config, "EXCHANGE_TOKEN_HASH", "")
    assert point().get("/api/exchange/v1/packages", headers=HEADERS).status_code == 503
    # Сам токен вместо его хэша: точка не открывается и с этим токеном.
    monkeypatch.setattr(config, "EXCHANGE_TOKEN_HASH", "test-exchange-token")
    assert point().get("/api/exchange/v1/packages", headers=HEADERS).status_code == 503


def test_journal_names_what_keeps_the_consumer_out(exchange, monkeypatch):
    """Частный адрес потребителя приложение не пускает (за Caddy так выглядит любой клиент, если заголовку адреса не
    поверили), а сам токен вместо хэша не откроет точку никому: вкладка говорит об этом заранее."""
    monkeypatch.setattr(config, "EXCHANGE_CONSUMER_IPS", f"{CONSUMER_IP} 10.20.30.40")
    monkeypatch.setattr(config, "EXCHANGE_TOKEN_HASH", "test-exchange-token")

    settings = exchange.admin.get("/api/exchange-journal").json()["settings"]

    assert settings["token"] is False and settings["addresses"] == 2
    assert settings["point_problems"] == [
        "адрес потребителя 10.20.30.40/32 внутренний: приложение не отличает его от адреса Caddy и не пускает, нужен "
        "внешний адрес потребителя",
        "нет хэша токена (EXCHANGE_TOKEN_HASH: SHA-256 токена в hex, 64 знака, не сам токен)",
    ]
    assert point("10.20.30.40").get("/api/exchange/v1/packages", headers=HEADERS).status_code == 403


def test_point_gives_the_same_file_again_marks_the_takes_and_answers_404_for_no_package(exchange, monkeypatch):
    number = _release(exchange, monkeypatch)
    client = point()

    assert client.get("/api/exchange/v1/packages", headers=HEADERS).json() == {"packages": [number]}
    first = client.get(f"/api/exchange/v1/packages/{number}", headers=HEADERS)
    [row] = packages()
    second = client.get(f"/api/exchange/v1/packages/{number}", headers=HEADERS)

    assert first.status_code == second.status_code == 200
    assert first.headers["content-type"] == "application/json"
    assert first.content == second.content
    assert exchange.consumer.open(first.content)["number"] == number
    [again] = packages()
    assert row["first_taken_at"] is not None and again["first_taken_at"] == row["first_taken_at"]
    assert again["last_taken_at"] >= row["last_taken_at"]
    assert client.get(f"/api/exchange/v1/packages/{number + 1}", headers=HEADERS).status_code == 404


def test_packages_are_neither_changed_nor_deleted(exchange, monkeypatch):
    import psycopg

    number = _release(exchange, monkeypatch)
    for statement in ("UPDATE exchange_packages SET envelope = '\\x00' WHERE number = %s",
                      "UPDATE exchange_packages SET signal_ids = '{}' WHERE number = %s",
                      "DELETE FROM exchange_packages WHERE number = %s"):
        with connection.get_connection() as conn, pytest.raises(psycopg.errors.RaiseException):
            conn.execute(statement, (number,))


def test_two_builds_at_once_keep_the_order_of_numbers(exchange, monkeypatch):
    """Пакет с большим номером не виден раньше меньшего: номер, сборка и выкладка идут под одним замком одной
    транзакцией. Первая сборка задержана внутри (номер уже взят, пакет ещё не записан): вторая ждёт её замка."""
    first, second = (add_signal(key, first_seen=msk(2026, 9, 20, 10, 0)) for key in ("first", "second"))
    freeze(monkeypatch, exchange.after_close)
    inside, release = threading.Event(), threading.Event()
    seal = exchange_envelope.seal
    calls: list[int] = []

    def slow_seal(payload, *keys):
        calls.append(1)
        if len(calls) == 1:
            inside.set()
            assert release.wait(10)
        return seal(payload, *keys)

    monkeypatch.setattr(exchange_envelope, "seal", slow_seal)
    results: dict[str, int] = {}

    def build(name, signal_id):
        results[name] = exchange_packages.build_addition([signal_id], SEPT, by=name).built.number

    a = threading.Thread(target=build, args=("a", first))
    a.start()
    assert inside.wait(10)
    b = threading.Thread(target=build, args=("b", second))
    b.start()
    time.sleep(0.5)
    # Первая держит замок с выданным номером: её пакета ещё не видно, вторая не дошла до сборки.
    assert point().get("/api/exchange/v1/packages", headers=HEADERS).json() == {"packages": []}
    assert len(calls) == 1
    release.set()
    a.join(10)
    b.join(10)

    assert results["a"] < results["b"]
    assert point().get("/api/exchange/v1/packages", headers=HEADERS).json() == {"packages": [results["a"], results["b"]]}
    assert [package["cards"][0]["signal_id"] for package in exchange.take()] == [first, second]


# --- вкладка «Журнал обмена» и дозагрузка ----------------------------------------------------------------------------


def test_journal_tab_and_addition_are_for_the_administrator_only(exchange, monkeypatch):
    _release(exchange, monkeypatch)

    assert exchange.analyst.get("/api/exchange-journal").status_code == 403
    refused = exchange.analyst.post("/api/exchange-journal/additions", json={"month": SEPT, "numbers": [1]})
    assert refused.status_code == 403
    journal = exchange.admin.get("/api/exchange-journal")
    assert journal.status_code == 200, journal.text
    [package] = journal.json()["packages"]
    assert (package["kind"], package["month"], package["number"]) == ("release", SEPT, 1)
    assert package["cards"] == [{"signal_id": exchange.s["sep"], "title": "Сигнал sep", "category": None}]
    assert package["first_taken_at"] is None
    settings = journal.json()["settings"]
    assert settings["problems"] == [] and settings["point_problems"] == []
    assert settings["addresses"] == 1 and settings["token"] is True
    assert config.EXCHANGE_SIGNING_KEYS not in journal.text and CONSUMER_IP not in journal.text


def test_addition_moves_a_duplicate_to_its_root_refuses_unknown_and_removed_and_skips_sent(exchange, monkeypatch):
    freeze(monkeypatch, exchange.after_close)
    root = add_signal("root", first_seen=msk(2026, 9, 20, 10, 0))
    duplicate = add_signal("duplicate", first_seen=msk(2026, 9, 21, 10, 0))
    _merge(duplicate, root)
    archived = add_signal("archived", first_seen=msk(2026, 9, 22, 10, 0))
    removed = add_signal("removed", first_seen=msk(2026, 9, 23, 10, 0))
    feed = feed_signal(exchange.a["low"])
    with connection.get_connection() as conn:
        conn.execute("UPDATE signals SET archived_at = now() WHERE id = %s", (archived,))
        conn.execute("UPDATE signals SET otbor_decision = 'reject' WHERE id = %s", (removed,))
        conn.execute("UPDATE articles SET pending_deletion = TRUE WHERE id = %s", (exchange.a["low"],))
        conn.commit()
    admin = exchange.admin

    refused = admin.post("/api/exchange-journal/additions",
                         json={"month": SEPT, "numbers": [duplicate, 999_999, archived, removed, feed]})
    assert refused.status_code == 409
    assert refused.json()["detail"] == (
        f"Дозагрузка не собрана, нет таких сигналов: 999999; не действуют: {archived} (в архиве), "
        f"{removed} (отказ «Отбора»), {feed} (статья помечена на удаление).")
    assert packages() == []

    sent = admin.post("/api/exchange-journal/additions", json={"month": SEPT, "numbers": [duplicate]})
    assert sent.status_code == 200, sent.text
    assert sent.json() == {
        "package": 1, "sent": [root], "skipped": [], "moved": [{"requested": duplicate, "root": root}],
        "message": f"Пакет №1: дозагрузка к выпуску за сентябрь 2026, карточек 1: №{root}; дубль №{duplicate} ушёл "
                   f"корнем склейки №{root}.",
    }
    other = add_signal("other", first_seen=msk(2026, 9, 24, 10, 0))
    again = admin.post("/api/exchange-journal/additions", json={"month": SEPT, "numbers": [root, other]}).json()
    assert (again["package"], again["sent"]) == (2, [other])
    assert again["skipped"] == [{"signal_id": root, "package": 1, "month": SEPT}]
    nothing = admin.post("/api/exchange-journal/additions", json={"month": SEPT, "numbers": [root]}).json()
    assert nothing["package"] is None and nothing["message"].startswith("Пакет не собран: все номера уже отправлены")

    [first, second] = exchange.take()
    assert (first["kind"], first["month"], [card["signal_id"] for card in first["cards"]]) == ("addition", SEPT, [root])
    assert [card["signal_id"] for card in second["cards"]] == [other]
    assert sorted(sent_signals()) == sorted([root, other])
    journal = admin.get("/api/exchange-journal").json()["packages"]
    author = f"user{admin.id}@example.test"  # почта учётки, как её подставляет Person
    assert [(row["number"], row["kind"], row["built_by"]) for row in journal] == [
        (2, "addition", author), (1, "addition", author)]


def test_addition_command_reports_like_the_tab(exchange, monkeypatch, capsys):
    from oiltech_digest import cli

    freeze(monkeypatch, exchange.after_close)
    one = add_signal("one", first_seen=msk(2026, 9, 20, 10, 0))

    with pytest.raises(SystemExit) as refused:
        cli.main(["exchange-addition", "--month", SEPT, "--ids", f"{one}, 123456"])
    assert "нет таких сигналов: 123456" in str(refused.value)
    cli.main(["exchange-addition", "--month", SEPT, "--ids", str(one)])
    assert "exchange-addition: пакет №1: дозагрузка к выпуску за сентябрь 2026, карточек 1" in capsys.readouterr().out
