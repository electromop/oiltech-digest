"""Текст страницы издателя для находки радара, когда быстрый запрос его не дал (поручение владельца 04.11).

При «Отборе» находка без текста издателя во вход агента не идёт и уходит в «Отклонённые → Поиск». Прогон 51351
(04.11, 13 тем): из 52 ссылок, которые подтвердил поиск, 25 не открылись с NL: отказ 403, молчание сайтов для
инвесторов, PDF, страницы на JavaScript, сайты .ru, которые зарубежному адресу не отвечают. Порядок цикла Михаила
04.11: поиск, добор на NL, сбор на РФ, дедуп прогона и только потом «Отбор». Здесь добор и сбор одной ссылки (read),
по порядку цены:

1. Запрос с NL (probe_url, как раньше); PDF теперь читается: текст первых страниц через pypdf.
2. Ядро РФ (download по просьбе воркера, адрес /api/external-worker/page): сайты, которые не пускают зарубежные адреса.
3. Браузер на NL: страницы на JavaScript и сайты, которые отказывают запросу, но не браузеру; оболочка без текста —
   ещё одна попытка до полной загрузки страницы.

Страница проверки браузера («Проверяем браузер», «Just a moment») — не текст издателя. Правило владельца не меняется:
во вход «Отбора» идёт только текст страницы издателя, пересказ поиска им не считается.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import io
import ipaddress
import logging
import re
import socket
import ssl
import time
from typing import Any, Callable, Iterator
from urllib.parse import urljoin, urlsplit

from oiltech_digest import config

logger = logging.getLogger(__name__)

# Документы, которые не читаем: текст из их байтов ушёл бы дальше мусором. PDF читается (pdf_text).
NOT_PAGE_SUFFIXES = (".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip")
BINARY_PREFIXES = (b"%PDF", b"PK\x03\x04", b"\xd0\xcf\x11\xe0", b"\x89PNG", b"\xff\xd8\xff", b"GIF8")
# Сигнатура PDF — в первом килобайте, как у разбора документов (documents/parsing.py): бывает мусорный префикс.
_PDF_HEADER_WINDOW = 1024
# Текст PDF — с первых страниц: вход «Отбора» до 6 000 знаков, а у выпуска Federal Register 294 страницы (04.11).
PDF_PAGES = 30
PDF_TEXT_CHARS = 20_000

# Проверка браузера вместо страницы: 04.11 scientica.ru отдала NL «Проверяем браузер», и находка ушла в «Отклонённые»
# как страница-список. По тексту — только у короткой страницы: статья может и сама говорить о такой проверке.
_BOT_CHECK_TITLE_RE = re.compile(r"^\s*(just a moment|attention required|checking your browser|please wait|ddos-guard"
                                 r"|access denied|проверяем браузер|проверка браузера|один момент)", re.IGNORECASE)
_BOT_CHECK_TEXT_RE = re.compile(r"checking your browser|enable javascript and cookies to continue|ddos protection by"
                                r"|проверяем браузер|проверка браузера", re.IGNORECASE)
BOT_CHECK_TEXT_CHARS = 1500

# Браузер: сначала как у сбора (готовность разметки и пауза), затем — до полной загрузки страницы. wam.ae 04.11 к
# domcontentloaded (~28 с) отдавал оболочку «WAM» в 197 знаков, к load (~43 с) — новость в 2 891 знак.
BROWSER_FIRST = {"timeout_ms": 30_000, "settle_ms": 3500}
BROWSER_AGAIN = {"timeout_ms": 60_000, "wait_until": "load", "settle_ms": 6000}
BROWSER_PAGE_CHARS = 500

# Сбор на РФ (download): срок соединения и чтения и общий срок. Общий меньше срока ответа воркеру
# (CORE_ANSWER_SECONDS): ядро успевает сказать «не открылась», а не обрывается на полуслове.
CORE_TIMEOUT_SECONDS = 20.0
CORE_DEADLINE_SECONDS = 45.0
CORE_ANSWER_SECONDS = CORE_DEADLINE_SECONDS + 15
REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Сбор на РФ на время задачи воркера NL: тот же клиент, что берёт и сдаёт задачу (external_worker._run_job).
CORE_FETCH: ContextVar[Callable[[str], dict[str, Any]] | None] = ContextVar("publisher_page_core_fetch", default=None)


@contextmanager
def core_pages(client: Any) -> Iterator[None]:
    token = CORE_FETCH.set(getattr(client, "fetch_page", None))
    try:
        yield
    finally:
        CORE_FETCH.reset(token)


def read(url: str, fallback_title: str = "", *, timeout: float, weak_title: Callable[[str], bool]) -> dict[str, Any]:
    """Текст страницы издателя: запрос с NL, ядро РФ, браузер — до первой страницы с текстом и заголовком публикации.

    Страница с текстом, но с заголовком страницы-списка (weak_title, правило поиска) — запасной ответ: дальше ищется
    лучшая, а не нашлась — отдаётся она, и её отсеет проверка поиска, как раньше. Ядро — только у задачи воркера NL
    (core_pages): на ядре запрос и так идёт с РФ. Браузер не открывает PDF (Chromium его скачивает, а не рисует).
    Итог — как у прежней докачки, плюс fetcher (request, core, browser); ошибки шагов — в error."""
    errors: list[str] = []
    weak: dict[str, Any] | None = None
    pdf_url = urlsplit(url).path.lower().endswith(".pdf")
    got_pdf = False
    steps = (("request", lambda: _by_request(url, fallback_title, timeout)),
             ("core", lambda: _by_core(url, fallback_title)),
             ("browser", lambda: _by_browser(url, fallback_title, weak_title)))
    for fetcher, step in steps:
        if fetcher == "core" and (got_pdf or CORE_FETCH.get() is None):
            continue
        if fetcher == "browser" and (got_pdf or pdf_url or not config.SIGNAL_PAGE_BROWSER_ENABLED):
            continue
        try:
            result = step()
        except Exception as exc:  # noqa: BLE001 - сбой шага не роняет тему: дальше следующий шаг
            result = _failed(f"{type(exc).__name__}: {str(exc)[:160]}")
        got_pdf = got_pdf or bool(result.get("pdf"))
        if not has_text(result):
            errors.append(f"{fetcher}: {result.get('error') or _too_short(result)}")
            continue
        result = {**result, "fetcher": fetcher}
        if not weak_title(str(result.get("title") or "")):
            return result
        weak = weak or result
        errors.append(f"{fetcher}: заголовок страницы-списка")
    return weak or _failed("; ".join(errors) or "нечем открыть")


def has_text(result: dict[str, Any]) -> bool:
    return bool(result.get("ok")) and _chars(result) >= config.MIN_ARTICLE_TEXT_CHARS


def _chars(result: dict[str, Any]) -> int:
    return len(" ".join(str(result.get("raw_text") or "").split()))


def page_text(content: bytes | str, fallback_title: str = "") -> dict[str, Any]:
    """Заголовок, дата и текст уже скачанной страницы: HTML — разбором статьи, PDF — текстом первых страниц.

    Заголовок PDF — заголовок находки: в свойствах файла чаще имя исходника («Microsoft Word - 2026-22396.docx»), чем
    название публикации; так же разбор HTML берёт заголовок находки, когда у страницы нет своего."""
    from oiltech_digest.ingestion.article_page import parse_article_page

    raw = content if isinstance(content, bytes) else content.encode("utf-8", "ignore")
    if is_pdf(raw):
        try:
            text = pdf_text(raw)
        except Exception as exc:  # noqa: BLE001 - pypdf кидает десяток разных классов на битом файле
            return {**_failed(f"pdf: {type(exc).__name__}"), "pdf": True}
        return {"ok": True, "error": None, "raw_text": text, "published_at": None, "title": fallback_title,
                "pdf": True}
    if looks_binary(raw):
        return _failed("not_html")
    title, published_at, raw_text = parse_article_page(content, fallback_title)
    if is_bot_check(title, raw_text):
        return _failed("проверка браузера вместо страницы")
    return {"ok": True, "error": None, "raw_text": raw_text or "", "published_at": published_at, "title": title or ""}


def is_pdf(content: bytes) -> bool:
    return b"%PDF-" in content[:_PDF_HEADER_WINDOW]


def looks_binary(content: bytes | str) -> bool:
    head = content[:1024] if isinstance(content, bytes) else content[:1024].encode("utf-8", "ignore")
    return head.lstrip().startswith(BINARY_PREFIXES) or b"\x00" in head


def pdf_text(content: bytes) -> str:
    """Текст первых страниц PDF (до PDF_PAGES страниц и PDF_TEXT_CHARS знаков). Битая страница не роняет документ;
    PDF под пустым паролем (запрет печати) читается, под настоящим — ошибка."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content), strict=False)
    if reader.is_encrypted:
        try:
            opened = reader.decrypt("")
        except Exception:  # noqa: BLE001 - pypdf кидает разное на битой криптографии
            opened = 0
        if not opened:
            raise ValueError("PDF под паролем")
    parts: list[str] = []
    size = 0
    for number in range(min(len(reader.pages), PDF_PAGES)):
        try:
            text = " ".join((reader.pages[number].extract_text() or "").split())
        except Exception:  # noqa: BLE001
            text = ""
        if text:
            parts.append(text)
            size += len(text) + 1
        if size >= PDF_TEXT_CHARS:
            break
    return " ".join(parts)[:PDF_TEXT_CHARS]


