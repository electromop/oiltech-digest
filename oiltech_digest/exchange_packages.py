"""Пакеты для потребителя выпуска (#198, спека #197): выпуск закрытого месяца и дозагрузка по номерам.

Пакет выпуска: состав выпуска платформы на закрытии месяца, тот же, что у PDF и выгрузки (repository.
issue_composition): статья идёт своим сигналом ленты, дубль корнем склейки; пустой месяц даёт пустой пакет; выпуск у
месяца один. Его собирает шаг планировщика (release_step) в первом цикле после закрытия месяца, когда готовы все
материалы (#199, material_readiness), за флагом EXCHANGE_ENABLED и не раньше месяца EXCHANGE_FIRST_MONTH; месяцы до
запуска собирает команда (build_release). Неготовый выпуск ждёт (NotReady: не сбой шага); «Отправить как есть»
(as_is: вкладка «Журнал обмена» и команда) собирает его сразу и пишет в журнал обмена, какие материалы были не готовы
и чего им не хватало. Статья без сигнала ленты номера карточки не имеет: выпуск ждёт, пока сигнал заведут (отказ со
списком), и «как есть» тоже.

Дозагрузка: номера и месяц выпуска; дубль переводится в корень; несуществующий или не действующий номер (правило
exchange_store.inactive_reason_sql) даёт отказ со списком, уже отправленный пропускается с пометкой.

Карточка та же, что на экране витрины (showcase_cards, exchange_card). Номер пакету даёт последовательность в базе под
замком сборки, сборка и выкладка идут одной транзакцией: пакет с большим номером не виден раньше меньшего.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import json
from typing import Any, Sequence

from oiltech_digest import config, exchange_card, exchange_envelope, feed_window, material_readiness
from oiltech_digest.db import connection, exchange_store, repository, showcase_cards

# Вид пакета словами: «пакет №1: выпуск за сентябрь 2026», «пакет №2: дозагрузка к выпуску за сентябрь 2026»,
# «пакет №3: обновление» (#200: месяца у обновления нет).
KIND_LABELS = {exchange_store.RELEASE: "выпуск за", exchange_store.ADDITION: "дозагрузка к выпуску за",
               exchange_store.UPDATE: "обновление"}


class Refused(Exception):
    """Пакет не собран: причина словами для администратора (ответ команды, вкладки и шага)."""


class NotReady(Refused):
    """Выпуск ждёт готовности материалов (#199): шаг выпуска ждёт, а не падает; команда и вкладка называют, чего не
    хватает, и как отправить выпуск как есть."""

    def __init__(self, month: str, found: Sequence[material_readiness.Material]) -> None:
        self.month = month
        self.materials = list(found)
        self.summary = (f"выпуск за {_label(month)} ждёт готовности материалов, "
                        f"{material_readiness.not_ready_text(found)}")
        super().__init__(f"{self.summary}; отправить как есть: exchange-release --month {month} --as-is или кнопкой "
                         "«Отправить как есть» во вкладке «Журнал обмена»")


@dataclass
class Built:
    number: int
    kind: str
    month: str | None
    signal_ids: list[int]
    details: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        text = f"пакет №{self.number}: {package_label(self.kind, self.month)}, карточек {len(self.signal_ids)}"
        if self.kind == exchange_store.UPDATE:
            text += (f", склеек {len(self.details.get('merges') or [])}, "
                     f"снятий {len(self.details.get('removals') or [])}")
        return text

    def report(self) -> str:
        """Ответ сборки словами: что собрано, а у выпуска «как есть» ещё что было не готово."""
        if not self.details.get("as_is"):
            return self.describe()
        lacking = self.details.get("not_ready") or []
        total = int(self.details.get("articles") or 0) + int(self.details.get("signals") or 0)
        if not lacking:
            return f"{self.describe()}; отправлен как есть, все материалы были готовы"
        return (f"{self.describe()}; отправлен как есть, " + material_readiness.lacking_text(
            [(f"№{one['signal_id']}", one["missing"]) for one in lacking], total))


def package_label(kind: str, month: str | None) -> str:
    """Вид и месяц пакета словами: «выпуск за сентябрь 2026»."""
    return f"{KIND_LABELS.get(kind, kind)} {_label(month)}" if month else KIND_LABELS.get(kind, kind)


def package_notes(details: dict[str, Any]) -> list[str]:
    """Что сборка сделала сверх состава, строками для вкладки «Журнал обмена» и команды exchange-status: дубли ушли
    корнем склейки, уже отправленные и ушедшие склейкой пропущены, выпуск отправлен как есть и что в нём было не
    готово; у обновления (#200) склейки, снятия, возвраты, оставшиеся сигналы склеек, расклейки и что ждёт."""
    notes = ([f"Дубль №{one['requested']} ушёл корнем склейки №{one['root']}" for one in details.get("moved") or ()]
             + [f"№{one['signal_id']} уже отправлен: пакет №{one['package']}, выпуск за {_label(one['month'])}"
                for one in details.get("skipped") or ()]
             + [f"№{one} не отправлен: он ушёл потребителю склейкой" for one in details.get("merged_away") or ()]
             + [f"№{one['signal_id']} ушёл склейкой в №{one['merged_into']}" for one in details.get("merges") or ()]
             + [f"№{one['signal_id']} снят: {one['reason']}" for one in details.get("removals") or ()]
             + [f"№{one} вернулся: снятие на платформе отменено" for one in details.get("returned") or ()]
             + [f"№{one} пришёл карточкой: оставшийся сигнал склейки" for one in details.get("roots") or ()]
             + [f"№{one['signal_id']} расклеен на платформе, у потребителя склеен с №{one['merged_into']}"
                for one in details.get("unmerged") or ()]
             + list(details.get("waiting") or ()))
    if details.get("as_is"):
        notes += [f"Отправлен как есть: №{one['signal_id']} не был готов ({material_readiness.labels(one['missing'])})"
                  for one in details.get("not_ready") or ()] or ["Отправлен как есть: все материалы были готовы"]
    return notes


def _moved(requested: Sequence[int], roots: dict[int, int]) -> list[dict[str, int]]:
    """Дубли среди номеров и корни их склейки: пакет называет, куда ушёл каждый."""
    return [{"requested": one, "root": roots[one]} for one in requested if one in roots and roots[one] != one]


def checked_month(value: str) -> str:
    try:
        return feed_window.month_key(feed_window.parse_month((value or "").strip()))
    except ValueError:
        raise Refused(f"месяц выпуска задаётся как ГГГГ-ММ, получено «{value}»") from None


def _label(month: str) -> str:
    return feed_window.month_label(feed_window.parse_month(month))


def sending_keys() -> tuple[bytes, bytes]:
    try:
        return exchange_envelope.sending_keys()
    except exchange_envelope.NotConfigured as error:
        raise Refused(f"пакет не собран: {error}") from None


def _publish(conn, keys: tuple[bytes, bytes], *, kind: str, month: str, signal_ids: Sequence[int], by: str | None,
             details: dict[str, Any]) -> Built:
    """Собрать и выложить пакет в транзакции conn под замком сборки (exchange_store.lock уже взят): номер, карточки
    экрана витрины, конверт, строка пакета и журнал отправленного. Коммит за вызывающим."""
    rows = showcase_cards.read(conn, signal_ids)
    previous = exchange_store.sent(conn, rows)
    # Номер, ушедший потребителю склейкой (#200), больше не отправляется: у потребителя его карточки нет.
    gone = [one for one in sorted(rows) if (previous.get(one) or {}).get("state") in (exchange_store.MERGED,
                                                                                      exchange_store.UNMERGED)]
    if gone:
        details["merged_away"] = gone
    # Сигнал, удалённый между выбором состава и чтением карточек, в пакет не идёт (карточки у него нет).
    ids = [one for one in sorted(rows) if one not in gone]
    number = exchange_store.next_number(conn)
    found = showcase_cards.publications(conn, ids)
    moment = exchange_store.now(conn)
    cards, journal = [], []
    for signal_id in ids:
        try:
            body = exchange_card.card({**rows[signal_id], "evidence": found.get(signal_id, [])})
        except exchange_card.CardError as error:
            raise Refused(f"пакет не собран: {error}") from None
        fingerprint = exchange_card.fingerprint(body)
        before = previous.get(signal_id)
        seen = before["fingerprint_seen_at"] if before and before["card_fingerprint"] == fingerprint else moment
        cards.append(exchange_card.stamped(body, seen))
        journal.append((signal_id, fingerprint, seen))
    content = {"number": number, "kind": kind, "month": month, "cards": cards}
    envelope = exchange_envelope.seal(json.dumps(content, ensure_ascii=False).encode("utf-8"), *keys)
    exchange_store.insert_package(conn, number=number, kind=kind, month=month,
                                  fingerprint=exchange_card.fingerprint(content), envelope=envelope, signal_ids=ids,
                                  details=details, built_by=by)
    exchange_store.record_sent(conn, journal, package=number, month=month)
    return Built(number=number, kind=kind, month=month, signal_ids=ids, details=details)


def build_release(month: str, *, by: str | None, as_is: bool = False) -> Built:
    """Пакет выпуска названного закрытого месяца: состав выпуска платформы, как у PDF и выгрузки. Ждёт готовности всех
    материалов (NotReady); as_is: «Отправить как есть», сразу, с тем, что есть, и с записью, что было не готово. Второй
    пакет выпуска того же месяца не собирается."""
    month = checked_month(month)
    if feed_window.current().is_open(month):
        raise Refused(f"выпуск за {_label(month)} ещё открыт: пакет собирается после закрытия месяца "
                      f"({config.FEED_ROLLOVER_DAY}-го числа следующего)")
    keys = sending_keys()
    with connection.get_connection() as conn:
        exchange_store.lock(conn)
        existing = exchange_store.release_months(conn, [month]).get(month)
        if existing is not None:
            raise Refused(f"выпуск за {_label(month)} уже собран: пакет №{existing}")
        composition = repository.issue_composition(month, conn=conn)
        articles = composition["article_ids"]
        found = material_readiness.materials(conn, articles, composition["signal_ids"])
        # Статья выпуска без сигнала ленты (собрана до 01.08.2026 или сигнал не заведён): у неё нет номера карточки.
        # Выпуск у месяца один, и потребитель его состав потом не меняет: пакет ждёт, пока сигнал заведут.
        orphans = [one.id for one in found if one.kind == "article" and one.signal_id is None]
        if orphans:
            since, until = exchange_store.collected_days(conn, orphans)
            raise Refused(
                f"выпуск за {_label(month)} не собран: у статей выпуска нет сигнала ленты (без него у карточки нет "
                f"номера): №{', №'.join(map(str, orphans))}. Завести сигналы: команда sync-feed-signals --since "
                f"{since} --until {until} --apply; шаг соберёт выпуск следующим циклом")
        lacking = [one for one in found if one.missing]
        if lacking and not as_is:
            raise NotReady(month, found)
        members = [one.signal_id for one in found]
        roots = {one.signal_id: one.root for one in found if one.root is not None}
        details: dict[str, Any] = {"articles": len(articles), "signals": len(composition["signal_ids"]),
                                   "moved": _moved(members, roots)}
        if as_is:
            details["as_is"] = True
            # Дубль и корень в выпуске сразу: одна карточка, одна строка.
            details["not_ready"] = list({one.root: {"signal_id": one.root, "missing": list(one.missing)}
                                         for one in lacking}.values())
        built = _publish(conn, keys, kind=exchange_store.RELEASE, month=month,
                         signal_ids=[roots[one] for one in members if one in roots], by=by, details=details)
        conn.commit()
    return built


def _months_between(first: str, last: date) -> list[str]:
    months, current = [], feed_window.parse_month(first)
    while current <= last:
        months.append(feed_window.month_key(current))
        current = (current + timedelta(days=32)).replace(day=1)
    return months


def pending_release_months(*, now: datetime | None = None) -> list[str]:
    """Закрытые месяцы с первого из настройки, у которых ещё нет пакета выпуска: их собирает шаг выпуска, и до сборки
    каждый цикл их материалы догоняет перепроверка готовности. Флаг выключен или первый месяц не задан: []."""
    if not config.EXCHANGE_ENABLED or not config.EXCHANGE_FIRST_MONTH:
        return []
    first = checked_month(config.EXCHANGE_FIRST_MONTH)
    last_closed = (feed_window.current(now=now).start - timedelta(days=1)).replace(day=1)
    months = _months_between(first, last_closed)
    if not months:
        return []
    with connection.get_connection() as conn:
        done = exchange_store.release_months(conn, months)
    return [month for month in months if month not in done]


@dataclass
class ReleaseStep:
    """Итог шага выпуска: собранные пакеты и месяцы, чей выпуск ждёт готовности материалов."""

    built: list[Built]
    waiting: list[NotReady]


def release_step(*, by: str = "планировщик") -> ReleaseStep | None:
    """Шаг планировщика: каждый закрытый месяц с первого из настройки, у которого нет пакета выпуска, собрать, когда
    готовы все его материалы; неготовый ждёт следующего цикла. Флаг выключен: None (шаг ничего не делает)."""
    if not config.EXCHANGE_ENABLED:
        return None
    if not config.EXCHANGE_FIRST_MONTH:
        raise Refused("не задан первый месяц шага (EXCHANGE_FIRST_MONTH, ГГГГ-ММ)")
    step = ReleaseStep(built=[], waiting=[])
    for month in pending_release_months():
        try:
            step.built.append(build_release(month, by=by))
        except NotReady as waiting:
            step.waiting.append(waiting)
        except Refused:
            # Команда стартовой загрузки успела собрать тот же месяц между чтением и сборкой: выпуск у месяца один.
            with connection.get_connection() as conn:
                if month not in exchange_store.release_months(conn, [month]):
                    raise
    return step


@dataclass
class Addition:
    built: Built | None
    skipped: list[dict[str, Any]]
    moved: list[dict[str, int]]

    def describe(self) -> list[str]:
        """Ответ дозагрузки строками: что ушло (пакет и номера), какие дубли ушли корнем, что пропущено."""
        lines = ([f"{self.built.describe()}: №{', №'.join(map(str, self.built.signal_ids))}"] if self.built
                 else ["пакет не собран: все номера уже отправлены потребителю"])
        return lines + [note[:1].lower() + note[1:] for note in package_notes({"moved": self.moved,
                                                                                "skipped": self.skipped})]


def _refusal(missing: list[int], inactive: list[tuple[int, int, str]]) -> str:
    parts = []
    if missing:
        parts.append("нет таких сигналов: " + ", ".join(map(str, missing)))
    if inactive:
        parts.append("не действуют: " + ", ".join(
            f"{requested} ({reason})" if requested == root else f"{requested} (дубль, корень {root}: {reason})"
            for requested, root, reason in inactive))
    return "дозагрузка не собрана, " + "; ".join(parts)


def build_addition(numbers: Sequence[int], month: str, *, by: str | None) -> Addition:
    """Дозагрузка номеров с месяцем выпуска: дубль корнем склейки, отказ со списком на несуществующие и не действующие
    номера, уже отправленные пропускаются. Всё уже отправлено: пакета нет."""
    month = checked_month(month)
    requested = list(dict.fromkeys(int(one) for one in numbers))
    if not requested:
        raise Refused("дозагрузка не собрана: нет номеров сигналов")
    keys = sending_keys()
    with connection.get_connection() as conn:
        exchange_store.lock(conn)
        roots = exchange_store.roots(conn, requested)
        missing = [one for one in requested if one not in roots]
        reasons = exchange_store.inactive(conn, set(roots.values()))
        inactive = [(one, roots[one], reasons[roots[one]]) for one in requested if one in roots and roots[one] in reasons]
        if missing or inactive:
            raise Refused(_refusal(missing, inactive))
        targets = list(dict.fromkeys(roots[one] for one in requested))
        already = exchange_store.sent(conn, targets)
        moved = _moved(requested, roots)
        skipped = [{"signal_id": one, "package": int(already[one]["first_package"]), "month": already[one]["month"]}
                   for one in targets if one in already]
        fresh = [one for one in targets if one not in already]
        if not fresh:
            return Addition(built=None, skipped=skipped, moved=moved)
        details = {"requested": requested, "moved": moved, "skipped": skipped}
        built = _publish(conn, keys, kind=exchange_store.ADDITION, month=month, signal_ids=fresh, by=by,
                         details=details)
        conn.commit()
    return Addition(built=built, skipped=skipped, moved=moved)


def readiness(month: str | None = None) -> dict[str, Any]:
    """Готовность выпуска месяца для вкладки «Журнал обмена» и команд (#199): сколько материалов и сколько готово,
    неготовые с тем, чего им не хватает (номер карточки ведёт на витрину), собран ли пакет выпуска и можно ли отправить
    выпуск как есть (месяц закрыт, пакета нет). Месяц по умолчанию: первый закрытый, чей выпуск ждёт шага, иначе первый
    открытый (его выпуск закрывается следующим)."""
    window = feed_window.current()
    if month:
        month = checked_month(month)
    else:
        try:
            waiting = pending_release_months()
        except Refused:
            waiting = []
        month = waiting[0] if waiting else feed_window.month_key(window.start)
    with connection.get_connection() as conn:
        found = material_readiness.month_materials(conn, month)
        release = exchange_store.release_months(conn, [month]).get(month)
        titles = showcase_cards.titles(conn, [one.root for one in found if one.missing and one.root is not None])
    closed = not window.is_open(month)
    # Статья без сигнала ленты держит выпуск и «как есть» (у неё нет номера карточки): build_release откажет.
    orphans = [one.id for one in found if one.kind == "article" and one.signal_id is None]
    # Причина готовой фразой: её как есть показывают вкладка и команда.
    reason = None
    if release is not None:
        reason = f"Выпуск за {_label(month)} уже собран: пакет №{release}."
    elif not closed:
        reason = (f"Выпуск за {_label(month)} ещё открыт: отправить его как есть можно после закрытия месяца "
                  f"({config.FEED_ROLLOVER_DAY}-го числа следующего).")
    elif orphans:
        reason = (f"У статей выпуска нет сигнала ленты, без него у карточки нет номера: №{', №'.join(map(str, orphans))}. "
                  "Завести сигналы командой sync-feed-signals.")
    lacking = material_readiness.describe(found, titles)
    return {"month": month, "closed": closed, "release": release, "total": len(found),
            "ready": len(found) - len(lacking), "not_ready": lacking,
            "as_is": {"allowed": reason is None, "reason": reason}}
