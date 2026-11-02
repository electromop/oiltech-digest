"""Правило повторов на записи текста статьи (#140): с чем сверять и что отрезать.

Правило стоит на общем пути записи (insert_article и update_article_full_text), а не в одном парсере:
страж на одном пути из шести порчу не останавливает (урок #52). Статьи пишутся на ядре, поэтому
внешний сбор с NL идёт тем же путём.
"""

from __future__ import annotations

from datetime import datetime

from oiltech_digest import config
from oiltech_digest.ingestion import article_tails

# Повтор ищется среди статей источника за 30 дней от статьи. Ближайшие по времени: у EnergyLand за
# месяц полторы тысячи статей, а список соседних новостей живёт несколько дней.
REPEAT_WINDOW_DAYS = 30
NEIGHBOURS_LIMIT = 200

# Сначала ближайшие строки, потом их тексты: иначе база разворачивала бы тексты всего окна ради сортировки.
_NEIGHBOURS_SQL = """
    SELECT title, right(raw_text || COALESCE(text_tail, ''), %(window)s)
    FROM articles
    WHERE id IN (
        SELECT id FROM articles
        WHERE source_id = %(source_id)s
          AND raw_text IS NOT NULL
          AND (%(article_id)s::bigint IS NULL OR id <> %(article_id)s::bigint)
          AND collected_at BETWEEN COALESCE(%(around)s::timestamptz, now()) - make_interval(days => %(days)s)
                               AND COALESCE(%(around)s::timestamptz, now()) + make_interval(days => %(days)s)
        ORDER BY abs(extract(epoch FROM collected_at - COALESCE(%(around)s::timestamptz, now())))
        LIMIT %(limit)s
    )
"""


def repeated_tail(conn, source_id: int | None, title: str, text: str | None, *, article_id: int | None = None,
                  around: datetime | None = None, already_cut: int = 0) -> article_tails.RepeatedTail | None:
    """Хвост текста, повторяющийся у других статей источника. `around` — от какого момента считать окно."""
    if source_id is None or len(text or "") <= config.MIN_FULL_TEXT_CHARS:
        return None
    neighbours = conn.execute(_NEIGHBOURS_SQL, {
        "window": article_tails.END_WINDOW_CHARS, "source_id": int(source_id), "article_id": article_id,
        "around": around, "days": REPEAT_WINDOW_DAYS, "limit": NEIGHBOURS_LIMIT,
    }).fetchall()
    return article_tails.find_repeated_tail(text, neighbours, own_title=title, already_cut=already_cut)


def split_for_write(conn, source_id: int | None, title: str, text: str | None, *, article_id: int | None = None,
                    around: datetime | None = None) -> tuple[str | None, str | None]:
    """(что положить в raw_text, что в text_tail)."""
    found = repeated_tail(conn, source_id, title, text, article_id=article_id, around=around)
    if found is None or found.blocked:
        return text, None
    return found.kept, found.tail


def split_article_text(conn, article_id: int, text: str) -> tuple[str, str | None]:
    """То же для новой версии текста уже сохранённой статьи."""
    row = conn.execute("SELECT source_id, title, collected_at FROM articles WHERE id = %s", (article_id,)).fetchone()
    if row is None:
        return text, None
    return split_for_write(conn, row[0], row[1], text, article_id=article_id, around=row[2])


def store_cut(conn, article_id: int, old_text: str, kept: str, tail: str) -> None:
    """Перечистка: отрезать конец сохранённого текста. Хэш остаётся: он считан по тексту до среза.

    Статусы дозагрузки не трогаем, текст не качался. Текст успели заменить: ничего не пишем."""
    conn.execute(
        "UPDATE articles SET raw_text = %s, text_tail = %s, updated_at = now() WHERE id = %s AND raw_text = %s",
        (kept, tail, article_id, old_text),
    )
