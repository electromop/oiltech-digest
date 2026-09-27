"""Порог «Требуют внимания» (вердикт stale) на экране «Источники».

Решение владельца 28.09: одно правило для всех источников — 7 суток без нового
материала. При 3 днях под порог попадали и редко пишущие источники: в плитке
«Требуют внимания» было 58 из 129, и сломанные источники в этом списке тонули.

Число живёт в одном месте — config.SOURCE_STALE_DAYS (переменная окружения
SOURCE_STALE_DAYS). Отчёт, API и команды CLI своей копии не держат: без явного
порога все берут его оттуда в момент вызова.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from oiltech_digest import api, cli, config
from oiltech_digest.db import connection, repository

# Сколько молчит источник: возраст его последнего материала по дате сбора.
SILENCE = {
    "Silent 5 days": timedelta(days=5),
    "Silent 8 days": timedelta(days=8),
    "Silent 7 days minus a minute": timedelta(days=7) - timedelta(minutes=1),
    "Silent 7 days plus a minute": timedelta(days=7) + timedelta(minutes=1),
}
# Вердикты при пороге 7 суток.
EXPECTED = {
    "Silent 5 days": "ok",
    "Silent 8 days": "stale",
    "Silent 7 days minus a minute": "ok",
    "Silent 7 days plus a minute": "stale",
}


@pytest.fixture
def silent_sources(isolated_db):
    """Четыре включённых источника, у каждого одна статья.

    Возраст отсчитывается от now() самой базы, а не от часов теста: у Postgres в colima
    свои часы, и расхождение в минуту перевернуло бы граничные случаи.
    """
    with connection.get_connection() as conn:
        for index, (name, silence) in enumerate(SILENCE.items()):
            source_id = conn.execute(
                """
                INSERT INTO sources (name, source_type, url, enabled, parse_strategy, category)
                VALUES (%s, 'News', %s, TRUE, 'rss', 'международные')
                RETURNING id
                """,
                (name, f"https://example.com/{index}"),
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language)
                VALUES (%s, %s, %s, now() - %s, now() - %s, 'Text', 'en')
                """,
                (source_id, f"Article {index}", f"https://example.com/{index}/1", silence, silence),
            )
        conn.commit()


def _verdicts(rows) -> dict[str, str]:
    return {row["name"]: row["verdict"] for row in rows}


def _get_health(query: str = "") -> list[dict]:
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    try:
        response = TestClient(app).get(f"/api/source-health?limit=500{query}")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    return response.json()


def test_source_silent_longer_than_seven_days_needs_attention(silent_sources):
    """5 суток тишины — «Работает штатно», 8 — «Требует внимания».

    Граница — скользящие 7 × 24 часа от последнего собранного материала (collected_at),
    а не календарные дни: без минуты 7 суток — ещё штатно, 7 суток и минута — уже нет.
    """
    assert config.SOURCE_STALE_DAYS == 7
    assert _verdicts(repository.source_health_report(limit=10)) == EXPECTED


def test_sources_screen_request_gets_the_same_verdicts(silent_sources):
    """Экран зовёт отчёт без порога (`/api/source-health?limit=500`): порог решает сервер."""
    assert _verdicts(_get_health()) == EXPECTED


def test_cli_source_health_and_source_retry_use_the_same_rule(silent_sources, monkeypatch, capsys):
    cli.main(["source-health"])
    header = capsys.readouterr().out.splitlines()[0]
    assert "stale_days=7" in header
    assert "stale=2" in header and "ok=2" in header

    # source-retry форсирует сбор только у тех, кто требует внимания, — не у молчащих 5 суток.
    retried: list[int] = []
    monkeypatch.setattr(
        "oiltech_digest.ingestion.rss_parser.parse_all",
        lambda **kwargs: retried.append(kwargs["source_id"]) or {},
    )
    cli.main(["source-retry"])
    names = {row["id"]: row["name"] for row in repository.source_health_report(limit=10)}
    assert {names[source_id] for source_id in retried} == {"Silent 8 days", "Silent 7 days plus a minute"}


def test_threshold_lives_in_config_and_an_explicit_one_still_wins(silent_sources, monkeypatch, capsys):
    """Одно место правды: сменили config.SOURCE_STALE_DAYS — сменились отчёт, API и CLI.

    Явный порог (разовый срез из CLI или API) по-прежнему главнее умолчания.
    """
    monkeypatch.setattr(config, "SOURCE_STALE_DAYS", 10)

    assert set(_verdicts(repository.source_health_report(limit=10)).values()) == {"ok"}
    assert set(_verdicts(_get_health()).values()) == {"ok"}
    assert set(_verdicts(_get_health("&stale_days=3")).values()) == {"stale"}

    cli.main(["source-health"])
    header = capsys.readouterr().out.splitlines()[0]
    assert "stale_days=10" in header and "stale=0" in header
    cli.main(["source-health", "--stale-days", "3"])
    header = capsys.readouterr().out.splitlines()[0]
    assert "stale_days=3" in header and "stale=4" in header
