"""Хранение обмена с потребителем выпуска (#198, спека #197): пакеты для потребителя и журнал отправленного.

exchange_packages: каждый пакет со своим номером из последовательности exchange_package_numbers, видом, месяцем,
отпечатком содержимого, готовым файлом конверта, составом и тем, когда его собрали и забрали. Номер выдаётся под замком
сборки (lock), пакет пишется в той же транзакции: пакет с большим номером не виден раньше меньшего. Пакеты не удаляются
и не меняются (триггер exchange_packages_immutable), у выпуска месяц уникален.

exchange_sent_signals: журнал отправленного, строка на сигнал: первый пакет и месяц выпуска, отпечаток последней
отправленной карточки, когда платформа увидела этот отпечаток (дата обновления карточки в пакете), последний пакет,
состояние у потребителя: действует, снят, ушёл склейкой в номер, расклеен на платформе (у потребителя склеен), и пакет
и момент последней смены состояния (#200).

Квитанции потребителя лежат в полях пакета (receipt_*), отказы точки обмена (пакет откликов, квитанция) в
exchange_point_refusals: строка на номер и причину, повтор считается попыткой.

Соединение модуль получает от вызывающего (exchange_packages, exchange_api): сборка и выкладка идут одной транзакцией.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from oiltech_digest.db import showcase_cards
from oiltech_digest.db.repository import SIGNAL_ORIGIN_FEED, feed_signal_order_sql

# Ключ pg_advisory_xact_lock сборки пакетов: номер, сборка и выкладка идут под ним одной транзакцией.
_LOCK = 19_811_198
RELEASE, ADDITION, UPDATE = "release", "addition", "update"
# Состояние сигнала у потребителя (журнал отправленного): действует, снят, ушёл склейкой, расклеен на платформе.
ACTIVE, REMOVED, MERGED, UNMERGED = "active", "removed", "merged", "unmerged"
# Итоги квитанции потребителя словами: принят, уже был, отклонён.
RECEIPT_OUTCOMES = {"accepted": "принят", "already": "уже был", "rejected": "отклонён"}
# Отказ точки обмена: что не принято (пакет откликов или квитанция), причина не длиннее стольких знаков.
REFUSED_REACTIONS, REFUSED_RECEIPT = "reactions", "receipt"
_REASON_CHARS = 500
# Сколько шагов склейки пройти до корня, как у repository.resolve_signal_merge_root: склейки сводятся к корню звездой
# (feed_signals._FLATTEN_MERGED), предел лишь от петли.
_CHAIN_STEPS = 10


def inactive_reason_sql(alias: str = "s") -> str:
    """Правило «сигнал действует» у потребителя: почему не действует, словами; NULL: действует. Правило одно в коде: его
    берут дозагрузка и обновления (#200). Не действует только то, что человек или «Отбор» убрали явно: архив (в том
    числе удалённый материал коллеги), «Убрано людьми», отказ «Отбора» (в том числе после возврата «Карточкой»), статья
    сигнала ленты помечена на удаление. Правила показа, которые срабатывают сами (срок события, качество радара), смена
    категории и архив источника на него не влияют."""
    a = alias
    return (
        f"CASE WHEN {a}.archived_at IS NOT NULL THEN 'в архиве'"
        f" WHEN {a}.human_removal IS NOT NULL THEN 'убран людьми'"
        f" WHEN {a}.otbor_decision = 'reject' THEN 'отказ «Отбора»'"
        f" WHEN {a}.origin = '{SIGNAL_ORIGIN_FEED}' AND EXISTS (SELECT 1 FROM articles da"
        f" WHERE da.id = {a}.origin_article_id AND da.pending_deletion) THEN 'статья помечена на удаление' END"
    )


def lock(conn) -> None:
    """Замок сборки пакетов до конца транзакции: номера выдаются и пакеты выкладываются по одному."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK,))


def now(conn) -> datetime:
    return conn.execute("SELECT now()").fetchone()[0]


