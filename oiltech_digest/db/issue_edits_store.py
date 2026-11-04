"""Правки человека в выпуске (#162, ADR 0006): поля материала на сигнале и «Главное в выпуске» месяца.

Поля материала живут на его сигнале (у статьи ленты на её сигнале ленты): подпись выпуска, «Гипотеза для Компании» и
выбор картинки. Заголовок и суть правятся записью журнала ОС (repository.record_signal_feedback_event), как у находок
радара. «Главное» хранится строкой на раздел месяца (таблица issue_highlights), не в черновике выпуска: он нужен только письму
и DOCX. Читает поля сборщик выпуска (repository.issue_material_fields, processing.digest_materials).
"""

from __future__ import annotations

from psycopg.rows import dict_row
from psycopg.types.json import Json

from oiltech_digest.db import connection

# Выбор картинки материала: своя картинка публикации или заглушка темы (её печать рисует и при своей картинке).
PICTURE_OWN, PICTURE_PLACEHOLDER = "own", "placeholder"
PICTURES = (PICTURE_OWN, PICTURE_PLACEHOLDER)
# Кто поставил гипотезу (card_set_by.hypothesis): человек; агент гипотезы (#163) пишет свою отметку.
SET_BY_HUMAN = "human"


def set_material(signal_id: int, *, label: str | None = None, hypothesis: str | None = None,
                 picture: str | None = None, author: str | None = None) -> None:
    """Поля материала выпуска на сигнале. None: поле не трогается; пустая подпись: снова подпись по типу события;
    пустая гипотеза: гипотезы нет, но это решение человека (агент её не напишет заново). Гипотеза человека пишется с
    отметкой «человек» там же, где у прочих полей карточки (card_set_by), его почтой и временем: почта остаётся и у
    удалённой учётки, как автор блока ОС. picture: PICTURE_OWN или PICTURE_PLACEHOLDER."""
    assignments: list[str] = []
    params: list = []
    if label is not None:
        assignments.append("issue_label = NULLIF(%s, '')")
        params.append(label)
    if hypothesis is not None:
        assignments.append("hypothesis = NULLIF(%s, ''), hypothesis_set_at = now(), hypothesis_author = %s,"
                           " card_set_by = COALESCE(card_set_by, '{}'::jsonb) || %s::jsonb")
        params.extend([hypothesis, author, Json({"hypothesis": SET_BY_HUMAN})])
    if picture is not None:
        assignments.append("issue_picture = %s")
        params.append(PICTURE_PLACEHOLDER if picture == PICTURE_PLACEHOLDER else None)
    if not assignments:
        return
    with connection.get_connection() as conn:
        conn.execute(f"UPDATE signals SET {', '.join(assignments)}, updated_at = now() WHERE id = %s",
                     (*params, int(signal_id)))
        conn.commit()


def highlights(month: str) -> dict[str, dict]:
    """«Главное в выпуске», написанное человеком: строка на раздел месяца по ключу раздела, кто правил почтой."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT h.section, h.text, h.updated_at, u.email AS updated_by_email
            FROM issue_highlights h
            LEFT JOIN users u ON u.id = h.updated_by
            WHERE h.month = %s
            """,
            (month,),
        )
        return {row["section"]: row for row in cur.fetchall()}


def save_highlight(month: str, section: str, text: str, *, user_id: int) -> None:
    """Строка «Главного» раздела месяца; пустая стирает написанное, и обложка снова берёт перечень рубрик."""
    with connection.get_connection() as conn:
        if text:
            conn.execute(
                """
                INSERT INTO issue_highlights (month, section, text, updated_by) VALUES (%s, %s, %s, %s)
                ON CONFLICT (month, section) DO UPDATE SET
                  text = EXCLUDED.text, updated_by = EXCLUDED.updated_by, updated_at = now()
                """,
                (month, section, text, user_id),
            )
        else:
            conn.execute("DELETE FROM issue_highlights WHERE month = %s AND section = %s", (month, section))
        conn.commit()
