"""Контракты пакета документов. Держатся отдельно, чтобы парсер, нарезчик и
проверяльщик были чистыми функциями без обратных зависимостей."""

from __future__ import annotations

from dataclasses import dataclass


# Единица ссылки на место в документе. У PDF — страница, у презентации — слайд,
# у текстового документа — порядковый номер блока (страниц в исходнике docx нет).
ANCHOR_UNITS = {"pdf": "страница", "pptx": "слайд", "docx": "блок"}


@dataclass(frozen=True)
class Anchor:
    """Одно место в документе и его текст."""

    number: int  # 1-based
    text: str


@dataclass(frozen=True)
class ParsedDocument:
    kind: str  # "pdf" | "pptx" | "docx"
    anchors: list[Anchor]

    @property
    def anchor_unit(self) -> str:
        return ANCHOR_UNITS.get(self.kind, "блок")

    @property
    def total_chars(self) -> int:
        return sum(len(a.text) for a in self.anchors)


@dataclass(frozen=True)
class Chunk:
    """Фрагмент для одного обращения к модели, с сохранённой привязкой к якорям."""

    index: int  # 0-based
    text: str
    anchor_from: int
    anchor_to: int


class DocumentError(ValueError):
    """Базовая ошибка приёма документа. Текст сообщения показывается пользователю."""


class UnsupportedFormatError(DocumentError):
    """Формат не поддерживается или файл не опознан по сигнатуре."""


class UnreadableDocumentError(DocumentError):
    """Файл повреждён или защищён паролем."""


class ScannedDocumentError(DocumentError):
    """В документе нет текстового слоя — это скан. Распознавание вне области v1."""
