"""Допуск по коду (#113, спека #110, ADR 0004): войти можно только допущенной учётке.

Новая учётка и учётка, с которой сняли допуск, после верного пароля получают ответ «нужен код
допуска». Код — восемь цифр, одноразовый, 24 часа, не больше пяти попыток ввода; в базе только
его отпечаток. Новый код одной учётке — не чаще раза в 10 минут и только когда прежний уже не
действует: повторные входы не засыпают чат.

Код уходит в закрытый чат подтверждающих в Telegram. С РФ-ядра Telegram недоступен (решение
владельца), поэтому ядро ставит задачу `admission_notify` в очередь external-notify, а сообщение
отправляет воркер сбора на NL (`send_notification`). Отправка включается флагом
ADMISSION_NOTIFY_ENABLED после пересборки NL (контракт 3); до этого и при сбое отправки код
выдаёт команда на сервере (`issue_code(force=True)`). Текст сообщения с кодом стирается из задачи,
как только она завершилась или упала окончательно (`settle_notifications`)."""

from __future__ import annotations

from datetime import datetime
import logging
import re
import secrets
from typing import Any
from zoneinfo import ZoneInfo

from oiltech_digest import auth, config, lanes
from oiltech_digest.db import connection

logger = logging.getLogger(__name__)

KIND = "admission_notify"
CODE_DIGITS = 8
CODE_TTL = "24 hours"
RESEND_AFTER = "10 minutes"
MAX_ATTEMPTS = 5
_MSK = ZoneInfo("Europe/Moscow")
_TELEGRAM_API = "https://api.telegram.org"

REQUIRED = "Нужен код допуска: код отправлен подтверждающим, получите его у них."
NO_CODE = "Код уже использован или не выдавался. Войдите с паролем ещё раз, чтобы получить новый."
EXPIRED = "Срок кода истёк. Войдите с паролем ещё раз, чтобы получить новый."
EXHAUSTED = ("Код больше не действует: попытки ввода закончились. Войдите с паролем ещё раз — новый код "
             "придёт не раньше чем через 10 минут после прежнего.")


class AdmissionRefused(Exception):
    """Код не принят; текст — для человека."""


def is_admitted(user_id: int) -> bool:
    with connection.get_connection() as conn:
        row = conn.execute("SELECT admitted_at FROM users WHERE id = %s", (user_id,)).fetchone()
    return row is not None and row[0] is not None


def admit(user_id: int) -> None:
    with connection.get_connection() as conn:
        conn.execute("UPDATE users SET admitted_at = now() WHERE id = %s", (user_id,))
        conn.commit()


def revoke(user_id: int) -> None:
    """Снять допуск: сессии закрываются, прежний код пропадает — новый можно выдать сразу.
    Данные учётки не трогаются."""
    with connection.get_connection() as conn:
        conn.execute("UPDATE users SET admitted_at = NULL, updated_at = now() WHERE id = %s", (user_id,))
        conn.execute("DELETE FROM user_sessions WHERE user_id = %s", (user_id,))
        conn.execute("DELETE FROM admission_codes WHERE user_id = %s", (user_id,))
        conn.commit()


def issue_code(user_id: int, *, force: bool = False) -> tuple[str, datetime] | None:
    """Новый код учётке; None — выдавать рано: прежний ещё действует или выдан меньше 10 минут назад.

    force — команда на сервере: выдаёт всегда и заменяет прежний. Проверка и замена — одним
    запросом, чтобы два одновременных входа не выдали два кода."""
    code = f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"
    salt, digest = auth.hash_password(code)
    with connection.get_connection() as conn:
        row = conn.execute(
            f"""
            INSERT INTO admission_codes (user_id, code_salt, code_hash, expires_at)
            VALUES (%(user_id)s, %(salt)s, %(digest)s, now() + interval '{CODE_TTL}')
            ON CONFLICT (user_id) DO UPDATE
            SET code_salt = EXCLUDED.code_salt, code_hash = EXCLUDED.code_hash,
                created_at = now(), expires_at = EXCLUDED.expires_at, used_at = NULL, attempts = 0,
                notify_job_id = NULL, sent_at = NULL, send_error = NULL
            WHERE %(force)s
               OR (admission_codes.created_at <= now() - interval '{RESEND_AFTER}'
                   AND (admission_codes.used_at IS NOT NULL
                        OR admission_codes.expires_at <= now()
                        OR admission_codes.attempts >= {MAX_ATTEMPTS}))
            RETURNING expires_at
            """,
            {"user_id": user_id, "salt": salt, "digest": digest, "force": force},
        ).fetchone()
        conn.commit()
    return (code, row[0]) if row else None


def request_code(user: dict[str, Any]) -> None:
    """Вход недопущенной учётки с верным паролем: выдать код и поставить отправку, если пора.

    Без флага отправки код не выдаётся: его некому доставить, выдаёт команда на сервере."""
    if not config.ADMISSION_NOTIFY_ENABLED:
        return
    user_id = int(user["id"])
    issued = issue_code(user_id)
    if issued is None:
        return
    from oiltech_digest import background_jobs

    code, expires_at = issued
    job = background_jobs.enqueue(
        KIND,
        {"user_id": user_id, "text": notify_text(user_id, str(user.get("email") or ""), code, expires_at)},
        queue_name=lanes.NOTIFY,
        execution_region="external",
        capability=None,
    )
    with connection.get_connection() as conn:
        conn.execute("UPDATE admission_codes SET notify_job_id = %s WHERE user_id = %s", (int(job["id"]), user_id))
        conn.commit()


