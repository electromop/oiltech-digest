"""Отметки людей на статьях ленты и карточках радара — одна точка чтения и записи (#111).

Отметка — статус, который человек ставит статье («в дайджест», «шум», «дубль», «архив») или
карточке радара (то же, вместо «новая» — «наблюдать»), и комментарий к нему. Отметка общая
(ADR 0004): одна на статью и одна на карточку, кто бы её ни поставил; у неё хранится, кто и
когда поставил её последним. Нет строки — «новая» / «наблюдать».

Все запросы, которые читают или пишут отметку, берут её отсюда: SQL-обрывки для выборок и
функции записи; мимо них пишет только разовый перенос (db/marks_migration.py). Личные таблицы
`user_article_states` и `user_signal_states` — архив только для чтения: их читают перенос и
статистика «кто сколько пометил» (repository.monthly_user_activity) — после переноса она видит
только прежние месяцы, новые — по журналу (#112).
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


def set_article_mark(article_id: int, *, status: str | None = None, analyst_comment: str | None = None,
                     user_id: int | None = None) -> str | None:
    """Поставить отметку статье; status=None — не трогаем (только комментарий). Возвращает
    ПРЕЖНЮЮ отметку (None — её не было): журнал пишет прежнее и новое значение."""
    return _set_mark("article_marks", "article_id", ARTICLE_DEFAULT_STATUS, article_id,
                     status=status, analyst_comment=analyst_comment, user_id=user_id)


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
    """Поставить отметку карточке радара, как set_article_mark; KeyError — карточки нет."""
    with connection.get_connection() as conn:
        if not conn.execute("SELECT 1 FROM signals WHERE id = %s", (signal_id,)).fetchone():
            raise KeyError(f"Signal not found: {signal_id}")
    return _set_mark("signal_marks", "signal_id", SIGNAL_DEFAULT_STATUS, signal_id,
                     status=status, analyst_comment=analyst_comment, user_id=user_id)


def _set_mark(table: str, key: str, default_status: str, row_id: int, *, status: str | None,
              analyst_comment: str | None, user_id: int | None) -> str | None:
    """Запись отметки и чтение прежней — одной транзакцией. Пустой запрос ничего не пишет.

    Прежнее значение читается под блокировкой строки, а строки до первой отметки нет: сначала
    вставляем строку по умолчанию (ON CONFLICT DO NOTHING). Одновременная первая отметка ждёт на
    уникальном ключе, пока первая не закончит, и читает уже её значение — две первые отметки не
    запишут в журнал одно и то же «было». Последний поставивший и время — того, кто менял статус:
    комментарий отметку не ставит. `table`, `key` — имена из этого модуля, не ввод."""
    with connection.get_connection() as conn:
        if status is None and analyst_comment is None:
            row = conn.execute(f"SELECT status FROM {table} WHERE {key} = %s", (row_id,)).fetchone()
            return row[0] if row else None
        created = conn.execute(
            f"INSERT INTO {table} ({key}, status) VALUES (%s, %s) ON CONFLICT ({key}) DO NOTHING",
            (row_id, default_status),
        ).rowcount == 1
        previous = None if created else conn.execute(
            f"SELECT status FROM {table} WHERE {key} = %s FOR UPDATE", (row_id,),
        ).fetchone()[0]
        conn.execute(
            f"""
            UPDATE {table} SET
              status = COALESCE(%(status)s::text, status),
              analyst_comment = COALESCE(%(comment)s::text, analyst_comment),
              set_by_user_id = CASE WHEN %(status)s::text IS NULL THEN set_by_user_id ELSE %(user_id)s::bigint END,
              updated_at = CASE WHEN %(status)s::text IS NULL THEN updated_at ELSE now() END
            WHERE {key} = %(id)s
            """,
            {"status": status, "comment": analyst_comment, "user_id": user_id, "id": row_id},
        )
        conn.commit()
        return previous
