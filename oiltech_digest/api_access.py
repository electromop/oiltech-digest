"""Доступ к API: кто вошёл (сессия по cookie) и только ли администратор. Отдельно от api.py, чтобы маршруты в своих
модулях (журнал ОС, #189) брали те же зависимости без циклического импорта; api.py отдаёт их под прежними именами, и
подмена в тестах (`api.app.dependency_overrides[api.require_user]`) действует на все маршруты."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import re
from typing import Any

from fastapi import Cookie, Depends, HTTPException

from oiltech_digest import auth_limits, config
from oiltech_digest.db import repository

logger = logging.getLogger(__name__)

EXCHANGE_CLOSED = "Точка обмена закрыта для этого адреса"
# Хэш токена точки обмена в настройке: SHA-256 в hex. Сам токен вместо хэша точку не откроет никому.
_TOKEN_HASH = re.compile(r"[0-9a-f]{64}")


def consumer_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Адреса потребителя из настройки: IP или сеть через пробел, как их читает Caddy (запятая его роняет). Хоть одна
    запись испорчена: точка закрыта всем, а в журнале предупреждение (молча пропущенная запись открыла бы точку не тому,
    кого имели в виду; Caddy с такой записью не запустится)."""
    networks = []
    for item in (config.EXCHANGE_CONSUMER_IPS or "").split():
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.warning("EXCHANGE_CONSUMER_IPS: «%s» не адрес и не сеть (через пробел, без запятых), точка обмена "
                           "закрыта", item)
            return []
    return networks


def exchange_setup_problems() -> list[str]:
    """Что в настройках точки обмена не даст потребителю войти: пусто, если всё задано верно. Видны во вкладке «Журнал
    обмена» и в команде exchange-status."""
    problems = []
    networks = consumer_networks()
    if not networks:
        problems.append("нет адреса потребителя или он испорчен (EXCHANGE_CONSUMER_IPS, IP или сеть через пробел)")
    for network in networks:
        if auth_limits.is_internal_network(network):
            problems.append(f"адрес потребителя {network} внутренний: приложение не отличает его от адреса Caddy и не "
                            "пускает, нужен внешний адрес потребителя")
    if not _TOKEN_HASH.fullmatch(config.EXCHANGE_TOKEN_HASH or ""):
        problems.append("нет хэша токена (EXCHANGE_TOKEN_HASH: SHA-256 токена в hex, 64 знака, не сам токен)")
    return problems


def exchange_point_refusal(host: str | None, authorization: str | None) -> tuple[int, str] | None:
    """Почему запрос к точке обмена не пускать (код и текст) или None. Сначала адрес (настоящий адрес клиента,
    auth_limits.client_address; адрес не задан: закрыто всем), потом токен (на ядре только его SHA-256). Один отказ на
    зависимость маршрутов точки (exchange_api) и ворота тела (request_guard): они проверяют до чтения тела."""
    networks = consumer_networks()
    address = auth_limits.client_address(host)
    if not networks or address is None or not any(ipaddress.ip_address(address) in net for net in networks):
        return 403, EXCHANGE_CLOSED
    if not _TOKEN_HASH.fullmatch(config.EXCHANGE_TOKEN_HASH or ""):
        return 503, "Точка обмена не настроена: нет хэша токена (EXCHANGE_TOKEN_HASH)"
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return 401, "Нужен токен точки обмена"
    if not hmac.compare_digest(hashlib.sha256(token.encode("utf-8")).hexdigest(), config.EXCHANGE_TOKEN_HASH):
        return 401, "Неверный токен точки обмена"
    return None


def external_worker_refusal(authorization: str | None) -> tuple[int, str] | None:
    """Почему запрос воркера NL не пускать (код и текст) или None. Один отказ на зависимость маршрутов
    (api.require_external_worker) и ворота тела (request_guard), которые проверяют токен до чтения тела."""
    if not config.EXTERNAL_WORKER_TOKEN_HASH:
        return 503, "External worker auth is not configured"
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return 401, "External worker token required"
    if not hmac.compare_digest(hashlib.sha256(token.encode("utf-8")).hexdigest(), config.EXTERNAL_WORKER_TOKEN_HASH):
        return 401, "Invalid external worker token"
    return None


def optional_user(
    session_token: str | None = Cookie(default=None, alias=config.AUTH_COOKIE_NAME),
) -> dict[str, Any] | None:
    """Кто вошёл, если вошёл: для ответов, где без входа видно меньше (readiness). Ошибка базы: гость, ответы с
    require_user решают доступ сами."""
    if not session_token:
        return None
    try:
        return repository.get_user_by_session(session_token)
    except Exception:  # noqa: BLE001 - необязательный вход не валит ответ, который нужен и без базы
        logger.warning("optional_user: сессию не проверить", exc_info=True)
        return None


def require_user(session_token: str | None = Cookie(default=None, alias=config.AUTH_COOKIE_NAME)) -> dict[str, Any]:
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = repository.get_user_by_session(session_token)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_admin(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Доступ только администратору (настройка тегов/скоринга/источников, пользователи)."""
    if (user.get("role") or "user") != "admin":
        raise HTTPException(status_code=403, detail="Требуются права администратора")
    return user
