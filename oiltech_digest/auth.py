"""Email/password auth helpers for the admin UI."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

# Почта: до @ латиница, цифры и . _ % + -, после @ метки домена через точку (после normalize_email: строчные).
# Регистрация открыта (16.11), адрес вводит посторонний человек, а почта уходит в сообщение подтверждающим в
# Telegram, в вывод команды на сервере и на экран «Пользователи». Прежнее правило «что угодно без пробела»
# пропускало < > " ' и управляющие символы. Длина по RFC 5321: всего до 254, до @ до 64. Нынешние адреса
# прода (16.11) проходят все.
_EMAIL_RE = re.compile(r"[a-z0-9._%+-]{1,64}@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_EMAIL_MAX_LENGTH = 254
_PBKDF2_ROUNDS = 200_000


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_email(email: str) -> bool:
    value = normalize_email(email)
    return len(value) <= _EMAIL_MAX_LENGTH and _EMAIL_RE.fullmatch(value) is not None


def validate_password(password: str) -> bool:
    return len(password or "") >= 8


def hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, password_hash: str) -> bool:
    _, digest = hash_password(password, salt_hex=salt_hex)
    return hmac.compare_digest(digest, password_hash)


def create_session_token() -> str:
    return secrets.token_urlsafe(32)
