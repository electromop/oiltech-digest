from __future__ import annotations

import uuid

import psycopg
import pytest

from oiltech_digest.db import connection
from oiltech_digest.db import repository
from oiltech_digest import api
from oiltech_digest.config import DATABASE_URL
from oiltech_digest.ingestion import source_overrides


@pytest.fixture()
def isolated_db(monkeypatch):
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

    try:
        connection.init_db()
        yield schema
    finally:
        with psycopg.connect(DATABASE_URL) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            admin.commit()
