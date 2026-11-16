"""Ворота тела запроса (защита входа 16.11.2026): предел размера по пути и проверка входа до чтения тела.

FastAPI читает тело целиком раньше зависимостей (require_user, токен воркера; fastapi/routing.py), поэтому без входа любой
POST принял бы тело любого размера, а у сервера 2 ГБ памяти. Пределы стоят в Caddy (Caddyfile, request_body) и здесь
вторым слоем: Caddyfile смонтирован одним файлом и после выката остаётся старым до caddy reload. Числа одни (тест сверяет
Caddyfile с этим модулем): вход и регистрация 64 КБ (тело меньше 1 КБ); загрузка документа и воркеры NL 32 МБ (файл до
25 МБ, материал коллег до 20 МБ, итог прогона радара около 3,3 МБ); остальное 2 МБ (самый большой текст интерфейса,
импорт справочника компаний до 500 тыс. знаков, около 1 МБ). Большое тело пускается только с входом: у воркеров NL
токен, у загрузки документа живая сессия, оба проверяются до чтения тела."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

import anyio
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

from oiltech_digest import config
from oiltech_digest.api_access import external_worker_refusal
from oiltech_digest.db import repository

AUTH_BODY_BYTES = 64_000
LARGE_BODY_BYTES = 32_000_000
DEFAULT_BODY_BYTES = 2_000_000
_AUTH_PREFIX = "/api/auth/"
_WORKER_PREFIX = "/api/external-worker/"
_UPLOAD_PATH = "/api/documents"
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


def body_limit(path: str) -> int:
    if path.startswith(_AUTH_PREFIX):
        return AUTH_BODY_BYTES
    if path == _UPLOAD_PATH or path.startswith(_WORKER_PREFIX):
        return LARGE_BODY_BYTES
    return DEFAULT_BODY_BYTES


def _too_large(limit: int) -> JSONResponse:
    size = f"{limit // 1_000_000} МБ" if limit >= 1_000_000 else f"{limit // 1000} КБ"
    return JSONResponse({"detail": f"Слишком большой запрос: не больше {size}"}, status_code=413)


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers") or ():
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _large_body_refusal(scope: Scope, path: str) -> tuple[int, str] | None:
    """Большое тело только с входом: токен воркера NL или живая сессия у загрузки документа."""
    connection = HTTPConnection(scope)
    if path.startswith(_WORKER_PREFIX):
        return external_worker_refusal(connection.headers.get("authorization"))
    token = connection.cookies.get(config.AUTH_COOKIE_NAME)
    if not token or not await anyio.to_thread.run_sync(repository.session_is_active, token):
        return 401, "Not authenticated"
    return None


class RequestGuard:
    """ASGI-слой перед приложением: заявленный размер больше предела, отказ 413 сразу; без Content-Length счёт
    полученных байт, и сверх предела 413 уходит клиенту, а приложение получает обрыв (его ответ отбрасывается)."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        limit = body_limit(path)
        declared = _declared_length(scope)
        if declared is not None and declared > limit:
            await _too_large(limit)(scope, receive, send)
            return
        if limit == LARGE_BODY_BYTES and scope.get("method") in _BODY_METHODS:
            refusal = await _large_body_refusal(scope, path)
            if refusal is not None:
                await JSONResponse({"detail": refusal[1]}, status_code=refusal[0])(scope, receive, send)
                return

        received = 0
        refused = False
        started = False

        async def counted_receive() -> Message:
            nonlocal received, refused
            message = await receive()
            if message["type"] == "http.request" and not refused:
                received += len(message.get("body", b""))
                if received > limit:
                    refused = True
                    if not started:
                        await _too_large(limit)(scope, receive, send)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if refused:
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        await self.app(scope, counted_receive, guarded_send)
