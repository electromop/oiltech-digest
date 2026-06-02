"""HTTP client with retries, per-host pacing, and soft cooldowns after blocks.

The goal is reliability from a server environment without looking overly aggressive:
  - thread-local sessions for connection reuse;
  - minimum interval + small jitter per host;
  - respect Retry-After for 429/503 when present;
  - temporary cooldown for hosts returning 403/429 repeatedly;
  - SSL fallback only for certificate failures.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from urllib.parse import urlsplit

import requests

from oiltech_digest.config import (
    HTTP_BLOCK_COOLDOWN_SECONDS,
    HTTP_JITTER_SECONDS,
    HTTP_MIN_INTERVAL_SECONDS,
    PROXY_HOST_OVERRIDES,
    PROXY_TIMEOUT,
    PROXY_URL,
    REQUEST_TIMEOUT,
    RETRY_ATTEMPTS,
    RETRY_BACKOFF_BASE,
)

logger = logging.getLogger(__name__)

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36 OilTechDigest/1.0"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/html;q=0.8, */*;q=0.7",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
}

_thread_local = threading.local()
_host_lock = threading.Lock()
_host_next_allowed: dict[str, float] = {}
_host_cooldown_until: dict[str, float] = {}


def fetch(url: str, timeout: int = REQUEST_TIMEOUT) -> bytes | None:
    """GET with retries, soft pacing and cooldown handling."""
    return _request(url, timeout=timeout, quiet=False, retries=RETRY_ATTEMPTS)


def probe(url: str, timeout: int = 10) -> bytes | None:
    """Single-pass GET for RSS discovery and candidate testing."""
    return _request(url, timeout=timeout, quiet=True, retries=1)


def _request(url: str, timeout: int, quiet: bool, retries: int) -> bytes | None:
    host = _host(url)
    if _is_host_cooling_down(host):
        logger.debug("HTTP %s — host cooldown active, skip", url)
        return None

    proxies = _proxy_for(host)
    if proxies:
        timeout = max(timeout, PROXY_TIMEOUT)
        _maybe_log_proxy(proxies)

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        _wait_for_host_slot(host)
        try:
            resp = _get_session().get(
                url,
                timeout=timeout,
                headers=_DEFAULT_HEADERS,
                allow_redirects=True,
                proxies=proxies,
            )
            if resp.status_code in {403, 429, 503}:
                _register_block(host, resp)
                # 403/429: хост сам попросил паузу — cooldown уже выставлен, поэтому
                # повторять в этом же запросе бессмысленно (иначе следующая попытка
                # залипнет в _wait_for_host_slot на весь cooldown). 503 (временная
                # ошибка сервера) — оставляем на обычные ретраи.
                if resp.status_code in {403, 429}:
                    return None
            resp.raise_for_status()
            return resp.content
        except requests.exceptions.SSLError as exc:
            last_err = exc
            content = _fetch_insecure(url, timeout, quiet=quiet, proxies=proxies)
            if content is not None:
                return content
            break
        except requests.RequestException as exc:
            last_err = exc
            if attempt < retries:
                time.sleep(_retry_delay(attempt))

    if quiet:
        logger.debug("HTTP %s — отказ после %d попыток: %s", url, retries, last_err)
    else:
        logger.warning("HTTP %s — отказ после %d попыток: %s", url, retries, last_err)
    return None


def _fetch_insecure(
    url: str, timeout: int, quiet: bool = False, proxies: dict[str, str] | None = None
) -> bytes | None:
    """Fallback GET without certificate validation, only after SSLError."""
    try:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = _get_session().get(
            url,
            timeout=timeout,
            headers=_DEFAULT_HEADERS,
            allow_redirects=True,
            verify=False,
            proxies=proxies,
        )
        if resp.status_code in {403, 429, 503}:
            _register_block(_host(url), resp)
        resp.raise_for_status()
        logger.info("HTTP %s — SSL обойдён через verify=False (fallback)", url)
        return resp.content
    except requests.RequestException as exc:
        if quiet:
            logger.debug("HTTP %s — SSL-fallback не удался: %s", url, exc)
        else:
            logger.warning("HTTP %s — SSL-fallback не удался: %s", url, exc)
        return None


def _get_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=20)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _thread_local.session = session
    return session


def _host(url: str) -> str:
    return (urlsplit(url).netloc or "").lower()


def _proxy_for(host: str) -> dict[str, str] | None:
    """requests-style proxies mapping for a host, or None for a direct request.

    Per-host overrides win over the global PROXY_URL — this is the hook for the
    future RU/INTL routing task (PROXY_HOST_OVERRIDES is empty for now).
    """
    url = ""
    if host:
        for suffix, override in PROXY_HOST_OVERRIDES.items():
            if host == suffix or host.endswith("." + suffix):
                url = override
                break
    url = url or PROXY_URL
    if not url:
        return None
    return {"http": url, "https": url}


_proxy_logged = False


def _maybe_log_proxy(proxies: dict[str, str]) -> None:
    """Log proxy activation once per process, with credentials masked."""
    global _proxy_logged
    if not _proxy_logged:
        _proxy_logged = True
        logger.info("HTTP — запросы идут через прокси %s", _mask_proxy(next(iter(proxies.values()))))


def _mask_proxy(url: str) -> str:
    """Hide credentials in a proxy URL so it is safe to log."""
    try:
        parts = urlsplit(url)
        cred = "***@" if (parts.username or parts.password) else ""
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{cred}{parts.hostname or ''}{port}"
    except Exception:  # noqa: BLE001
        return "***"


def _wait_for_host_slot(host: str) -> None:
    if not host:
        return
    while True:
        with _host_lock:
            now = time.monotonic()
            cooldown_until = _host_cooldown_until.get(host, 0.0)
            next_allowed = _host_next_allowed.get(host, 0.0)
            target = max(cooldown_until, next_allowed)
            if target <= now:
                delay = HTTP_MIN_INTERVAL_SECONDS + random.uniform(0, HTTP_JITTER_SECONDS)
                _host_next_allowed[host] = now + delay
                return
            sleep_for = min(max(target - now, 0.0), 5.0)
        time.sleep(sleep_for)


def _is_host_cooling_down(host: str) -> bool:
    if not host:
        return False
    with _host_lock:
        return _host_cooldown_until.get(host, 0.0) > time.monotonic()


def _register_block(host: str, response: requests.Response) -> None:
    if not host:
        return
    retry_after = _retry_after_seconds(response)
    cooldown = max(retry_after, HTTP_BLOCK_COOLDOWN_SECONDS if response.status_code in {403, 429} else 120)
    with _host_lock:
        _host_cooldown_until[host] = time.monotonic() + cooldown
    logger.warning(
        "HTTP %s — host cooldown %ss after status %s",
        host,
        cooldown,
        response.status_code,
    )


def _retry_after_seconds(response: requests.Response) -> int:
    raw = response.headers.get("Retry-After")
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _retry_delay(attempt: int) -> float:
    base = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
    return base + random.uniform(0, HTTP_JITTER_SECONDS)
