"""Email/password auth helpers for the admin UI."""

from __future__ import annotations

from functools import lru_cache
import hashlib
import hmac
from pathlib import Path
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


PASSWORD_TOO_SHORT = "Пароль должен быть не короче 8 символов"
WEAK_PASSWORD = ("Пароль слишком простой: такие подбирают первыми. Не берите частые пароли, ряды клавиш и цифр, повторы, "
                 "даты, название сервиса и свою почту.")
# Слабый пароль (защита входа 16.11.2026): лимиты входа его не спасают, 10 попыток на почту за 15 минут хватает на
# 12345678. Правила по NIST SP 800-63B: частые пароли, повторы и последовательности, слова сервиса и сама почта.
_COMMON_PASSWORDS_FILE = Path(__file__).with_name("common_passwords.txt")
# Сердцевина пароля без цифр и знаков по краям (Password2026! даёт password): частые слова и начала паролей.
_COMMON_CORES = frozenset({
    "password", "passw0rd", "p@ssword", "p@ssw0rd", "pass", "qwerty", "qwertz", "azerty", "admin", "administrator",
    "welcome", "iloveyou", "letmein", "monkey", "dragon", "master", "shadow", "sunshine", "princess", "football",
    "baseball", "superman", "batman", "trustno", "hello", "love", "test", "user", "login", "secret", "changeme",
    "default", "root", "guest", "abc", "abcd", "summer", "winter", "spring", "autumn", "пароль", "привет", "любовь",
    "лето", "зима", "весна", "осень", "oil", "neft", "нефть", "digest", "дайджест",
})
_SERVICE_WORDS = ("oiltech", "oil-tech", "oil_tech", "ойлтех")
_KEYBOARD_RUNS = (
    "1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm", "йцукенгшщзхъ", "фывапролджэ", "ячсмитьбю",
    "qazwsxedcrfvtgbyhnujmikolp", "1qaz2wsx3edc4rfv5tgb6yhn7ujm8ik9ol0p", "zaq1xsw2cde3vfr4bgt5nhy6mju7",
    "1q2w3e4r5t6y7u8i9o0p", "q1w2e3r4t5y6u7i8o9p0", "qweasdzxc", "йфяцычувскамепинртгоьшлбщдюзжхэ",
)
_EDGE_CHARS = "0123456789!@#$%^&*()_+-=.,?;:~`'\"[]{}<>/\\| "


def password_problem(password: str, email: str = "") -> str | None:
    """Почему пароль не годится (текст для человека) или None. Регистрация, заведение учётки и смена пароля; вход
    правило не проверяет: действующие пароли не трогаются."""
    if not validate_password(password):
        return PASSWORD_TOO_SHORT
    return WEAK_PASSWORD if _is_weak(password, normalize_email(email)) else None


def _is_weak(password: str, email: str) -> bool:
    value = password.strip().lower()
    core = value.strip(_EDGE_CHARS)
    if value in _common_passwords() or core in _COMMON_CORES:
        return True
    if len(set(value)) <= 2 or _is_sequence(value) or _is_repeated_block(value):
        return True
    # Только цифры и знаки, короче 12: дата, телефон или пин-код.
    if not core and len(value) < 12:
        return True
    if any(_on_keyboard_run(part) for part in (value, core) if len(part) >= 3):
        return True
    if any(word in value.replace(" ", "") for word in _SERVICE_WORDS):
        return True
    local = email.split("@", 1)[0]
    return bool(local) and (value == email or _letters(value) == _letters(local) or _letters(core) == _letters(local))


@lru_cache(maxsize=1)
def _common_passwords() -> frozenset[str]:
    lines = _COMMON_PASSWORDS_FILE.read_text(encoding="utf-8").splitlines()
    return frozenset(line.strip().lower() for line in lines if line.strip() and not line.startswith("#"))


def _is_sequence(value: str) -> bool:
    steps = {ord(b) - ord(a) for a, b in zip(value, value[1:])}
    return steps in ({1}, {-1})


def _is_repeated_block(value: str) -> bool:
    return any(value == (value[:size] * (len(value) // size + 1))[:len(value)] for size in range(1, 5))


def _on_keyboard_run(part: str) -> bool:
    return any(part in run or part in run[::-1] for run in _KEYBOARD_RUNS)


def _letters(value: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]", "", value)


def hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, password_hash: str) -> bool:
    _, digest = hash_password(password, salt_hex=salt_hex)
    return hmac.compare_digest(digest, password_hash)


# Фиктивная учётка для почты, которой нет (защита входа 16.11.2026): PBKDF2 занимает 116 мс на проде, и без него ответ
# на неизвестную почту приходил сразу: по времени было видно, есть ли учётка.
_DUMMY_SALT = "00" * 16
_DUMMY_HASH = "0" * 64


def burn_password_check(password: str) -> bool:
    """Та же работа, что у проверки настоящего пароля; всегда False."""
    verify_password(password, _DUMMY_SALT, _DUMMY_HASH)
    return False


def create_session_token() -> str:
    return secrets.token_urlsafe(32)
