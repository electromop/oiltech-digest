"""Пакеты откликов потребителя (#200, спека #197): приём в точке обмена, своя таблица откликов, счёт по корню склейки
на карточке у администратора и выгрузка откликов за месяц.

Потребитель (двойник по описанию формата, tests/exchange_consumer.py) подписывает пакет своим ключом и шифрует на ключ
платформы. Платформа проверяет по порядку: формат и версию, подпись до расшифровки, получателя, расшифровку своим ключом,
содержимое (поля ровно по формату, виды, номера сигналов, которые уходили потребителю). Первое несовпадение: отказ с
причиной, откликов в базе не прибавляется, отказ виден в журнале обмена. Тот же номер с тем же содержимым: «уже было»,
другое содержимое: 409. Отклики лежат отдельно от отметок людей платформы: ни «Оценок людей», ни «Есть ОС», ни защиты
от перепечаток."""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from oiltech_digest import cli, exchange_packages
from oiltech_digest.db import connection, repository
from tests.digest_user_path_support import SEPT, add_signal, feed_signal, msk
from tests.digest_user_path_support import issue  # noqa: F401: фикстура выпуска
from tests.exchange_consumer import reactions
from tests.exchange_support import exchange  # noqa: F401: фикстура обмена
from tests.exchange_support import HEADERS, freeze, point

DAY = "2026-10-06"


def _sent(exchange, monkeypatch, *keys: str) -> dict[str, int]:
    """Сигналы, ушедшие потребителю дозагрузкой сентября (потребитель их забрал)."""
    freeze(monkeypatch, exchange.after_close)
    ids = {key: add_signal(key, first_seen=msk(2026, 9, 20 + index, 10, 0)) for index, key in enumerate(keys)}
    exchange_packages.build_addition(list(ids.values()), SEPT, by="test")
    exchange.take()
    return ids


def _send(exchange, number: int, content) -> object:
    return exchange.consumer.send_reactions(point(), HEADERS, number, exchange.consumer.seal(content))


def _counts() -> tuple[int, int]:
    with connection.get_connection() as conn:
        return conn.execute("SELECT (SELECT count(*) FROM exchange_reaction_packages),"
                            " (SELECT count(*) FROM exchange_reactions)").fetchone()


def _merge(duplicate: int, root: int) -> None:
    with connection.get_connection() as conn:
        conn.execute("UPDATE signals SET merged_into_signal_id = %s WHERE id = %s", (root, duplicate))
        conn.commit()


def _screen(person, signal_id: int) -> dict:
    answer = person.get("/api/signals", params={"limit": 200})
    assert answer.status_code == 200, answer.text
    [card] = [row for row in answer.json() if row["id"] == signal_id]
    return card


def test_reactions_package_is_accepted_once_and_a_repeat_with_the_same_content_does_not_double(exchange, monkeypatch):
    ids = _sent(exchange, monkeypatch, "one", "two")
    content = reactions(1, [(ids["one"], "work", True, DAY), (ids["two"], "useful", True, DAY),
                            (ids["one"], "not_new", False, "2026-10-07")])

    first = _send(exchange, 1, content)
    assert first.status_code == 200, first.text
    assert first.json() == {"number": 1, "outcome": "accepted", "message": "Принято: пакет откликов №1, откликов 3."}
    # Повтор: тот же пакет заново зашифрован (другой шифротекст), содержимое то же.
    again = _send(exchange, 1, content)
    assert again.status_code == 200, again.text
    assert again.json() == {"number": 1, "outcome": "already", "message": "Уже было: пакет откликов №1 принят раньше."}
    assert _counts() == (1, 3)
    other = _send(exchange, 1, reactions(1, [(ids["one"], "save", True, DAY)]))
    assert other.status_code == 409
    assert other.json()["detail"] == "Отклонено: пакет откликов №1 уже принят с другим содержимым."
    assert _counts() == (1, 3)

    with connection.get_connection() as conn:
        rows = conn.execute("SELECT package_number, position, signal_id, reaction, placed, day::text, source"
                            " FROM exchange_reactions ORDER BY position").fetchall()
    assert rows == [(1, 0, ids["one"], "work", True, DAY, "Тестовый потребитель"),
                    (1, 1, ids["two"], "useful", True, DAY, "Тестовый потребитель"),
                    (1, 2, ids["one"], "not_new", False, "2026-10-07", "Тестовый потребитель")]
    journal = exchange.admin.get("/api/exchange-journal").json()
    [package] = journal["reaction_packages"]
    assert (package["number"], package["source"], package["reactions"], package["repeats"]) == (
        1, "Тестовый потребитель", 3, 1)
    assert package["answer"] == "Принято: пакет откликов №1, откликов 3."
    assert journal["refusals"][0]["reason"] == "Отклонено: пакет откликов №1 уже принят с другим содержимым."


