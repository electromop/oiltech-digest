"""Общее для тестов обмена с потребителем выпуска (#198): платформа на тестовой базе глазами потребителя.

Фикстура `exchange` поверх фикстуры выпуска (digest_user_path_support.issue): ключ подписи платформы, двойник
потребителя со своим ключом (tests/exchange_consumer.py), адрес потребителя и хэш его токена в настройках ядра. Точку
обмена потребитель зовёт со своего адреса; чужой адрес и ворота тела проверяют свои тесты."""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from oiltech_digest import api, config, exchange_envelope, feed_window
from oiltech_digest.db import connection
from tests.digest_user_path_support import msk
from tests.exchange_consumer import Consumer

CONSUMER_IP = "203.0.113.7"
STRANGER_IP = "198.51.100.9"
TOKEN = "test-exchange-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def freeze(monkeypatch, moment: datetime) -> None:
    """Часы окна месяца (feed_window): закрыт ли месяц, решает это время."""
    monkeypatch.setattr(feed_window, "_now", lambda: moment)


def point(ip: str = CONSUMER_IP) -> TestClient:
    """Клиент точки обмена с адреса ip (как его видит приложение за Caddy)."""
    return TestClient(api.app, client=(ip, 50000))


def packages() -> list[dict]:
    """Строки пакетов в базе по номерам."""
    from psycopg.rows import dict_row

    with connection.get_connection() as conn:
        return conn.cursor(row_factory=dict_row).execute(
            "SELECT number, kind, month, signal_ids, details, built_by, first_taken_at, last_taken_at"
            " FROM exchange_packages ORDER BY number").fetchall()


def sent_signals() -> dict[int, dict]:
    from psycopg.rows import dict_row

    with connection.get_connection() as conn:
        rows = conn.cursor(row_factory=dict_row).execute("SELECT * FROM exchange_sent_signals").fetchall()
    return {int(row["signal_id"]): row for row in rows}


@pytest.fixture()
def exchange(issue, monkeypatch):
    """Выпуск сентября на тестовой базе и потребитель: ключи, адрес и токен в настройках ядра."""
    signing = Ed25519PrivateKey.generate().private_bytes_raw()
    consumer = Consumer(exchange_envelope._signing_public(signing))
    monkeypatch.setattr(config, "EXCHANGE_SIGNING_KEYS", b64(signing))
    monkeypatch.setattr(config, "EXCHANGE_CONSUMER_KEYS", b64(consumer.public_key))
    monkeypatch.setattr(config, "EXCHANGE_CONSUMER_IPS", CONSUMER_IP)
    monkeypatch.setattr(config, "EXCHANGE_TOKEN_HASH", hashlib.sha256(TOKEN.encode()).hexdigest())
    return SimpleNamespace(**issue, consumer=consumer, take=lambda: consumer.take(point(), HEADERS),
                           after_close=msk(2026, 10, 6, 9, 0))
