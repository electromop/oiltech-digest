"""Пакет обновления для потребителя выпуска (#200, спека #197), глазами потребителя.

После выпуска карточка у потребителя следует за платформой: обновление несёт изменённые карточки (дата обновления:
когда платформа увидела новый отпечаток), склейки отправленных сигналов (оставшийся сигнал, которого потребитель не
видел, приходит карточкой в том же пакете), снятия по явному решению и возвраты (карточка с датой новее снятия). Без
изменений пакет не выкладывается. Двойник потребителя (tests/exchange_consumer.State) принимает пакеты по правилам
потребителя: пакет, который тот отклонил бы, роняет тест."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from oiltech_digest import cli, config, exchange_packages, exchange_updates
from oiltech_digest.db import connection, repository
from oiltech_digest.feed_window import MSK
from tests.digest_user_path_support import SEPT, add_signal, feed_signal, msk, set_card
from tests.digest_user_path_support import issue  # noqa: F401: фикстура выпуска
from tests.exchange_consumer import State
from tests.exchange_support import exchange  # noqa: F401: фикстура обмена
from tests.exchange_support import freeze, packages, ready, sent_signals


class Follower:
    """Потребитель, который забирает новые пакеты и принимает их по своим правилам."""

    def __init__(self, exchange) -> None:
        self.exchange, self.state, self.seen = exchange, State(), set()

    def take(self) -> list[dict]:
        fresh = [one for one in self.exchange.take() if one["number"] not in self.seen]
        for package in fresh:
            self.state.apply(package)
            self.seen.add(package["number"])
        return fresh


def _update(**kwargs) -> exchange_updates.Update:
    return exchange_updates.build_update(by="test", trigger="manual", **kwargs)


def _merge(duplicate: int, root: int | None) -> None:
    with connection.get_connection() as conn:
        conn.execute("UPDATE signals SET merged_into_signal_id = %s WHERE id = %s", (root, duplicate))
        conn.commit()


def _sql(statement: str, *params) -> None:
    with connection.get_connection() as conn:
        conn.execute(statement, params)
        conn.commit()


def _sent(exchange, monkeypatch, *keys: str) -> tuple[Follower, dict[str, int]]:
    freeze(monkeypatch, exchange.after_close)
    ids = {key: add_signal(key, first_seen=msk(2026, 9, 14 + index, 10, 0)) for index, key in enumerate(keys)}
    exchange_packages.build_addition(list(ids.values()), SEPT, by="test")
    consumer = Follower(exchange)
    consumer.take()
    return consumer, ids


def _correct(person, signal_id: int, title: str) -> None:
    answer = person.post("/api/signals/feedback", json={"signal_id": signal_id, "corrected_title": title})
    assert answer.status_code == 200, answer.text


def _ids(package: dict) -> tuple[list[int], list[dict], list[int]]:
    return [card["signal_id"] for card in package["cards"]], package["merges"], package["removals"]


def test_update_goes_only_when_the_sent_card_changes(exchange, monkeypatch, hypothesis_on):
    sep = exchange.s["sep"]
    exchange.analyst.mark_signal(sep)
    ready(sep)
    freeze(monkeypatch, exchange.after_close)
    exchange_packages.build_release(SEPT, by="test")
    consumer = Follower(exchange)
    consumer.take()
    before = consumer.state.cards[sep]["updated_at"]

    assert _update().built is None
    assert len(packages()) == 1 and consumer.take() == []

    changes = [
        ("правка администратора", lambda: _correct(exchange.admin, sep, "Правка после выпуска"),
         lambda card: card["title"] == "Правка после выпуска"),
        ("новая публикация", lambda: repository.upsert_signal_evidence(sep, {
            "source_url": "https://radar.example.org/sep-2", "title": "Ещё публикация", "publisher": "radar.example.org",
            "published_at": None, "strength": 0.9}),
         lambda card: "https://radar.example.org/sep-2" in [one["url"] for one in card["publications"]]),
        ("новый атрибут", lambda: set_card(sep, region="Европа"), lambda card: card["region"] == "Европа"),
        ("гипотеза", lambda: set_card(sep, hypothesis="Оценить датчики.", card_set_by={"hypothesis": "human"}),
         lambda card: card["hypothesis"] == "Оценить датчики."),
    ]
    for what, change, seen in changes:
        change()
        built = _update().built
        assert built is not None, what
        [package] = consumer.take()
        assert (package["kind"], _ids(package)) == ("update", ([sep], [], [])), what
        card = consumer.state.cards[sep]
        assert seen(card), what
        assert datetime.fromisoformat(card["updated_at"]) > datetime.fromisoformat(before), what
        before = card["updated_at"]
        assert _update().built is None, what
    assert sent_signals()[sep]["state"] == "active"


def test_merge_goes_as_a_merge_the_unseen_root_comes_as_a_card_and_the_merged_number_is_never_sent_again(
        exchange, monkeypatch):
    consumer, ids = _sent(exchange, monkeypatch, "dup", "other")
    dup, other = ids["dup"], ids["other"]
    root = add_signal("root", first_seen=msk(2026, 9, 20, 10, 0))
    _merge(dup, root)

    built = _update().built
    [package] = consumer.take()

    assert _ids(package) == ([root], [{"signal_id": dup, "merged_into": root}], [])
    assert dup not in consumer.state.cards and consumer.state.merged[dup] == root and root in consumer.state.cards
    journal = sent_signals()
    assert (journal[dup]["state"], journal[dup]["merged_into"], journal[dup]["state_package"]) == (
        "merged", root, built.number)
    assert (journal[root]["first_package"], journal[root]["month"], journal[root]["state"]) == (
        built.number, SEPT, "active")

    # Склеенный номер больше не отправляется: ни его правка, ни дозагрузка по его номеру.
    set_card(dup, region="Азия")
    assert _update().built is None
    assert exchange_packages.build_addition([dup], SEPT, by="test").built is None

    # Склейка отправленного сигнала в отправленный: только склейка и изменившаяся карточка корня.
    _merge(other, root)
    [package] = (_update(), consumer.take())[1]
    assert _ids(package) == ([root], [{"signal_id": other, "merged_into": root}], [])

    # Расклейка на платформе в формат не передаётся: журнал помечает номер, номер не уходит.
    _merge(dup, None)
    _update()
    for one in consumer.take():
        assert dup not in _ids(one)[0] and dup not in _ids(one)[2]
        assert all(merge["signal_id"] != dup for merge in one["merges"])
    assert (sent_signals()[dup]["state"], sent_signals()[dup]["merged_into"]) == ("unmerged", root)
    set_card(dup, region="Африка")
    built = _update().built
    assert built is None or dup not in built.signal_ids
    notes = [note for row in exchange.admin.get("/api/exchange-journal").json()["sent_states"]
             if row["signal_id"] == dup for note in [row["state_label"]]]
    assert notes == [f"расклеен на платформе, у потребителя склеен с №{root}"]


def test_explicit_decisions_remove_self_acting_rules_do_not_and_a_return_comes_newer_than_the_removal(
        exchange, monkeypatch):
    keys = ("archived", "people", "otbor", "old_event", "quality", "category")
    consumer, ids = _sent(exchange, monkeypatch, *keys)
    feed = feed_signal(exchange.a["low"])
    kept_feed = feed_signal(exchange.a["mid"])
    exchange_packages.build_addition([feed, kept_feed], SEPT, by="test")
    consumer.take()

    _sql("UPDATE signals SET archived_at = now() WHERE id = %s", ids["archived"])
    _sql("UPDATE signals SET human_removal = '{\"author\": \"admin@example.test\"}'::jsonb WHERE id = %s",
         ids["people"])
    _sql("UPDATE signals SET otbor_decision = 'reject' WHERE id = %s", ids["otbor"])
    _sql("UPDATE articles SET pending_deletion = TRUE WHERE id = %s", exchange.a["low"])
    set_card(ids["old_event"], event_date=date(2020, 1, 15), event_date_precision="day")
    set_card(ids["quality"], oilfield_relevance="none", mixed_events=True)
    set_card(ids["category"], signal_category="business")
    _sql("UPDATE sources SET archived_at = now() WHERE id = %s", exchange.source)

    built = _update().built
    [package] = consumer.take()

    removed = sorted([ids["archived"], ids["people"], ids["otbor"], feed])
    assert package["removals"] == removed and package["merges"] == []
    assert set(_ids(package)[0]) <= {ids["old_event"], ids["quality"]}
    assert sorted(consumer.state.removed) == removed
    journal = sent_signals()
    assert {one for one, row in journal.items() if row["state"] == "removed"} == set(removed)
    assert {journal[one]["state_package"] for one in removed} == {built.number}
    assert _update().built is None

    # Возврат: снятое снова действует, карточка приходит с датой новее снятия, пометка у потребителя снимается.
    _sql("UPDATE signals SET archived_at = NULL WHERE id = %s", ids["archived"])
    [package] = (_update(), consumer.take())[1]
    assert _ids(package) == ([ids["archived"]], [], [])
    assert ids["archived"] not in consumer.state.removed
    assert sent_signals()[ids["archived"]]["state"] == "active"


def test_a_return_waits_until_the_consumer_took_the_removal(exchange, monkeypatch):
    """Потребитель помечает снятие временем, когда принял пакет: возврат с датой раньше его не снял бы пометку."""
    consumer, ids = _sent(exchange, monkeypatch, "one")
    one = ids["one"]
    _sql("UPDATE signals SET archived_at = now() WHERE id = %s", one)
    removal = _update().built
    _sql("UPDATE signals SET archived_at = NULL WHERE id = %s", one)

    held = _update()
    assert held.built is None
    assert held.notes == [f"Возврат №{one} ждёт: потребитель ещё не забрал пакет №{removal.number} со снятием"]
    consumer.take()
    assert one in consumer.state.removed
    [package] = (_update(), consumer.take())[1]
    assert _ids(package) == ([one], [], [])
    assert one not in consumer.state.removed


def test_merge_into_a_signal_removed_by_the_same_update_waits_for_the_next_one(exchange, monkeypatch):
    consumer, ids = _sent(exchange, monkeypatch, "dup", "root")
    _merge(ids["dup"], ids["root"])
    _sql("UPDATE signals SET archived_at = now() WHERE id = %s", ids["root"])

    first = _update()
    [package] = consumer.take()
    assert _ids(package) == ([], [], [ids["root"]])
    assert first.notes == [f"Склейка №{ids['dup']} в №{ids['root']} ждёт: №{ids['root']} снимается этим пакетом"]
    [package] = (_update(), consumer.take())[1]
    assert _ids(package) == ([], [{"signal_id": ids["dup"], "merged_into": ids["root"]}], [])
    assert consumer.state.merged[ids["dup"]] == ids["root"]


def test_step_runs_once_a_day_behind_the_flag_and_the_tab_builds_an_update_now(exchange, monkeypatch, capsys):
    consumer, ids = _sent(exchange, monkeypatch, "one")
    set_card(ids["one"], region="Европа")

    assert exchange_updates.update_step() is None
    cli.main(["exchange-update"])
    assert "exchange-update: шаг выключен (EXCHANGE_ENABLED)" in capsys.readouterr().out
    monkeypatch.setattr(config, "EXCHANGE_ENABLED", True)
    cli.main(["exchange-update"])
    assert "exchange-update: пакет №2: обновление, карточек 1, склеек 0, снятий 0" in capsys.readouterr().out
    set_card(ids["one"], region="Азия")
    cli.main(["exchange-update"])
    assert "exchange-update: обновление сегодня уже было" in capsys.readouterr().out
    tomorrow = datetime.now(MSK) + timedelta(days=1)
    monkeypatch.setattr(exchange_updates, "_now", lambda: tomorrow)
    assert exchange_updates.update_step().built.number == 3

    set_card(ids["one"], region="Африка")
    assert exchange.analyst.post("/api/exchange-journal/updates").status_code == 403
    now = exchange.admin.post("/api/exchange-journal/updates")
    assert now.status_code == 200, now.text
    assert now.json() == {"package": 4, "message": "Пакет №4: обновление, карточек 1, склеек 0, снятий 0."}
    nothing = exchange.admin.post("/api/exchange-journal/updates").json()
    assert nothing == {"package": None, "message": "Изменений нет: пакет обновления не собран."}
    journal = exchange.admin.get("/api/exchange-journal").json()
    assert [run["trigger"] for run in journal["updates"]] == ["manual", "manual", "schedule", "schedule"]
    assert journal["packages"][0]["kind"] == "update" and journal["packages"][0]["month"] is None
    assert [package["number"] for package in consumer.take()] == [2, 3, 4]


@pytest.mark.parametrize("missing", ["EXCHANGE_SIGNING_KEYS", "EXCHANGE_CONSUMER_KEYS"])
def test_update_without_keys_is_refused_with_a_reason(exchange, monkeypatch, missing):
    _sent(exchange, monkeypatch, "one")
    monkeypatch.setattr(config, missing, "")
    with pytest.raises(exchange_packages.Refused, match=missing):
        _update()
