"""Лимиты входа и регистрации (защита входа 16.11.2026, решение владельца: «по полному»).

С одного адреса за час: заявок регистрации AUTH_REGISTER_PER_IP_HOUR (5; ошибки ввода сюда не доходят) и неудачных
попыток входа и ввода кода AUTH_FAILURES_PER_IP_HOUR (20; удачные не в счёт: офис заказчика за одним адресом). На одну
почту, и несуществующую (по паузе учётку не узнать): неверных паролей AUTH_FAILURES_PER_EMAIL за
AUTH_EMAIL_WINDOW_MINUTES (10 за 15 минут), считая после последнего верного пароля (и на шаге кода: окно защищает
пароль, а его знают). С адреса, откуда за AUTH_KNOWN_ADDRESS_DAYS дней входили с верным паролем этой почты, пауза почты
не действует: зная почту, злоумышленник не закроет вход её владельцу из офиса или из дома.

Счётчики в базе (auth_attempts): переживают выкат и годятся при любом числе процессов. Попытка пишется до проверки
пароля под замками адреса и почты, а «ждёт итога» считается неудачей: пачка одновременных запросов лимит не проскочит,
и отказ приходит до хэша пароля (PBKDF2, 116 мс на проде), поэтому поток запросов не занимает процессор.

Адрес клиента: request.client.host. Его ставит uvicorn по X-Forwarded-For от Caddy, если адрес Caddy есть в
FORWARDED_ALLOW_IPS (docker-compose.yml). Внутренний адрес значит, что заголовку не поверили и все клиенты выглядят одним
адресом: тогда лимит адреса и знакомый адрес не действуют (иначе один подборщик закрыл бы вход всем), а в журнале
предупреждение."""

from __future__ import annotations

from datetime import datetime
import ipaddress
import logging
import math
import time
from typing import Any, Literal
from zoneinfo import ZoneInfo

from oiltech_digest import auth, config
from oiltech_digest.db import connection, repository

logger = logging.getLogger(__name__)

# Вид попытки и её исход. Неудача для адреса: всё, кроме верного пароля; для почты: неверный пароль (неверный код при
# верном пароле почту не закрывает: у кода свой счёт попыток, admission.MAX_ATTEMPTS). PENDING ждёт итога и считается
# неудачей. Заявка регистрации пишется с исходом DONE. Снятое командой auth-unlock помечено forgiven и не считается.
SignInKind = Literal["login", "admit"]
Outcome = Literal["ok", "bad_password", "bad_code"]
LOGIN: SignInKind = "login"
ADMIT: SignInKind = "admit"
OK: Outcome = "ok"
BAD_PASSWORD: Outcome = "bad_password"
BAD_CODE: Outcome = "bad_code"
_REGISTER, _DONE = "register", "done"

# Условия одной записью на все запросы модуля.
_SIGN_IN = "kind IN ('login', 'admit')"
_COUNTED_REGISTRATION = "kind = 'register' AND NOT forgiven"
_ADDRESS_FAILURE = "kind IN ('login', 'admit') AND outcome IN ('pending', 'bad_password', 'bad_code') AND NOT forgiven"
_EMAIL_FAILURE = "kind IN ('login', 'admit') AND outcome IN ('pending', 'bad_password') AND NOT forgiven"
# Верный пароль обнуляет окно почты и тогда, когда строку потом сняли командой: факт верного пароля остаётся.
_LAST_VERIFIED = ("COALESCE((SELECT max(created_at) FROM auth_attempts WHERE email = %(email)s "
                  "AND kind IN ('login', 'admit') AND outcome IN ('ok', 'bad_code')), '-infinity')")
_HOUR = "interval '1 hour'"
_EMAIL_WINDOW = "make_interval(mins => %(window)s)"
_EXPIRED = "created_at < now() - make_interval(days => %(days)s)"

# Ключи pg_advisory_xact_lock(int, int): пространство пар не пересекается с одиночным ключом замка регистраций.
_ADDRESS_LOCK = 16112
_EMAIL_LOCK = 16113
# Почта длиннее 254 знаков не бывает (RFC 5321): ключ режется, чтобы в таблицу не легли мегабайты из тела запроса.
_EMAIL_KEY_LENGTH = 254
_INTERNAL_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16", "100.64.0.0/10",
    "::1/128", "fc00::/7", "fe80::/10",
))
_WARN_EVERY_SECONDS = 600
_last_warning = float("-inf")
_MSK = ZoneInfo("Europe/Moscow")

