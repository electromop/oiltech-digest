"""Email/password auth helpers for the admin UI."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PBKDF2_ROUNDS = 200_000


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(normalize_email(email)))


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
