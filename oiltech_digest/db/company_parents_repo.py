"""Список дочерних обществ «дочка → мать» в базе (таблица company_parents): чтение для экрана и фильтра, правка
с экрана «Дочерние общества». Проверки пары и разбор вставки — company_parents.py, без базы.

Проверка и запись идут одной транзакцией под блокировкой таблицы (editing): две правки одновременно не дадут
дочке двух матерей и не замкнут цикл. Чтение блокировку не ждёт.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

from oiltech_digest.db import connection

_COLUMNS = "id, child_name, parent_name, note, created_by, created_at"


def list_rows() -> list[dict[str, Any]]:
    """Строки списка для экрана: кто добавил — почтой учётки (учётку удалили — пусто). По матери, затем по
    дочке: дочки одной матери рядом."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT cp.id, cp.child_name, cp.parent_name, cp.note, cp.created_by, cp.created_at,
                   u.email AS created_by_email
            FROM company_parents cp
            LEFT JOIN users u ON u.id = cp.created_by
            ORDER BY cp.parent_name, cp.child_name, cp.id
            """
        )
        return cur.fetchall()


def pairs() -> list[tuple[str, str]]:
    """Пары (дочка, мать) как написаны, в порядке добавления: для проверки и раскрытия компании в фильтре."""
    with connection.get_connection() as conn:
        return _pairs(conn)


def delete(pair_id: int) -> bool:
    """Удалить строку списка; False — её нет. Удаление цикла и второй матери не создаёт: проверка не нужна."""
    with connection.get_connection() as conn:
        deleted = conn.execute("DELETE FROM company_parents WHERE id = %s RETURNING id", (pair_id,)).fetchone()
        conn.commit()
    return deleted is not None


class Editing:
    """Правка списка внутри editing(): пары для проверки и запись — в одной транзакции."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def pairs(self) -> list[tuple[str, str]]:
        return _pairs(self._conn)

    def add(self, rows: Sequence[dict[str, Any]], *, user_id: int | None) -> list[dict[str, Any]]:
        """Добавить строки (child_name, parent_name, note): все или, при сбое, ни одной."""
        cur = self._conn.cursor(row_factory=dict_row)
        saved = []
        for row in rows:
            cur.execute(
                f"INSERT INTO company_parents (child_name, parent_name, note, created_by) VALUES (%s, %s, %s, %s) "
                f"RETURNING {_COLUMNS}",
                (row["child_name"], row["parent_name"], row.get("note"), user_id),
            )
            saved.append(cur.fetchone())
        return saved


@contextmanager
def editing() -> Iterator[Editing]:
    """Транзакция правки списка: таблица закрыта для других правок до конца блока (SHARE ROW EXCLUSIVE: чтение
    идёт, запись ждёт). Блок закончился — запись принята; исключение (в том числе отказ API) — ничего не
    записано."""
    with connection.get_connection() as conn:
        conn.execute("LOCK TABLE company_parents IN SHARE ROW EXCLUSIVE MODE")
        yield Editing(conn)
        conn.commit()


def _pairs(conn: psycopg.Connection) -> list[tuple[str, str]]:
    rows = conn.execute("SELECT child_name, parent_name FROM company_parents ORDER BY id").fetchall()
    return [(child, parent) for child, parent in rows]