ADDRESS_SIGN_IN = "Слишком много неудачных попыток входа с вашего адреса. Попробуйте через {minutes} мин."
EMAIL_SIGN_IN = "Слишком много неудачных попыток входа с этой почтой. Попробуйте через {minutes} мин."
ADDRESS_REGISTER = ("Слишком много регистраций с вашего адреса за последний час. Попробуйте через {minutes} мин. "
                    "или попросите администратора завести учётку.")
REGISTRATIONS_CAPPED = ("Сейчас слишком много новых регистраций. Попробуйте через {minutes} мин. или попросите "
                        "администратора завести учётку.")


class Refused(Exception):
    """Лимит исчерпан: ответ 429, текст для человека и через сколько секунд можно снова (Retry-After)."""

    def __init__(self, template: str, retry_after: Any) -> None:
        self.retry_after = max(1, int(math.ceil(float(retry_after or 1))))
        self.detail = template.format(minutes=math.ceil(self.retry_after / 60))
        super().__init__(self.detail)


def canonical_ip(value: str) -> str:
    """Адрес одной записью (IPv4 внутри IPv6 как IPv4); не адрес: ValueError."""
    address = ipaddress.ip_address(value.strip())
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return str(address)


def client_address(host: str | None) -> str | None:
    """Адрес клиента для лимитов: публичный IP. Внутренний, не IP или пустой: None, лимит адреса не действует."""
    global _last_warning
    try:
        address = canonical_ip(host or "")
    except ValueError:
        return None
    if any(ipaddress.ip_address(address) in network for network in _INTERNAL_NETWORKS):
        now = time.monotonic()
        if now - _last_warning >= _WARN_EVERY_SECONDS:
            _last_warning = now
            logger.warning("auth_limits: адрес клиента %s внутренний, лимит адреса не действует; проверьте "
                           "FORWARDED_ALLOW_IPS у app: uvicorn верит X-Forwarded-For только от этих адресов", address)
        return None
    return address


def _email_key(email: str) -> str:
    return auth.normalize_email(email)[:_EMAIL_KEY_LENGTH]


def _lock(conn, namespace: int, key: str) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (namespace, key))


def _in_window(conn, condition: str, window: str, params: dict[str, Any]) -> tuple[int, Any]:
    """Сколько строк условия в окне и через сколько секунд старшая из них выйдет из окна."""
    return conn.execute(
        f"""
        SELECT count(*), extract(epoch FROM min(created_at) + {window} - now())
        FROM auth_attempts WHERE {condition} AND created_at > now() - {window}
        """,
        params,
    ).fetchone()


def record_registration(ip: str | None, email: str) -> None:
    """Заявка регистрации прошла проверку полей: сверх лимита адреса Refused, иначе заявка записана (и без адреса)."""
    with connection.get_connection() as conn:
        if ip is not None:
            _lock(conn, _ADDRESS_LOCK, ip)
            count, retry_after = _in_window(conn, f"client_ip = %(ip)s AND {_COUNTED_REGISTRATION}", _HOUR, {"ip": ip})
            if count >= config.AUTH_REGISTER_PER_IP_HOUR:
                raise Refused(ADDRESS_REGISTER, retry_after)
        conn.execute("INSERT INTO auth_attempts (kind, client_ip, email, outcome) VALUES (%s, %s, %s, %s)",
                     (_REGISTER, ip, _email_key(email), _DONE))
        conn.commit()


