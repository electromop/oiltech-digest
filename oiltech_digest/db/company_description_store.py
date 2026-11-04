"""Описание Компании для агента «Гипотеза для Компании» (#162, #163): таблица company_descriptions, строка на
сохранение, действует последняя. Экран «Описание Компании» в «Настройках» показывает текст, кто и когда правил, и
прежние правки; агент гипотезы (#163) берёт действующий текст при выдаче задачи.

Вносится только публичная версия описания, утверждённая заказчиком для внешней модели (ответ эксперта 03.11):
внутренняя в платформу не вносится. Текста нет ни в репозитории, ни в брендинге выпуска.
"""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row

from oiltech_digest.db import connection

# Сколько прежних правок показывает экран.
HISTORY_LIMIT = 20


def current() -> dict[str, Any]:
    """Действующее описание: текст, кто (почта учётки) и когда правил, и прежние правки (кто, когда, сколько знаков)
    от новой к старой. Описания ещё нет: пустой текст."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT d.id, d.text, d.created_at AS updated_at, u.email AS updated_by
            FROM company_descriptions d
            LEFT JOIN users u ON u.id = d.created_by
            ORDER BY d.id DESC
            LIMIT %s
            """,
            (HISTORY_LIMIT,),
        )
        rows = cur.fetchall()
    if not rows:
        return {"text": "", "updated_by": None, "updated_at": None, "history": []}
    return {
        "text": rows[0]["text"],
        "updated_by": rows[0]["updated_by"],
        "updated_at": rows[0]["updated_at"],
        "history": [{"updated_by": row["updated_by"], "updated_at": row["updated_at"], "chars": len(row["text"])}
                    for row in rows],
    }


def save(text: str, *, user_id: int) -> dict[str, Any]:
    """Новая версия описания: прежние остаются (кто и когда правил). Уже написанные гипотезы она не пересчитывает:
    в рецепте агента отпечаток описания, по которому он писал (#163)."""
    with connection.get_connection() as conn:
        conn.execute("INSERT INTO company_descriptions (text, created_by) VALUES (%s, %s)", (text, user_id))
        conn.commit()
    return current()
