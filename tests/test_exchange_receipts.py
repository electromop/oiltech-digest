"""Квитанции потребителя на пакеты (#200, спека #197): после приёма каждого пакета из точки обмена потребитель отдаёт
PUT /api/exchange/v1/receipts/{номер} с итогом (принят, уже был, отклонён) и своим ответом одной строкой.

Квитанция ложится к своему пакету; повтор с тем же содержимым отвечает 2xx и ничего не двоит, другое содержимое для
того же номера даёт 409, номер, которого нет, 404; квитанция не по формату: 422. Вкладка «Журнал обмена» показывает у
пакета итог и ответ потребителя, а отказ точки (409, 404, 422) событием с причиной."""

from __future__ import annotations

from oiltech_digest import exchange_packages
from oiltech_digest.db import connection
from tests.digest_user_path_support import SEPT
from tests.digest_user_path_support import issue  # noqa: F401: фикстура выпуска
from tests.exchange_support import exchange  # noqa: F401: фикстура обмена
from tests.exchange_support import HEADERS, freeze, point, ready


def _release(exchange, monkeypatch) -> int:
    exchange.analyst.mark_signal(exchange.s["sep"])
    ready(exchange.s["sep"])
    freeze(monkeypatch, exchange.after_close)
    return exchange_packages.build_release(SEPT, by="test").number


def _receipt(number: int, body) -> object:
    return point().put(f"/api/exchange/v1/receipts/{number}", json=body, headers=HEADERS)


def _stored(number: int) -> tuple:
    with connection.get_connection() as conn:
        return conn.execute("SELECT receipt_outcome, receipt_message, receipt_at FROM exchange_packages"
                            " WHERE number = %s", (number,)).fetchone()


def test_receipt_lies_with_its_package_a_repeat_does_not_double_other_content_is_409_and_unknown_404(
        exchange, monkeypatch):
    number = _release(exchange, monkeypatch)
    rejected = {"number": number, "outcome": "rejected",
                "message": "Отклонено: содержимое не по формату версии 2, cards[0].hypothesis: нет поля."}

    first = _receipt(number, rejected)
    assert first.status_code == 200, first.text
    assert first.json() == {"number": number, "receipt": "принята"}
    outcome, message, at = _stored(number)
    assert (outcome, message) == ("rejected", rejected["message"]) and at is not None

    again = _receipt(number, rejected)
    assert again.status_code == 200 and again.json() == {"number": number, "receipt": "уже была"}
    assert _stored(number) == (outcome, message, at)

    other = _receipt(number, {**rejected, "outcome": "accepted", "message": "Принято: пакет №1."})
    assert other.status_code == 409
    assert other.json()["detail"] == f"На пакет №{number} уже есть другая квитанция: отклонён."
    assert _stored(number) == (outcome, message, at)

    unknown = _receipt(number + 1, {**rejected, "number": number + 1})
    assert unknown.status_code == 404 and unknown.json()["detail"] == f"Пакета №{number + 1} нет."


def test_receipt_out_of_format_is_refused_with_a_reason(exchange, monkeypatch):
    number = _release(exchange, monkeypatch)
    good = {"number": number, "outcome": "accepted", "message": "Принято: пакет №1."}
    for body, reason in (
        ({**good, "number": number + 7}, f"номер в квитанции {number + 7}, а в адресе {number}"),
        ({**good, "outcome": "lost"}, "outcome: неизвестный итог «lost»"),
        ({**good, "extra": 1}, "лишнее поле extra"),
        ({key: value for key, value in good.items() if key != "message"}, "нет поля message"),
        ({**good, "number": str(number)}, "number: нужно целое число"),
        ({**good, "message": 5}, "message: нужна строка"),
        ([good], "квитанция не JSON-объект"),
    ):
        answer = _receipt(number, body)
        assert answer.status_code == 422, (body, answer.text)
        assert answer.json()["detail"] == f"Квитанция не по формату: {reason}.", body
    assert _stored(number)[0] is None


def test_journal_shows_the_receipt_of_a_package_and_refusals_of_the_point(exchange, monkeypatch):
    number = _release(exchange, monkeypatch)
    _receipt(number, {"number": number, "outcome": "rejected", "message": "Отклонено: подпись не сходится."})
    for _ in range(2):
        _receipt(number, {"number": number, "outcome": "accepted", "message": "Принято."})
    _receipt(number + 3, {"number": number + 3, "outcome": "accepted", "message": "Принято."})

    journal = exchange.admin.get("/api/exchange-journal").json()
    [package] = journal["packages"]
    assert package["receipt"]["outcome"] == "rejected"
    assert package["receipt"]["message"] == "Отклонено: подпись не сходится."
    assert package["receipt"]["at"] is not None
    refusals = {(one["kind"], one["number"]): one for one in journal["refusals"]}
    assert refusals[("receipt", number)]["reason"] == f"На пакет №{number} уже есть другая квитанция: отклонён."
    assert refusals[("receipt", number)]["attempts"] == 2
    assert refusals[("receipt", number + 3)]["reason"] == f"Пакета №{number + 3} нет."
    assert exchange.analyst.get("/api/exchange-journal").status_code == 403
