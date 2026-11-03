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

# Тесты ходят через TestClient по http:// — браузерный cookie-jar не вернёт Secure-cookie
# по незащищённому соединению, что ломает роундтрип логина. В проде (HTTPS) флаг остаётся True.
config.AUTH_COOKIE_SECURE = False
# Тесты окна месяца проверяют механизм на дне смены 5 (решение 21.09) и не зависят ни от
# .env разработчика, ни от правила прода (config.FEED_ROLLOVER_DAY_DEFAULT, 05.10 — 11).
config.FEED_ROLLOVER_DAY = 5
# Модель поиска радара (#138) — явная строка .env ядра без умолчания в коде; тесты идут с заданной, как прод после
# выката, и не зависят от .env разработчика. Отказ без неё проверяется отдельно (test_signal_search_broad).
config.SIGNAL_RESEARCH_MODEL = "gpt-6.1-sol"
# Модели «Дедупа» — явные настройки без отката на модель ленты (спека #128, история 48): без них прогон радара и задача
# перепечаток падают до вызовов. Тестам — подставные имена, а не .env разработчика; поведение без модели проверяют
# свои тесты (test_stage_models).
config.SIGNAL_DEDUP_MODEL = "test-dedup-model"
config.OPENAI_REPRINT_MODEL = "test-reprint-model"


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
    # поэтому патч самого модуля connection его НЕ перехватывает — без этой строки
    # apply_overrides() в тестах ходит в НАСТОЯЩУЮ базу вместо изолированной схемы.
    monkeypatch.setattr(source_overrides, "get_connection", connect)
    # documents_repo зовёт connection.get_connection() атрибутом модуля, поэтому
    # подмены выше ему хватает. Строка здесь — чтобы при рефакторинге на прямой
    # импорт дефект вскрылся сразу, а не тихой записью в боевую схему.
    monkeypatch.setattr(documents_repo, "_conn", connect, raising=False)

    try:
        connection.init_db()
        yield schema
    finally:
        with psycopg.connect(DATABASE_URL) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            admin.commit()