def _foreign_signature(exchange, content) -> bytes:
    """Подпись чужим ключом, а в поле signer отпечаток ключа потребителя."""
    stranger = Ed25519PrivateKey.generate()
    data = json.loads(exchange.consumer.seal(content, signing_key=stranger))
    data["signer"] = json.loads(exchange.consumer.seal(content))["signer"]
    return json.dumps(data).encode()


def _broken_ciphertext(exchange, content) -> bytes:
    """Шифротекст испорчен и заново подписан ключом потребителя: подпись сходится, расшифровка нет."""
    data = json.loads(exchange.consumer.seal(content))
    raw = bytearray(base64.b64decode(data["ciphertext"]))
    raw[-1] ^= 0xFF
    data["ciphertext"] = base64.b64encode(bytes(raw)).decode()
    data["signature"] = base64.b64encode(
        exchange.consumer.signing_key.sign(b"digest-exchange-package/2" + bytes(raw))).decode()
    return json.dumps(data).encode()


@pytest.mark.parametrize("case, reason", [
    ("unknown signer", "пакет подписан ключом {signer}, которого платформа не знает"),
    ("foreign signature", "подпись не сходится"),
    ("foreign recipient", "пакет зашифрован на ключ {recipient}, а не на ключ платформы"),
    ("broken ciphertext", "пакет не расшифровывается ключом платформы"),
    ("version 1", "формат «digest-exchange-package» версии 1, точка принимает «digest-exchange-package» версии 2"),
    ("extra envelope field", "файл пакета: лишнее поле note"),
    ("extra field", "содержимое: лишнее поле note"),
    ("missing field", "содержимое: нет поля source"),
    ("long source", "source: нужна непустая строка до 200 знаков"),
    ("unknown kind", "reactions[0].reaction: неизвестный вид «like»"),
    ("placed not bool", "reactions[0].placed: нужно true или false"),
    ("bad day", "reactions[0].day: нужна дата ГГГГ-ММ-ДД"),
    ("empty", "reactions: пустой список"),
    ("other number", "номер в пакете 2, а в адресе 1"),
    ("not sent", "reactions[1].signal_id: сигнал {stranger} потребителю не отправлялся"),
])
def test_foreign_or_broken_reactions_package_is_refused_with_a_reason_and_nothing_is_stored(
        exchange, monkeypatch, case, reason):
    ids = _sent(exchange, monkeypatch, "one")
    stranger = add_signal("stranger", first_seen=msk(2026, 9, 25, 10, 0))
    good = reactions(1, [(ids["one"], "work", True, DAY)])
    one = good["reactions"][0]
    consumer = exchange.consumer
    data = {
        "unknown signer": lambda: consumer.seal(good, signing_key=Ed25519PrivateKey.generate()),
        "foreign signature": lambda: _foreign_signature(exchange, good),
        "foreign recipient": lambda: consumer.seal(
            good, recipient=X25519PrivateKey.generate().public_key().public_bytes_raw()),
        "broken ciphertext": lambda: _broken_ciphertext(exchange, good),
        "version 1": lambda: consumer.seal(good, version=1),
        "extra envelope field": lambda: consumer.seal(good, note="x"),
        "extra field": lambda: consumer.seal({**good, "note": "x"}),
        "missing field": lambda: consumer.seal({key: value for key, value in good.items() if key != "source"}),
        "long source": lambda: consumer.seal({**good, "source": "ж" * 201}),
        "unknown kind": lambda: consumer.seal({**good, "reactions": [{**one, "reaction": "like"}]}),
        "placed not bool": lambda: consumer.seal({**good, "reactions": [{**one, "placed": 1}]}),
        "bad day": lambda: consumer.seal({**good, "reactions": [{**one, "day": "06.10.2026"}]}),
        "empty": lambda: consumer.seal({**good, "reactions": []}),
        "other number": lambda: consumer.seal({**good, "number": 2}),
        "not sent": lambda: consumer.seal({**good, "reactions": [one, {**one, "signal_id": stranger}]}),
    }[case]()
    envelope = json.loads(data)
    expected = reason.format(signer=envelope.get("signer"), recipient=envelope.get("recipient"), stranger=stranger)

    answer = consumer.send_reactions(point(), HEADERS, 1, data)

    assert answer.status_code == 422, answer.text
    assert answer.json()["detail"] == f"Отклонено: {expected}."
    assert _counts() == (0, 0)
    [refusal] = exchange.admin.get("/api/exchange-journal").json()["refusals"]
    assert (refusal["kind"], refusal["number"], refusal["reason"]) == ("reactions", 1, f"Отклонено: {expected}.")


