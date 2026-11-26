"""Картинки постов LinkedIn на сервере платформы.

LinkedIn отдаёт картинку поста по ссылке со сроком (одна-три недели) и со своего домена, закрытого в России: к сборке
выпуска за месяц такая ссылка уже не открывается, а читатель без VPN её не видит. Поэтому сбор скачивает первую картинку
поста к себе (том документов ядра) и пишет статье свой адрес: `PUBLIC_BASE_URL` + `/media/post-images/<имя>`. Полный
адрес нужен PDF выпуска (страница собирается в браузере без адреса сайта), письму и DOCX.

Защита. Качается только https с доменов картинок LinkedIn (`*.licdn.com`), и после переадресаций тоже; загрузка идёт
через core_page.download: только публичные адреса, переадресации проверяются заново, предел размера и общий срок.
Хранится только изображение (JPEG, PNG, GIF, WebP по первым байтам) до 5 МБ, имя файла: хэш содержимого и расширение.
Раздача отдаёт только файлы с таким именем из своей папки, без входа: картинку грузят PDF и почтовые программы.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
import re
from typing import Any, Callable
from urllib.parse import urlsplit

from oiltech_digest import config
from oiltech_digest.ingestion import core_page

logger = logging.getLogger(__name__)

DIR_NAME = "post_images"
ROUTE = "/media/post-images"
MAX_BYTES = 5 * 1024 * 1024
_ALLOWED_HOST = "licdn.com"
_NAME = re.compile(r"^[0-9a-f]{64}\.(jpg|png|gif|webp)$")
MEDIA_TYPES = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}

Download = Callable[[str], dict[str, Any]]


def allowed_source(url: str | None) -> bool:
    """https с домена картинок LinkedIn, без логина и без чужого порта."""
    parts = urlsplit(str(url or ""))
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and not parts.username and not parts.password and port in (None, 443)
            and (host == _ALLOWED_HOST or host.endswith("." + _ALLOWED_HOST)))


def image_kind(data: bytes) -> str | None:
    """Вид изображения по первым байтам; не изображение: None."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def storage_dir() -> Path:
    return Path(config.UPLOAD_DIR) / DIR_NAME


def save(url: str | None, *, download: Download = core_page.download) -> str | None:
    """Скачать картинку поста и вернуть её адрес на платформе; нельзя или не вышло: None, статья идёт без картинки."""
    if not config.PUBLIC_BASE_URL or not allowed_source(url):
        return None
    result = download(str(url))
    if not result.get("ok") or not allowed_source(result.get("final_url")):
        logger.info("картинка поста не скачана: %s", result.get("error") or "переадресация с домена LinkedIn")
        return None
    data = result.get("content") or b""
    kind = image_kind(data) if 0 < len(data) <= MAX_BYTES else None
    if kind is None:
        logger.info("картинка поста не сохранена: не изображение или больше %d МБ", MAX_BYTES // (1024 * 1024))
        return None
    name = f"{hashlib.sha256(data).hexdigest()}.{kind}"
    folder = storage_dir()
    path = folder / name
    if not path.exists():
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / f".{name}.{os.getpid()}.part"
        temporary.write_bytes(data)
        os.replace(temporary, path)
    return f"{config.PUBLIC_BASE_URL}{ROUTE}/{name}"


def file_for(name: str) -> Path | None:
    """Файл картинки по имени из адреса раздачи; чужое имя или нет файла: None."""
    if not _NAME.fullmatch(name or ""):
        return None
    path = storage_dir() / name
    return path if path.is_file() else None


def media_type(name: str) -> str:
    return MEDIA_TYPES[name.rsplit(".", 1)[-1]]
