"""Сбор на РФ для добора находок радара (поручение владельца 04.11): страницу, которую воркер NL не открыл, качает ядро
со своего адреса (шаг «core» в publisher_page.read).

Воркер просит ядро (ask) из своей задачи радара: адрес /api/external-worker/jobs/{id}/page с арендой задачи, как у
остальных адресов задачи, и только для прогона радара. Ядро качает (download) только http(s) и только публичные адреса:
иначе токен воркера открыл бы внутреннюю сеть ядра. Сайты любые, не только .ru: 04.11 investor.ovintiv.com не открылся
с NL, а с ядра РФ открылся (решение по замеру, разбор 28 в документах проекта). Ответ — байты страницы как есть:
разбирает их воркер тем же разбором, что и свои.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import ipaddress
import logging
import socket
import ssl
import threading
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

from oiltech_digest import config

logger = logging.getLogger(__name__)

# Срок соединения и чтения и общий срок ядра. Общий жёсткий: каждый запрос получает остаток срока, чтение по сроку
# обрывает сторож (_read). Ответ воркер ждёт дольше общего срока (ANSWER_SECONDS): ядро успевает сказать «не открылась».
TIMEOUT_SECONDS = 20.0
DEADLINE_SECONDS = 45.0
ANSWER_SECONDS = DEADLINE_SECONDS + 15
REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# Куски чтения малы: разжатый кусок не раздуется до сотен мегабайт раньше проверки предела.
_CHUNK = 16 * 1024


class PageRefused(ValueError):
    """Адрес, который ядро не качает: не http(s), с логином, внутренний, не разрешается, больше предела."""


@dataclass(frozen=True)
class _Target:
    scheme: str
    host: str
    port: int
    ip: str
    path: str
    host_header: str


def ask(client: Any, job: dict[str, Any], url: str) -> dict[str, Any]:
    """Воркер NL: попросить ядро скачать страницу в задаче job. Ядро без этого адреса (сборка до 04.11) отвечает 404:
    страницы нет, прогон идёт дальше без ядра."""
    response = client.session.post(
        f"{client.core_api_url}/api/external-worker/jobs/{job['id']}/page",
        json={"lease_token": job["lease_token"], "url": url},
        timeout=ANSWER_SECONDS,
    )
    if response.status_code == 404:
        return {"ok": False, "error": "у ядра нет сбора страниц (сборка ядра до 04.11)"}
    response.raise_for_status()
    answer = response.json() or {}
    return {**answer, "content": base64.b64decode(answer.get("content_b64") or "")}


def download(url: str) -> dict[str, Any]:
    """Ядро РФ: скачать страницу по просьбе воркера. Только http(s) и только публичные адреса.

    Имя сайта разрешается здесь, и все его адреса обязаны быть публичными (ipaddress.is_global). Соединение идёт на
    проверенный адрес, имя сайта — в SNI и в проверке сертификата, а не во втором разрешении имени: подмена DNS после
    проверки ничего не даёт. Переадресации — вручную, каждая проверяется заново; размер — до SIGNAL_PAGE_MAX_BYTES,
    по разжатому. Сертификат: certifi, затем набор с недостающими промежуточными (extra_ca.pem сбора), затем без
    проверки (российские сайты на «Российском CA»); шага legacy-TLS сбора здесь нет."""
    deadline = time.monotonic() + DEADLINE_SECONDS
    current = url
    try:
        for _ in range(REDIRECTS + 1):
            target = _public_target(current)
            response = _get(target, deadline=deadline)
            location = response.headers.get("Location") if response.status in _REDIRECT_STATUSES else None
            if location:
                response.release_conn()
                current = urljoin(current, location)
                continue
            if response.status >= 400:
                response.release_conn()
                return {"ok": False, "status": response.status, "error": f"http_{response.status}",
                        "final_url": current}
            return {"ok": True, "status": response.status, "error": None, "final_url": current,
                    "content": _read(response, deadline=deadline)}
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
    # Какой адрес внутренний, воркеру не говорим: ответ не раскрывает сеть ядра.
    if not all(_is_public(address) for address in addresses):
        raise PageRefused("адрес сайта во внутренней сети")
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


def _limits(deadline: float) -> Any:
    import urllib3

    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError(f"не уложилась в {DEADLINE_SECONDS:.0f} с")
    return urllib3.Timeout(connect=min(TIMEOUT_SECONDS, left), read=min(TIMEOUT_SECONDS, left))


def _get(target: _Target, *, deadline: float) -> Any:
    """GET на проверенный адрес без переадресаций и повторов; ответ не дочитан (preload_content=False). Каждая
    попытка (и TLS с другим набором сертификатов) получает остаток общего срока."""
    import certifi
    import urllib3

    from oiltech_digest.ingestion.http_client import _DEFAULT_HEADERS, _extended_ca_bundle

    headers = {**_DEFAULT_HEADERS, "Host": target.host_header, "Accept-Encoding": "gzip, deflate"}
    request = {"headers": headers, "redirect": False, "retries": False, "preload_content": False}
    if target.scheme == "http":
        return urllib3.HTTPConnectionPool(target.ip, target.port, timeout=_limits(deadline), retries=False).urlopen(
            "GET", target.path, **request)
    for bundle in filter(None, (certifi.where(), _extended_ca_bundle())):
        pool = urllib3.HTTPSConnectionPool(target.ip, target.port, timeout=_limits(deadline), retries=False,
                                           server_hostname=target.host, cert_reqs="CERT_REQUIRED", ca_certs=bundle,
                                           assert_hostname=target.host)
        try:
            return pool.urlopen("GET", target.path, **request)
        except (urllib3.exceptions.SSLError, ssl.SSLError) as exc:
            logger.info("core page %s — сертификат не проверен (%s), следующий набор", target.host, str(exc)[:120])
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    pool = urllib3.HTTPSConnectionPool(target.ip, target.port, timeout=_limits(deadline), retries=False,
                                       server_hostname=target.host, cert_reqs="CERT_NONE", assert_hostname=False)
    return pool.urlopen("GET", target.path, **request)


def _read(response: Any, *, deadline: float) -> bytes:
    """Тело ответа до предела размера и до общего срока. Медленная отдача по байту держала бы поток ядра без конца:
    по сроку сторож закрывает соединение на чтение (HTTPResponse.shutdown, из другого потока), и чтение обрывается.
    Разжатие — явно: без decode_content=True urllib3 отдавал сжатый gzip (замер на ядре 04.11: wam.ae, scientica.ru,
    aer.ca, ovintiv — ответ 200, а разбор видел «не HTML»)."""
    limit = config.SIGNAL_PAGE_MAX_BYTES
    body = bytearray()
    watchdog = threading.Timer(max(deadline - time.monotonic(), 0.0), _shutdown, (response,))
    watchdog.daemon = True
    watchdog.start()
    try:
        for chunk in response.stream(_CHUNK, decode_content=True):
            body += chunk
            if len(body) > limit:
                raise PageRefused(f"страница больше {limit // (1024 * 1024)} МБ")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"не дочитана за {DEADLINE_SECONDS:.0f} с")
    finally:
        watchdog.cancel()
        response.release_conn()
    return bytes(body)


def _shutdown(response: Any) -> None:
    try:
        response.shutdown()
    except Exception:  # noqa: BLE001 - соединение уже отдано или закрыто: читать и так нечего
        pass