def notify_text(user_id: int, email: str, code: str, expires_at: datetime) -> str:
    """Сообщение подтверждающим: номер учётки, начало почты, код и срок. Полной почты нет."""
    local = email.split("@", 1)[0]
    start = local[: min(3, max(1, len(local) // 2))]
    until = expires_at.astimezone(_MSK).strftime("%d.%m.%Y %H:%M")
    return (f"Учётка №{user_id} ({start}…) ждёт допуска.\n"
            f"Код: {code} — действует до {until} МСК, не больше {MAX_ATTEMPTS} попыток ввода.\n"
            "Передайте код владельцу учётки.")


def redeem(user_id: int, code: str) -> None:
    """Принять код: допуск учётке. Неверный код засчитывает попытку; иначе — AdmissionRefused."""
    entered = re.sub(r"[\s-]", "", code or "")
    with connection.get_connection() as conn:
        row = conn.execute(
            "SELECT code_salt, code_hash, used_at, attempts, expires_at > now() "
            "FROM admission_codes WHERE user_id = %s FOR UPDATE",
            (user_id,),
        ).fetchone()
        if row is None or row[2] is not None:
            raise AdmissionRefused(NO_CODE)
        salt, digest, _, attempts, live = row
        if attempts >= MAX_ATTEMPTS:
            raise AdmissionRefused(EXHAUSTED)
        if not live:
            raise AdmissionRefused(EXPIRED)
        if not (entered.isdigit() and auth.verify_password(entered, salt, digest)):
            conn.execute("UPDATE admission_codes SET attempts = attempts + 1 WHERE user_id = %s", (user_id,))
            conn.commit()
            left = MAX_ATTEMPTS - attempts - 1
            raise AdmissionRefused(f"Код не подошёл. Осталось попыток: {left}." if left else EXHAUSTED)
        conn.execute("UPDATE admission_codes SET used_at = now() WHERE user_id = %s", (user_id,))
        conn.execute("UPDATE users SET admitted_at = now() WHERE id = %s", (user_id,))
        conn.commit()


def settle_notifications() -> None:
    """Отметить у кода, ушло ли сообщение, и стереть текст из завершённых задач отправки.

    Зовётся после complete и fail воркера и сторожем очередей ядра (окончательный сбой по
    истёкшей аренде)."""
    with connection.get_connection() as conn:
        conn.execute(
            """
            UPDATE admission_codes c
            SET sent_at = COALESCE(j.finished_at, now()), send_error = NULL
            FROM background_jobs j
            WHERE j.id = c.notify_job_id AND j.status = 'ok' AND j.payload_json ? 'text'
            """
        )
        conn.execute(
            """
            UPDATE admission_codes c
            SET send_error = COALESCE(j.error_message, 'отправка не удалась')
            FROM background_jobs j
            WHERE j.id = c.notify_job_id AND j.status = 'failed' AND j.payload_json ? 'text'
            """
        )
        conn.execute(
            f"""
            UPDATE background_jobs SET payload_json = payload_json - 'text'
            WHERE kind = '{KIND}' AND status IN ('ok', 'failed') AND payload_json ? 'text'
            """
        )
        conn.commit()


def without_text(payload: dict[str, Any]) -> dict[str, Any]:
    """Задача отправки в списке задач — без кода: экран задач видят все допущенные."""
    return {key: value for key, value in (payload or {}).items() if key != "text"}


# --- NL: отправка в Telegram ------------------------------------------------------------------


class TelegramError(RuntimeError):
    """Telegram не принял сообщение — повторяемый сбой задачи. Текст без токена бота."""


def send_notification(payload: dict[str, Any], heartbeat=None) -> dict[str, Any]:
    """Обработчик задачи admission_notify на воркере: один запрос sendMessage к Bot API.

    Токен и номер чата — в настройках воркера (.env.external-worker). Ошибка — исключение:
    воркер вернёт задачу ядру повторяемым сбоем."""
    import requests

    token = config.ADMISSION_TELEGRAM_BOT_TOKEN
    chat_id = config.ADMISSION_TELEGRAM_CHAT_ID
    if not token or not chat_id:
        raise TelegramError("Telegram: не заданы ADMISSION_TELEGRAM_BOT_TOKEN / ADMISSION_TELEGRAM_CHAT_ID")
    text = str((payload or {}).get("text") or "")
    if not text:
        raise TelegramError("Telegram: в задаче нет текста сообщения")
    try:
        response = requests.post(f"{_TELEGRAM_API}/bot{token}/sendMessage",
                                 json={"chat_id": chat_id, "text": text}, timeout=20)
    except requests.RequestException as exc:
        # В тексте исключения requests — адрес запроса, а в нём токен.
        raise TelegramError(f"Telegram недоступен: {type(exc).__name__}") from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or not body.get("ok"):
        description = str(body.get("description") or "")[:200].replace(token, "***")
        raise TelegramError(f"Telegram: {response.status_code} {description}".strip())
    return {KIND: True, "message_id": (body.get("result") or {}).get("message_id")}
