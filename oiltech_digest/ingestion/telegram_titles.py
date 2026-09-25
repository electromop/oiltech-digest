"""Разовая починка склеенных заголовков Telegram — по сохранённым данным, без сети.

До 25.09 парсер брал текст поста через `text_content()`, а он теряет `<br>`: строки
склеивались («…в Иллинойсе<br>ExxonMobil…» → «ИллинойсеExxonMobil»), и заголовок —
первое предложение — захватывал начало второй строки. Сохранённый текст склеен так же,
а перечитать пост нельзя: t.me с РФ-ядра отвечает с перебоями (замер 25.09).

Поэтому стык строк восстанавливается по самому заголовку. Заглавная сразу после
строчной внутри «слова», которого корпус целиком не знает, — начало новой строки, если
левая часть — известное слово или правая — частое. Бренды вида «КазМунайГаз», «кВт»,
«ExxonMobil» корпус знает целиком, их не режем. Выборка 45 из 862 правок на проде
(25.09) — все 45 дали ровно первую строку поста.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import re
from typing import Any

# Модулем, а не функцией: тестовая фикстура подменяет connection.get_connection, и прямой
# импорт функции писал бы в настоящую базу (так было с source_overrides).
from oiltech_digest.db import connection
from oiltech_digest.ingestion.telegram_parser import title_from_text

# Слово «известно», если встречается в стольких статьях: склейка — только в своей.
KNOWN_WORD_ARTICLES = 3
# Правая часть — частое слово (начало строки): «утроиласьТатнефть», «шуткойГубернатор».
COMMON_WORD_ARTICLES = 20
# Левая часть при частом правом слове — не обрывок бренда («Рус|Гидро», «Мега|Фон»).
MIN_LEFT_CHARS = 4

_LETTERS = re.compile(r"[A-Za-zА-Яа-яЁё]+")
_HTTP_GLUE = re.compile(r"[А-Яа-яЁё](?=https?://)")


def document_frequency(conn) -> Counter:
    """В скольких статьях встречается слово (в нижнем регистре) — по всему корпусу."""
    frequency: Counter = Counter()
    with conn.cursor(name="telegram_titles_words") as cur:
        cur.itersize = 2000
        cur.execute(
            """
            SELECT a.title, left(coalesce(a.raw_text, ''), 4000), coalesce(c.title_ru, ''), coalesce(c.summary, '')
            FROM articles a LEFT JOIN article_cards c ON c.article_id = a.id
            """
        )
        for row in cur:
            frequency.update({word.lower() for word in _LETTERS.findall(" ".join(part or "" for part in row))})
    return frequency


def split_glued_lines(title: str, frequency: Mapping[str, int]) -> list[str]:
    """Заголовок, разрезанный по восстановленным стыкам строк."""
    cuts: list[int] = []
    for match in _LETTERS.finditer(title):
        run = match.group(0)
        if frequency.get(run.lower(), 0) >= KNOWN_WORD_ARTICLES:
            continue
        bounds = [j for j in range(1, len(run)) if run[j - 1].islower() and run[j].isupper()]
        candidates = list(enumerate(bounds))
        if match.start() > 0 and title[match.start() - 1] == "#":
            # Хэштег рубрики слитный с заглавными («#ЦифраДняСтенки»): стык строки — последний.
            candidates = candidates[-1:]
        for index, bound in candidates:
            left = run[:bound]
            right = run[bound:bounds[index + 1]] if index + 1 < len(bounds) else run[bound:]
            known_left = frequency.get(left.lower(), 0) >= KNOWN_WORD_ARTICLES
            common_right = len(left) >= MIN_LEFT_CHARS and frequency.get(right.lower(), 0) >= COMMON_WORD_ARTICLES
            if known_left or common_right:
                cuts.append(match.start() + bound)
                break
    cuts.extend(match.end() for match in _HTTP_GLUE.finditer(title))
    segments: list[str] = []
    start = 0
    for cut in sorted(set(cuts)):
        segments.append(title[start:cut])
        start = cut
    segments.append(title[start:])
    return [segment.strip() for segment in segments if segment.strip()]


def repaired_title(title: str, frequency: Mapping[str, int]) -> str:
    """Заголовок по правилу исправленного парсера — будто стыки строк были на месте."""
    segments = split_glued_lines(title, frequency)
    if len(segments) < 2:
        return title
    return title_from_text("\n".join(segments))


def repair(*, apply: bool = False, frequency: Mapping[str, int] | None = None) -> dict[str, Any]:
    """Найти и (с apply) записать новые заголовки статей Telegram.

    `title_ru` русского поста — копия заголовка (перевод не нужен) и меняется вместе с
    ним; если перевод уже другой — не трогаем. Обе записи — только если значение не
    изменилось с чтения.
    """
    with connection.get_connection() as conn:
        if frequency is None:
            frequency = document_frequency(conn)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT a.id, a.title, c.title_ru
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN article_cards c ON c.article_id = a.id
            WHERE s.parse_strategy = 'telegram'
            ORDER BY a.id
            """
        )
        rows = cur.fetchall()
        changes = []
        for article_id, title, title_ru in rows:
            new_title = repaired_title(title or "", frequency)
            if new_title and new_title != title:
                changes.append({
                    "article_id": int(article_id),
                    "before": title,
                    "after": new_title,
                    "title_ru": title_ru is not None and title_ru == (title or "")[:200],
                })
        if apply:
            for change in changes:
                conn.execute(
                    "UPDATE articles SET title = %s WHERE id = %s AND title = %s",
                    (change["after"][:500], change["article_id"], change["before"]),
                )
                if change["title_ru"]:
                    conn.execute(
                        "UPDATE article_cards SET title_ru = %s, updated_at = now() "
                        "WHERE article_id = %s AND title_ru = %s",
                        (change["after"][:200], change["article_id"], change["before"][:200]),
                    )
            conn.commit()
    return {"scanned": len(rows), "changed": len(changes), "applied": apply, "changes": changes}