def is_bot_check(title: str, text: str) -> bool:
    if _BOT_CHECK_TITLE_RE.search(title or ""):
        return True
    return len(text or "") < BOT_CHECK_TEXT_CHARS and bool(_BOT_CHECK_TEXT_RE.search(text or ""))


def _by_request(url: str, fallback_title: str, timeout: float) -> dict[str, Any]:
    from oiltech_digest.ingestion.source_diagnostics import probe_url

    if urlsplit(url).path.lower().endswith(NOT_PAGE_SUFFIXES):
        return _failed("not_html")
    probe, content = probe_url(url, timeout=timeout)
    if content is None:
        return _failed(probe.error or f"http_{probe.status}")
    return page_text(content, fallback_title)


def _by_core(url: str, fallback_title: str) -> dict[str, Any]:
    answer = CORE_FETCH.get()(url)
    if not answer.get("ok"):
        return _failed(str(answer.get("error") or f"http_{answer.get('status')}"))
    return page_text(answer.get("content") or b"", fallback_title)


def _by_browser(url: str, fallback_title: str, weak_title: Callable[[str], bool]) -> dict[str, Any]:
    """Две попытки браузера, итог — лучшая: с текстом, с заголовком публикации, длиннее. Вторая — если первая дала
    оболочку: без текста, с заголовком страницы-списка или короче BROWSER_PAGE_CHARS (у оболочки без своего
    заголовка разбор берёт заголовок находки, и 200 знаков баннера о cookie прошли бы порог текста)."""
    from oiltech_digest.ingestion import playwright_parser

    def rank(result: dict[str, Any]) -> tuple[bool, bool, int]:
        return has_text(result), not weak_title(str(result.get("title") or "")), _chars(result)

    best: dict[str, Any] | None = None
    for attempt in (BROWSER_FIRST, BROWSER_AGAIN):
        content = playwright_parser.fetch_rendered(url, **attempt)
        if content is None:
            # Отказ (403) повтором не лечится, а срок первой попытки вторая, более долгая, только повторит.
            break
        result = page_text(content, fallback_title)
        if best is None or rank(result) > rank(best):
            best = result
        if rank(best)[:2] == (True, True) and _chars(best) >= BROWSER_PAGE_CHARS:
            break
    return best or _failed(playwright_parser.last_fetch_status() or "error")


