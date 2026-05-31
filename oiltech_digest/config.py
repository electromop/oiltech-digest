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
SOURCES_XLSX = REPO_ROOT / "1_Список_источников_для_дайджеста.xlsx"
DIRECTIONS_XLSX = REPO_ROOT / "2_Направления_и_ключевые_слова.xlsx"
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