def begin_sign_in(kind: SignInKind, ip: str | None, email: str) -> int:
    """Перед проверкой пароля (вход, код допуска): лимит адреса, пауза почты, затем попытка «ждёт итога». Номер
    попытки отдать finish_sign_in; сверх лимита Refused, и пароль не проверяется."""
    key = _email_key(email)
    with connection.get_connection() as conn:
        if ip is not None:
            _lock(conn, _ADDRESS_LOCK, ip)
            count, retry_after = _in_window(conn, f"client_ip = %(ip)s AND {_ADDRESS_FAILURE}", _HOUR, {"ip": ip})
            if count >= config.AUTH_FAILURES_PER_IP_HOUR:
                raise Refused(ADDRESS_SIGN_IN, retry_after)
        _lock(conn, _EMAIL_LOCK, key)
        if not _known_address(conn, ip, key):
            count, retry_after = _in_window(
                conn, f"email = %(email)s AND {_EMAIL_FAILURE} AND created_at > {_LAST_VERIFIED}", _EMAIL_WINDOW,
                {"email": key, "window": config.AUTH_EMAIL_WINDOW_MINUTES},
            )
            if count >= config.AUTH_FAILURES_PER_EMAIL:
                raise Refused(EMAIL_SIGN_IN, retry_after)
        attempt_id = conn.execute(
            "INSERT INTO auth_attempts (kind, client_ip, email) VALUES (%s, %s, %s) RETURNING id", (kind, ip, key)
        ).fetchone()[0]
        conn.commit()
    return int(attempt_id)


def _known_address(conn, ip: str | None, email: str) -> bool:
    """С этого адреса за AUTH_KNOWN_ADDRESS_DAYS дней входили с верным паролем этой почты."""
    if ip is None:
        return False
    row = conn.execute(
        """
        SELECT EXISTS (SELECT 1 FROM auth_attempts
                       WHERE email = %s AND client_ip = %s AND outcome = 'ok'
                         AND created_at > now() - make_interval(days => %s))
        """,
        (email, ip, config.AUTH_KNOWN_ADDRESS_DAYS),
    ).fetchone()
    return bool(row[0])


def finish_sign_in(attempt_id: int, outcome: Outcome) -> None:
    """Итог попытки: OK (верный пароль), BAD_PASSWORD или BAD_CODE."""
    with connection.get_connection() as conn:
        conn.execute("UPDATE auth_attempts SET outcome = %s WHERE id = %s AND outcome = 'pending'",
                     (outcome, attempt_id))
        conn.commit()


def authenticate(kind: SignInKind, ip: str | None, email: str, password: str) -> tuple[dict[str, Any] | None, int]:
    """Вход и шаг кода: лимиты адреса и почты, затем пароль. Учётка (None: пароль неверный, неудача уже в счёте) и номер
    попытки; её итог (OK или BAD_CODE) пишет вызывающий через finish_sign_in. Сверх лимита Refused, пароль не
    проверяется."""
    attempt = begin_sign_in(kind, ip, email)
    user = repository.authenticate_user(email, password)
    if user is None:
        finish_sign_in(attempt, BAD_PASSWORD)
    return user, attempt


# --- команды и чистка ------------------------------------------------------------------------------------------------


def unlock(*, email: str | None = None, ip: str | None = None) -> int:
    """Снять паузу почте или лимит адресу (команда auth-unlock): неудачи и заявки получают пометку forgiven и больше не
    считаются. Адрес приводится к одной записи (ValueError, если это не адрес). Сколько строк снято."""
    clauses, params = [], {}
    if email:
        clauses.append("email = %(email)s")
        params["email"] = _email_key(email)
    if ip:
        clauses.append("client_ip = %(ip)s")
        params["ip"] = canonical_ip(ip)
    if not clauses:
        raise ValueError("нужна почта или адрес")
    with connection.get_connection() as conn:
        cur = conn.execute(
            f"""
            UPDATE auth_attempts SET forgiven = TRUE
            WHERE {' AND '.join(clauses)} AND (({_ADDRESS_FAILURE}) OR ({_COUNTED_REGISTRATION}))
            """,
            params,
        )
        conn.commit()
        return cur.rowcount or 0


