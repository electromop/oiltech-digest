"""Отрицательные отклики потребителя выпуска во входе разбора ОС (#200, спека #197), шов 2: недельный срез и разбор ОС
через настоящий клиент воркера NL с подставной моделью (tests/os_journal_support.py).

Отрицательные отклики («Не про нефтесервис», «Ошибка в карточке», «Не новое», «Мало фактов») с прошлого среза входят в
пакет среза как отметки без автора с ролью «сотрудник потребителя», рядом с отметками людей платформы; положительные не
входят, снятый отклик гасит поставленный. Предложение ссылается на отклики отдельным списком рядом с номерами отметок и
ложится в журнал ОС; в эталон ОС отклики не входят. Сборка воркера ниже контракта 14 задачу разбора с откликами не
получает: срез падает с причиной."""

from __future__ import annotations

from oiltech_digest import exchange_packages
from oiltech_digest.db import connection, rule_check_rows
from tests.digest_user_path_support import SEPT
from tests.digest_user_path_support import issue  # noqa: F401: фикстура выпуска
from tests.exchange_consumer import reactions
from tests.exchange_support import exchange  # noqa: F401: фикстура обмена
from tests.exchange_support import HEADERS, point
from tests.os_journal_support import business, improvement, journal, mark, review
from tests.otbor_support import contour  # noqa: F401: фикстура
from tests.signal_card_support import worker

DAY = "2026-10-06"


def _send(exchange, number: int, items) -> list[int]:
    """Пакет откликов от потребителя; номера принятых откликов по порядку."""
    answer = exchange.consumer.send_reactions(point(), HEADERS, number, exchange.consumer.seal(reactions(number, items)))
    assert answer.status_code == 200, answer.text
    with connection.get_connection() as conn:
        return [int(row[0]) for row in conn.execute(
            "SELECT id FROM exchange_reactions WHERE package_number = %s ORDER BY position", (number,)).fetchall()]


def _collect(person) -> dict:
    answer = person.post("/api/feedback-journal/slices")
    assert answer.status_code == 200, answer.text
    return answer.json()


def _reference() -> list:
    return [rule_check_rows.otbor_rows(["decision", "category"], None, 100, reasons={}),
            rule_check_rows.scores_rows(None, 100),
            rule_check_rows.card_rows(["theme", "region"], None, 100)]


def test_negative_reactions_come_into_the_slice_without_authors_and_a_proposal_cites_them(
        exchange, contour, monkeypatch):
    admin, colleague = exchange.admin, exchange.colleague
    quiet, marked = business(exchange.a["hi"]), business(exchange.a["mid"])
    exchange_packages.build_addition([quiet, marked], SEPT, by="test")
    exchange.take()
    reference = _reference()
    by_colleague = mark(colleague, marked, comment="Это уже было в прошлом месяце")
    ids = _send(exchange, 1, [(quiet, "few_facts", True, DAY), (quiet, "useful", True, DAY),
                              (quiet, "card_error", True, DAY), (quiet, "card_error", False, DAY),
                              (quiet, "few_facts", True, "2026-10-07"), (marked, "not_new", True, DAY),
                              (marked, "work", True, DAY)])
    few_facts = [ids[0], ids[4]]
    summary = "Оператор заключил контракт на бурение 40 скважин на шельфе, работы начнутся в 2027 году."

    assert _collect(admin)["signals"] == 2
    model = review(monkeypatch, {quiet: {"improvements": [
        {**improvement("summary", summary, [], "Мало фактов"), "reactions": few_facts}]}})

    calls = {int(call["input"].splitlines()[0].split("№")[1]): call["input"] for call in model.calls}
    assert sorted(calls) == sorted([quiet, marked])
    assert f"Отклик №{ids[0]} (сотрудник потребителя, {DAY}): Мало фактов" in calls[quiet]
    assert f"Отклик №{ids[4]} (сотрудник потребителя, 2026-10-07): Мало фактов" in calls[quiet]
    # Положительные отклики и снятый «Ошибка в карточке» в срез не идут; автора и метки источника нет.
    assert "Полезный сигнал" not in calls[quiet] and "Ошибка в карточке" not in calls[quiet]
    assert "Тестовый потребитель" not in calls[quiet]
    assert f"Отклик №{ids[5]} (сотрудник потребителя, {DAY}): Не новое" in calls[marked]
    assert f"Отметка №{by_colleague['event_id']} (участник" in calls[marked] and "В работу" not in calls[marked]
    assert "colleague@example.test" not in calls[marked]
    assert "reactions" in model.calls[0]["schema"]["schema"]["properties"]["improvements"]["items"]["properties"]

    [proposal] = journal(admin, kind="signal_proposal")
    assert (proposal["signal_id"], proposal["event_ids"], proposal["status"]) == (quiet, [], "pending")
    assert [(one["id"], one["reaction"], one["role"]) for one in proposal["reactions"]] == [
        (ids[0], "Мало фактов", "сотрудник потребителя"), (ids[4], "Мало фактов", "сотрудник потребителя")]
    assert (proposal["changes"][0]["field"], proposal["changes"][0]["reaction_ids"]) == ("summary", few_facts)
    # Отклики в эталон ОС не входят.
    assert _reference() == reference
    # Следующий срез: новых откликов нет, сигнал без новых отметок в разбор не идёт.
    assert _collect(admin)["signals"] == 0


