"""Тесты нарезчика документа на фрагменты."""

from __future__ import annotations

import pytest

from oiltech_digest.documents.chunking import chunk_anchors
from oiltech_digest.documents.model import Anchor

SEP = "\n"


def _text(number: int, length: int) -> str:
    """Текст заданной длины, уникальный для каждого якоря.

    Уникальность нужна тестам: по тексту фрагмента однозначно находится его место
    в исходном потоке, иначе проверка покрытия ловила бы чужое совпадение.
    """
    token = f"с{number}ф"
    parts: list[str] = []
    size = 0
    index = 0
    while size < length:
        piece = f"{token}{index:05d} "
        parts.append(piece)
        size += len(piece)
        index += 1
    return "".join(parts)[:length]


def _pages(lengths: list[int], start: int = 1) -> list[Anchor]:
    return [Anchor(number=start + i, text=_text(start + i, n)) for i, n in enumerate(lengths)]


def _stream(anchors: list[Anchor]) -> str:
    return SEP.join(a.text for a in anchors if a.text.strip())


def _spans(anchors: list[Anchor]) -> dict[int, tuple[int, int]]:
    spans: dict[int, tuple[int, int]] = {}
    position = 0
    for anchor in anchors:
        if not anchor.text.strip():
            continue
        if spans:
            position += len(SEP)
        spans[anchor.number] = (position, position + len(anchor.text))
        position += len(anchor.text)
    return spans


def _assert_covers_everything(chunks, anchors) -> None:
    """Ни одного знака исходного текста не потеряно и нет дыр между фрагментами."""
    stream = _stream(anchors)
    assert chunks, "непустой документ обязан дать хотя бы один фрагмент"
    covered = 0
    for chunk in chunks:
        start = stream.find(chunk.text)
        assert start != -1, f"фрагмент {chunk.index} не найден в исходном тексте"
        assert start <= covered, f"перед фрагментом {chunk.index} осталась непокрытая дыра"
        covered = max(covered, start + len(chunk.text))
    assert covered == len(stream), "хвост документа не попал ни в один фрагмент"


def _assert_ranges_exact(chunks, anchors) -> None:
    """anchor_from..anchor_to = ровно те якоря, чей текст реально лежит во фрагменте."""
    stream = _stream(anchors)
    spans = _spans(anchors)
    for chunk in chunks:
        start = stream.find(chunk.text)
        end = start + len(chunk.text)
        touched = sorted(n for n, (s, e) in spans.items() if s < end and e > start)
        assert touched, f"фрагмент {chunk.index} не привязан ни к одному якорю"
        assert (chunk.anchor_from, chunk.anchor_to) == (touched[0], touched[-1])


def _shared_overlap(previous: str, current: str) -> int:
    """Сколько знаков конца previous буквально повторено в начале current."""
    limit = min(len(previous), len(current))
    for size in range(limit, 0, -1):
        if previous[-size:] == current[:size]:
            return size
    return 0


def test_empty_input_gives_no_chunks():
    assert chunk_anchors([]) == []


def test_blank_anchors_only_give_no_chunks():
    anchors = [Anchor(number=1, text=""), Anchor(number=2, text="   \n\t ")]
    assert chunk_anchors(anchors) == []


def test_single_short_anchor_kept_whole():
    anchors = _pages([120])
    chunks = chunk_anchors(anchors)
    assert len(chunks) == 1
    assert chunks[0].text == anchors[0].text
    assert (chunks[0].index, chunks[0].anchor_from, chunks[0].anchor_to) == (0, 1, 1)


def test_many_short_anchors_are_glued_into_one_chunk():
    anchors = _pages([50] * 10)
    chunks = chunk_anchors(anchors)
    assert len(chunks) == 1
    assert chunks[0].text == _stream(anchors)
    assert (chunks[0].anchor_from, chunks[0].anchor_to) == (1, 10)


def test_long_anchor_is_split_within_one_anchor():
    anchors = [Anchor(number=7, text=_text(7, 500))]
    chunks = chunk_anchors(anchors, max_chars=100, overlap_chars=20)

    assert len(chunks) > 1
    assert all((c.anchor_from, c.anchor_to) == (7, 7) for c in chunks)
    assert all(len(c.text) <= 100 for c in chunks)
    # перекрытие не меньше запрошенного и берётся внутри самого якоря
    for previous, current in zip(chunks, chunks[1:]):
        assert _shared_overlap(previous.text, current.text) >= 20
    _assert_covers_everything(chunks, anchors)


