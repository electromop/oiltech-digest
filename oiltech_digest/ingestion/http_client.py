"""HTTP GET с ретраями, экспоненциальным backoff и SSL-fallback.

Перенесено и отревизировано из прототипа `oil-tech-digest-bot/parser.py`
(`_fetch_with_retry`, `_DEFAULT_HEADERS`). Решения ревизии:
  - вынесено в отдельный модуль (единая ответственность), параметры — из config;
  - браузерный User-Agent сохранён (без него многие источники отдают 403);
  - SSL-fallback `verify=False` оставлен ТОЛЬКО как запасной путь при SSLError
    и с явным предупреждением в лог — НЕ дефолт. Нужен для рунет-серверов с
    неполной цепочкой сертификатов на macOS (Python.framework без полного CA-bundle).
    RSS — публичный контент, риск ограниченный, но факт обхода логируется.
"""

from __future__ import annotations

import logging
import time

import requests

from oiltech_digest.config import REQUEST_TIMEOUT, RETRY_ATTEMPTS, RETRY_BACKOFF_BASE

logger = logging.getLogger(__name__)

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36 OilTechDigest/1.0"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/html;q=0.8, */*;q=0.7",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
}


def fetch(url: str, timeout: int = REQUEST_TIMEOUT) -> bytes | None:
    """GET с ретраями. Возвращает тело ответа или None при окончательной неудаче."""
    last_err: Exception | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = requests.get(
                url, timeout=timeout, headers=_DEFAULT_HEADERS, allow_redirects=True
            )
            resp.raise_for_status()
            return resp.content
        except requests.exceptions.SSLError as e:
            last_err = e
            content = _fetch_insecure(url, timeout)
            if content is not None:
                return content
            break  # SSL-проблема не лечится повтором
        except requests.RequestException as e:
            last_err = e
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))
    logger.warning("HTTP %s — отказ после %d попыток: %s", url, RETRY_ATTEMPTS, last_err)
    return None


def probe(url: str, timeout: int = 10) -> bytes | None:
    """Одноразовый GET без ретраев — для зондирования кандидатов RSS при автообнаружении.

    404/таймаут на несуществующем пути — ожидаемая ситуация, поэтому здесь НЕ нужны
    ретраи с backoff (иначе каждый промах висел бы секундами). Тихо возвращает None
    при любой ошибке; при SSLError пробует один раз verify=False (рунет на macOS).
    """
    try:
        resp = requests.get(
            url, timeout=timeout, headers=_DEFAULT_HEADERS, allow_redirects=True
        )
        resp.raise_for_status()
        return resp.content
    except requests.exceptions.SSLError:
        return _fetch_insecure(url, timeout)
    except requests.RequestException:
        return None


def _fetch_insecure(url: str, timeout: int) -> bytes | None:
    """Запасной GET без проверки сертификата (только при SSLError)."""
    try:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = requests.get(
            url, timeout=timeout, headers=_DEFAULT_HEADERS,
            allow_redirects=True, verify=False,
        )
        resp.raise_for_status()
        logger.warning("HTTP %s — SSL обойдён через verify=False (fallback, небезопасно)", url)
        return resp.content
    except requests.RequestException as e:
        logger.warning("HTTP %s — SSL-fallback не удался: %s", url, e)
        return None