def roots(conn, signal_ids: Sequence[int]) -> dict[int, int]:
    """Корень склейки каждого сигнала (у не дубля он сам) одним запросом на все номера: то же правило, что у
    repository.resolve_signal_merge_root для одного номера. Нет такого сигнала или корня не найти: номера в ответе нет."""
    ids = sorted({int(one) for one in signal_ids})
    if not ids:
        return {}
    rows = conn.execute(
        """
        WITH RECURSIVE chain(start_id, id, merged_into, steps) AS (
          SELECT s.id, s.id, s.merged_into_signal_id, 0 FROM signals s WHERE s.id = ANY(%s)
          UNION ALL
          SELECT c.start_id, p.id, p.merged_into_signal_id, c.steps + 1
          FROM chain c JOIN signals p ON p.id = c.merged_into
          WHERE c.steps < %s
        )
        SELECT start_id, id FROM chain WHERE merged_into IS NULL
        """,
        (ids, _CHAIN_STEPS),
    ).fetchall()
    return {int(start): int(root) for start, root in rows}


def feed_signals(conn, article_ids: Sequence[int]) -> dict[int, int]:
    """Сигнал ленты каждой статьи, тем же порядком, что у читателей выпуска (repository.feed_signal_order_sql: не дубль
    раньше дубля, затем ранний); статьи без сигнала ленты в ответе нет."""
    ids = sorted({int(one) for one in article_ids})
    if not ids:
        return {}
    rows = conn.execute(
        f"""
        SELECT DISTINCT ON (s.origin_article_id) s.origin_article_id, s.id
        FROM signals s
        WHERE s.origin_article_id = ANY(%s) AND s.origin = '{SIGNAL_ORIGIN_FEED}'
        ORDER BY s.origin_article_id, {feed_signal_order_sql('s')}
        """,
        (ids,),
    ).fetchall()
    return {int(article): int(signal) for article, signal in rows}


def collected_days(conn, article_ids: Sequence[int]) -> tuple[str, str]:
    """Дни сбора статей «с» и «до, не включая» (ГГГГ-ММ-ДД, пояс сессии базы, как у sync-feed-signals)."""
    since, until = conn.execute(
        "SELECT min(collected_at)::date, max(collected_at)::date + 1 FROM articles WHERE id = ANY(%s)",
        ([int(one) for one in article_ids],),
    ).fetchone()
    return since.isoformat(), until.isoformat()


def inactive(conn, signal_ids: Sequence[int]) -> dict[int, str]:
    """Сигналы, которые не действуют у потребителя, с причиной."""
    ids = sorted({int(one) for one in signal_ids})
    if not ids:
        return {}
    rows = conn.execute(
        f"SELECT s.id, {inactive_reason_sql('s')} FROM signals s WHERE s.id = ANY(%s)", (ids,)).fetchall()
    return {int(signal_id): reason for signal_id, reason in rows if reason}


def release_months(conn, months: Sequence[str]) -> dict[str, int]:
    """Месяцы, у которых пакет выпуска уже собран, и его номер."""
    rows = conn.execute("SELECT month, number FROM exchange_packages WHERE kind = %s AND month = ANY(%s)",
                        (RELEASE, list(months))).fetchall()
    return {str(month): int(number) for month, number in rows}


