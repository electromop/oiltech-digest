"""Общее для тестов готовности материала выпуска (#199): задачи шагов конвейера в очереди и прод-маршрут ИИ. Сами тесты:
test_material_readiness.py (отметка «в дайджест», цепочка шагов, перепроверка) и test_exchange_readiness.py (выпуск
ждёт готовности, «Отправить как есть», журнал обмена, экран «Дайджест»)."""

from __future__ import annotations

from psycopg.rows import dict_row

from oiltech_digest import config
from oiltech_digest.db import connection
from tests.signal_card_support import MODEL

PIPELINE_KINDS = ("process_articles", "signal_summary", "signal_card", "signal_scores")
BULK = "external-ai-bulk"


def jobs(since: int = 0) -> list[dict]:
    with connection.get_connection() as conn:
        return conn.cursor(row_factory=dict_row).execute(
            "SELECT id, kind, queue_name, status, payload_json FROM background_jobs WHERE kind = ANY(%s) AND id > %s"
            " ORDER BY id", (list(PIPELINE_KINDS), since)).fetchall()


def steps(since: int = 0) -> list[tuple]:
    """Задачи шагов конвейера по порядку постановки: вид, полоса, номера, стадии обработки ленты, повод."""
    return [(job["kind"], job["queue_name"], job["payload_json"].get("signal_ids") or job["payload_json"].get("article_ids"),
             job["payload_json"].get("only"), job["payload_json"].get("trigger")) for job in jobs(since)]


def last_job() -> int:
    with connection.get_connection() as conn:
        return int(conn.execute("SELECT COALESCE(max(id), 0) FROM background_jobs").fetchone()[0])


def fail_all() -> None:
    """Задачи шагов упали: сбой модели."""
    with connection.get_connection() as conn:
        conn.execute("UPDATE background_jobs SET status = 'failed', finished_at = now() WHERE kind = ANY(%s)",
                     (list(PIPELINE_KINDS),))
        conn.commit()


def write(*signal_ids: int, **fields: str) -> None:
    """Поля сигналов, как их записал шаг конвейера."""
    with connection.get_connection() as conn:
        conn.execute(f"UPDATE signals SET {', '.join(f'{name} = %s' for name in fields)} WHERE id = ANY(%s)",
                     (*fields.values(), list(signal_ids)))
        conn.commit()


def external(monkeypatch) -> None:
    """Прод-маршрут ИИ: внешний воркер, своя полоса пересчётов, «Карточка» включена."""
    monkeypatch.setattr(config, "EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr(config, "AI_EXECUTION_REGION", "external")
    monkeypatch.setattr(config, "AI_BULK_LANE_ENABLED", True)
    monkeypatch.setattr(config, "SIGNAL_CARD_ENABLED", True)
    monkeypatch.setattr(config, "SIGNAL_CARD_MODEL", MODEL)
