"""Страница LinkedIn и её пост: адрес страницы, вид страницы и запись статьи из поста, который отдал сервис Apify.

Страница LinkedIn это страница компании (`linkedin.com/company/<имя>/`, у её витрины `showcase`) или профиль эксперта
(`linkedin.com/in/<имя>/`). Одна страница: один источник; её пост становится статьёй этого источника и идёт обычным
конвейером. Модуль без сети и без базы: адреса и разбор ответа сервиса, поэтому всё проверяется на выдуманных постах.

Пост сервиса: `content` (текст), `postedAt` (время), `linkedinUrl` (адрес). У репоста (`repostedBy`) это исходный пост
чужого автора, его время, и время репоста `repostedAt`; у цитаты (`repost`) свой короткий текст страницы и
процитированный пост. Ещё бывают статья LinkedIn (`article`), документ (`document`), картинки и видео.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any
import unicodedata
from urllib.parse import urlsplit

from oiltech_digest.ingestion import dates, normalize
from oiltech_digest.ingestion.telegram_parser import title_from_text

STRATEGY = "linkedin"
SOURCE_TYPE = "LinkedIn"
HOST = "www.linkedin.com"
COMPANY = "company"
PROFILE = "profile"
# Раздел адреса страницы и вид страницы: витрина (showcase) это та же страница компании.
_SECTIONS = {"company": COMPANY, "showcase": COMPANY, "in": PROFILE}
_POST_PATHS = ("/posts/", "/feed/update/", "/pulse/")

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_.%]{0,99}$")
_URL = re.compile(r"https?://\S+")
# Хвост строки без смысла перед ссылкой («Читайте >> https://…», «👇», двоеточие, тире).
_TRAILER = re.compile(r"[\s>\u00bb\u2192\u2b07\u2b06\ufe0f\U0001f447\U0001f449:\u2013\u2014-]+$")
_LETTER = re.compile(r"[A-Za-zА-Яа-яЁё]")
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
FALLBACK_TITLE = "LinkedIn post"


@dataclass(frozen=True)
class Page:
    """Страница LinkedIn: раздел адреса (company, showcase, in) и имя страницы в адресе."""

    section: str
    slug: str

    @property
    def kind(self) -> str:
        return _SECTIONS[self.section]

    @property
    def url(self) -> str:
        return f"https://{HOST}/{self.section}/{self.slug}/"


@dataclass(frozen=True)
class Post:
    """Пост, разобранный в статью. `seen_at`: когда пост появился на странице (у репоста время репоста), по нему
    сбор спрашивает сервис только о более новых постах. `image_source`: адрес картинки у LinkedIn, если она есть."""

    url: str
    title: str
    text: str
    published_at: datetime | None
    seen_at: datetime | None
    language: str
    image_source: str | None

    def record(self, source_id: int, image_url: str | None = None) -> dict[str, Any]:
        """Запись статьи для repository.insert_article. Текст поста целиком: дозагружать нечего."""
        return {
            "source_id": source_id,
            "title": self.title[:500],
            "url": self.url,
            "published_at": self.published_at,
            "raw_text": self.text,
            "text_truncated": False,
            "language": self.language,
            "content_hash": normalize.compute_content_hash(self.title, self.url),
            "image_url": image_url,
        }


def page_from_url(raw: str | None) -> Page | None:
    """Страница из адреса: `https://uk.linkedin.com/company/acme/posts/?lipi=…` и `linkedin.com/in/jane-doe` дают
    страницу; адрес не LinkedIn, поста или поиска даёт None."""
    value = (raw or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return None
    chunks = [chunk for chunk in parts.path.split("/") if chunk]
    if len(chunks) < 2 or chunks[0] not in _SECTIONS:
        return None
    slug = chunks[1]
    return Page(chunks[0], slug) if _SLUG_RE.fullmatch(slug) else None


def post_url(raw: Any) -> str | None:
    """Адрес поста без хвостов: `uk.linkedin.com/posts/…?utm…` и `www.linkedin.com/posts/…` это один пост."""
    parts = urlsplit(str(raw or "").strip())
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return None
    path = parts.path.rstrip("/")
    if not path.startswith(_POST_PATHS):
        return None
    return f"https://{HOST}{path}"


def clean_text(text: Any) -> str:
    """Пробелы, пустые строки и NFKC: «жирный» и «курсив» постов LinkedIn набирают математическими буквами Юникода
    (𝗔 вместо A), а для предфильтра, «Дедупа» и модели это не слова. NFKC делает их обычными буквами, неразрывный
    пробел обычным пробелом."""
    text = unicodedata.normalize("NFKC", str(text or ""))
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def post_title(text: str) -> str:
    """Первое предложение поста без ссылок: «… >> https://lnkd.in/…» это приглашение перейти, а не смысл."""
    lines = [_TRAILER.sub("", _URL.sub("", line)).strip() for line in (text or "").splitlines()]
    title = title_from_text("\n".join(line for line in lines if line))
    return title if title != "Telegram post" else FALLBACK_TITLE


