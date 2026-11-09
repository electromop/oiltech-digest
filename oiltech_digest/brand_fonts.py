"""Шрифт бренда для интерфейса и печати выпуска: файлы на сервере, в репозитории их нет.

Файлы лежат в каталоге шрифтов под именами из FILES: на сервере это том брендинга (`/app/branding/fonts/`), его
видят все контейнеры, которые печатают PDF. Каталог задаёт BRAND_FONTS_DIR, без неё он рядом с файлом брендинга
выпуска (DIGEST_BRANDING_PATH). Нет каталога или файла: интерфейс и печать берут запасной шрифт из своего списка
семейств. Имена семейств в коде нейтральные, имя файла на сервере тоже; что внутри файла, код не читает."""

from __future__ import annotations

from pathlib import Path

from oiltech_digest import config

SANS = "Brand Sans"
CONDENSED = "Brand Sans Condensed"
# Файл на сервере: семейство и насыщенность. Те же имена у @font-face интерфейса (frontend/src/styles/base.css).
FILES: dict[str, tuple[str, int]] = {
    "brand-sans-regular.ttf": (SANS, 400),
    "brand-sans-bold.ttf": (SANS, 700),
    "brand-sans-condensed-regular.ttf": (CONDENSED, 400),
    "brand-sans-condensed-bold.ttf": (CONDENSED, 700),
}


def fonts_dir() -> Path | None:
    """Каталог шрифтов: BRAND_FONTS_DIR, иначе fonts рядом с файлом брендинга, иначе нет."""
    if config.BRAND_FONTS_DIR:
        return Path(config.BRAND_FONTS_DIR)
    if config.DIGEST_BRANDING_PATH:
        return Path(config.DIGEST_BRANDING_PATH).parent / "fonts"
    return None


def font_path(name: str) -> Path | None:
    """Файл шрифта по имени из FILES. Чужое имя, нет каталога или файла: None."""
    folder = fonts_dir()
    if name not in FILES or folder is None:
        return None
    path = folder / name
    return path if path.is_file() else None