def test_reactions_ground_a_removal_but_not_a_category_another_card_or_below_the_threshold(
        exchange, contour, monkeypatch):
    """Отклик «Не про нефтесервис» обосновывает предложение убрать карточку (убирает администратор, согласовав его),
    категорию отклик не обосновывает; номер отклика чужой карточки и откликов меньше порога: не пишется."""
    admin = exchange.admin
    one, other, third = business(exchange.a["hi"]), business(exchange.a["mid"]), business(exchange.a["low"])
    exchange_packages.build_addition([one, other, third], SEPT, by="test")
    exchange.take()
    ids = _send(exchange, 1, [(one, "few_facts", True, DAY), (other, "few_facts", True, DAY),
                              (third, "not_oilfield", True, DAY), (third, "not_oilfield", True, DAY)])
    admin.put("/api/feedback-journal/settings", json={"values": {"card_threshold_marks": 2}})

    _collect(admin)
    review(monkeypatch, {
        one: {"improvements": [{**improvement("summary", "Новая суть с фактами.", [], "Мало фактов"),
                                "reactions": [ids[0]]}]},
        other: {"improvements": [{**improvement("summary", "Другая суть.", [], "Мало фактов"),
                                  "reactions": [ids[0], ids[1]]}]},
        third: {"improvements": [{**improvement("removed", "да", [], "Не про нефтесервис"),
                                  "reactions": [ids[2], ids[3]]},
                                 {**improvement("signal_category", "технология", [], "Не про нефтесервис"),
                                  "reactions": [ids[2], ids[3]]}]},
    })

    [proposal] = journal(admin, kind="signal_proposal")
    assert proposal["signal_id"] == third and proposal["event_ids"] == []
    assert [(change["field"], change["reaction_ids"]) for change in proposal["changes"]] == [
        ("removed", [ids[2], ids[3]])]
    # Сам разбор ничего не меняет: карточка на витрине, пока администратор не согласует.
    assert third in {row["id"] for row in admin.get("/api/signals", params={"limit": 200,
                                                                              "showcase": "business"}).json()}


def test_old_nl_build_does_not_get_a_review_with_reactions(exchange, contour, monkeypatch):
    admin = exchange.admin
    signal_id = business(exchange.a["hi"])
    exchange_packages.build_addition([signal_id], SEPT, by="test")
    exchange.take()
    _send(exchange, 1, [(signal_id, "card_error", True, DAY)])
    _collect(admin)

    assert worker("external-ai", contract_number=13).claim() is None

    [failed] = admin.get("/api/feedback-journal/overview").json()["slices"]
    assert failed["status"] == "failed"
    assert "нужен контракт 14" in failed["note"]
