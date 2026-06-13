"""Конфигурация: пути, строка подключения к БД, константы парсера."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Корень репозитория (на уровень выше пакета oiltech_digest/)
REPO_ROOT = Path(__file__).resolve().parents[1]

# Загружаем .env из корня репозитория (если есть)
load_dotenv(REPO_ROOT / ".env")

# --- База данных ---
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://oiltech:oiltech_local_dev@localhost:5432/oiltech_digest",
)

# --- Данные-источники ---
SOURCES_XLSX = REPO_ROOT / "data" / "seed" / "1_Список_источников_для_дайджеста.xlsx"
DIRECTIONS_XLSX = REPO_ROOT / "data" / "seed" / "2_Направления_и_ключевые_слова.xlsx"
SOURCES_SHEET = "Sources_Expanded"
EXPORTS_DIR = REPO_ROOT / "exports"

# --- Параметры HTTP / парсинга ---
REQUEST_TIMEOUT = 20          # сек на один HTTP-запрос
RSS_PROBE_TIMEOUT = int(os.environ.get("RSS_PROBE_TIMEOUT", "4"))
MAX_WORKERS = 10              # параллелизм при обходе лент / автообнаружении
RETRY_ATTEMPTS = 3            # попыток HTTP с экспоненциальным backoff
RETRY_BACKOFF_BASE = 1.0      # базовая задержка backoff (1с, 2с, 4с)
HTTP_MIN_INTERVAL_SECONDS = float(os.environ.get("HTTP_MIN_INTERVAL_SECONDS", "1.5"))
HTTP_JITTER_SECONDS = float(os.environ.get("HTTP_JITTER_SECONDS", "0.4"))
HTTP_BLOCK_COOLDOWN_SECONDS = int(os.environ.get("HTTP_BLOCK_COOLDOWN_SECONDS", "900"))
REQUEST_ARTICLE_LIMIT = int(os.environ.get("REQUEST_ARTICLE_LIMIT", "6"))
BACKGROUND_JOB_WORKERS = int(os.environ.get("BACKGROUND_JOB_WORKERS", "2"))
BACKGROUND_JOB_INLINE = os.environ.get("BACKGROUND_JOB_INLINE", "1").lower() not in {"0", "false", "no"}
BACKGROUND_JOB_POLL_SECONDS = float(os.environ.get("BACKGROUND_JOB_POLL_SECONDS", "2"))
BACKGROUND_JOB_STALE_MINUTES = int(os.environ.get("BACKGROUND_JOB_STALE_MINUTES", "60"))
BACKGROUND_JOB_RETENTION_DAYS = int(os.environ.get("BACKGROUND_JOB_RETENTION_DAYS", "30"))
BACKGROUND_JOB_QUEUES = [
    item.strip()
    for item in os.environ.get("BACKGROUND_JOB_QUEUES", "default").split(",")
    if item.strip()
]
BACKGROUND_JOB_RETRY_BASE_SECONDS = int(os.environ.get("BACKGROUND_JOB_RETRY_BASE_SECONDS", "30"))
EXPORT_JOB_RETENTION_DAYS = int(os.environ.get("EXPORT_JOB_RETENTION_DAYS", "30"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# --- Прокси для парсинга (residential, напр. 2captcha) ---
# PROXY_URL — полная строка подключения: "http://user:pass@host:port"
# (у 2captcha HTTP/HTTPS-прокси с авторизацией порт обычно 8080).
# Если переменная задана, ВЕСЬ парсинг (RSS discovery, ленты, full-text статей)
# идёт через прокси. Пусто (по умолчанию) — прямые запросы, как при локальной
# разработке. OpenAI через прокси НЕ ходит (у него отдельный клиент) — намеренно:
# чтобы не жечь платный трафик и не ловить блок OpenAI за подозрительный IP.
PROXY_URL = os.environ.get("PROXY_URL", "").strip()

# Таймаут (сек) для запросов через прокси: residential заметно медленнее прямого,
# поэтому при активном прокси берём max(обычный таймаут, PROXY_TIMEOUT).
PROXY_TIMEOUT = int(os.environ.get("PROXY_TIMEOUT", "40"))

def _parse_proxy_host_overrides(raw: str) -> dict[str, str]:
    """Parse 'host=proxy_url,host2=proxy_url2' from env into a suffix map."""
    overrides: dict[str, str] = {}
    for chunk in (raw or "").split(","):
        if not chunk.strip() or "=" not in chunk:
            continue
        host, proxy_url = chunk.split("=", 1)
        host = host.strip().lower().lstrip(".")
        proxy_url = proxy_url.strip()
        if host and proxy_url:
            overrides[host] = proxy_url
    return overrides


# Карта "домен → строка прокси". Совпавший суффикс хоста имеет приоритет
# над PROXY_URL: например, override для "rbc.ru" сработает и для "www.rbc.ru".
PROXY_HOST_OVERRIDES: dict[str, str] = _parse_proxy_host_overrides(
    os.environ.get("PROXY_HOST_OVERRIDES", "")
)

# --- OpenAI / AI processing ---
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-nano")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_TIMEOUT = int(os.environ.get("OPENAI_TIMEOUT", "60"))
OPENAI_REASONING_EFFORT = os.environ.get("OPENAI_REASONING_EFFORT", "minimal")

# USD per 1M tokens. Defaults follow the model docs snapshot used when this code
# was written; override in .env if pricing changes or another model is selected.
OPENAI_INPUT_USD_PER_MTOK = float(os.environ.get("OPENAI_INPUT_USD_PER_MTOK", "0.05"))
OPENAI_OUTPUT_USD_PER_MTOK = float(os.environ.get("OPENAI_OUTPUT_USD_PER_MTOK", "0.40"))

# --- Auth ---
AUTH_COOKIE_NAME = os.environ.get("AUTH_COOKIE_NAME", "oiltech_session")
AUTH_SESSION_DAYS = int(os.environ.get("AUTH_SESSION_DAYS", "30"))
