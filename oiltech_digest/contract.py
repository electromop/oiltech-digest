"""Договор ядра (РФ) и воркеров зарубежного контура (NL) о задаче, возвращённой на остановке.

Стороны выкатываются порознь: ядро — скриптом на РФ, воркеры — владельцем на NL, по
одному. Пересборка NL останавливает воркер посреди задачи (SIGTERM). Раньше такая задача
висела до конца аренды (600 с), а оплаченная часть ИИ-пакета выбрасывалась и оплачивалась
заново (ADR 0001). Теперь воркер возвращает её сам (release) с тем, что успел, а ядро
решает здесь, что осталось сделать.
"""

from __future__ import annotations

from typing import Any

# Пакеты по статьям: итог — по строке на статью, её можно применить без остальных.
_ARTICLE_BATCHES = frozenset({"process_articles", "recheck_relevance", "translate_titles"})
# Виды, чей частичный итог ядро принимает. Сбор ИИ не зовёт — повторить его дёшево;
# итог документа — одна карточка на весь файл, половина карточки не нужна никому.
PARTIAL_KINDS = _ARTICLE_BATCHES | {"reprint_review"}


def accepts_partial(kind: str | None, result: dict[str, Any] | None) -> bool:
    return str(kind or "") in PARTIAL_KINDS and bool(result) and bool((result or {}).get("partial"))


def without_reservation(payload: dict[str, Any]) -> dict[str, Any]:
    """Резерв статей держится за выданной задачей; вернувшаяся в очередь его отпускает,
    при следующей выдаче он считается заново."""
    return {key: value for key, value in (payload or {}).items() if key != "reserved_article_ids"}


def remaining_after_partial(kind: str | None, payload: dict[str, Any],
                            result: dict[str, Any] | None) -> dict[str, Any] | None:
    """Что осталось задаче после частичного итога; None — ничего, задача выполнена.

    Сделанное вычитается, чтобы следующая выдача не звала модель за уже записанное:
    - явный список статей — без обработанных (в том числе с ошибкой: полный прогон тоже
      не повторил бы их внутри задачи);
    - пакет «N необработанных статей» — N уменьшается на сделанное, а выборка при выдаче
      сама обходит записанные;
    - пары судьи перепечаток — без рассуженных.
    """
    rest = without_reservation(payload)
    if not accepts_partial(kind, result):
        return rest
    kind = str(kind)
    if kind in _ARTICLE_BATCHES:
        done = {int(item["article_id"]) for item in result.get("articles") or []}
        article_ids = [int(item) for item in rest.get("article_ids") or []]
        if article_ids:
            left = [article_id for article_id in article_ids if article_id not in done]
            return {**rest, "article_ids": left} if left else None
        if kind == "process_articles":
            left_limit = int(rest.get("limit") or 5) - len(done)
            return {**rest, "limit": left_limit} if left_limit > 0 else None
        return rest
    judged = {(int(item["a_id"]), int(item["b_id"])) for item in result.get("verdicts") or []}
    pairs = [pair for pair in rest.get("pairs") or [] if (int(pair["a_id"]), int(pair["b_id"])) not in judged]
    return {**rest, "pairs": pairs} if pairs else None
