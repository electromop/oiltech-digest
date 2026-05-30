"""Подключение к PostgreSQL и инициализация схемы."""

from __future__ import annotations

from pathlib import Path

import psycopg

from oiltech_digest.config import DATABASE_URL

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_connection() -> psycopg.Connection:
    """Новое подключение к БД. Вызывающий отвечает за закрытие (или используйте `with`)."""
    return psycopg.connect(DATABASE_URL)


def init_db() -> list[str]:
    """Выполнить schema.sql (идемпотентно). Возвращает список таблиц после создания."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_connection() as conn:
        conn.execute(sql)  # schema.sql без параметров → допускается несколько команд
        conn.commit()
    return list_tables()


def list_tables() -> list[str]:
    """Список таблиц в схеме public."""
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name"
        )
        return [row[0] for row in cur.fetchall()]
