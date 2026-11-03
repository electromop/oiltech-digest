"""Правка русских заголовков принятых статей открытых месяцев (решение владельца 03.11): enqueue-title-edit.

Новые принятые статьи правит «Суть и перевод» сам; заголовки, написанные до правки, — только этим прогоном, по слову
владельца: сначала всегда сухой прогон с N и ценой.
"""

from __future__ import annotations

import time
from datetime import datetime

from fastapi.testclient import TestClient

from oiltech_digest import api, feed_window
from oiltech_digest.db import connection
from oiltech_digest.processing import external_ai
from oiltech_digest.processing.openai_client import AIResponse
from oiltech_digest.processing.seed import seed_default_scoring_criteria

OFF_RULES = "Хуситы ударили по НПЗ в Эр-Рияде — Новости о нефти и газе в России и мире - Нефть и капитал"
EDITED = "Хуситы ударили по НПЗ в Эр-Рияде"
COPY = object()


def _cli(*argv: str) -> None:
    """Команда через настоящий разбор аргументов, но без cli.main: тот перенастраивает логирование."""
    from oiltech_digest import cli

    args = cli.build_parser().parse_args(list(argv))
    args.func(args)


def _freeze(monkeypatch) -> None:
    """3 ноября: открыты октябрь и ноябрь (окно ленты до 11-го), сентябрь закрыт."""
    monkeypatch.setattr(feed_window, "_now", lambda: datetime(2026, 11, 3, 12, 0, tzinfo=feed_window.MSK))


def _article(title: str, *, title_ru=COPY, relevant: bool | None = True, published: str = "2026-10-20 12:00+00",
             pending_deletion: bool = False, digest: bool = False) -> int:
    """Статья ленты с карточкой; title_ru по умолчанию — копия заголовка, как у русского до правки."""
    with connection.get_connection() as conn:
        source = conn.execute("SELECT id FROM sources WHERE name = 'Лента'").fetchone() or conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('Лента', 'News', 'https://feed.example', TRUE, 'rss') RETURNING id").fetchone()
        article_id = conn.execute(
            "INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language, "
            "pending_deletion) VALUES (%s, %s, %s, %s, %s, 'Текст публикации.', 'ru', %s) RETURNING id",
            (source[0], title, f"https://feed.example/{time.monotonic_ns()}", published, published, pending_deletion),
        ).fetchone()[0]
        conn.execute("INSERT INTO article_cards (article_id, summary, title_ru, relevant) VALUES (%s, 'Суть', %s, %s)",
                     (article_id, title[:200] if title_ru is COPY else title_ru, relevant))
        if digest:
            user_id = conn.execute("INSERT INTO users (email, password_salt, password_hash) "
                                   "VALUES (%s, 's', 'h') RETURNING id", (f"{article_id}@example.com",)).fetchone()[0]
            conn.execute("INSERT INTO article_marks (set_by_user_id, article_id, status) VALUES (%s, %s, 'digest')",
                         (user_id, article_id))
        conn.commit()
    return int(article_id)


def _translation_run(model: str, input_tokens: int, output_tokens: int) -> None:
    with connection.get_connection() as conn:
        conn.execute(
            "INSERT INTO ai_processing_runs (stage, provider, model, input_tokens, output_tokens, total_tokens, "
            "cost_usd, status) VALUES ('translation', 'openai', %s, %s, %s, %s, 0, 'ok')",
            (model, input_tokens, output_tokens, input_tokens + output_tokens),
        )
        conn.commit()


def _jobs() -> list[tuple]:
    with connection.get_connection() as conn:
        return conn.execute("SELECT kind, queue_name, payload_json FROM background_jobs ORDER BY id").fetchall()