def _failed(error: str) -> dict[str, Any]:
    return {"ok": False, "error": error, "raw_text": "", "published_at": None, "title": ""}


def _too_short(result: dict[str, Any]) -> str:
    return f"короткий текст ({_chars(result)} знаков)"


# --- Сбор на РФ: ядро качает страницу по просьбе воркера NL ---------------------------------------------------


class PageRefused(ValueError):
    """Адрес, который ядро по просьбе воркера не качает: не http(s), с логином, внутренний, не разрешается, велик."""


@dataclass(frozen=True)
class _Target:
    scheme: str
    host: str
    port: int
    ip: str
    path: str
    host_header: str


def download(url: str) -> dict[str, Any]:
    """Ядро РФ: скачать страницу по просьбе воркера NL (сбор на РФ). Только http(s) и только публичные адреса.

    Имя сайта разрешается здесь, и все его адреса обязаны быть публичными (ipaddress.is_global): иначе токен воркера
    открыл бы внутреннюю сеть ядра. Соединение идёт на проверенный адрес, имя сайта — в SNI и в проверке сертификата,
    а не во втором разрешении имени: подмена DNS после проверки ничего не даёт. Переадресации — вручную, каждая
    проверяется заново; размер — до SIGNAL_PAGE_MAX_BYTES. Сертификат — как у сбора (http_client): certifi, затем
    набор с недостающими промежуточными, затем без проверки (российские сайты на «Российском CA»)."""
    deadline = time.monotonic() + CORE_DEADLINE_SECONDS
    current = url
    try:
        for _ in range(REDIRECTS + 1):
            target = _public_target(current)
            response = _get(target, timeout=CORE_TIMEOUT_SECONDS)
            location = response.headers.get("Location") if response.status in _REDIRECT_STATUSES else None
            if location:
                response.release_conn()
                current = urljoin(current, location)
                continue
            if response.status >= 400:
                response.release_conn()
                return {"ok": False, "status": response.status, "error": f"http_{response.status}",
                        "final_url": current}
            content = _read(response, deadline=deadline)
            return {"ok": True, "status": response.status, "error": None, "final_url": current, "content": content,
                    "content_type": response.headers.get("Content-Type")}
        return {"ok": False, "status": None, "error": "больше пяти переадресаций", "final_url": current}
    except PageRefused as exc:
        return {"ok": False, "status": None, "error": f"ядро не качает: {exc}", "final_url": current}
    except Exception as exc:  # noqa: BLE001 - сбой сети ядра — ответ воркеру, а не 500
        return {"ok": False, "status": None, "error": f"{type(exc).__name__}: {str(exc)[:160]}", "final_url": current}


