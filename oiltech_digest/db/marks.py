"""Отметки людей на статьях ленты и карточках радара — одна точка чтения и записи (#111).

Отметка — статус, который человек ставит статье («в дайджест», «шум», «дубль», «архив») или
карточке радара (то же, вместо «новая» — «наблюдать»), и комментарий к нему. Все запросы,
которые читают или пишут отметку, берут её отсюда: SQL-обрывки для выборок и функции записи.
"""

from __future__ import annotations

from oiltech_digest.db import connection
from oiltech_digest.feed_window import signal_month_sql

ARTICLE_DEFAULT_STATUS = "new"
SIGNAL_DEFAULT_STATUS = "watch"


def _statuses(statuses: tuple[str, ...]) -> str:
    return ", ".join(f"'{status}'" for status in statuses)


# --- Статьи ---------------------------------------------------------------------------------

def article_mark_join(user_param: str, article_alias: str = "a", mark_alias: str = "uas") -> str:
    """LEFT JOIN отметки статьи для выборок с колонкой статуса (лента, счётчики, архив)."""
    return (
        f"LEFT JOIN user_article_states {mark_alias}"
        f" ON {mark_alias}.article_id = {article_alias}.id AND {mark_alias}.user_id = {user_param}"
    )


def article_status_sql(mark_alias: str = "uas") -> str:
    """Статус статьи из приджойненной отметки; нет отметки — «новая»."""
    return f"COALESCE({mark_alias}.status, '{ARTICLE_DEFAULT_STATUS}')"


def article_marked_sql(article_ref: str, *statuses: str) -> str:
    """У статьи стоит одна из этих отметок (защиты, качество источников, обучение)."""
    return (
        f"EXISTS (SELECT 1 FROM user_article_states m_a"
        f" WHERE m_a.article_id = {article_ref} AND m_a.status IN ({_statuses(statuses)}))"
    )


def get_article_status(user_id: int, article_id: int) -> str | None:
    """Текущая отметка статьи, или None — её никто не ставил.

    Нужна, чтобы записать в журнал ПРЕЖНЕЕ значение: после записи его уже не достать."""
    with connection.get_connection() as conn:
        row = conn.execute(
            "SELECT status FROM user_article_states WHERE user_id = %s AND article_id = %s",
            (user_id, article_id),
        ).fetchone()
        return row[0] if row else None


def set_article_mark(user_id: int, article_id: int, *, status: str | None = None,
                     analyst_comment: str | None = None) -> None:
    """Поставить отметку статье. status=None — не трогаем (только комментарий)."""
    with connection.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_article_states (user_id, article_id, status, analyst_comment)
            VALUES (%s, %s, COALESCE(%s, 'new'), %s)
            ON CONFLICT (user_id, article_id) DO UPDATE SET
              status = COALESCE(%s, user_article_states.status),
              analyst_comment = COALESCE(%s, user_article_states.analyst_comment),
              updated_at = now()
            """,
            (user_id, article_id, status, analyst_comment, status, analyst_comment),
        )
        conn.commit()


# --- Карточки радара ------------------------------------------------------------------------

def signal_mark_join(user_param: str, signal_alias: str = "s", mark_alias: str = "uss") -> str:
    """LEFT JOIN отметки карточки радара для выборок с колонкой статуса (список, сводка)."""
    return (
        f"LEFT JOIN user_signal_states {mark_alias}"
        f" ON {mark_alias}.signal_id = {signal_alias}.id AND {mark_alias}.user_id = {user_param}"
    )


def signal_status_sql(mark_alias: str = "uss") -> str:
    """Статус карточки из приджойненной отметки; нет отметки — «наблюдать»."""
    return f"COALESCE({mark_alias}.status, '{SIGNAL_DEFAULT_STATUS}')"


def signal_marked_sql(signal_ref: str, *statuses: str) -> str:
    """У карточки стоит одна из этих отметок."""
    return (
        f"EXISTS (SELECT 1 FROM user_signal_states m_s"
        f" WHERE m_s.signal_id = {signal_ref} AND m_s.status IN ({_statuses(statuses)}))"
    )


def signal_marked_other_than_watch_sql(signal_ref: str) -> str:
    """У карточки стоит отметка, отличная от «наблюдать» (разобранная карточка)."""
    return (
        f"EXISTS (SELECT 1 FROM user_signal_states m_s"
        f" WHERE m_s.signal_id = {signal_ref} AND m_s.status <> '{SIGNAL_DEFAULT_STATUS}')"
    )


def signal_digest_state(user_id: int, signal_id: int) -> tuple[str, str | None] | None:
    """Месяц выпуска карточки радара («ГГГГ-ММ», feed_window.signal_month_sql) и её отметка
    (None — не ставили); None — карточки нет.

    Для отказа отметке «в дайджест» у карточки закрытого месяца (PATCH /api/signals/{id}):
    отказ зависит и от месяца, и от того, выбрана ли карточка сейчас — снятие отметки другим
    статусом тоже вывело бы её из выпуска закрытого месяца."""
    with connection.get_connection() as conn:
        row = conn.execute(
            f"""
            SELECT {signal_month_sql('s')}, uss.status
            FROM signals s
            {signal_mark_join('%s')}
            WHERE s.id = %s
            """,
            (user_id, signal_id),
        ).fetchone()
    return (row[0], row[1]) if row else None


def set_signal_mark(user_id: int, signal_id: int, *, status: str | None = None,
                    analyst_comment: str | None = None) -> None:
    """Поставить отметку карточке радара; KeyError — карточки нет."""
    if status is None and analyst_comment is None:
        return
    with connection.get_connection() as conn:
        exists = conn.execute("SELECT 1 FROM signals WHERE id = %s", (signal_id,)).fetchone()
        if not exists:
            raise KeyError(f"Signal not found: {signal_id}")
        conn.execute(
            """
            INSERT INTO user_signal_states (user_id, signal_id, status, analyst_comment)
            VALUES (%s, %s, COALESCE(%s, 'watch'), %s)
            ON CONFLICT (user_id, signal_id) DO UPDATE SET
              status = COALESCE(%s, user_signal_states.status),
              analyst_comment = COALESCE(%s, user_signal_states.analyst_comment),
              updated_at = now()
            """,
            (user_id, signal_id, status, analyst_comment, status, analyst_comment),
        )
        conn.commit()
