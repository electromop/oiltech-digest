"""HTTP API обмена с потребителем выпуска (#198, спека #197): точка обмена и вкладка «Журнал обмена».

Точка обмена /api/exchange/v1 (версия адресов точки, не формата): список номеров пакетов и пакет по номеру, отдача
отмечает первую и последнюю выдачу. Пускает только адрес потребителя и только с его токеном: проверка в Caddy
(Caddyfile), в воротах тела до чтения тела (request_guard) и здесь, зависимостью маршрутов (api_access.
exchange_point_refusal). Вкладка «Журнал обмена»: только администратору (остальным 403), пакеты, дозагрузка, готовность
выпуска месяца и «Отправить как есть» (#199). Роутер подключает api.py.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, Field

from oiltech_digest import exchange_packages
from oiltech_digest.api_access import exchange_point_refusal, require_admin
from oiltech_digest.db import connection, exchange_store

POINT = "/api/exchange/v1"
_MAX_NUMBER = 2**63 - 1
router = APIRouter()


def _consumer(request: Request) -> None:
    """Потребитель: его адрес и его токен. Тот же отказ ставят ворота тела раньше приложения."""
    client = request.client
    refusal = exchange_point_refusal(client.host if client else None, request.headers.get("authorization"))
    if refusal is not None:
        raise HTTPException(status_code=refusal[0], detail=refusal[1])


@router.get(f"{POINT}/packages", dependencies=[Depends(_consumer)])
def point_packages() -> dict[str, list[int]]:
    """Номера пакетов для потребителя по возрастанию: он забирает те, что ещё не брал, по порядку номеров."""
    with connection.get_connection() as conn:
        return {"packages": exchange_store.numbers(conn)}


@router.get(f"{POINT}/packages/{{number}}", dependencies=[Depends(_consumer)])
def point_package(number: Annotated[int, Path(ge=1, le=_MAX_NUMBER)]) -> Response:
    """Файл пакета ровно таким, каким его собрали: повторный запрос отдаёт тот же файл. Нет такого: 404."""
    with connection.get_connection() as conn:
        data = exchange_store.take(conn, number)
        conn.commit()
    if data is None:
        raise HTTPException(status_code=404, detail=f"Пакета №{number} нет")
    return Response(content=data, media_type="application/json")


def _sentence(text: str) -> str:
    """Причина отказа сборки (со строчной, без точки) предложением для экрана."""
    return text[:1].upper() + text[1:] + ("" if text.endswith(".") else ".")


@router.get("/api/exchange-journal")
def exchange_journal(limit: int = Query(200, ge=1, le=1000),
                     user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """Пакеты для потребителя (вид, номер, месяц, состав, когда собран и забран) и настройки обмена без ключей."""
    return exchange_packages.journal(limit=limit)


_MONTH = r"^[0-9]{4}-(0[1-9]|1[0-2])$"


@router.get("/api/exchange-journal/readiness")
def exchange_readiness(month: str | None = Query(None, pattern=_MONTH),
                       user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """Готовность выпуска месяца (#199): сколько готово, неготовые с тем, чего им не хватает, можно ли отправить выпуск
    как есть. Без месяца: первый закрытый, чей выпуск ждёт, иначе первый открытый."""
    return exchange_packages.readiness(month)


class ReleaseRequest(BaseModel):
    month: str = Field(pattern=_MONTH)


@router.post("/api/exchange-journal/releases")
def exchange_release_as_is(payload: ReleaseRequest, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """«Отправить как есть» (#199): пакет выпуска закрытого месяца сразу, с тем, что есть; какие материалы были не
    готовы и чего им не хватало, ложится в журнал обмена. Открытый месяц, уже собранный выпуск, статья без сигнала
    ленты, ключей нет: отказ 409 с причиной."""
    try:
        built = exchange_packages.build_release(payload.month, by=user.get("email"), as_is=True)
    except exchange_packages.Refused as exc:
        raise HTTPException(status_code=409, detail=_sentence(str(exc))) from exc
    return {"package": built.number, "month": built.month, "sent": built.signal_ids,
            "not_ready": built.details.get("not_ready") or [], "message": _sentence(built.report())}


class AdditionRequest(BaseModel):
    month: str = Field(pattern=_MONTH)
    numbers: list[Annotated[int, Field(ge=1, le=_MAX_NUMBER)]] = Field(min_length=1, max_length=1000)


@router.post("/api/exchange-journal/additions")
def exchange_addition(payload: AdditionRequest, user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """Дозагрузка по номерам с месяцем выпуска: что ушло (пакет и номера), что пропущено (уже отправлено), какие дубли
    ушли корнем склейки; несуществующий или не действующий номер: отказ 409 со списком."""
    try:
        result = exchange_packages.build_addition(payload.numbers, payload.month, by=user.get("email"))
    except exchange_packages.Refused as exc:
        raise HTTPException(status_code=409, detail=_sentence(str(exc))) from exc
    built = result.built
    return {
        "package": built.number if built else None,
        "sent": built.signal_ids if built else [],
        "skipped": result.skipped,
        "moved": result.moved,
        "message": _sentence("; ".join(result.describe())),
    }
