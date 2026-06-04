"""Тесты нормализации — чистые функции, без сети и БД."""

from datetime import datetime, timedelta, timezone

from oiltech_digest.ingestion import normalize


def test_clean_html_strips_tags_and_entities():
    assert normalize.clean_html("<p>Привет&nbsp;&amp; мир</p>") == "Привет & мир"
    assert normalize.clean_html("  a   b  ") == "a b"
    assert normalize.clean_html("") == ""
    assert normalize.clean_html(None) == ""


def test_content_hash_is_deterministic_and_normalizing():
    # Разный регистр заголовка, схема, utm и хвостовой слэш → одинаковый хеш
    h1 = normalize.compute_content_hash("Заголовок", "https://example.com/news/1?utm_source=x")
    h2 = normalize.compute_content_hash("заголовок", "http://example.com/news/1/")
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


def test_content_hash_differs_for_different_content():
    a = normalize.compute_content_hash("A", "https://example.com/1")
    b = normalize.compute_content_hash("B", "https://example.com/2")
    assert a != b


def test_parse_date_valid_and_invalid():
    dt = normalize.parse_date({"published": "Wed, 07 May 2026 12:00:00 +0000"})
    assert dt is not None and dt.tzinfo is not None

    # Нет полей даты или мусор → None (статья всё равно сохранится)
    assert normalize.parse_date({"foo": "bar"}) is None
    assert normalize.parse_date({"published": "не дата вовсе"}) is None


def test_parse_date_naive_gets_utc():
    dt = normalize.parse_date({"published": "2026-05-07 12:00:00"})
    assert dt is not None and dt.tzinfo is not None


def test_is_future_date_flags_far_future_only():
    now = datetime.now(timezone.utc)
    assert normalize.is_future_date(now + timedelta(days=30)) is True
    assert normalize.is_future_date(now - timedelta(days=1)) is False
    assert normalize.is_future_date(now + timedelta(hours=1)) is False  # в пределах допуска
    assert normalize.is_future_date(None) is False


def test_parse_date_rejects_future_event_dates():
    # Дата-анонс из будущего (как Equinor «Q3 results — analyst conference») → None,
    # чтобы такие «события» не выдавались за дату публикации.
    future = (datetime.now(timezone.utc) + timedelta(days=120)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    assert normalize.parse_date({"published": future}) is None
