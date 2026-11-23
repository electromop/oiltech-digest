"""Отклики потребителя выпуска (#200, спека #197): приём пакета откликов в точке обмена, счёт по корню склейки на
карточке у администратора и выгрузка откликов за месяц.

Пакет откликов: файл формата версии 2 (exchange_envelope.open_package: формат и версия, подпись открытым ключом
потребителя до расшифровки, получатель, расшифровка ключом платформы), содержимое ровно по формату: номер (свой счёт
потребителя), вид reactions, метка источника и непустой список откликов (номер сигнала, вид, поставлен или снят, день по
Москве). Номер сигнала должен быть из отправленных потребителю. Первое несовпадение: отказ с причиной, откликов в базе
не прибавляется, отказ ложится в журнал обмена. Тот же номер с тем же содержимым: «уже было», с другим: 409.

Счёт по корню склейки на момент чтения: «в работе у N групп» = поставлено минус снято «В работу», и так по каждому виду.
Отклики живут отдельно от отметок людей платформы (exchange_reactions_store) и меняют карточку только через предложение
разбора ОС (отрицательные: feedback_review).
"""

from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
import re
from typing import Any, Sequence

from oiltech_digest import exchange_card, exchange_envelope, feed_window
from oiltech_digest.db import connection, exchange_reactions_store, exchange_store, showcase_cards

# Виды откликов в порядке кнопок на карточке потребителя и их подписи.
LABELS = {
    "save": "Сохранить", "work": "В работу", "export": "В выгрузку", "useful": "Полезный сигнал",
    "not_oilfield": "Не про нефтесервис", "not_new": "Не новое", "few_facts": "Мало фактов",
    "card_error": "Ошибка в карточке",
}
KINDS = tuple(LABELS)
# Отрицательные отклики: идут во вход недельного среза разбора ОС.
NEGATIVE = ("not_oilfield", "card_error", "not_new", "few_facts")
_CONTENT_FIELDS = ("number", "kind", "source", "reactions")
_REACTION_FIELDS = ("signal_id", "reaction", "placed", "day")
_SOURCE_CHARS = 200
_MAX_NUMBER = 2**63
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class Rejected(exchange_envelope.Rejected):
    """Содержимое пакета откликов не по формату."""


def _number(value: Any) -> bool:
    return type(value) is int and 0 < value < _MAX_NUMBER


def _day(value: Any) -> bool:
    if not isinstance(value, str) or not _DAY.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _reaction(item: Any, where: str) -> None:
    if not isinstance(item, dict):
        raise Rejected(f"{where}: нужен объект")
    exchange_envelope.exact_fields(item, _REACTION_FIELDS, where)
    if not _number(item["signal_id"]):
        raise Rejected(f"{where}.signal_id: нужно целое число больше нуля")
    if not isinstance(item["reaction"], str) or item["reaction"] not in LABELS:
        raise Rejected(f"{where}.reaction: неизвестный вид «{str(item['reaction'])[:40]}»")
    if type(item["placed"]) is not bool:
        raise Rejected(f"{where}.placed: нужно true или false")
    if not _day(item["day"]):
        raise Rejected(f"{where}.day: нужна дата ГГГГ-ММ-ДД")


def parse(payload: bytes, number: int) -> dict[str, Any]:
    """Содержимое пакета откликов, проверенное по формату; номер тот же, что в адресе. Не так: Rejected с причиной."""
    try:
        content = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Rejected("содержимое не читается как JSON") from None
    if not isinstance(content, dict):
        raise Rejected("содержимое не JSON-объект")
    exchange_envelope.exact_fields(content, _CONTENT_FIELDS, "содержимое")
    if not _number(content["number"]):
        raise Rejected("number: нужно целое число больше нуля")
    if content["kind"] != "reactions":
        raise Rejected(f"kind: «{str(content['kind'])[:40]}», а у пакета откликов «reactions»")
    source = content["source"]
    if not isinstance(source, str) or not source.strip() or len(source) > _SOURCE_CHARS:
        raise Rejected(f"source: нужна непустая строка до {_SOURCE_CHARS} знаков")
    items = content["reactions"]
    if not isinstance(items, list):
        raise Rejected("reactions: нужен список")
    if not items:
        raise Rejected("reactions: пустой список")
    for index, item in enumerate(items):
        _reaction(item, f"reactions[{index}]")
    if content["number"] != number:
        raise Rejected(f"номер в пакете {content['number']}, а в адресе {number}")
    return content