def test_in_work_counts_by_the_merge_root_and_only_the_administrator_sees_the_line(exchange, monkeypatch):
    """Отклики пришли под номером дубля до склейки и под номером корня после неё: счёт по корню на момент чтения
    сходится. «В работе у N групп»: поставлено минус снято «В работу»."""
    ids = _sent(exchange, monkeypatch, "dup", "root")
    dup, root = ids["dup"], ids["root"]
    assert _send(exchange, 1, reactions(1, [(dup, "work", True, DAY), (root, "work", True, DAY),
                                            (dup, "useful", True, DAY), (root, "save", True, DAY)])).status_code == 200
    _merge(dup, root)
    # После склейки у потребителя: третья группа берёт в работу, лишнюю отметку группы на обоих сигналах он снимает.
    assert _send(exchange, 2, reactions(2, [(root, "work", True, "2026-10-08"), (root, "work", False, "2026-10-08"),
                                            (root, "not_oilfield", True, "2026-10-08")])).status_code == 200

    card = _screen(exchange.admin, root)
    assert card["consumer_reactions"] == {"work": 2, "save": 1, "useful": 1, "not_oilfield": 1}
    assert "consumer_reactions" not in _screen(exchange.analyst, root)
    assert all(row["id"] != dup for row in exchange.admin.get("/api/signals", params={"limit": 200}).json())


def test_reactions_are_not_marks_of_people(exchange, monkeypatch):
    """Ни «Оценок людей» на карточке, ни фильтра «Есть ОС», ни защиты копии от перепечатки."""
    freeze(monkeypatch, exchange.after_close)
    feed = feed_signal(exchange.a["hi"])
    exchange_packages.build_addition([feed], SEPT, by="test")
    exchange.take()
    assert _send(exchange, 1, reactions(1, [(feed, "card_error", True, DAY), (feed, "work", True, DAY)])).status_code == 200

    # Сигнал ленты виден в бизнес-ленте или на радаре: по его категории.
    card = None
    for showcase in ("business", "technology"):
        rows = exchange.admin.get("/api/signals", params={"limit": 200, "showcase": showcase}).json()
        card = next((row for row in rows if row["id"] == feed), card)
    assert card is not None and card["feedback_blocks"] == []
    for showcase in ("business", "technology"):
        with_feedback = exchange.admin.get("/api/signals", params={"limit": 200, "showcase": showcase,
                                                                   "feedback": "with"}).json()
        assert all(row["id"] != feed for row in with_feedback)
    with connection.get_connection() as conn:
        assert repository._kept_by_people(conn, exchange.a["hi"]) is None


def test_reactions_of_a_month_are_exported_by_a_command(exchange, monkeypatch, tmp_path, capsys):
    from openpyxl import load_workbook

    ids = _sent(exchange, monkeypatch, "dup", "root", "quiet")
    _send(exchange, 1, reactions(1, [(ids["dup"], "work", True, "2026-09-30"), (ids["root"], "work", True, DAY),
                                     (ids["root"], "card_error", True, DAY), (ids["quiet"], "save", True, "2026-09-29")]))
    _merge(ids["dup"], ids["root"])
    target = tmp_path / "reactions.xlsx"

    cli.main(["exchange-reactions", "--month", "2026-10", "--out", str(target)])

    assert f"exchange-reactions: отклики за октябрь 2026: сигналов 1, файл {target}" in capsys.readouterr().out
    sheet = load_workbook(target).active
    rows = list(sheet.iter_rows(values_only=True))
    assert rows[0][:4] == ("Сигнал", "Заголовок", "Месяц выпуска", "В работе у групп")
    # Октябрь: отклики корня (дубль склеен с ним); счёт на конец месяца по корню, с откликом дубля сентября.
    [row] = rows[1:]
    values = dict(zip(rows[0], row))
    assert (values["Сигнал"], values["Месяц выпуска"], values["В работе у групп"]) == (ids["root"], SEPT, 2)
    assert values["Ошибка в карточке"] == 1 and values["Сохранить"] == 0


def test_card_names_the_reactions_as_the_server_does():
    """Строка откликов на карточке витрины пишет виды теми же словами, что выгрузка и разбор ОС; «В работу» на
    карточке словами «в работе у N групп»."""
    import re
    from pathlib import Path

    from oiltech_digest import exchange_reactions

    source = (Path(__file__).resolve().parents[1]
              / "frontend/src/features/signals/SignalConsumerReactions.tsx").read_text(encoding="utf-8")
    block = source.split("const LABELS", 1)[1].split("\n};", 1)[0]

    labels = {kind: label for kind, label in exchange_reactions.LABELS.items() if kind != "work"}
    assert dict(re.findall(r'(\w+): "([^"]+)"', block)) == labels
