from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from oiltech_digest.db import connection
from oiltech_digest.ingestion import source_overrides


def _add_source(conn, name: str, **fields) -> int:
    columns = {"name": name, "source_type": "News", "enabled": True, **fields}
    keys = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    return conn.execute(
        f"INSERT INTO sources ({keys}) VALUES ({placeholders}) RETURNING id",
        tuple(columns.values()),
    ).fetchone()[0]


def test_apply_overrides_changes_only_config_and_resets_dedup_state(isolated_db, monkeypatch):
    """Оверрайд меняет конфиг источника, сбрасывает дедуп-состояние и НЕ трогает статьи.

    Реестр применяется на КАЖДОМ деплое (bootstrap → apply-source-overrides), поэтому три
    свойства критичны:
    1) статьи не затрагиваются — механизм не может потерять корпус;
    2) при смене listing_url/стратегии сбрасываются last_listing_hash и last_seen_* — иначе
       первый парс по новому URL закоротит на старом хэше от прежней попытки и добавит 0
       (ровно эти «замораживатели» уже ломали сбор, см. историю анти-заморозки);
    3) идемпотентность — повторный прогон ничего не трогает (иначе каждый деплой сбрасывал бы
       дедуп-состояние всем источникам и провоцировал перезагрузку старых статей).
    """
    now = datetime.now(timezone.utc)

    with connection.get_connection() as conn:
        source_id = _add_source(
            conn,
            "Тестовый источник",
            url="https://example.com",
            parse_strategy="request",
            listing_url="https://example.com",          # старый (неверный) листинг — главная
            last_listing_hash="старый-хэш",
            last_seen_article_url="https://example.com/old",
            last_seen_published_at=now - timedelta(days=5),
        )
        article_id = conn.execute(
            """
            INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language)
            VALUES (%s, 'Статья', 'https://example.com/a', %s, %s, 'text', 'ru')
            RETURNING id
            """,
            (source_id, now, now),
        ).fetchone()[0]
        conn.commit()

    monkeypatch.setattr(
        source_overrides,
        "SOURCE_OVERRIDES",
        {"Тестовый источник": {"parse_strategy": "request",
                               "listing_url": "https://example.com/press-center"}},
    )

    stats = source_overrides.apply_overrides()
    assert stats["changed"] == 1
    assert stats["not_found"] == 0

    with connection.get_connection() as conn:
        row = conn.execute(
            """
            SELECT listing_url, parse_strategy, last_listing_hash,
                   last_seen_article_url, last_seen_published_at
            FROM sources WHERE id = %s
            """,
            (source_id,),
        ).fetchone()
        articles_left = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        article_still_there = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE id = %s", (article_id,)
        ).fetchone()[0]

    listing_url, strategy, listing_hash, seen_url, seen_at = row
    assert listing_url == "https://example.com/press-center"   # конфиг применён
    assert strategy == "request"
    # Дедуп-состояние сброшено — иначе новый листинг не дал бы ни одной статьи.
    assert listing_hash is None
    assert seen_url is None
    assert seen_at is None
    # Статьи не тронуты — механизм не может потерять корпус.
    assert articles_left == 1
    assert article_still_there == 1

    # Идемпотентность: второй прогон ничего не меняет.
    stats_again = source_overrides.apply_overrides()
    assert stats_again["changed"] == 0
    assert stats_again["unchanged"] == 1


def test_apply_overrides_reports_unknown_source_name(isolated_db, monkeypatch):
    """Неизвестное имя не роняет применение, но и НЕ применяется молча — считается в not_found.

    Реестр ключуется точным sources.name: опечатка в имени = оверрайд просто не сработает.
    На проде это выглядит как «починил, а источник всё так же молчит», поэтому счётчик
    not_found — единственный сигнал. В bootstrap-логе деплоя он должен быть 0.
    """
    monkeypatch.setattr(
        source_overrides,
        "SOURCE_OVERRIDES",
        {"Источник Которого Нет": {"parse_strategy": "rss", "rss_url": "https://example.com/rss"}},
    )

    stats = source_overrides.apply_overrides()

    assert stats["not_found"] == 1
    assert stats["changed"] == 0


def test_source_overrides_registry_is_well_formed():
    """Гигиена самого реестра — ловит опечатки до деплоя.

    Реестр правится руками и применяется на проде без ревью данных, поэтому проверяем:
    у каждой записи есть parse_strategy из известного набора, URL-поля выглядят как URL,
    имена не задублированы (dict-литерал молча схлопнул бы дубль ключа).
    """
    registry = source_overrides.SOURCE_OVERRIDES
    assert registry, "реестр не должен быть пустым"

    allowed_strategies = {"rss", "request", "playwright", "telegram", "none"}
    for name, fields in registry.items():
        assert name.strip() == name, f"{name!r}: лишние пробелы в имени — не совпадёт с sources.name"
        assert "parse_strategy" in fields, f"{name!r}: parse_strategy обязателен"
        assert fields["parse_strategy"] in allowed_strategies, f"{name!r}: неизвестная стратегия"
        for key in ("listing_url", "rss_url", "url"):
            value = fields.get(key)
            if value is not None:
                assert value.startswith(("http://", "https://")), f"{name!r}.{key}: не URL"
        region = fields.get("network_region")
        if region is not None:
            assert region in {"auto", "ru", "external"}, f"{name!r}: неизвестный network_region"

    # Дубль ключа в dict-литерале молча теряется — ловим по исходнику.
    source = Path(source_overrides.__file__).read_text(encoding="utf-8")
    body = source[source.index("SOURCE_OVERRIDES"):]
    keys = re.findall(r'^\s{4}"([^"]+)":\s*\{', body, re.M)
    duplicates = {key for key in keys if keys.count(key) > 1}
    assert not duplicates, f"дубли ключей в реестре: {sorted(duplicates)}"
