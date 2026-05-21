"""Нормализация данных статьи: очистка HTML, парсинг дат, картинка, content_hash.

`clean_html` / `parse_date` / `extract_image` перенесены из прототипа
`oil-tech-digest-bot/parser.py`; `compute_content_hash` — новое.
"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from dateutil import parser as dateparser

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_html(text: str) -> str:
    """Снять HTML-теги, расшифровать entities, схлопнуть пробелы."""
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def parse_date(entry) -> datetime | None:
    """Дата публикации из RSS-entry (published/updated/created) → aware datetime (UTC).

    None, если ни одно поле не распарсилось — статья всё равно сохранится.
    """
    for field in ("published", "updated", "created"):
        raw = entry.get(field, "") if hasattr(entry, "get") else ""
        if raw:
            try:
                dt = dateparser.parse(raw)
            except (ValueError, TypeError, OverflowError):
                continue
            if dt is None:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
    return None


def extract_image(entry) -> str:
    """URL картинки из media_thumbnail / media_content / enclosures (для будущего #4)."""
    media = entry.get("media_thumbnail", []) if hasattr(entry, "get") else []
    if media and isinstance(media, list):
        return media[0].get("url", "")

    media_content = entry.get("media_content", []) if hasattr(entry, "get") else []
    if media_content and isinstance(media_content, list):
        for mc in media_content:
            if mc.get("medium") == "image" or "image" in mc.get("type", ""):
                return mc.get("url", "")

    enclosures = entry.get("enclosures", []) if hasattr(entry, "get") else []
    if enclosures:
        for enc in enclosures:
            if "image" in enc.get("type", ""):
                return enc.get("href", enc.get("url", ""))
    return ""


def _normalize_title(title: str) -> str:
    return _WS_RE.sub(" ", (title or "").strip().lower())


def _normalize_url(url: str) -> str:
    """host+path в нижнем регистре, без схемы, query (utm и пр.) и хвостового слэша."""
    try:
        parts = urlsplit((url or "").strip().lower())
        if not parts.netloc:
            return (url or "").strip().lower()
        return f"{parts.netloc}{parts.path.rstrip('/')}"
    except ValueError:
        return (url or "").strip().lower()


def compute_content_hash(title: str, url: str) -> str:
    """sha256 от нормализованных title|url. Мягкий сигнал кросс-источниковых дублей."""
    basis = f"{_normalize_title(title)}|{_normalize_url(url)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()
