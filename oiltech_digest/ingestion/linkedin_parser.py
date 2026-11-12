"""Публичная страница компании в LinkedIn: посты без входа и без API.

`linkedin.com/company/<имя>/` без входа отдаёт последние 10 постов компании и в вёрстке, и
в разметке schema.org (`<script type="application/ld+json">`, `SocialMediaPosting`): полный
текст, точная дата, адрес поста, автор. Берём только разметку: она стабильнее вёрстки, и
открывать посты по одному незачем, один заход на компанию в сутки (замер 12.11: у
Halliburton и Baker Hughes 10 постов: две недели ленты).

Только с NL (`network_region = 'external'`): из РФ LinkedIn закрыт с 2016 года. Частые
заходы с одного адреса LinkedIn закрывает (12.11: шестой заход за две минуты: стена входа
с кодом 999, следующий: проверка Cloudflare), поэтому:
- ядро ставит в очередь не больше LINKEDIN_SOURCES_PER_CYCLE компаний за цикл, каждую не
  чаще LINKEDIN_MIN_INTERVAL_HOURS (`due_sources`), и не подряд, а через
  LINKEDIN_SPACING_MINUTES (`run_after` задачи);
- стена входа или проверка: не «новостей нет», а `blocked:…` в итоге, и ядро ставит всем
  компаниям паузу LINKEDIN_BLOCK_COOLDOWN_HOURS (`external_fetch.apply_scrape_result`); задачи,
  поставленные до блока, ядро на паузе не выдаёт (`build_scrape_source_payload`).
Аккаунт и куки не используются никогда.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any
import unicodedata
from urllib.parse import urlsplit

from lxml import etree, html

from oiltech_digest.ingestion import dates, normalize
from oiltech_digest.ingestion.telegram_parser import title_from_text

STRATEGY = "linkedin"
HOST = "www.linkedin.com"

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_.%]{0,99}$")
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_URL = re.compile(r"https?://\S+")
_LETTER = re.compile(r"[A-Za-zА-Яа-яЁё]")


@dataclass(frozen=True)
class LinkedInPost:
    url: str
    title: str
    text: str
    published_at: datetime | None


def company_slug(raw: str | None) -> str | None:
    """Имя компании из адреса (`https://uk.linkedin.com/company/slbglobal/posts`) или само имя."""
    value = (raw or "").strip()
    if not value:
        return None
    if "/" not in value and "." not in value:
        return value if _SLUG_RE.fullmatch(value) else None
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    host = (parts.netloc or "").lower()
    if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return None
    chunks = [chunk for chunk in parts.path.split("/") if chunk]
    if len(chunks) < 2 or chunks[0] not in {"company", "showcase"}:
        return None
    return chunks[1] if _SLUG_RE.fullmatch(chunks[1]) else None


def company_url(raw: str | None) -> str | None:
    slug = company_slug(raw)
    return f"https://{HOST}/company/{slug}/" if slug else None


def page_status(content: bytes | str | None) -> str:
    """Что отдал LinkedIn: ok, или blocked:authwall / blocked:challenge, или empty.

    Стена входа приходит с кодом 999, а не 403: общий рендер (playwright_parser) её не
    считает блоком и отдаёт страницу. Конечный адрес рендер вписывает в `<base href>`."""
    if not content:
        return "empty"
    text = content.decode("utf-8", "ignore") if isinstance(content, bytes) else content
    head = text[:30000]
    if "/authwall" in head or re.search(r"<title>\s*Sign Up \| LinkedIn", head):
        return "blocked:authwall"
    if re.search(r"<title>\s*Just a moment", head) or "challenges.cloudflare.com" in head:
        return "blocked:challenge"
    return "ok"


def extract_posts(content: bytes | str | None, slug: str | None = None) -> list[LinkedInPost]:
    """Посты компании из разметки schema.org, от новых к старым.

    Чужой автор (репост другой компании или сотрудника) не берётся: он не новость этой
    компании, а его исходник, если он нам нужен, приходит своим источником."""
    if not content:
        return []
    if isinstance(content, bytes):
        # Рендер отдаёт UTF-8 байтами, а `<meta charset>` у страницы нет: lxml без строки читал
        # бы их как latin-1 («Tovar’s» → «Tovarâs»).
        content = content.decode("utf-8", "replace")
    try:
        doc = html.fromstring(content)
    except (ValueError, TypeError, etree.ParserError):
        return []
    posts: dict[str, LinkedInPost] = {}
    for script in doc.xpath('//script[@type="application/ld+json"]'):
        for node in _graph(script.text_content()):
            post = _post_from_node(node, slug)
            if post is not None and post.url not in posts:
                posts[post.url] = post
    ordered = sorted(posts.values(), key=lambda post: post.published_at or datetime.min.replace(tzinfo=timezone.utc),
                     reverse=True)
    return ordered


def post_record(post: LinkedInPost) -> dict[str, Any]:
    """Пост: запись статьи для ядра (без source_id: его ставит ядро)."""
    return {
        "title": post.title[:500],
        "url": post.url,
        "published_at": post.published_at,
        "raw_text": post.text,
        # Текст поста полный: дозагрузка со страницы поста ничего не добавит.
        "text_truncated": False,
        "language": guess_language(post.text),
        "content_hash": normalize.compute_content_hash(post.title, post.url),
    }


def listing_hash(posts: list[LinkedInPost]) -> str | None:
    if not posts:
        return None
    return hashlib.sha256("\n".join(post.url for post in posts).encode("utf-8")).hexdigest()


def due_sources(sources: list[dict[str, Any]], *, now: datetime, interval_hours: float,
                per_cycle: int) -> list[dict[str, Any]]:
    """Какие компании ставить в этот цикл: без паузы после блока, не заходившие
    interval_hours, давние первыми, не больше per_cycle."""
    def last(source: dict[str, Any]) -> datetime:
        return dates.as_utc(source.get("last_parsed_at")) or datetime.min.replace(tzinfo=timezone.utc)

    ready = []
    for source in sources:
        paused = dates.as_utc(source.get("external_cooldown_until"))
        if paused is not None and paused > now:
            continue
        if (now - last(source)).total_seconds() < interval_hours * 3600:
            continue
        ready.append(source)
    ready.sort(key=lambda source: (last(source), int(source.get("id") or 0)))
    return ready[:max(0, per_cycle)]


def guess_language(text: str) -> str:
    letters = _LETTER.findall(text or "")
    if not letters:
        return "en"
    return "ru" if len(_CYRILLIC.findall(text)) / len(letters) > 0.3 else "en"


def _graph(raw: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw or "")
    except ValueError:
        return []
    items = data if isinstance(data, list) else [data]
    nodes: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        graph = item.get("@graph")
        nodes.extend(node for node in (graph if isinstance(graph, list) else [item]) if isinstance(node, dict))
    return nodes


def _post_from_node(node: dict[str, Any], slug: str | None) -> LinkedInPost | None:
    if node.get("@type") != "SocialMediaPosting":
        return None
    url = _post_url(node.get("url") or node.get("mainEntityOfPage"))
    text = _clean_text(node.get("text") or node.get("articleBody") or "")
    if not url or not text:
        return None
    author = node.get("author") if isinstance(node.get("author"), dict) else {}
    if slug and company_slug(str(author.get("url") or "")) not in {None, slug}:
        return None
    headline = _clean_text(str(node.get("headline") or "")).replace("\n", " ")
    title = headline if len(headline) >= 15 else _title(text)
    return LinkedInPost(url=url, title=title, text=text, published_at=dates.as_utc(node.get("datePublished")))


def _title(text: str) -> str:
    """Заголовок: первое предложение поста, без ссылок (`… >> https://lnkd.in/…`): они не
    смысл, а приглашение перейти."""
    lines = [_URL.sub("", line) for line in text.splitlines()]
    lines = [re.sub(r"[\s>»→⬇⬆️👉:–—-]+$", "", line).strip() for line in lines]
    title = title_from_text("\n".join(line for line in lines if line))
    return title if title != "Telegram post" else "LinkedIn post"


def _post_url(raw: Any) -> str | None:
    """Адрес поста без хвостов: `uk.linkedin.com/posts/…?utm…` и `www…` — один пост."""
    if isinstance(raw, dict):
        raw = raw.get("@id") or raw.get("url")
    parts = urlsplit(str(raw or "").strip())
    host = (parts.netloc or "").lower()
    if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return None
    path = parts.path.rstrip("/")
    if not (path.startswith("/posts/") or path.startswith("/feed/update/") or path.startswith("/pulse/")):
        return None
    return f"https://{HOST}{path}"


def _clean_text(text: str) -> str:
    """Пробелы и пустые строки, и NFKC: «жирный» и «курсив» постов LinkedIn набирают математическими буквами
    Юникода (Welltec 12.11: «𝗔 𝗴𝗿𝗼𝘂𝗻𝗱𝗯𝗿𝗲𝗮𝗸𝗶𝗻𝗴 𝘀𝗼𝗹𝘂𝘁𝗶𝗼𝗻»), а для предфильтра, «Дедупа» и модели это не слова.
    Неразрывный пробел NFKC делает обычным."""
    text = unicodedata.normalize("NFKC", str(text))
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()
