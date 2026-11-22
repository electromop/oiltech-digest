"""HTTP API обмена с потребителем выпуска (#198, спека #197): точка обмена и вкладка «Журнал обмена».

Точка обмена /api/exchange/v1 (версия адресов точки, не формата): список номеров пакетов и пакет по номеру, отдача
отмечает первую и последнюю выдачу; квитанция потребителя на пакет и пакет откликов (#200). Пускает только адрес
потребителя и только с его токеном: проверка в Caddy (Caddyfile), в воротах тела до чтения тела (request_guard) и здесь,
зависимостью маршрутов (api_access.exchange_point_refusal). Отказ точки (квитанция, пакет откликов) ложится в журнал
обмена событием с причиной. Вкладка «Журнал обмена»: только администратору (остальным 403), пакеты, дозагрузка,
готовность выпуска месяца и «Отправить как есть» (#199), «Собрать обновление сейчас» и пакеты откликов (#200). Роутер
подключает api.py.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from oiltech_digest import exchange_packages, exchange_reactions, exchange_updates
from oiltech_digest.api_access import exchange_point_refusal, require_admin
from oiltech_digest.db import connection, exchange_store

POINT = "/api/exchange/v1"
_MAX_NUMBER = 2**63 - 1
# Ответ потребителя в квитанции: одна строка, не длиннее стольких знаков (отказ с длинным списком карточек влезает).
_RECEIPT_CHARS = 20_000
_RECEIPT_FIELDS = ("number", "outcome", "message")
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


def _receipt(body: bytes, number: int) -> tuple[str, str]:
    """Квитанция из тела запроса по формату: ровно number, outcome, message; номер тот же, что в адресе. Не так:
    ValueError с причиной."""
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        raise ValueError("тело не читается как JSON") from None
    if not isinstance(data, dict):
        raise ValueError("квитанция не JSON-объект")
    extra = sorted(str(name) for name in data if name not in _RECEIPT_FIELDS)
    if extra:
        raise ValueError(f"лишнее поле {extra[0][:40]}")
    missing = [name for name in _RECEIPT_FIELDS if name not in data]
    if missing:
        raise ValueError(f"нет поля {missing[0]}")
    if type(data["number"]) is not int:
        raise ValueError("number: нужно целое число")
    if not isinstance(data["outcome"], str) or data["outcome"] not in exchange_store.RECEIPT_OUTCOMES:
        raise ValueError(f"outcome: неизвестный итог «{str(data['outcome'])[:40]}»")
    if not isinstance(data["message"], str):
        raise ValueError("message: нужна строка")
    if len(data["message"]) > _RECEIPT_CHARS:
        raise ValueError(f"message длиннее {_RECEIPT_CHARS} знаков")
    if data["number"] != number:
        raise ValueError(f"номер в квитанции {data['number']}, а в адресе {number}")
    return data["outcome"], data["message"]


def _take_receipt(number: int, body: bytes) -> tuple[int, dict[str, Any]]:
    """Квитанция на пакет: 200 записана или уже была; иначе код и причина, а отказ ложится в журнал обмена."""
    try:
        outcome, message = _receipt(body, number)
        refusal = None
    except ValueError as problem:
        refusal = 422, f"Квитанция не по формату: {problem}."
    with connection.get_connection() as conn:
        if refusal is None:
            found = exchange_store.record_receipt(conn, number, outcome, message)
            if found in ("recorded", "same"):
                conn.commit()
                return 200, {"number": number, "receipt": "принята" if found == "recorded" else "уже была"}
            if found == "missing":
                refusal = 404, f"Пакета №{number} нет."
            else:
                had = exchange_store.RECEIPT_OUTCOMES.get(exchange_store.receipt_outcome(conn, number) or "", "")
                refusal = 409, f"На пакет №{number} уже есть другая квитанция: {had}."
        exchange_store.record_refusal(conn, exchange_store.REFUSED_RECEIPT, number, refusal[1])
        conn.commit()
    return refusal[0], {"detail": refusal[1]}


def _json(status: int, answer: dict[str, Any]) -> Response:
    return Response(content=json.dumps(answer, ensure_ascii=False), status_code=status, media_type="application/json")


@router.put(f"{POINT}/reactions/{{number}}", dependencies=[Depends(_consumer)])
async def point_reactions(number: Annotated[int, Path(ge=1, le=_MAX_NUMBER)], request: Request) -> Response:
    """Пакет откликов потребителя под его номером: файл формата версии 2. 200: принят или уже был (тот же номер и то же
    содержимое); 409: номер занят другим содержимым; 422: не по формату, чужая подпись или получатель, номер сигнала не
    из отправленных; 503: на платформе нет ключей откликов. Отказ ложится в журнал обмена."""
    body = await request.body()
    status, answer = await run_in_threadpool(exchange_reactions.accept, number, body)
    return _json(status, answer)


@router.put(f"{POINT}/receipts/{{number}}", dependencies=[Depends(_consumer)])
async def point_receipt(number: Annotated[int, Path(ge=1, le=_MAX_NUMBER)], request: Request) -> Response:
    """Квитанция потребителя на пакет: JSON без конверта {number, outcome, message}. Повтор с тем же содержимым: 200,
    другое содержимое для того же номера: 409, пакета нет: 404, не по формату: 422."""
    body = await request.body()
    status, answer = await run_in_threadpool(_take_receipt, number, body)
    return _json(status, answer)


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


@router.post("/api/exchange-journal/updates")
def exchange_update_now(user: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """«Собрать обновление сейчас» (#200): изменённые карточки, склейки, снятия и возвраты отправленных сигналов, если
    на платформе что-то изменилось; пустое обновление не выкладывается. Ключей нет: 409 с причиной."""
    try:
        result = exchange_updates.build_update(by=user.get("email"), trigger=exchange_updates.MANUAL)
    except exchange_packages.Refused as exc:
        raise HTTPException(status_code=409, detail=_sentence(str(exc))) from exc
    built = result.built
    return {"package": built.number if built else None,
            "message": _sentence("; ".join([result.message, *result.notes]))}


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
