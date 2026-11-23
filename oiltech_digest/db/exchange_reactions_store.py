"""Хранение откликов потребителя выпуска (#200, спека #197): пакеты откликов и сами отклики, своей таблицей, отдельно от
журнала отметок ОС (signal_feedback_events): у журнала много читателей (блоки «Оценки людей», фильтр «Есть ОС», защита от
перепечаток), и анонимное нажатие там меняло бы поведение платформы.

Отклик хранится с номером сигнала, как пришёл: после склейки у потребителя новые отклики идут под номером корня, а уже
выданные остались под номером дубля, поэтому счёт идёт по корню склейки на момент чтения (exchange_store.roots).
Соединение модуль получает от вызывающего.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from psycopg.rows import dict_row

from oiltech_digest.db import exchange_store

# Ключ pg_advisory_xact_lock приёма пакетов откликов: два одинаковых пакета одновременно не двоят отклики.
_LOCK = 20_011_200


def lock(conn) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK,))


def package(conn, number: int) -> dict[str, Any] | None:
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT * FROM exchange_reaction_packages WHERE number = %s", (int(number),)).fetchone()


def repeat(conn, number: int) -> None:
    """Повтор пакета с тем же содержимым: только когда пришёл и сколько раз."""
    conn.execute("UPDATE exchange_reaction_packages SET last_received_at = now(), repeats = repeats + 1"
                 " WHERE number = %s", (int(number),))


def unsent(conn, signal_ids: Sequence[int]) -> set[int]:
    """Номера, которые потребителю не отправлялись (их нет в журнале отправленного)."""
    ids = sorted({int(one) for one in signal_ids})
    if not ids:
        return set()
    known = conn.execute("SELECT signal_id FROM exchange_sent_signals WHERE signal_id = ANY(%s)", (ids,)).fetchall()
    return set(ids) - {int(row[0]) for row in known}


def insert(conn, *, number: int, fingerprint: str, source: str, signer: str, answer: str,
           reactions: Sequence[dict[str, Any]]) -> None:
    conn.execute(
        "INSERT INTO exchange_reaction_packages (number, content_fingerprint, source, signer, reactions, answer)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (int(number), fingerprint, source, signer, len(reactions), answer),
    )
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO exchange_reactions (package_number, position, signal_id, reaction, placed, day, source)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [(int(number), position, int(item["signal_id"]), item["reaction"], bool(item["placed"]),
              date.fromisoformat(item["day"]), source) for position, item in enumerate(reactions)],
        )


def packages(conn, *, limit: int) -> list[dict[str, Any]]:
    """Пакеты откликов для журнала обмена, новые сверху."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT number, source, signer, reactions, answer, received_at, last_received_at, repeats"
        " FROM exchange_reaction_packages ORDER BY number DESC LIMIT %s", (int(limit),)).fetchall()


def net_by_root(conn, *, until: date | None = None) -> dict[int, dict[str, int]]:
    """Счёт откликов по корню склейки на момент чтения: поставлено минус снято по каждому виду (до дня until, не
    включая). Корень берётся по нынешним склейкам платформы: отклики дубля до склейки и корня после неё сходятся."""
    rows = conn.execute(
        "SELECT signal_id, reaction, sum(CASE WHEN placed THEN 1 ELSE -1 END) FROM exchange_reactions"
        + (" WHERE day < %s" if until else "") + " GROUP BY signal_id, reaction",
        (until,) if until else (),
    ).fetchall()
    roots = exchange_store.roots(conn, [row[0] for row in rows])
    totals: dict[int, dict[str, int]] = {}
    for signal_id, reaction, net in rows:
        root = roots.get(int(signal_id), int(signal_id))
        kinds = totals.setdefault(root, {})
        kinds[reaction] = kinds.get(reaction, 0) + int(net)
    return totals


def roots_reacted(conn, since: date, until: date) -> set[int]:
    """Корни склейки сигналов, у которых были отклики в дни [since, until)."""
    rows = conn.execute("SELECT DISTINCT signal_id FROM exchange_reactions WHERE day >= %s AND day < %s",
                        (since, until)).fetchall()
    roots = exchange_store.roots(conn, [row[0] for row in rows])
    return {roots.get(int(row[0]), int(row[0])) for row in rows}


def accepted_between(conn, kinds: Sequence[str], since: datetime, until: datetime) -> list[dict[str, Any]]:
    """Отклики названных видов, принятые в окне (since, until], с корнем склейки их сигнала на момент чтения: вход
    недельного среза разбора ОС."""
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT id, signal_id, reaction, placed, day, accepted_at FROM exchange_reactions"
        " WHERE reaction = ANY(%s) AND accepted_at > %s AND accepted_at <= %s ORDER BY id",
        (list(kinds), since, until),
    ).fetchall()
    roots = exchange_store.roots(conn, [row["signal_id"] for row in rows])
    for row in rows:
        row["root"] = roots.get(int(row["signal_id"]), int(row["signal_id"]))
    return rows


def by_id(conn, reaction_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    ids = sorted({int(one) for one in reaction_ids})
    if not ids:
        return {}
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT id, signal_id, reaction, placed, day, source, accepted_at FROM exchange_reactions WHERE id = ANY(%s)",
        (ids,)).fetchall()
    return {int(row["id"]): row for row in rows}
