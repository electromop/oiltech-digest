"""Отметки людей на статьях ленты и карточках радара — одна точка чтения и записи (#111).

Отметка — статус, который человек ставит статье («в дайджест», «шум», «дубль», «архив») или
карточке радара (то же, вместо «новая» — «наблюдать»), и комментарий к нему. Отметка общая
(ADR 0004): одна на статью и одна на карточку, кто бы её ни поставил; у неё хранится, кто и
когда поставил её последним. Нет строки — «новая» / «наблюдать».

Все запросы, которые читают или пишут отметку, берут её отсюда: SQL-обрывки для выборок и
функции записи. Личные таблицы `user_article_states` и `user_signal_states` — архив только для
чтения (перенос — db/marks_migration.py); приложение их не читает и не пишет.
"""

from __future__ import annotations

from oiltech_digest.db import connection
from oiltech_digest.feed_window import signal_month_sql

ARTICLE_DEFAULT_STATUS = "new"
SIGNAL_DEFAULT_STATUS = "watch"


def _statuses(statuses: tuple[str, ...]) -> str:
    return ", ".join(f"'{status}'" for status in statuses)


# --- Статьи ---------------------------------------------------------------------------------

def article_mark_join(article_alias: str = "a", mark_alias: str = "am") -> str:
    """LEFT JOIN отметки статьи для выборок с колонкой статуса (лента, счётчики, архив)."""
    return f"LEFT JOIN article_marks {mark_alias} ON {mark_alias}.article_id = {article_alias}.id"


def article_status_sql(mark_alias: str = "am") -> str:
    """Статус статьи из приджойненной отметки; нет отметки — «новая»."""
    return f"COALESCE({mark_alias}.status, '{ARTICLE_DEFAULT_STATUS}')"


def article_marked_sql(article_ref: str, *statuses: str) -> str:
    """У статьи стоит одна из этих отметок (защиты, качество источников, обучение)."""
    return (
        f"EXISTS (SELECT 1 FROM article_marks m_a"
        f" WHERE m_a.article_id = {article_ref} AND m_a.status IN ({_statuses(statuses)}))"
    )


def get_article_status(article_id: int) -> str | None:
    """Текущая отметка статьи, или None — её никто не ставил."""
    with connection.get_connection() as conn:
        row = conn.execute("SELECT status FROM article_marks WHERE article_id = %s", (article_id,)).fetchone()
        return row[0] if row else None


def set_article_mark(article_id: int, *, status: str | None = None, analyst_comment: str | None = None,
                     user_id: int | None = None) -> str | None:
    """Поставить отметку статье; status=None — не трогаем (только комментарий).

    Возвращает ПРЕЖНЮЮ отметку (None — её не было): журнал пишет прежнее и новое значение, а
    после записи прежнее уже не достать. Чтение и запись — под одной блокировкой строки: две
    одновременные отметки не запишут в журнал одно и то же «было»."""
    with connection.get_connection() as conn:
        row = conn.execute(
            "SELECT status FROM article_marks WHERE article_id = %s FOR UPDATE", (article_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO article_marks (article_id, status, analyst_comment, set_by_user_id)
            VALUES (%s, COALESCE(%s, 'new'), %s, %s)
            ON CONFLICT (article_id) DO UPDATE SET
              status = COALESCE(%s, article_marks.status),
              analyst_comment = COALESCE(%s, article_marks.analyst_comment),
              set_by_user_id = EXCLUDED.set_by_user_id,
              updated_at = now()
            """,
            (article_id, status, analyst_comment, user_id, status, analyst_comment),
        )
        conn.commit()
        return row[0] if row else None


# --- Карточки радара ------------------------------------------------------------------------

def signal_mark_join(signal_alias: str = "s", mark_alias: str = "sm") -> str:
    """LEFT JOIN отметки карточки радара для выборок с колонкой статуса (список, сводка)."""
    return f"LEFT JOIN signal_marks {mark_alias} ON {mark_alias}.signal_id = {signal_alias}.id"


def signal_status_sql(mark_alias: str = "sm") -> str:
    """Статус карточки из приджойненной отметки; нет отметки — «наблюдать»."""
    return f"COALESCE({mark_alias}.status, '{SIGNAL_DEFAULT_STATUS}')"


def signal_marked_sql(signal_ref: str, *statuses: str) -> str:
    """У карточки стоит одна из этих отметок."""
    return (
        f"EXISTS (SELECT 1 FROM signal_marks m_s"
        f" WHERE m_s.signal_id = {signal_ref} AND m_s.status IN ({_statuses(statuses)}))"
    )


def signal_marked_other_than_watch_sql(signal_ref: str) -> str:
    """У карточки стоит отметка, отличная от «наблюдать» (разобранная карточка)."""
    return (
        f"EXISTS (SELECT 1 FROM signal_marks m_s"
        f" WHERE m_s.signal_id = {signal_ref} AND m_s.status <> '{SIGNAL_DEFAULT_STATUS}')"
    )


def signal_digest_state(signal_id: int) -> tuple[str, str | None] | None:
    """Месяц выпуска карточки радара («ГГГГ-ММ», feed_window.signal_month_sql) и её отметка
    (None — не ставили); None — карточки нет.

    Для отказа отметке «в дайджест» у карточки закрытого месяца (PATCH /api/signals/{id}):
    отказ зависит и от месяца, и от того, выбрана ли карточка сейчас — снятие отметки другим
    статусом тоже вывело бы её из выпуска закрытого месяца."""
    with connection.get_connection() as conn:
        row = conn.execute(
            f"""
            SELECT {signal_month_sql('s')}, sm.status
            FROM signals s
            {signal_mark_join()}
            WHERE s.id = %s
            """,
            (signal_id,),
        ).fetchone()
    return (row[0], row[1]) if row else None


def set_signal_mark(signal_id: int, *, status: str | None = None, analyst_comment: str | None = None,
                    user_id: int | None = None) -> str | None:
    """Поставить отметку карточке радара; KeyError — карточки нет.

    Возвращает прежнюю отметку (None — её не было), как set_article_mark."""
    with connection.get_connection() as conn:
        exists = conn.execute("SELECT 1 FROM signals WHERE id = %s", (signal_id,)).fetchone()
        if not exists:
            raise KeyError(f"Signal not found: {signal_id}")
        row = conn.execute(
            "SELECT status FROM signal_marks WHERE signal_id = %s FOR UPDATE", (signal_id,),
        ).fetchone()
        if status is None and analyst_comment is None:
            return row[0] if row else None
        conn.execute(
            """
            INSERT INTO signal_marks (signal_id, status, analyst_comment, set_by_user_id)
            VALUES (%s, COALESCE(%s, 'watch'), %s, %s)
            ON CONFLICT (signal_id) DO UPDATE SET
              status = COALESCE(%s, signal_marks.status),
              analyst_comment = COALESCE(%s, signal_marks.analyst_comment),
              set_by_user_id = EXCLUDED.set_by_user_id,
              updated_at = now()
            """,
            (signal_id, status, analyst_comment, user_id, status, analyst_comment),
        )
        conn.commit()
        return row[0] if row else None