def _public_target(url: str) -> _Target:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise PageRefused("только http и https")
    if parts.username or parts.password:
        raise PageRefused("логин в адресе")
    if not parts.hostname:
        raise PageRefused("нет имени сайта")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise PageRefused("неверный порт") from None
    addresses = _resolve(parts.hostname, port)
    if not addresses:
        raise PageRefused("имя сайта не разрешается")
    inner = next((address for address in addresses if not _is_public(address)), None)
    if inner is not None:
        raise PageRefused(f"внутренний адрес {inner}")
    # IPv4 первым: у ядра может не быть маршрута IPv6.
    ip = min(addresses, key=lambda address: ipaddress.ip_address(address).version)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return _Target(scheme, parts.hostname, port, ip, path, parts.netloc.rsplit("@", 1)[-1])


def _resolve(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        return []
    return sorted({str(info[4][0]) for info in infos})


def _is_public(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:  # адрес с зоной (fe80::1%eth0) — только внутренний
        return False
    return parsed.is_global and not parsed.is_multicast


def _get(target: _Target, *, timeout: float) -> Any:
    """GET на проверенный адрес без переадресаций и повторов; ответ не дочитан (preload_content=False)."""
    import certifi
    import urllib3

    from oiltech_digest.ingestion.http_client import _DEFAULT_HEADERS, _extended_ca_bundle

    headers = {**_DEFAULT_HEADERS, "Host": target.host_header, "Accept-Encoding": "gzip, deflate"}
    limits = urllib3.Timeout(connect=timeout, read=timeout)
    request = {"headers": headers, "redirect": False, "retries": False, "preload_content": False}
    if target.scheme == "http":
        return urllib3.HTTPConnectionPool(target.ip, target.port, timeout=limits, retries=False).urlopen(
            "GET", target.path, **request)
    for bundle in filter(None, (certifi.where(), _extended_ca_bundle())):
        pool = urllib3.HTTPSConnectionPool(target.ip, target.port, timeout=limits, retries=False,
                                           server_hostname=target.host, cert_reqs="CERT_REQUIRED", ca_certs=bundle,
                                           assert_hostname=target.host)
        try:
            return pool.urlopen("GET", target.path, **request)
        except (urllib3.exceptions.SSLError, ssl.SSLError) as exc:
            logger.info("page %s — сертификат не проверен (%s), следующий набор", target.host, str(exc)[:120])
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    pool = urllib3.HTTPSConnectionPool(target.ip, target.port, timeout=limits, retries=False,
                                       server_hostname=target.host, cert_reqs="CERT_NONE", assert_hostname=False)
    return pool.urlopen("GET", target.path, **request)


def _read(response: Any, *, deadline: float) -> bytes:
    limit = config.SIGNAL_PAGE_MAX_BYTES
    body = bytearray()
    try:
        # Разжатие — явно: без decode_content=True stream отдавал сжатые gzip байты (замер на ядре 04.11: wam.ae,
        # scientica.ru, aer.ca, ovintiv — 200, а разбор видел «не HTML»). Предел — по разжатому.
        for chunk in response.stream(64 * 1024, decode_content=True):
            body += chunk
            if len(body) > limit:
                raise PageRefused(f"страница больше {limit // (1024 * 1024)} МБ")
            if time.monotonic() > deadline:
                raise TimeoutError(f"не дочитана за {CORE_DEADLINE_SECONDS:.0f} с")
    finally:
        response.release_conn()
    return bytes(body)
