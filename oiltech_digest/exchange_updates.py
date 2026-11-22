"""Пакет обновления для потребителя выпуска (#200, спека #197): после выпуска карточка у потребителя следует за
платформой.

Раз в сутки (шаг планировщика exchange-update за флагом EXCHANGE_ENABLED) и по кнопке «Собрать обновление сейчас»
сборка сверяет журнал отправленного с платформой под замком сборки пакетов:
- изменённые карточки: карточки отправленных действующих сигналов пересобираются той же функцией чтения, что у экрана
  витрины (showcase_cards), и сравниваются по отпечатку; изменённая идёт карточкой с датой обновления: когда платформа
  увидела новый отпечаток;
- склейки: отправленный сигнал, который на платформе стал дублем, уходит склейкой в корень; корень, которого потребитель
  ещё не видел, приходит карточкой в том же пакете (у потребителя он получает месяц склеенной карточки). Номер, ушедший
  склейкой, больше не отправляется; расклейка на платформе в формат не передаётся, журнал отправленного помечает номер
  «расклеен на платформе, у потребителя склеен»;
- снятия по правилу «сигнал действует» (exchange_store.inactive_reason_sql): архив, «Убрано людьми», отказ «Отбора»,
  пометка статьи на удаление. Правила показа, которые срабатывают сами (срок события, качество радара), смена категории
  и архив источника снятия не дают;
- возвраты: снятый сигнал снова действует, его карточка приходит с датой новее снятия. Потребитель помечает снятие
  временем, когда принял пакет, поэтому возврат ждёт, пока потребитель не заберёт пакет со снятием: иначе карточка с
  датой раньше его пометки пометку не сняла бы.
Сигнал в пакете встречается один раз: карточкой, склейкой или снятием. Склейка в сигнал, который снимается этим же
пакетом, ждёт следующего обновления: такой пакет потребитель отклонил бы. Пустое обновление не выкладывается (номер
пакета не тратится), но запуск записывается: шаг идёт раз в сутки по Москве.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timezone
import json
from typing import Any

from oiltech_digest import config, exchange_card, exchange_envelope, exchange_packages
from oiltech_digest.db import connection, exchange_store, showcase_cards
from oiltech_digest.db.exchange_store import ACTIVE, MERGED, REMOVED, UNMERGED, UPDATE
from oiltech_digest.feed_window import MSK

SCHEDULE, MANUAL = "schedule", "manual"
NOTHING = "Изменений нет: пакет обновления не собран"
# Сколько шагов цепочки склеек у потребителя пройти до оставшегося сигнала (предел лишь от петли).
_CHAIN_STEPS = 50


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Update:
    """Итог сборки обновления: пакет (или None), что ждёт следующего обновления, ответ словами."""

    built: exchange_packages.Built | None
    notes: list[str] = field(default_factory=list)
    message: str = ""


@dataclass
class _Plan:
    cards: dict[int, dict[str, Any]] = field(default_factory=dict)
    changed: list[int] = field(default_factory=list)
    returned: list[int] = field(default_factory=list)
    roots: dict[int, str] = field(default_factory=dict)
    merges: list[tuple[int, int]] = field(default_factory=list)
    removals: dict[int, str] = field(default_factory=dict)
    flips: list[tuple[int, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.cards or self.merges or self.removals)


def _bodies(conn, ids: list[int]) -> tuple[dict[int, dict[str, Any]], dict[int, str]]:
    """Карточки экрана витрины в полях формата (без даты обновления) и почему карточку не собрать."""
    rows = showcase_cards.read(conn, ids)
    found = showcase_cards.publications(conn, list(rows))
    bodies, problems = {}, {}
    for signal_id, row in rows.items():
        try:
            bodies[signal_id] = exchange_card.card({**row, "evidence": found.get(signal_id, [])})
        except exchange_card.CardError as error:
            problems[signal_id] = str(error)
    return bodies, problems


def _structure(conn, plan: _Plan, journal: dict[int, dict[str, Any]], roots: dict[int, int]) -> dict[int, str]:
    """Снятия и склейки отправленных сигналов, которые у потребителя есть карточкой; не действующие сигналы."""
    known = sorted(one for one, row in journal.items() if row["state"] in (ACTIVE, REMOVED))
    unseen = {roots[one] for one in known if one in roots and roots[one] != one and roots[one] not in journal}
    inactive = exchange_store.inactive(conn, [one for one in known if roots.get(one) == one] + sorted(unseen))
    for one in known:
        if journal[one]["state"] != ACTIVE:
            continue
        if one not in roots:
            plan.removals[one] = "сигнала нет на платформе"
        elif roots[one] == one and one in inactive:
            plan.removals[one] = inactive[one]
    for one in known:
        root = roots.get(one)
        if root is None or root == one:
            continue
        if root in plan.removals:
            plan.notes.append(f"Склейка №{one} в №{root} ждёт: №{root} снимается этим пакетом")
            continue
        if root not in journal:
            if root in inactive:
                if journal[one]["state"] == ACTIVE:
                    plan.removals[one] = f"склеен с №{root}, а тот {inactive[root]}"
                continue
            month = journal[one]["month"]
            plan.roots[root] = min(plan.roots.get(root, month), month)
        plan.merges.append((one, root))
    return inactive


def _cards(conn, plan: _Plan, journal: dict[int, dict[str, Any]], roots: dict[int, int],
           inactive: dict[int, str]) -> None:
    """Изменённые карточки, возвраты и оставшиеся сигналы склеек, которых потребитель не видел."""
    acting = [one for one, row in journal.items() if row["state"] in (ACTIVE, REMOVED) and roots.get(one) == one
              and one not in inactive and one not in plan.removals]
    bodies, problems = _bodies(conn, sorted(acting) + sorted(plan.roots))
    for root in sorted(plan.roots):
        if root not in bodies:
            plan.notes.append(f"Склейка в №{root} ждёт: {problems.get(root, 'сигнала нет')}")
            del plan.roots[root]
            plan.merges = [(one, into) for one, into in plan.merges if into != root]
        else:
            plan.cards[root] = bodies[root]
    for one in sorted(acting):
        body, row = bodies.get(one), journal[one]
        if body is None:
            plan.notes.append(f"Карточка №{one} не обновлена: {problems.get(one, 'сигнала нет')}")
        elif row["state"] == REMOVED:
            if row["state_package"] is not None and row["state_package_taken_at"] is None:
                plan.notes.append(f"Возврат №{one} ждёт: потребитель ещё не забрал пакет №{row['state_package']} "
                                  "со снятием")
                continue
            plan.cards[one] = body
            plan.returned.append(one)
        elif exchange_card.fingerprint(body) != row["card_fingerprint"]:
            plan.cards[one] = body
            plan.changed.append(one)


def _flips(plan: _Plan, journal: dict[int, dict[str, Any]], roots: dict[int, int]) -> None:
    """Номера, ушедшие склейкой: склеены ли они на платформе так же, как у потребителя (с учётом склеек этого пакета).
    Нет: «расклеен на платформе, у потребителя склеен»; формат расклейку не передаёт, меняется только журнал."""
    merged_into = {one: int(row["merged_into"]) for one, row in journal.items()
                   if row["state"] in (MERGED, UNMERGED) and row["merged_into"] is not None}
    merged_into.update(plan.merges)

    def consumer_root(signal_id: int) -> int | None:
        for _ in range(_CHAIN_STEPS):
            if signal_id not in merged_into:
                return signal_id
            signal_id = merged_into[signal_id]
        return None

    for one, row in sorted(journal.items()):
        if row["state"] in (MERGED, UNMERGED):
            state = MERGED if roots.get(one) is not None and roots.get(one) == consumer_root(one) else UNMERGED
            if state != row["state"]:
                plan.flips.append((one, state))


def _plan(conn, journal: dict[int, dict[str, Any]]) -> _Plan:
    plan = _Plan()
    roots = exchange_store.roots(conn, list(journal))
    inactive = _structure(conn, plan, journal, roots)
    _cards(conn, plan, journal, roots, inactive)
    _flips(plan, journal, roots)
    return plan


def _details(plan: _Plan, journal: dict[int, dict[str, Any]]) -> dict[str, Any]:
    return {
        "changed": plan.changed, "returned": plan.returned, "roots": sorted(plan.roots),
        "merges": [{"signal_id": one, "merged_into": into} for one, into in plan.merges],
        "removals": [{"signal_id": one, "reason": reason} for one, reason in sorted(plan.removals.items())],
        "unmerged": [{"signal_id": one, "merged_into": journal[one]["merged_into"]}
                     for one, state in plan.flips if state == UNMERGED],
        "waiting": plan.notes,
    }


def build_update(*, by: str | None, trigger: str, scheduled_since: datetime | None = None) -> Update:
    """Собрать и выложить обновление, если на платформе что-то изменилось; запуск записывается в любом случае.
    scheduled_since: запуск шага, и шаг с этого момента уже был: ничего не делать. Ключей нет: Refused."""
    keys = exchange_packages._keys()
    with connection.get_connection() as conn:
        exchange_store.lock(conn)
        if scheduled_since is not None and exchange_store.scheduled_update_since(conn, scheduled_since):
            return Update(built=None, message="обновление сегодня уже было")
        journal = exchange_store.journal_rows(conn)
        plan = _plan(conn, journal)
        moment = exchange_store.now(conn)
        details = _details(plan, journal)
        exchange_store.set_states(conn, [(one, state, None) for one, state in plan.flips], package=None,
                                  moment=moment)
        if plan.empty():
            note = "; ".join([NOTHING.lower(), *plan.notes])
            exchange_store.record_update_run(conn, trigger=trigger, package=None, author=by, note=note)
            conn.commit()
            return Update(built=None, notes=plan.notes, message=NOTHING)
        number = exchange_store.next_number(conn)
        content = {
            "number": number, "kind": UPDATE,
            "cards": [exchange_card.stamped(plan.cards[one], moment) for one in sorted(plan.cards)],
            "merges": details["merges"], "removals": sorted(plan.removals),
        }
        envelope = exchange_envelope.seal(json.dumps(content, ensure_ascii=False).encode("utf-8"), *keys)
        exchange_store.insert_package(conn, number=number, kind=UPDATE, month=None,
                                      fingerprint=exchange_card.fingerprint(content), envelope=envelope,
                                      signal_ids=sorted(plan.cards), details=details, built_by=by)
        prints = {one: exchange_card.fingerprint(body) for one, body in plan.cards.items()}
        exchange_store.set_cards(conn, [(one, prints[one]) for one in sorted(plan.cards) if one not in plan.roots],
                                 package=number, moment=moment, returned=plan.returned)
        exchange_store.add_roots(conn, [(root, month, prints[root]) for root, month in sorted(plan.roots.items())],
                                 package=number, moment=moment)
        exchange_store.set_states(conn, [(one, MERGED, into) for one, into in plan.merges]
                                  + [(one, REMOVED, None) for one in sorted(plan.removals)], package=number,
                                  moment=moment)
        built = exchange_packages.Built(number=number, kind=UPDATE, month=None, signal_ids=sorted(plan.cards),
                                        details=details)
        exchange_store.record_update_run(conn, trigger=trigger, package=number, author=by,
                                         note="; ".join([built.describe(), *plan.notes]))
        conn.commit()
    return Update(built=built, notes=plan.notes, message=built.describe())


def update_step(*, now: datetime | None = None) -> Update | None:
    """Шаг планировщика: обновление раз в сутки по Москве, за флагом EXCHANGE_ENABLED. Флаг выключен: None."""
    if not config.EXCHANGE_ENABLED:
        return None
    moment = (now or _now()).astimezone(MSK)
    return build_update(by="планировщик", trigger=SCHEDULE, scheduled_since=datetime.combine(moment.date(), time(),
                                                                                             MSK))


def state_label(row: dict[str, Any]) -> str:
    """Состояние отправленного сигнала у потребителя словами для журнала обмена."""
    if row["state"] == REMOVED:
        return f"снят пакетом №{row['state_package']}"
    if row["state"] == MERGED:
        return f"ушёл склейкой в №{row['merged_into']} пакетом №{row['state_package']}"
    if row["state"] == UNMERGED:
        return f"расклеен на платформе, у потребителя склеен с №{row['merged_into']}"
    return "действует"


def journal(*, limit: int) -> dict[str, Any]:
    """Для вкладки «Журнал обмена»: последние запуски обновления и отправленные сигналы в особых состояниях."""
    with connection.get_connection() as conn:
        runs = exchange_store.update_runs(conn, limit=10)
        states = exchange_store.special_states(conn, limit=limit)
    for row in states:
        row["state_label"] = state_label(row)
    return {"updates": runs, "sent_states": states}