def _refused(conn, number: int, status: int, reason: str) -> tuple[int, dict[str, Any]]:
    detail = f"Отклонено: {reason}."
    exchange_store.record_refusal(conn, exchange_store.REFUSED_REACTIONS, number, detail)
    conn.commit()
    return status, {"detail": detail}


def accept(number: int, data: bytes) -> tuple[int, dict[str, Any]]:
    """Пакет откликов из точки обмена: (код ответа, тело). 200: принят или уже был; 409: номер занят другим
    содержимым; 422: пакет не по формату или не тот; 503: на платформе нет ключей откликов. Отказ: в журнал обмена."""
    try:
        payload, signer = exchange_envelope.open_package(data)
        content = parse(payload, number)
    except exchange_envelope.NotConfigured as problem:
        with connection.get_connection() as conn:
            return _refused(conn, number, 503, f"платформа не принимает отклики: {problem}")
    except exchange_envelope.Rejected as problem:
        with connection.get_connection() as conn:
            return _refused(conn, number, 422, str(problem))
    items = content["reactions"]
    with connection.get_connection() as conn:
        exchange_reactions_store.lock(conn)
        unsent = exchange_reactions_store.unsent(conn, [item["signal_id"] for item in items])
        if unsent:
            index = next(index for index, item in enumerate(items) if item["signal_id"] in unsent)
            return _refused(conn, number, 422, f"reactions[{index}].signal_id: сигнал {items[index]['signal_id']} "
                                               "потребителю не отправлялся")
        fingerprint = exchange_card.fingerprint(content)
        existing = exchange_reactions_store.package(conn, number)
        if existing is not None:
            if existing["content_fingerprint"] != fingerprint:
                return _refused(conn, number, 409, f"пакет откликов №{number} уже принят с другим содержимым")
            exchange_reactions_store.repeat(conn, number)
            conn.commit()
            return 200, {"number": number, "outcome": "already",
                         "message": f"Уже было: пакет откликов №{number} принят раньше."}
        answer = f"Принято: пакет откликов №{number}, откликов {len(items)}."
        exchange_reactions_store.insert(conn, number=number, fingerprint=fingerprint, source=content["source"],
                                        signer=signer, answer=answer, reactions=items)
        conn.commit()
    return 200, {"number": number, "outcome": "accepted", "message": answer}


def _positive(kinds: dict[str, int]) -> dict[str, int]:
    return {kind: kinds[kind] for kind in KINDS if kinds.get(kind, 0) > 0}


def card_counts(card_ids: Sequence[int]) -> dict[int, dict[str, int]]:
    """Счёт откликов карточек витрины по корню склейки (только у кого он есть): вид → поставлено минус снято."""
    wanted = {int(one) for one in card_ids}
    if not wanted:
        return {}
    with connection.get_connection() as conn:
        totals = exchange_reactions_store.net_by_root(conn)
    return {root: counts for root, kinds in totals.items() if root in wanted and (counts := _positive(kinds))}


def export_month(month: str, target: Path) -> int:
    """Отклики за месяц в xlsx: строка на корень склейки сигнала, у которого в этом месяце были отклики; счёт на конец
    месяца по корню (в работе у N групп и по каждому виду), месяц выпуска, с которым сигнал ушёл. Возвращает число
    строк."""
    from openpyxl import Workbook

    start = feed_window.parse_month(month)
    end = (start + timedelta(days=32)).replace(day=1)
    with connection.get_connection() as conn:
        reacted = exchange_reactions_store.roots_reacted(conn, start, end)
        totals = exchange_reactions_store.net_by_root(conn, until=end)
        titles = showcase_cards.titles(conn, reacted)
        sent = exchange_store.sent(conn, reacted)
    others = [kind for kind in KINDS if kind != "work"]
    book = Workbook()
    sheet = book.active
    sheet.title = f"Отклики {month}"
    sheet.append(["Сигнал", "Заголовок", "Месяц выпуска", "В работе у групп", *(LABELS[kind] for kind in others)])
    rows = []
    for root in reacted:
        kinds = totals.get(root, {})
        rows.append([root, (titles.get(root) or {}).get("title"), (sent.get(root) or {}).get("month"),
                     max(kinds.get("work", 0), 0), *(max(kinds.get(kind, 0), 0) for kind in others)])
    for row in sorted(rows, key=lambda one: (-one[3], one[0])):
        sheet.append(row)
    target.parent.mkdir(parents=True, exist_ok=True)
    book.save(target)
    return len(rows)


def journal(*, limit: int) -> list[dict[str, Any]]:
    """Пакеты откликов для вкладки «Журнал обмена» и команды exchange-status, новые сверху."""
    with connection.get_connection() as conn:
        return exchange_reactions_store.packages(conn, limit=limit)

