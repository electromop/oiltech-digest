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
    folder = _fonts(tmp_path, "brand-sans-regular.ttf", "brand-sans-bold.ttf", "brand-sans-condensed-bold.ttf")
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
    css = digest_pdf._CSS
    assert "'Brand Sans',Arial,sans-serif" in css
    assert "'Brand Sans Condensed','Arial Narrow',Arial,sans-serif" in css


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
