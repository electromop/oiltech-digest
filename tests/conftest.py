from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from oiltech_digest.db import connection
from oiltech_digest.db import documents_repo, repository
from oiltech_digest import api
from oiltech_digest import config
from oiltech_digest.config import DATABASE_URL
from oiltech_digest.ingestion import source_overrides
from tests import db_clock_support

# Тесты ходят через TestClient по http://: браузерный cookie-jar не вернёт Secure-cookie
# по незащищённому соединению, что ломает роундтрип логина. В проде (HTTPS) флаг остаётся True.
config.AUTH_COOKIE_SECURE = False
# Тесты окна месяца проверяют механизм на дне смены 5 (решение 21.09) и не зависят ни от
# .env разработчика, ни от правила прода (config.FEED_ROLLOVER_DAY_DEFAULT, 05.10: 11).
config.FEED_ROLLOVER_DAY = 5
# Модель поиска радара (#138): явная строка .env ядра без умолчания в коде; тесты идут с заданной, как прод после
# выката, и не зависят от .env разработчика. Отказ без неё проверяется отдельно (test_signal_search_broad).
config.SIGNAL_RESEARCH_MODEL = "gpt-6.1-sol"
# Модели «Дедупа», явные настройки без отката на модель ленты (спека #128, история 48): без них прогон радара и задача
# перепечаток падают до вызовов. Тестам: подставные имена, а не .env разработчика; поведение без модели проверяют
# свои тесты (test_stage_models).
config.SIGNAL_DEDUP_MODEL = "test-dedup-model"
config.OPENAI_REPRINT_MODEL = "test-reprint-model"
# Радар раз в N суток (решение владельца 04.11): тесты суточного шага идут с N=1, как по умолчанию в коде, а не с .env
# разработчика или прода; правило N суток проверяют свои тесты (test_background_jobs).
config.SIGNAL_DISCOVERY_DAILY_EVERY_DAYS = 1
# Лимиты входа (защита входа 16.11.2026): тесты идут с умолчаниями кода, а не с .env разработчика; свои пороги ставит
# tests/test_auth_limits.py.
config.AUTH_REGISTER_PER_IP_HOUR = 5
config.AUTH_FAILURES_PER_IP_HOUR = 20
config.AUTH_FAILURES_PER_EMAIL = 10
config.AUTH_EMAIL_WINDOW_MINUTES = 15
config.AUTH_KNOWN_ADDRESS_DAYS = 30
config.AUTH_ATTEMPTS_RETENTION_DAYS = 30
# Обмен с потребителем выпуска (#198): по умолчанию выключен и без ключей, адресов и токена, как прод до выката; ключи и
# адреса ставят свои тесты, а не .env разработчика.
config.EXCHANGE_ENABLED = False
# Раздел «Гипотеза для Компании» (#162): по умолчанию выключен (решение владельца 22.11), как прод; включает его
# фикстура hypothesis_on в тестах самого раздела, а не .env разработчика.
config.ISSUE_HYPOTHESIS_ENABLED = False
config.EXCHANGE_FIRST_MONTH = ""
config.EXCHANGE_SIGNING_KEYS = ""
config.EXCHANGE_DECRYPTION_KEYS = ""
config.EXCHANGE_CONSUMER_KEYS = ""
config.EXCHANGE_CONSUMER_SIGNING_KEYS = ""
config.EXCHANGE_CONSUMER_IPS = ""
config.EXCHANGE_TOKEN_HASH = ""


@pytest.fixture(scope="session")
def db_clock_steady():
    """Часы тестовой базы не прыгают, иначе выдача задач плавает (db_clock_support, 20.10).

    Один замер на прогон, при первом тесте с базой. Базы нет: замер пропускается, тесты с
    базой упадут сами, понятной ошибкой подключения."""
    if os.environ.get("OILTECH_TEST_ALLOW_CLOCK_DRIFT") == "1":
        return
    try:
        conn = psycopg.connect(DATABASE_URL, autocommit=True)
    except psycopg.OperationalError:
        return
    with conn:
        result = db_clock_support.check_clock(lambda: db_clock_support.sample(conn))
    if not result.steady:
        pytest.exit(db_clock_support.explain(result))


@pytest.fixture()
def isolated_db(monkeypatch, db_clock_steady):
    """Run repository integration tests in an isolated temporary Postgres schema."""
    schema = f"test_{uuid.uuid4().hex}"

    def connect():
        conn = psycopg.connect(DATABASE_URL)
        conn.execute(f'SET search_path TO "{schema}"')
        return conn

    with psycopg.connect(DATABASE_URL) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        admin.commit()

    monkeypatch.setattr(connection, "get_connection", connect)
    monkeypatch.setattr(repository, "get_connection", connect)
    monkeypatch.setattr(api, "get_connection", connect)
    # source_overrides импортирует get_connection напрямую (from ...connection import ...),
    # поэтому патч самого модуля connection его НЕ перехватывает: без этой строки
    # apply_overrides() в тестах ходит в НАСТОЯЩУЮ базу вместо изолированной схемы.
    monkeypatch.setattr(source_overrides, "get_connection", connect)
    # documents_repo зовёт connection.get_connection() атрибутом модуля, поэтому
    # подмены выше ему хватает. Строка здесь: чтобы при рефакторинге на прямой
    # импорт дефект вскрылся сразу, а не тихой записью в боевую схему.
    monkeypatch.setattr(documents_repo, "_conn", connect, raising=False)

    try:
        connection.init_db()
        yield schema
    finally:
        with psycopg.connect(DATABASE_URL) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            admin.commit()


@pytest.fixture()
def hypothesis_on(monkeypatch):
    """Раздел «Гипотеза для Компании» включён: тесты самого раздела (#162). По умолчанию он выключен."""
    monkeypatch.setattr(config, "ISSUE_HYPOTHESIS_ENABLED", True)
