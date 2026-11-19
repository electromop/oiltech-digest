"""Ворота тела запроса (защита входа 16.11.2026): предел размера по пути и проверка входа до чтения тела.

FastAPI читает тело целиком раньше зависимостей (require_user, токен воркера; fastapi/routing.py), поэтому без входа любой
POST принял бы тело любого размера, а у сервера 2 ГБ памяти. Пределы стоят в Caddy (Caddyfile, request_body) и здесь
вторым слоем: Caddyfile смонтирован одним файлом и после выката остаётся старым до caddy reload. Числа одни (тест сверяет
Caddyfile с этим модулем): вход и регистрация 64 КБ (тело меньше 1 КБ); загрузка документа и воркеры NL 32 МБ (файл до
25 МБ, материал коллег до 20 МБ, итог прогона радара около 3,3 МБ); точка обмена с потребителем выпуска 1 МБ (#198:
пакет откликов и квитанция, пакет откликов на тысячи нажатий меньше 1 МБ); остальное 2 МБ (самый большой текст
интерфейса, импорт справочника компаний до 500 тыс. знаков, около 1 МБ). Большое тело только со входом: токен воркера
NL ворота проверяют до чтения тела, а загрузка документа (api.upload_document) читает форму сама, после проверки входа.
Точку обмена ворота пускают только с адреса потребителя и с его токеном, проверка до чтения тела и при любом методе."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

from oiltech_digest.api_access import exchange_point_refusal, external_worker_refusal

AUTH_BODY_BYTES = 64_000
LARGE_BODY_BYTES = 32_000_000
EXCHANGE_BODY_BYTES = 1_000_000
DEFAULT_BODY_BYTES = 2_000_000
_AUTH_PREFIX = "/api/auth/"
_WORKER_PREFIX = "/api/external-worker/"
_EXCHANGE_PREFIX = "/api/exchange/"
_UPLOAD_PATH = "/api/documents"
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


def body_limit(path: str) -> int:
    if path.startswith(_AUTH_PREFIX):
        return AUTH_BODY_BYTES
    if path.startswith(_EXCHANGE_PREFIX):
        return EXCHANGE_BODY_BYTES
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
        if path.startswith(_EXCHANGE_PREFIX):
            # Тот же отказ, что у зависимости маршрутов точки (exchange_api), но до чтения тела и раньше предела: чужой
            # адрес получает 403, о пределах ему знать незачем.
            client = scope.get("client")
            refusal = exchange_point_refusal(client[0] if client else None,
                                             HTTPConnection(scope).headers.get("authorization"))
            if refusal is not None:
                await JSONResponse({"detail": refusal[1]}, status_code=refusal[0])(scope, receive, send)
                return
        limit = body_limit(path)
        declared = _declared_length(scope)
        if declared is not None and declared > limit:
            await _too_large(limit)(scope, receive, send)
            return
        if path.startswith(_WORKER_PREFIX) and scope.get("method") in _BODY_METHODS:
            # Тот же отказ, что у зависимости маршрутов воркера (api.require_external_worker), но до чтения тела.
            refusal = external_worker_refusal(HTTPConnection(scope).headers.get("authorization"))
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