def report(hours: int) -> dict[str, list[dict[str, Any]]]:
    """Сводка для команды auth-attempts за последние hours часов: адреса и почты с неудачами, кто сейчас упёрся."""
    params = {"hours": max(1, int(hours)), "window": config.AUTH_EMAIL_WINDOW_MINUTES}
    with connection.get_connection() as conn:
        addresses = conn.execute(
            f"""
            SELECT client_ip,
                   count(*) FILTER (WHERE {_ADDRESS_FAILURE}) AS failed,
                   count(*) FILTER (WHERE outcome = 'ok') AS ok,
                   count(*) FILTER (WHERE {_COUNTED_REGISTRATION}) AS registrations,
                   count(*) FILTER (WHERE {_ADDRESS_FAILURE} AND created_at > now() - {_HOUR}) AS failed_hour,
                   count(*) FILTER (WHERE {_COUNTED_REGISTRATION} AND created_at > now() - {_HOUR}) AS registrations_hour,
                   max(created_at) AS last_at
            FROM auth_attempts
            WHERE client_ip IS NOT NULL AND created_at > now() - make_interval(hours => %(hours)s)
            GROUP BY client_ip
            ORDER BY failed DESC, last_at DESC
            LIMIT 50
            """,
            params,
        ).fetchall()
        emails = conn.execute(
            f"""
            WITH verified AS (
                SELECT email, max(created_at) AS verified_at FROM auth_attempts
                WHERE {_SIGN_IN} AND outcome IN ('ok', 'bad_code') GROUP BY email
            )
            SELECT email,
                   count(*) FILTER (WHERE {_EMAIL_FAILURE}) AS failed,
                   count(*) FILTER (WHERE outcome = 'ok') AS ok,
                   count(*) FILTER (WHERE {_EMAIL_FAILURE} AND created_at > now() - {_EMAIL_WINDOW}
                                      AND created_at > COALESCE(verified_at, '-infinity')) AS failed_window,
                   max(created_at) AS last_at
            FROM auth_attempts LEFT JOIN verified USING (email)
            WHERE {_SIGN_IN} AND created_at > now() - make_interval(hours => %(hours)s)
            GROUP BY email, verified_at
            ORDER BY failed DESC, last_at DESC
            LIMIT 50
            """,
            params,
        ).fetchall()
    return {
        "addresses": [
            {"ip": ip, "failed": failed, "ok": ok, "registrations": registrations, "last_at": last_at,
             "limited": failed_hour >= config.AUTH_FAILURES_PER_IP_HOUR
             or registrations_hour >= config.AUTH_REGISTER_PER_IP_HOUR}
            for ip, failed, ok, registrations, failed_hour, registrations_hour, last_at in addresses
        ],
        "emails": [
            {"email": email, "failed": failed, "ok": ok, "last_at": last_at,
             "paused": failed_window >= config.AUTH_FAILURES_PER_EMAIL}
            for email, failed, ok, failed_window, last_at in emails
        ],
    }


def format_report(data: dict[str, list[dict[str, Any]]], hours: int) -> str:
    def when(value: datetime) -> str:
        return value.astimezone(_MSK).strftime("%d.%m %H:%M МСК")

    lines = [f"Попытки входа и регистрации за {hours} ч. Лимиты: с адреса {config.AUTH_FAILURES_PER_IP_HOUR} неудач и "
             f"{config.AUTH_REGISTER_PER_IP_HOUR} регистраций в час; на почту {config.AUTH_FAILURES_PER_EMAIL} неудач "
             f"за {config.AUTH_EMAIL_WINDOW_MINUTES} мин.", "Адреса:"]
    lines += [f"  {row['ip']:<39} неудач {row['failed']:>3}  верных {row['ok']:>3}  регистраций "
              f"{row['registrations']:>2}  последняя {when(row['last_at'])}" + ("  [лимит]" if row["limited"] else "")
              for row in data["addresses"]] or ["  нет"]
    lines.append("Почты:")
    lines += [f"  {row['email']:<40} неудач {row['failed']:>3}  верных {row['ok']:>3}  последняя "
              f"{when(row['last_at'])}" + ("  [пауза]" if row["paused"] else "")
              for row in data["emails"]] or ["  нет"]
    return "\n".join(lines)


def prune() -> int:
    """Удалить строки старше AUTH_ATTEMPTS_RETENTION_DAYS: шаг maintenance-cleanup."""
    with connection.get_connection() as conn:
        cur = conn.execute(f"DELETE FROM auth_attempts WHERE {_EXPIRED}", {"days": config.AUTH_ATTEMPTS_RETENTION_DAYS})
        conn.commit()
        return cur.rowcount or 0


def count_prunable() -> int:
    with connection.get_connection() as conn:
        row = conn.execute(f"SELECT count(*) FROM auth_attempts WHERE {_EXPIRED}",
                           {"days": config.AUTH_ATTEMPTS_RETENTION_DAYS}).fetchone()
    return int(row[0])