def test_long_anchor_between_short_ones_keeps_from_equal_to():
    anchors = _pages([80, 400, 80])
    chunks = chunk_anchors(anchors, max_chars=120, overlap_chars=30)

    long_chunks = [c for c in chunks if c.anchor_from == 2 and c.anchor_to == 2]
    assert len(long_chunks) > 1, "длинный якорь должен дать несколько фрагментов"
    _assert_covers_everything(chunks, anchors)
    _assert_ranges_exact(chunks, anchors)


def test_long_anchor_tail_is_not_a_scrap():
    # 250 знаков при max_chars=100 без перекрытия дали бы хвост в 50 знаков;
    # хвостовой фрагмент должен доезжать назад до полного размера.
    anchors = [Anchor(number=1, text=_text(1, 250))]
    chunks = chunk_anchors(anchors, max_chars=100, overlap_chars=0)

    assert [len(c.text) for c in chunks] == [100, 100, 100]
    assert all((c.anchor_from, c.anchor_to) == (1, 1) for c in chunks)
    _assert_covers_everything(chunks, anchors)


def test_page_that_fits_is_not_cut_in_half():
    anchors = _pages([60] * 12)
    chunks = chunk_anchors(anchors, max_chars=200, overlap_chars=0)

    assert len(chunks) > 1, "тест бессмыслен на одном фрагменте"
    for anchor in anchors:
        assert any(anchor.text in c.text for c in chunks), f"якорь {anchor.number} разрезан"
    _assert_covers_everything(chunks, anchors)
    _assert_ranges_exact(chunks, anchors)


def test_realistic_document_is_fully_covered_with_correct_ranges():
    # 60 страниц по 2000 знаков — тот случай, на котором обрезка в 6000 знаков
    # молча теряла бы 95% документа.
    anchors = _pages([2000] * 60)
    chunks = chunk_anchors(anchors)

    assert len(chunks) > 1
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert all(len(c.text) <= 12000 for c in chunks)
    assert chunks[0].anchor_from == 1
    assert chunks[-1].anchor_to == 60
    _assert_covers_everything(chunks, anchors)
    _assert_ranges_exact(chunks, anchors)


def test_neighbour_chunks_overlap_on_anchor_boundary():
    anchors = _pages([2000] * 20)
    overlap = 400
    chunks = chunk_anchors(anchors, max_chars=6000, overlap_chars=overlap)

    assert len(chunks) > 1
    for previous, current in zip(chunks, chunks[1:]):
        # разделитель между якорями лежит на стыке, поэтому общий кусок на 1 знак короче
        shared = overlap - len(SEP)
        assert current.text[:shared] == previous.text[-shared:]


def test_blank_anchors_are_skipped_without_renumbering():
    anchors = [
        Anchor(number=1, text=_text(1, 100)),
        Anchor(number=2, text="   \n  "),
        Anchor(number=3, text=_text(3, 100)),
    ]
    chunks = chunk_anchors(anchors, max_chars=150, overlap_chars=10)

    numbers = {c.anchor_from for c in chunks} | {c.anchor_to for c in chunks}
    assert numbers == {1, 3}, "номера уцелевших якорей не должны сдвигаться"
    assert all("   \n  " not in c.text for c in chunks)
    _assert_covers_everything(chunks, anchors)
    _assert_ranges_exact(chunks, anchors)


def test_result_is_deterministic():
    anchors = _pages([700] * 30)
    first = chunk_anchors(anchors, max_chars=2500, overlap_chars=200)
    second = chunk_anchors(anchors, max_chars=2500, overlap_chars=200)
    third = chunk_anchors(_pages([700] * 30), max_chars=2500, overlap_chars=200)

    assert first == second == third


@pytest.mark.parametrize(
    "max_chars, overlap_chars",
    [(0, 0), (-1, 0), (100, -1), (100, 100), (100, 200)],
)
def test_invalid_parameters_are_rejected(max_chars, overlap_chars):
    with pytest.raises(ValueError):
        chunk_anchors(_pages([100]), max_chars=max_chars, overlap_chars=overlap_chars)
