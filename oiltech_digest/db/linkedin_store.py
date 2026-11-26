"""Страницы LinkedIn в базе: источники вида linkedin, их дата начала сбора и расход сервиса Apify.

Расход пишется в общий учёт расхода (ai_processing_runs) своей стадией: строка на запуск сервиса, поставщик apify,
модуль сервиса вместо модели. Так его видит «Статистика», а месячный потолок считается одним запросом.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg.rows import dict_row

from oiltech_digest.db import connection, repository
from oiltech_digest.ingestion import linkedin_posts

STRATEGY = linkedin_posts.STRATEGY


class PageExists(ValueError):
    """Страница уже заведена (адрес или имя заняты): второй источник ей не нужен, а чужой не перезаписывается."""

    def __init__(self, existing: dict[str, Any]) -> None:
        super().__init__(f"страница уже заведена: #{existing['id']} {existing['name']}")
        self.existing = existing


def add_page(raw_url: str, *, name: str | None = None, priority: float = 1.0, category: str | None = None,
             collect_since: datetime | None = None) -> dict[str, Any]:
    """Завести страницу LinkedIn источником: команда add-linkedin-source и форма «Добавить источник». Имя по
    умолчанию: «<имя страницы в адресе> (LinkedIn)». Дата начала сбора по умолчанию та, что ставит база (две недели
    до заведения)."""
    page = linkedin_posts.page_from_url(raw_url)
    if page is None:
        raise ValueError(f"не адрес страницы LinkedIn: {raw_url!r} (нужен linkedin.com/company/<имя>/ "
                         "или linkedin.com/in/<имя>/)")
    name = (name or "").strip() or f"{page.slug} (LinkedIn)"
    existing = source_with_url(page.url) or _source_named(name)
    if existing is not None:
        raise PageExists(existing)
    source_id = repository.add_rss_source(name=name, rss_url="", source_type=linkedin_posts.SOURCE_TYPE, url=page.url,
                                          priority=priority, category=category, parse_strategy=STRATEGY)
    repository.set_source_collection(source_id, listing_url=page.url, network_region="auto", enabled=True)
    if collect_since is not None:
        set_collect_since(source_id, collect_since)
    return {"id": source_id, "name": name, "url": page.url, "kind": page.kind}


def _source_named(name: str) -> dict[str, Any] | None:
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT id, name, enabled, archived_at FROM sources WHERE name = %s AND source_type = %s",
                    (name, linkedin_posts.SOURCE_TYPE))
        return cur.fetchone()


def active_sources() -> list[dict[str, Any]]:
    """Включённые и не архивные страницы LinkedIn: их и собирает шаг."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT * FROM sources WHERE parse_strategy = %s AND enabled AND archived_at IS NULL ORDER BY id",
                    (STRATEGY,))
        return cur.fetchall()


def source_with_url(url: str) -> dict[str, Any] | None:
    """Уже заведённая страница с тем же адресом, в том числе выключенная и архивная: второй источник ей не нужен."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT id, name, enabled, archived_at FROM sources WHERE parse_strategy = %s AND url = %s"
                    " ORDER BY id LIMIT 1", (STRATEGY, url))
        return cur.fetchone()


def set_collect_since(source_id: int, since: datetime) -> None:
    """Дата начала сбора источника: раньше неё посты не берутся."""
    with connection.get_connection() as conn:
        conn.execute("UPDATE sources SET collect_since = %s, updated_at = now() WHERE id = %s", (since, source_id))
        conn.commit()


def record_run(*, stage: str, model: str, cost_usd: float, ok: bool, error: str | None = None) -> None:
    """Строка расхода одного запуска сервиса."""
    with connection.get_connection() as conn:
        conn.execute(
            "INSERT INTO ai_processing_runs (stage, provider, model, cost_usd, status, error_message)"
            " VALUES (%s, 'apify', %s, %s, %s, %s)",
            (stage, model, round(float(cost_usd), 6), "ok" if ok else "failed", (error or None) and error[:500]))
        conn.commit()


def spent_since(stage: str, since: datetime) -> float:
    """Расход стадии с момента since, $."""
    with connection.get_connection() as conn:
        row = conn.execute("SELECT COALESCE(sum(cost_usd), 0) FROM ai_processing_runs WHERE stage = %s"
                           " AND created_at >= %s", (stage, since)).fetchone()
    return float(row[0] or 0)