def test_dry_run_counts_russian_copies_off_the_rules_in_open_months_and_enqueues_nothing(
        isolated_db, monkeypatch, capsys):
    _freeze(monkeypatch)
    _article(OFF_RULES)
    # Отметка «в дайджест» — выбор человека, даже если гейт статью ещё не судил.
    _article("«Росатом» завершил оснащение АЭС «Руппур» (Бангладеш) системами управления", relevant=None, digest=True)
    _article("«Роснефть» совершенствует технологии ремонта скважин")  # по правилам
    _article("Halliburton wins a contract — Reuters", title_ru="Halliburton выиграла контракт")  # иноязычный
    _article(OFF_RULES, title_ru=EDITED)  # уже правлен
    _article(OFF_RULES, published="2026-09-20 12:00+00")  # закрытый месяц: выпуск только на просмотр
    _article(OFF_RULES, relevant=False)  # отклонён
    _article(OFF_RULES, pending_deletion=True)  # помечен на удаление
    _translation_run("gpt-6.1-terra", 1_000, 200)

    _cli("enqueue-title-edit")

    out = capsys.readouterr().out
    assert "N=2" in out and "в дайджест: 1" in out
    # (1 000·1 + 200·5) / 10⁶ = 0,002 за вызов; N = 2 → 0,004.
    assert "модель gpt-6.1-terra" in out and "$0.00200 за вызов" in out and "цена ≈ $0.00" in out
    assert "[dry-run]" in out and _jobs() == []


def test_jobs_edit_the_title_on_the_worker_and_the_core_writes_it(isolated_db, monkeypatch, capsys):
    """Задачи — process_articles с only=["translation"] в полосе пересчётов: правит тот же шаг воркера, что у новых
    статей, ядро пишет только заголовок; правленая статья в выборку больше не попадает, стоящая в задаче — тоже."""
    _freeze(monkeypatch)
    seed_default_scoring_criteria()
    first, second = _article(OFF_RULES), _article(OFF_RULES)
    with connection.get_connection() as conn:  # пакет без тематик воркер не примет
        conn.execute("INSERT INTO tags (name, enabled, sort_order) VALUES ('Бурение', TRUE, 1)")
        conn.commit()
    monkeypatch.setattr("oiltech_digest.config.EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr("oiltech_digest.config.AI_EXECUTION_REGION", "external")
    monkeypatch.setattr("oiltech_digest.config.AI_BULK_LANE_ENABLED", True)

    _cli("enqueue-title-edit", "--no-dry-run", "--batch-size", "1")

    assert _jobs() == [("process_articles", "external-ai-bulk",
                        {"article_ids": [one], "limit": 1, "offline": False, "only": ["translation"]})
                       for one in (first, second)]
    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))
    core = TestClient(api.app)
    auth = {"Authorization": "Bearer secret"}
    deadline = time.monotonic() + 2.0  # часы ВМ colima подводятся назад: задача на миг «из будущего»
    job = None
    while job is None and time.monotonic() < deadline:
        job = core.post("/api/external-worker/claim", headers=auth, json={
            "worker_id": "nl-ai-bulk-1", "queues": ["external-ai-bulk"], "capabilities": ["openai"],
        }).json()["job"]
    assert job is not None

    class _Editor:
        def complete_json(self, instructions, user_input, schema, **kwargs):
            assert schema["name"] == "article_title_edit"
            return AIResponse(data={"title_ru": EDITED}, model="gpt-6.1-terra", input_tokens=900, output_tokens=40)

    monkeypatch.setattr(external_ai, "make_client", lambda offline=False: _Editor())
    result = external_ai.process_payload(job["payload"])
    assert set(result["articles"][0]) == {"article_id", "errors", "translation"}
    done = core.post(f"/api/external-worker/jobs/{job['id']}/complete", headers=auth,
                     json={"lease_token": job["lease_token"], "result": result})

    assert done.status_code == 200
    with connection.get_connection() as conn:
        assert conn.execute("SELECT title_ru FROM article_cards WHERE article_id = %s", (first,)).fetchone()[0] == EDITED
    capsys.readouterr()
    _cli("enqueue-title-edit")
    assert "N=0" in capsys.readouterr().out
