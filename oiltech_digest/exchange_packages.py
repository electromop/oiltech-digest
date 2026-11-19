"""Пакеты для потребителя выпуска (#198, спека #197): выпуск закрытого месяца и дозагрузка по номерам.

Пакет выпуска: состав выпуска платформы на закрытии месяца, тот же, что у PDF и выгрузки (repository.
issue_composition): статья идёт своим сигналом ленты, дубль корнем склейки; пустой месяц даёт пустой пакет; выпуск у
месяца один. Его собирает шаг планировщика (release_step) в первом цикле после закрытия месяца, за флагом
EXCHANGE_ENABLED и не раньше месяца EXCHANGE_FIRST_MONTH; месяцы до запуска собирает команда (build_release). Готовность
материалов здесь не проверяется (#199): сборка берёт карточки как есть.

Дозагрузка: номера и месяц выпуска; дубль переводится в корень; несуществующий или не действующий номер (правило
exchange_store.signal_active_sql) даёт отказ со списком, уже отправленный пропускается с пометкой.

Карточка та же, что на экране витрины (signal_cards, exchange_card). Номер пакету даёт последовательность в базе под
замком сборки, сборка и выкладка идут одной транзакцией: пакет с большим номером не виден раньше меньшего.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
import json
from typing import Any, Sequence

from oiltech_digest import config, exchange_card, exchange_envelope, feed_window
from oiltech_digest.api_access import consumer_networks
from oiltech_digest.db import connection, exchange_store, repository, signal_cards

# Вид пакета словами: «пакет №1: выпуск за сентябрь 2026», «пакет №2: дозагрузка к выпуску за сентябрь 2026».
KIND_LABELS = {exchange_store.RELEASE: "выпуск за", exchange_store.ADDITION: "дозагрузка к выпуску за"}


class Refused(Exception):
    """Пакет не собран: причина словами для администратора (ответ команды, вкладки и шага)."""


@dataclass
class Built:
    number: int
    kind: str
    month: str
    signal_ids: list[int]
    details: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        return f"пакет №{self.number}: {package_label(self.kind, self.month)}, карточек {len(self.signal_ids)}"


def package_label(kind: str, month: str | None) -> str:
    """Вид и месяц пакета словами: «выпуск за сентябрь 2026»."""
    return f"{KIND_LABELS.get(kind, kind)} {_label(month)}" if month else KIND_LABELS.get(kind, kind)


def _month(value: str) -> str:
    try:
        return feed_window.month_key(feed_window.parse_month((value or "").strip()))
    except ValueError:
        raise Refused(f"месяц выпуска задаётся как ГГГГ-ММ, получено «{value}»") from None


def _label(month: str) -> str:
    return feed_window.month_label(feed_window.parse_month(month))


def _keys() -> tuple[bytes, bytes]:
    try:
        return exchange_envelope.sending_keys()
    except exchange_envelope.NotConfigured as error:
        raise Refused(f"пакет не собран: {error}") from None


def _publish(conn, keys: tuple[bytes, bytes], *, kind: str, month: str, signal_ids: Sequence[int], by: str | None,
             details: dict[str, Any]) -> Built:
    """Собрать и выложить пакет в транзакции conn под замком сборки (exchange_store.lock уже взят): номер, карточки
    экрана витрины, конверт, строка пакета и журнал отправленного. Коммит за вызывающим."""
    rows = signal_cards.read(conn, signal_ids)
    # Сигнал, удалённый между выбором состава и чтением карточек, в пакет не идёт (карточки у него нет).
    ids = sorted(rows)
    number = exchange_store.next_number(conn)
    found = signal_cards.publications(conn, ids)
    moment = exchange_store.now(conn)
    previous = exchange_store.sent(conn, ids)
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


def build_release(month: str, *, by: str | None) -> Built:
    """Пакет выпуска названного закрытого месяца: состав выпуска платформы, как у PDF и выгрузки. Второй пакет
    выпуска того же месяца не собирается."""
    month = _month(month)
    if feed_window.current().is_open(month):
        raise Refused(f"выпуск за {_label(month)} ещё открыт: пакет собирается после закрытия месяца "
                      f"({config.FEED_ROLLOVER_DAY}-го числа следующего)")
    keys = _keys()
    with connection.get_connection() as conn:
        exchange_store.lock(conn)
        existing = exchange_store.release_months(conn, [month]).get(month)
        if existing is not None:
            raise Refused(f"выпуск за {_label(month)} уже собран: пакет №{existing}")
        composition = repository.issue_composition(month, conn=conn)
        articles = composition["article_ids"]
        by_article = exchange_store.feed_signals(conn, articles)
        members = [by_article[one] for one in articles if one in by_article] + composition["signal_ids"]
        roots = exchange_store.roots(conn, members)
        details = {
            "articles": len(articles), "signals": len(composition["signal_ids"]),
            # Статья выпуска без сигнала ленты (собрана до 01.08.2026 или сигнал не заведён): номера у неё нет.
            "without_signal": [one for one in articles if one not in by_article],
            "moved": [{"requested": one, "root": roots[one]} for one in members if one in roots and roots[one] != one],
        }
        built = _publish(conn, keys, kind=exchange_store.RELEASE, month=month,
                         signal_ids=[roots[one] for one in members if one in roots], by=by, details=details)
        conn.commit()
    return built


def _months(first: str, last: date) -> list[str]:
    months, current = [], feed_window.parse_month(first)
    while current <= last:
        months.append(feed_window.month_key(current))
        current = (current + timedelta(days=32)).replace(day=1)
    return months


def release_step(*, by: str = "планировщик") -> list[Built] | None:
    """Шаг планировщика: каждый закрытый месяц с первого из настройки, у которого нет пакета выпуска, собрать. Флаг
    выключен: None (шаг ничего не делает)."""
    if not config.EXCHANGE_ENABLED:
        return None
    if not config.EXCHANGE_FIRST_MONTH:
        raise Refused("не задан первый месяц шага (EXCHANGE_FIRST_MONTH, ГГГГ-ММ)")
    first = _month(config.EXCHANGE_FIRST_MONTH)
    last_closed = (feed_window.current().start - timedelta(days=1)).replace(day=1)
    months = _months(first, last_closed)
    if not months:
        return []
    with connection.get_connection() as conn:
        done = exchange_store.release_months(conn, months)
    built = []
    for month in months:
        if month in done:
            continue
        try:
            built.append(build_release(month, by=by))
        except Refused:
            # Команда стартовой загрузки успела собрать тот же месяц между чтением и сборкой: выпуск у месяца один.
            with connection.get_connection() as conn:
                if month not in exchange_store.release_months(conn, [month]):
                    raise
    return built


@dataclass
class Addition:
    built: Built | None
    skipped: list[dict[str, Any]]
    moved: list[dict[str, int]]

    def describe(self) -> list[str]:
        lines = [self.built.describe()] if self.built else ["пакет не собран: все номера уже отправлены потребителю"]
        lines += [f"дубль №{one['requested']} ушёл корнем склейки №{one['root']}" for one in self.moved]
        lines += [f"№{one['signal_id']} уже отправлен: пакет №{one['package']}, выпуск за {_label(one['month'])}"
                  for one in self.skipped]
        return lines


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
    month = _month(month)
    requested = list(dict.fromkeys(int(one) for one in numbers))
    if not requested:
        raise Refused("дозагрузка не собрана: нет номеров сигналов")
    keys = _keys()
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
        moved = [{"requested": one, "root": roots[one]} for one in requested if roots[one] != one]
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


def journal(*, limit: int = 200) -> dict[str, Any]:
    """Состояние обмена для вкладки «Журнал обмена» и команды exchange-status: настройки (без ключей и адресов, только
    отпечатки и число адресов) и пакеты, новые сверху."""
    keys = exchange_envelope.describe()
    with connection.get_connection() as conn:
        packages = exchange_store.packages(conn, limit=limit)
    return {
        "settings": {
            "enabled": bool(config.EXCHANGE_ENABLED),
            "first_month": config.EXCHANGE_FIRST_MONTH or None,
            "signing_keys": keys["signing"],
            "consumer_keys": keys["consumer"],
            "addresses": len(consumer_networks()),
            "token": bool(config.EXCHANGE_TOKEN_HASH),
            "problems": keys["problems"],
        },
        "packages": packages,
    }
