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

# --- Параметры HTTP / парсинга ---
REQUEST_TIMEOUT = 20          # сек на один HTTP-запрос
MAX_WORKERS = 10              # параллелизм при обходе лент / автообнаружении
RETRY_ATTEMPTS = 3            # попыток HTTP с экспоненциальным backoff
RETRY_BACKOFF_BASE = 1.0      # базовая задержка backoff (1с, 2с, 4с)
