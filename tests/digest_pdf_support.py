"""Чтение PDF выпуска обратно и сайт издателя с картинками для тестов выпуска в формате эксперта (#161).

Текст листов, ссылки, растровые картинки и где они нарисованы, подписи картинок в размеченном PDF, название, тема,
закладки и шрифты документа. Тесты: test_digest_issue_pdf.py и test_digest_issue_cover.py.
"""

from __future__ import annotations

import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


def pdf_pages(pdf: bytes) -> list[str]:
    """Текст листов PDF, прочитанного обратно: пробелы сведены, лигатуры разложены (NFKC)."""
    import unicodedata

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf))
    return [" ".join(unicodedata.normalize("NFKC", page.extract_text() or "").split()) for page in reader.pages]


def pdf_links(pdf: bytes) -> list[list[str]]:
    """Ссылки листов PDF: адрес внешней ссылки или «#внутренняя» для перехода внутри документа."""
    from pypdf import PdfReader

    pages = []
    for page in PdfReader(io.BytesIO(pdf)).pages:
        targets = []
        for annotation in page.get("/Annots") or []:
            annotation = annotation.get_object()
            if annotation.get("/Subtype") != "/Link":
                continue
            action = annotation.get("/A")
            action = action.get_object() if action is not None else {}
            targets.append(str(action["/URI"]) if action.get("/S") == "/URI" else "#внутренняя")
        pages.append(targets)
    return pages


def png(width: int, height: int) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (40, 90, 160)).save(buffer, "PNG")
    return buffer.getvalue()


class ImageHost(str):
    """Адрес сайта издателя; `revive()` — сайт ожил, и картинка /revives.png, на которую он отказывал, отдаётся."""

    revived = False

    def revive(self) -> None:
        type(self).revived = True


@pytest.fixture()
def image_host():
    """Сайт издателя с картинками публикаций: годная 800×500, узкая 300×200, вытянутая 1200×300 (полоска); на прочие
    адреса отвечает 403, как сайт, закрытый для РФ-адресов. /revives.png отдаётся, только когда сайт ожил."""
    files = {"/fit.png": png(800, 500), "/narrow.png": png(300, 200), "/strip.png": png(1200, 300)}
    host_type = type("Host", (ImageHost,), {"revived": False})

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — имя задаёт http.server
            body = files["/fit.png"] if self.path == "/revives.png" and host_type.revived else files.get(self.path)
            self.send_response(200 if body else 403)
            if body:
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield host_type(f"http://127.0.0.1:{server.server_address[1]}")
    server.shutdown()
    server.server_close()


def pdf_images(pdf: bytes) -> list[list[tuple[int, int]]]:
    """Растровые картинки листов PDF (размер в точках). Заглушка темы — SVG, Chromium рисует её вектором: в этом
    списке её нет."""
    from pypdf import PdfReader

    return [[image.image.size for image in page.images] for page in PdfReader(io.BytesIO(pdf)).pages]


def pdf_image_boxes(pdf: bytes) -> list[list[tuple[tuple[int, int], float, float]]]:
    """Где на листах PDF нарисованы растровые картинки: (размер картинки в точках, ширина на листе в pt, левый край в
    pt). По матрице преобразования содержимого листа (cm), как её строит сам PDF."""
    from pypdf import PdfReader
    from pypdf.generic import ContentStream

    reader = PdfReader(io.BytesIO(pdf))

    def multiply(m: list[float], n: list[float]) -> list[float]:
        a, b, c, d, e, f = m
        A, B, C, D, E, F = n  # noqa: N806 — обозначения матрицы PDF
        return [a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D, e * A + f * C + E, e * B + f * D + F]

    def walk(stream, resources, ctm: list[float], found: list) -> None:
        xobjects = resources.get("/XObject") or {}
        stack: list[list[float]] = []
        for operands, operator in ContentStream(stream, reader).operations:
            if operator == b"q":
                stack.append(ctm)
            elif operator == b"Q":
                ctm = stack.pop()
            elif operator == b"cm":
                ctm = multiply([float(value) for value in operands], ctm)
            elif operator == b"Do":
                xobject = xobjects[operands[0]].get_object()
                if xobject.get("/Subtype") == "/Image":
                    found.append(((int(xobject["/Width"]), int(xobject["/Height"])),
                                  round((ctm[0] ** 2 + ctm[1] ** 2) ** 0.5, 1), round(ctm[4], 1)))
                elif xobject.get("/Subtype") == "/Form":
                    matrix = [float(value) for value in xobject.get("/Matrix", [1, 0, 0, 1, 0, 0])]
                    walk(xobject, xobject.get("/Resources") or resources, multiply(matrix, ctm), found)

    pages = []
    for page in reader.pages:
        found: list = []
        walk(page.get_contents(), page["/Resources"], [1, 0, 0, 1, 0, 0], found)
        pages.append(found)
    return pages


def pdf_document(pdf: bytes) -> dict:
    """Название, тема, закладки (заголовок и номер листа с 1), размер листов PDF в pt и встроенные шрифты (без
    приставки подмножества «AAAAAA+»)."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf))
    return {
        "title": reader.metadata.get("/Title"),
        "subject": reader.metadata.get("/Subject"),
        "bookmarks": [(item.title, reader.get_destination_page_number(item) + 1) for item in reader.outline],
        "sizes": {(round(float(page.mediabox.width), 1), round(float(page.mediabox.height), 1))
                  for page in reader.pages},
        "fonts": {str(font.get_object().get("/BaseFont")).lstrip("/").split("+")[-1]
                  for page in reader.pages for font in (page["/Resources"].get("/Font") or {}).values()},
    }


def pdf_figures(pdf: bytes) -> list[str]:
    """Подписи картинок размеченного PDF (/Alt у рисунков структуры документа) по порядку документа: так видно, что
    нарисовано на месте картинки карточки — своя картинка или рисунок какой темы."""
    from pypdf import PdfReader
    from pypdf.generic import ArrayObject, DictionaryObject

    reader = PdfReader(io.BytesIO(pdf))
    found: list[str] = []

    def walk(node) -> None:
        node = node.get_object()
        if isinstance(node, ArrayObject):
            for kid in node:
                walk(kid)
        elif isinstance(node, DictionaryObject):
            if node.get("/S") == "/Figure" and node.get("/Alt") is not None:
                found.append(str(node["/Alt"]))
            if "/K" in node:
                walk(node["/K"])

    walk(reader.trailer["/Root"]["/StructTreeRoot"])
    return found