def guess_language(text: str) -> str:
    letters = _LETTER.findall(text or "")
    if not letters:
        return "en"
    return "ru" if len(_CYRILLIC.findall(text)) / len(letters) > 0.3 else "en"


def parse_post(item: dict[str, Any]) -> Post | None:
    """Пост сервиса в статью; пост без адреса или без текста: None."""
    if not isinstance(item, dict):
        return None
    url = post_url(item.get("linkedinUrl")) or post_url(item.get("shareLinkedinUrl"))
    own = clean_text(item.get("content"))
    text = _full_text(item, own)
    if not url or not text:
        return None
    article = _dict(item.get("article"))
    title = post_title(own) if own else clean_text(article.get("title")).replace("\n", " ") or FALLBACK_TITLE
    published = _when(item.get("postedAt"))
    reposted = _when(item.get("repostedAt"))
    return Post(url=url, title=title, text=text, published_at=published, seen_at=reposted or published,
                language=guess_language(text), image_source=_first_image(item))


def _full_text(item: dict[str, Any], own: str) -> str:
    """Текст статьи: пост, ниже процитированный пост с автором, заголовок и описание статьи LinkedIn, название
    документа. Без процитированного «Отбор» и суть видели бы одну строку «Горжусь этим!»."""
    parts = [own]
    quoted = _dict(item.get("repost"))
    quoted_text = clean_text(quoted.get("content"))
    if quoted_text:
        author = clean_text(_dict(quoted.get("author")).get("name")).replace("\n", " ")
        parts.append((f"Quoted post by {author}:" if author else "Quoted post:") + "\n" + quoted_text)
    article = _dict(item.get("article"))
    document = _dict(item.get("document"))
    extras = [article.get("title"), article.get("description"), document.get("title")]
    for extra in (clean_text(value) for value in extras):
        if extra and extra not in "\n\n".join(parts):
            parts.append(extra)
    return "\n\n".join(part for part in parts if part)


def _first_image(item: dict[str, Any]) -> str | None:
    """Первая картинка поста: своя, обложка статьи LinkedIn, кадр видео, обложка документа."""
    images = item.get("postImages") if isinstance(item.get("postImages"), list) else []
    article = _dict(item.get("article"))
    video = _dict(item.get("postVideo"))
    covers = _dict(item.get("document")).get("coverPages")
    cover_urls = _dict(covers[0]).get("imageUrls") if isinstance(covers, list) and covers else None
    candidates = [
        _dict(images[0]).get("url") if images else None,
        _dict(article.get("image")).get("url"),
        video.get("thumbnailUrl"),
        cover_urls[0] if isinstance(cover_urls, list) and cover_urls else None,
    ]
    return next((str(url) for url in candidates if isinstance(url, str) and url.startswith("https://")), None)


def _when(value: Any) -> datetime | None:
    """Время поста: ISO в `date`, иначе миллисекунды в `timestamp`; всегда в UTC."""
    value = _dict(value)
    parsed = dates.as_utc(value.get("date"))
    if parsed is not None:
        return parsed
    stamp = value.get("timestamp")
    if isinstance(stamp, (int, float)) and stamp > 0:
        return datetime.fromtimestamp(stamp / 1000, tz=timezone.utc)
    return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
