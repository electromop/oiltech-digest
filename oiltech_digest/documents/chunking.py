"""Нарезка разобранного документа на фрагменты под один вызов модели.

Зачем модуль вообще нужен: в новостном конвейере вход модели обрезан жёстко
(`pipeline.py:_compact(text, 6000)`). Для статьи это приемлемо, для документа на
60 страниц (~120 000 знаков) — нет: модель молча увидела бы первые 2–4 страницы
и выдала «сводку по документу» по его началу. Нарезчик заменяет обрезку: он
режет ВЕСЬ текст на фрагменты по max_chars и сохраняет привязку к якорям,
чтобы извлечённое число можно было вернуть на страницу/слайд, откуда оно взято.

Модуль — чистые функции: ни базы, ни сети, ни обращений к модели.
"""

from __future__ import annotations

from .model import Anchor, Chunk

# Разделитель между текстами соседних якорей в общем потоке. Ровно один символ:
# от его длины зависит расчёт границ и бюджета, поэтому он константа, а не параметр.
_SEPARATOR = "\n"

# Span — место одного якоря в общем потоке: [start, end) и номер якоря.
_Span = tuple[int, int, int]


def chunk_anchors(
    anchors: list[Anchor],
    max_chars: int = 12000,
    overlap_chars: int = 400,
) -> list[Chunk]:
    """Режет якоря на фрагменты, каждый из которых влезает в один вызов модели.

    Правила:
      * границы фрагментов по возможности совпадают с границами якорей —
        страница не режется пополам, если целиком влезает в бюджет;
      * якорь длиннее max_chars режется на несколько фрагментов, у всех
        anchor_from == anchor_to == номер этого якоря;
      * между соседними фрагментами есть перекрытие до overlap_chars знаков,
        чтобы факт на стыке страниц не потерялся;
      * anchor_from..anchor_to всегда покрывают ВЕСЬ текст фрагмента, включая
        перекрытие: иначе ссылку модели на страницу нельзя было бы проверить;
      * пустые якоря пропускаются, нумерация остальных не сдвигается;
      * длина текста любого фрагмента не превышает max_chars.

    Функция детерминирована: одинаковый вход даёт одинаковый выход.
    """
    # Границы системы: параметры приходят из конфигурации, кривые значения
    # дали бы не исключение, а бесконечный цикл или молчаливую потерю текста.
    if max_chars <= 0:
        raise ValueError("max_chars должен быть положительным")
    if overlap_chars < 0:
        raise ValueError("overlap_chars не может быть отрицательным")
    if overlap_chars >= max_chars:
        raise ValueError(
            "overlap_chars должен быть меньше max_chars, иначе фрагменты не продвигаются по тексту"
        )

    stream, spans = _build_stream(anchors)
    if not spans:
        return []

    chunks: list[Chunk] = []
    i = 0
    while i < len(spans):
        content_start, unit_end, number = spans[i]
        # Перекрытие берём назад от начала содержимого. Минимум — длина разделителя:
        # иначе при overlap_chars=0 сам разделитель не попал бы ни в один фрагмент.
        prefix = min(max(overlap_chars, len(_SEPARATOR)), content_start)
        budget = max_chars - prefix

        if unit_end - content_start > budget:
            _append_long_anchor(chunks, stream, spans, i, max_chars, overlap_chars)
            i += 1
            continue

        # Жадная упаковка: добираем следующие якоря целиком, пока влезают.
        end = content_start
        j = i
        while j < len(spans) and spans[j][1] - content_start <= budget:
            end = spans[j][1]
            j += 1

        start = content_start - prefix
        chunks.append(
            Chunk(
                index=len(chunks),
                text=stream[start:end],
                anchor_from=_first_anchor_at(spans, i, start),
                anchor_to=spans[j - 1][2],
            )
        )
        i = j

    return chunks


def _build_stream(anchors: list[Anchor]) -> tuple[str, list[_Span]]:
    """Склеивает тексты непустых якорей в один поток и запоминает, где чей кусок.

    Пустые якоря (в т.ч. состоящие из пробелов — типичная «страница-разделитель»
    в PDF) выпадают из потока, но их номера НЕ переиспользуются: номер берётся
    из самого якоря, а не из позиции в списке.
    """
    parts: list[str] = []
    spans: list[_Span] = []
    position = 0
    for anchor in anchors:
        text = anchor.text or ""
        if not text.strip():
            continue
        if parts:
            position += len(_SEPARATOR)
        parts.append(text)
        spans.append((position, position + len(text), anchor.number))
        position += len(text)
    return _SEPARATOR.join(parts), spans


def _append_long_anchor(
    chunks: list[Chunk],
    stream: str,
    spans: list[_Span],
    index: int,
    max_chars: int,
    overlap_chars: int,
) -> None:
    """Режет один якорь, который не влезает в фрагмент целиком.

    Перекрытие здесь берётся только ВНУТРИ якоря. Хвост предыдущего якоря в
    первый кусок не тянем: иначе anchor_from уехал бы на предыдущую страницу и
    сломал правило «у кусков длинного якоря anchor_from == anchor_to».
    Разделитель перед якорем всё же захватываем — он не принадлежит ни одному
    якорю, но без него в тексте потока осталась бы непокрытая дыра.
    """
    content_start, unit_end, number = spans[index]
    first_start = content_start - len(_SEPARATOR) if index > 0 else content_start
    piece_start = first_start
    while True:
        piece_end = min(unit_end, piece_start + max_chars)
        chunks.append(
            Chunk(
                index=len(chunks),
                text=stream[piece_start:piece_end],
                anchor_from=number,
                anchor_to=number,
            )
        )
        if piece_end >= unit_end:
            return
        # max_chars > overlap_chars проверено на входе, значит сдвиг строго положительный.
        piece_start = piece_end - overlap_chars
        if unit_end - piece_start < max_chars:
            # Хвост якоря додвигаем назад до полного размера. Иначе при маленьком
            # overlap_chars остаток уехал бы в модель отдельным фрагментом в пару
            # знаков, где число уже не прочитать без окружающего текста.
            piece_start = max(first_start, unit_end - max_chars)


def _first_anchor_at(spans: list[_Span], index: int, start: int) -> int:
    """Номер первого якоря, чей текст попал во фрагмент, начинающийся с позиции start.

    Перекрытие может утянуть начало фрагмента в предыдущий якорь (а при коротких
    якорях — и в несколько), поэтому anchor_from считается по факту, а не берётся
    у первого якоря содержимого.
    """
    first = index
    position = index - 1
    while position >= 0 and spans[position][1] > start:
        first = position
        position -= 1
    return spans[first][2]