def sent(conn, signal_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """Строки журнала отправленного по сигналам, которые уже уходили потребителю."""
    ids = sorted({int(one) for one in signal_ids})
    if not ids:
        return {}
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT * FROM exchange_sent_signals WHERE signal_id = ANY(%s)", (ids,)).fetchall()
    return {int(row["signal_id"]): row for row in rows}


def next_number(conn) -> int:
    return int(conn.execute("SELECT nextval('exchange_package_numbers')").fetchone()[0])


def insert_package(conn, *, number: int, kind: str, month: str | None, fingerprint: str, envelope: bytes,
                   signal_ids: Sequence[int], details: dict[str, Any], built_by: str | None) -> None:
    conn.execute(
        """
        INSERT INTO exchange_packages (number, kind, month, content_fingerprint, envelope, signal_ids, details, built_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (number, kind, month, fingerprint, envelope, [int(one) for one in signal_ids], Json(details), built_by),
    )


def record_sent(conn, cards: Sequence[tuple[int, str, datetime]], *, package: int, month: str) -> None:
    """Журнал отправленного после пакета: новый сигнал получает строку с первым пакетом и месяцем выпуска; у ушедшего
    раньше меняются отпечаток и момент, когда его увидели (если карточка изменилась), и последний пакет."""
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO exchange_sent_signals (signal_id, first_package, last_package, month, card_fingerprint,
                                               fingerprint_seen_at, state)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (signal_id) DO UPDATE SET
              last_package = EXCLUDED.last_package,
              card_fingerprint = EXCLUDED.card_fingerprint,
              fingerprint_seen_at = CASE
                WHEN exchange_sent_signals.card_fingerprint = EXCLUDED.card_fingerprint
                THEN exchange_sent_signals.fingerprint_seen_at ELSE EXCLUDED.fingerprint_seen_at END,
              updated_at = now()
            """,
            [(int(signal_id), package, package, month, fingerprint, seen_at, ACTIVE)
             for signal_id, fingerprint, seen_at in cards],
        )


def numbers(conn) -> list[int]:
    """Номера всех пакетов для потребителя по возрастанию: точка обмена держит их все."""
    return [int(row[0]) for row in conn.execute("SELECT number FROM exchange_packages ORDER BY number").fetchall()]


def take(conn, number: int) -> bytes | None:
    """Файл пакета для точки обмена; выдача отмечается (первая и последняя). Нет пакета: None."""
    row = conn.execute(
        """
        UPDATE exchange_packages
        SET first_taken_at = COALESCE(first_taken_at, now()), last_taken_at = now()
        WHERE number = %s
        RETURNING envelope
        """,
        (int(number),),
    ).fetchone()
    return bytes(row[0]) if row is not None else None


def packages(conn, *, limit: int) -> list[dict[str, Any]]:
    """Пакеты для журнала обмена, новые сверху: вид, номер, месяц, когда собран и забран (и не лежит ли он незабранным
    дольше суток), квитанция потребителя, состав с заголовками карточек (с правками людей, как на витрине) и их
    категорией (по ней экран ведёт на витрину)."""
    rows = conn.cursor(row_factory=dict_row).execute(
        """
        SELECT number, kind, month, content_fingerprint, signal_ids, details, built_by, built_at, first_taken_at,
               last_taken_at, receipt_outcome, receipt_message, receipt_at,
               (first_taken_at IS NULL AND built_at < now() - interval '1 day') AS overdue
        FROM exchange_packages ORDER BY number DESC LIMIT %s
        """,
        (int(limit),),
    ).fetchall()
    titles = showcase_cards.titles(conn, [one for row in rows for one in row["signal_ids"]])
    for row in rows:
        row["cards"] = [{"signal_id": int(one), **titles.get(int(one), {"title": None, "category": None})}
                        for one in row.pop("signal_ids")]
        outcome, message, at = row.pop("receipt_outcome"), row.pop("receipt_message"), row.pop("receipt_at")
        row["receipt"] = {"outcome": outcome, "message": message, "at": at} if outcome else None
    return rows


def record_receipt(conn, number: int, outcome: str, message: str) -> str:
    """Квитанция потребителя на пакет: recorded (первая), same (повтор с тем же содержимым: ничего не меняется), other
    (на пакет уже другая), missing (пакета нет). Пишется один раз."""
    if conn.execute(
        "UPDATE exchange_packages SET receipt_outcome = %s, receipt_message = %s, receipt_at = now()"
        " WHERE number = %s AND receipt_outcome IS NULL RETURNING number",
        (outcome, message, int(number)),
    ).fetchone() is not None:
        return "recorded"
    row = conn.execute("SELECT receipt_outcome, receipt_message FROM exchange_packages WHERE number = %s",
                       (int(number),)).fetchone()
    if row is None:
        return "missing"
    return "same" if (row[0], row[1]) == (outcome, message) else "other"


def receipt_outcome(conn, number: int) -> str | None:
    row = conn.execute("SELECT receipt_outcome FROM exchange_packages WHERE number = %s", (int(number),)).fetchone()
    return row[0] if row else None


def record_refusal(conn, kind: str, number: int, reason: str) -> None:
    """Отказ точки обмена для журнала: тот же отказ того же номера считается ещё одной попыткой."""
    conn.execute(
        """
        INSERT INTO exchange_point_refusals (kind, number, reason) VALUES (%s, %s, %s)
        ON CONFLICT (kind, number, reason) DO UPDATE SET attempts = exchange_point_refusals.attempts + 1,
          last_at = now()
        """,
        (kind, int(number), reason[:_REASON_CHARS]),
    )


def refusals(conn, *, limit: int) -> list[dict[str, Any]]:
    """Отказы точки обмена, последние сверху: что не принято, номер, причина, попыток, первая и последняя."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT kind, number, reason, attempts, first_at, last_at FROM exchange_point_refusals"
        " ORDER BY last_at DESC, id DESC LIMIT %s",
        (int(limit),),
    ).fetchall()


# --- Обновление (#200) -------------------------------------------------------------------------------------------------


def journal_rows(conn) -> dict[int, dict[str, Any]]:
    """Весь журнал отправленного: что у потребителя сейчас (действует, снят, ушёл склейкой, расклеен), отпечаток
    последней отправленной карточки и пакет последней смены состояния с отметкой, забрал ли его потребитель."""
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT ss.*, p.first_taken_at AS state_package_taken_at FROM exchange_sent_signals ss"
        " LEFT JOIN exchange_packages p ON p.number = ss.state_package").fetchall()
    return {int(row["signal_id"]): row for row in rows}


def set_cards(conn, cards: Sequence[tuple[int, str]], *, package: int, moment: datetime, returned: Sequence[int]) -> None:
    """Карточки обновления в журнале: новый отпечаток и момент, когда его увидели, последний пакет; вернувшиеся
    снова действуют (пакет и момент возврата)."""
    back = {int(one) for one in returned}
    with conn.cursor() as cur:
        cur.executemany(
            """
            UPDATE exchange_sent_signals SET card_fingerprint = %s, fingerprint_seen_at = %s, last_package = %s,
              state = %s, state_package = CASE WHEN %s THEN %s ELSE state_package END,
              state_at = CASE WHEN %s THEN %s ELSE state_at END, updated_at = now()
            WHERE signal_id = %s
            """,
            [(fingerprint, moment, package, ACTIVE, signal_id in back, package, signal_id in back, moment, signal_id)
             for signal_id, fingerprint in cards],
        )


def add_roots(conn, roots: Sequence[tuple[int, str, str]], *, package: int, moment: datetime) -> None:
    """Оставшиеся сигналы склейки, пришедшие потребителю карточкой обновления: месяц выпуска склеенной карточки."""
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO exchange_sent_signals (signal_id, first_package, last_package, month, card_fingerprint,"
            " fingerprint_seen_at, state) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [(int(root), package, package, month, fingerprint, moment, ACTIVE) for root, month, fingerprint in roots],
        )


def set_states(conn, states: Sequence[tuple[int, str, int | None]], *, package: int | None,
               moment: datetime) -> None:
    """Смена состояния у потребителя: снят, ушёл склейкой (в merged_into), расклеен на платформе. С пакетом: он же
    последний пакет сигнала; без пакета (расклейка, её формат не передаёт) меняется только состояние."""
    with conn.cursor() as cur:
        cur.executemany(
            """
            UPDATE exchange_sent_signals SET state = %s, merged_into = COALESCE(%s, merged_into),
              state_package = COALESCE(%s, state_package), last_package = COALESCE(%s, last_package),
              state_at = %s, updated_at = now()
            WHERE signal_id = %s
            """,
            [(state, merged_into, package, package, moment, int(signal_id)) for signal_id, state, merged_into in states],
        )


def scheduled_update_since(conn, moment: datetime) -> bool:
    """Был ли запуск шага обновления с этого момента: шаг идёт раз в сутки."""
    return conn.execute("SELECT 1 FROM exchange_update_runs WHERE trigger = 'schedule' AND started_at >= %s LIMIT 1",
                        (moment,)).fetchone() is not None


def record_update_run(conn, *, trigger: str, package: int | None, author: str | None, note: str) -> None:
    conn.execute("INSERT INTO exchange_update_runs (trigger, package_number, author, note) VALUES (%s, %s, %s, %s)",
                 (trigger, package, author, note))


def update_runs(conn, *, limit: int) -> list[dict[str, Any]]:
    """Последние запуски обновления, новые сверху: как запущен, кем, пакет и итог словами."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT trigger, package_number, author, note, started_at FROM exchange_update_runs"
        " ORDER BY started_at DESC, id DESC LIMIT %s", (int(limit),)).fetchall()


def special_states(conn, *, limit: int) -> list[dict[str, Any]]:
    """Отправленные сигналы, которые у потребителя не действуют как прежде: сняты, ушли склейкой, расклеены на
    платформе; с заголовками карточек, как на витрине, и пакетом последней смены состояния."""
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT signal_id, state, merged_into, state_package, state_at FROM exchange_sent_signals"
        " WHERE state <> %s ORDER BY state_at DESC NULLS LAST, signal_id DESC LIMIT %s", (ACTIVE, int(limit)),
    ).fetchall()
    titles = showcase_cards.titles(conn, [row["signal_id"] for row in rows])
    for row in rows:
        row.update(titles.get(int(row["signal_id"]), {"title": None, "category": None}))
    return rows
