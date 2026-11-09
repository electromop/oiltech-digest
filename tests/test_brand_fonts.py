"""Шрифт бренда с сервера (#181): в репозитории файлов нет, интерфейс и печать PDF берут их из каталога шрифтов.

Каталог задаёт BRAND_FONTS_DIR, без неё он рядом с файлом брендинга выпуска. Нет каталога или файла: правила
@font-face нет, и работает запасной шрифт из списка семейства."""

from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from oiltech_digest import api, brand_fonts, config
from oiltech_digest.processing import digest_pdf

FONT = b"\x00\x01\x00\x00 font bytes"


def _fonts(tmp_path, *names):
    folder = tmp_path / "fonts"
    folder.mkdir()
    for name in names:
        (folder / name).write_bytes(FONT)
    return folder


def test_fonts_dir_is_next_to_the_branding_file_unless_set(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BRAND_FONTS_DIR", "")
    monkeypatch.setattr(config, "DIGEST_BRANDING_PATH", "/app/branding/digest_branding.json")
    assert str(brand_fonts.fonts_dir()) == "/app/branding/fonts"

    monkeypatch.setattr(config, "BRAND_FONTS_DIR", str(tmp_path))
    assert brand_fonts.fonts_dir() == tmp_path

    monkeypatch.setattr(config, "BRAND_FONTS_DIR", "")
    monkeypatch.setattr(config, "DIGEST_BRANDING_PATH", "")
    assert brand_fonts.fonts_dir() is None


def test_font_path_takes_only_the_known_files_that_exist(tmp_path, monkeypatch):
    folder = _fonts(tmp_path, "brand-sans-regular.ttf", "other.ttf")
    monkeypatch.setattr(config, "BRAND_FONTS_DIR", str(folder))

    assert brand_fonts.font_path("brand-sans-regular.ttf") == folder / "brand-sans-regular.ttf"
    assert brand_fonts.font_path("brand-sans-bold.ttf") is None  # файла нет
    assert brand_fonts.font_path("other.ttf") is None  # не из набора
    assert brand_fonts.font_path("../fonts/brand-sans-regular.ttf") is None


def test_pdf_font_faces_embed_the_server_files_under_neutral_families(tmp_path, monkeypatch):
    # Узкое обычное есть только у интерфейса: печать его не встраивает (лишние сотни КБ в каждом PDF и письме).
    folder = _fonts(tmp_path, *brand_fonts.FILES)
    monkeypatch.setattr(config, "BRAND_FONTS_DIR", str(folder))

    faces = digest_pdf.font_faces()

    data = "data:font/ttf;base64," + base64.b64encode(FONT).decode("ascii")
    assert faces.count("@font-face") == 3 and faces.count(data) == 3
    assert "font-family:'Brand Sans';font-style:normal;font-weight:400;" in faces
    assert "font-family:'Brand Sans';font-style:normal;font-weight:700;" in faces
    assert "font-family:'Brand Sans Condensed';font-style:normal;font-weight:700;" in faces


def test_pdf_without_server_fonts_has_no_font_face_and_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BRAND_FONTS_DIR", str(tmp_path / "нет"))

    assert digest_pdf.font_faces() == ""
    css = digest_pdf._css({"name": "Выпуск", "month_label": "Октябрь 2026"})
    assert "font-family:'Brand Sans',Arial,sans-serif;" in css
    assert "'Brand Sans Condensed','Arial Narrow',Arial,sans-serif" in css and "%" not in css.replace("%;", "")


def test_api_serves_the_font_from_the_server_and_404_otherwise(tmp_path, monkeypatch):
    folder = _fonts(tmp_path, "brand-sans-bold.ttf")
    monkeypatch.setattr(config, "BRAND_FONTS_DIR", str(folder))
    client = TestClient(api.app)

    response = client.get("/brand/fonts/brand-sans-bold.ttf")
    assert response.status_code == 200 and response.content == FONT
    assert response.headers["content-type"] == "font/ttf"
    assert "max-age" in response.headers["cache-control"]
    # Вход и экран входа рисуются тем же шрифтом: сессия не нужна.
    assert client.get("/brand/fonts/brand-sans-regular.ttf").status_code == 404
    assert client.get("/brand/fonts/other.ttf").status_code == 404

    monkeypatch.setattr(config, "BRAND_FONTS_DIR", "")
    monkeypatch.setattr(config, "DIGEST_BRANDING_PATH", "")
    assert client.get("/brand/fonts/brand-sans-bold.ttf").status_code == 404


# --- Шрифт, установленный у читателя: письмо и DOCX ------------------------------------------------
# Почтовые клиенты вырезают @font-face, Word берёт шрифт по имени: имя шрифта бренда у читателя задаёт блок fonts
# брендинга на сервере. Пусто: Arial, как без файлов шрифта.

OFFICE = {"office_family": "Corp Sans", "office_condensed_family": "Corp Sans Narrow"}


def test_letter_font_stack_adds_the_installed_font_after_the_embedded_one():
    from oiltech_digest.processing import digest

    assert digest.font_stack({}) == "'Brand Sans',Arial,Helvetica,sans-serif"
    assert digest.font_stack({"fonts": OFFICE}) == "'Brand Sans','Corp Sans',Arial,Helvetica,sans-serif"
    assert digest.font_stack({"fonts": OFFICE}, condensed=True, sep=", ") == (
        "'Brand Sans Condensed', 'Brand Sans', 'Corp Sans Narrow', 'Corp Sans', Arial, Helvetica, sans-serif")
    # Имя попадает в атрибут style письма: кавычки и разметка в нём не проходят.
    broken = {"fonts": {"office_family": "X';}</style><b>", "office_condensed_family": ""}}
    assert digest.font_stack(broken) == "'Brand Sans',Arial,Helvetica,sans-serif"


def test_letter_carries_the_installed_font_from_branding(monkeypatch):
    from oiltech_digest.processing import digest

    branding = {**digest.get_digest_branding(), "fonts": OFFICE}
    content = {"issue": {"title": "Выпуск"}, "hero": {}, "news": [], "footer": {}, "branding": branding}

    letter = digest.render_digest_email(content)

    assert "font-family:'Brand Sans','Corp Sans',Arial,Helvetica,sans-serif;" in letter
    assert "'Brand Sans Condensed','Brand Sans','Corp Sans Narrow','Corp Sans',Arial" in letter


def _docx_fonts(branding):
    import io
    import zipfile

    from oiltech_digest.processing import digest

    content = {"issue": {"title": "Выпуск"}, "hero": {}, "news": [], "footer": {}, "branding": branding}
    with zipfile.ZipFile(io.BytesIO(digest.render_digest_docx(content))) as archive:
        return archive.read("word/styles.xml").decode("utf-8")


def test_docx_takes_the_installed_font_from_branding():
    styles = _docx_fonts({"header": {}, "fonts": OFFICE})
    assert 'w:ascii="Corp Sans"' in styles and 'w:ascii="Corp Sans Narrow"' in styles

    plain = _docx_fonts({"header": {}})
    assert 'w:ascii="Arial"' in plain and "Corp Sans" not in plain
