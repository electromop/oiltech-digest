"""Пересчёт балла по профилю (сессия G, ADR 0002): без ИИ — rescore-recompute, с ИИ — enqueue-rescore.

Без ИИ — только балл, чей снимок совпадает с текущим набором профиля по id и текстам (другие
только веса). С ИИ — видимые статьи месяца из окна ленты, посчитанные не текущим набором;
сначала всегда сухой прогон с N и стоимостью.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from oiltech_digest import api, scoring_profiles
from oiltech_digest.db import connection, repository
from oiltech_digest.processing import external_ai, pipeline
from oiltech_digest.processing.seed import seed_default_scoring_criteria


def _stored_score(article_id: int) -> tuple:
    with connection.get_connection() as conn:
        return conn.execute(
            "SELECT profile, criteria_snapshot FROM article_scores WHERE article_id = %s", (article_id,)
        ).fetchone()


# --- Пересчёт по профилю: без ИИ и с ИИ --------------------------------------------------------

AI_SCORES = [80, 60, 40, 20, 100]   # с весами 30/25/20/15/10 итог 60


def _source_id() -> int:
    with connection.get_connection() as conn:
        row = conn.execute("SELECT id FROM sources WHERE name = 'Лента'").fetchone()
        if row is None:
            row = conn.execute(
                "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
                "VALUES ('Лента', 'News', 'https://feed.example', TRUE, 'rss') RETURNING id"
            ).fetchone()
            conn.commit()
    return int(row[0])


def _feed_article(n: int, *, published: str = "2026-09-10 12:00+00", pending_deletion: bool = False) -> int:
    source_id = _source_id()
    with connection.get_connection() as conn:
        article_id = conn.execute(
            "INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language, "
            "pending_deletion) VALUES (%s, %s, %s, %s, %s, 'x', 'ru', %s) RETURNING id",
            (source_id, f"Статья {n}", f"https://feed.example/{n}", published, published, pending_deletion),
        ).fetchone()[0]
        conn.execute("INSERT INTO article_cards (article_id, summary, relevant) VALUES (%s, 'Суть', TRUE)", (article_id,))
        conn.commit()
    return int(article_id)


def _give_score(article_id: int, criteria: list[dict], *, snapshot: object = "current") -> None:
    """Балл по набору criteria; snapshot=None — балл до профилей (без снимка и профиля)."""
    items = [{"criterion_id": int(c["id"]), "ai_score": a, "keyword_score": 0, "final_score": a}
             for c, a in zip(criteria, AI_SCORES)]
    total = round(sum(a * float(c["weight"]) / 100 for c, a in zip(criteria, AI_SCORES)), 2)
    stored = scoring_profiles.criteria_snapshot(criteria) if snapshot == "current" else snapshot
    repository.replace_article_score(article_id, total, pipeline.score_label(total), "e", items, "gpt",
                                     profile=criteria[0]["profile"], criteria_snapshot=stored or None)
    if snapshot is None:
        with connection.get_connection() as conn:
            conn.execute("UPDATE article_scores SET criteria_snapshot = NULL, profile = NULL WHERE article_id = %s",
                         (article_id,))
            conn.commit()


def _totals() -> dict[int, float]:
    with connection.get_connection() as conn:
        return {row[0]: float(row[1]) for row in conn.execute("SELECT article_id, total_score FROM article_scores")}


def _old_texts(criteria: list[dict]) -> list[dict]:
    """Снимок того же набора, но первый критерий тогда был описан иначе."""
    return scoring_profiles.criteria_snapshot([{**criteria[0], "description": "прежний текст"}, *criteria[1:]])


def test_recompute_without_ai_takes_only_matching_snapshots_of_its_profile(isolated_db):
    seed_default_scoring_criteria()
    business = repository.list_enabled_scoring_criteria()
    tech = repository.list_enabled_scoring_criteria("tech_radar")
    matching, changed, legacy, radar = (_feed_article(n) for n in range(1, 5))
    _give_score(matching, business)
    _give_score(changed, business, snapshot=_old_texts(business))
    _give_score(legacy, business, snapshot=None)
    _give_score(radar, tech)
    assert set(_totals().values()) == {60.0}
    # Заказчик поменял только веса: тексты и набор те же — модель спрашивать незачем.
    repository.save_scoring_criteria([{**row, "weight": w} for row, w in zip(business, (10, 10, 20, 30, 30))])

    dry = repository.recompute_total_scores_from_items(0.2, 0.8, dry_run=True)
    assert (dry["recomputed"], dry["skipped_changed"], dry["skipped_no_snapshot"]) == (1, 1, 1)
    assert set(_totals().values()) == {60.0}, "сухой прогон ничего не пишет"

    repository.recompute_total_scores_from_items(0.2, 0.8)

    # 80·0,1 + 60·0,1 + 40·0,2 + 20·0,3 + 100·0,3 = 58; остальные — не этим набором или не этим профилем.
    assert _totals() == {matching: 58.0, changed: 60.0, legacy: 60.0, radar: 60.0}
    assert [entry["weight"] for entry in _stored_score(matching)[1]] == [10.0, 10.0, 20.0, 30.0, 30.0]

    repository.save_scoring_criteria([{**row, "weight": w} for row, w in zip(tech, (10, 10, 20, 30, 30))],
                                     profile="tech_radar")
    repository.recompute_total_scores_from_items(0.2, 0.8, profile="tech_radar")
    assert _totals() == {matching: 58.0, changed: 60.0, legacy: 60.0, radar: 58.0}


def _freeze_feed_window(monkeypatch) -> None:
    from datetime import datetime

    from oiltech_digest import feed_window

    monkeypatch.setattr(feed_window, "_now", lambda: datetime(2026, 9, 29, 12, 0, tzinfo=feed_window.MSK))


def _cli(*argv: str) -> None:
    """Команда через настоящий разбор аргументов, но без cli.main: тот перенастраивает логирование."""
    from oiltech_digest import cli

    args = cli.build_parser().parse_args(list(argv))
    args.func(args)


def _scoring_run(model: str, input_tokens: int, output_tokens: int, *, days_ago: int = 1) -> None:
    with connection.get_connection() as conn:
        conn.execute(
            "INSERT INTO ai_processing_runs (stage, provider, model, input_tokens, output_tokens, total_tokens, "
            "cost_usd, status, created_at) VALUES ('scoring', 'openai', %s, %s, %s, %s, 0, 'ok', "
            "now() - make_interval(days => %s))",
            (model, input_tokens, output_tokens, input_tokens + output_tokens, days_ago),
        )
        conn.commit()


def test_enqueue_rescore_dry_run_prints_n_and_cost_and_enqueues_nothing(isolated_db, monkeypatch, capsys):
    _freeze_feed_window(monkeypatch)
    seed_default_scoring_criteria()
    business = repository.list_enabled_scoring_criteria()
    current, changed, legacy = (_feed_article(n) for n in range(1, 4))
    august = _feed_article(4, published="2026-08-10 12:00+00")
    hidden = _feed_article(5, pending_deletion=True)
    _give_score(current, business)
    _give_score(changed, business, snapshot=_old_texts(business))
    for article_id in (legacy, august, hidden):
        _give_score(article_id, business, snapshot=None)
    with connection.get_connection() as conn:
        user_id = conn.execute("INSERT INTO users (email, password_salt, password_hash) "
                               "VALUES ('d@example.com', 's', 'h') RETURNING id").fetchone()[0]
        conn.execute("INSERT INTO user_article_states (user_id, article_id, status) VALUES (%s, %s, 'digest')",
                     (user_id, legacy))
        conn.commit()
    for _ in range(3):
        _scoring_run("gpt-5.4-mini", 10_000, 5_000)
    _scoring_run("gpt-5-mini", 1_000, 1_000)
    for _ in range(5):
        _scoring_run("gpt-5.5", 1_000, 1_000, days_ago=40)   # вне 30 дней — не текущая модель

    _cli("enqueue-rescore", "--profile", "business", "--month", "2026-09")

    out = capsys.readouterr().out
    # В выборке — «другие тексты» и «без снимка» сентября; актуальный, августовский и скрытый — нет.
    assert "видимых оценённых статей 3" in out and "N=2" in out and "выбраны в дайджест: 1" in out
    # (10 000·0,75 + 5 000·4,5) / 10⁶ = 0,03 за вызов; N = 2 → 0,06.
    assert "модель gpt-5.4-mini" in out and "$0.03000 за вызов" in out and "стоимость ≈ $0.06" in out
    assert "[dry-run]" in out
    with connection.get_connection() as conn:
        assert conn.execute("SELECT count(*) FROM background_jobs").fetchone()[0] == 0


def test_enqueue_rescore_puts_scoring_only_batches_into_the_bulk_lane(isolated_db, monkeypatch, capsys):
    """Задачи — process_articles с only=["scoring"]: воркер зовёт одну оценку текущим набором
    business, ядро пишет балл со снимком — пересчитанная статья больше в выборку не попадает."""
    import time

    from oiltech_digest.processing.openai_client import OfflineAIClient

    _freeze_feed_window(monkeypatch)
    seed_default_scoring_criteria()
    business = repository.list_enabled_scoring_criteria()
    first, second = _feed_article(1), _feed_article(2)
    _give_score(first, business, snapshot=None)
    _give_score(second, business, snapshot=_old_texts(business))
    with connection.get_connection() as conn:   # пакет без тематик воркер не примет
        conn.execute("INSERT INTO tags (name, enabled, sort_order) VALUES ('Бурение', TRUE, 1)")
        conn.commit()
    monkeypatch.setattr("oiltech_digest.config.EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr("oiltech_digest.config.AI_EXECUTION_REGION", "external")
    monkeypatch.setattr("oiltech_digest.config.AI_BULK_LANE_ENABLED", True)

    _cli("enqueue-rescore", "--month", "2026-09", "--no-dry-run", "--batch-size", "1")

    with connection.get_connection() as conn:
        jobs = conn.execute("SELECT kind, queue_name, payload_json FROM background_jobs ORDER BY id").fetchall()
    assert [(kind, queue) for kind, queue, _ in jobs] == [("process_articles", "external-ai-bulk")] * 2
    assert [payload for _, _, payload in jobs] == [
        {"article_ids": [first], "limit": 1, "offline": False, "only": ["scoring"]},
        {"article_ids": [second], "limit": 1, "offline": False, "only": ["scoring"]},
    ]

    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))
    core = TestClient(api.app)
    auth = {"Authorization": "Bearer secret"}
    deadline = time.monotonic() + 2.0   # часы ВМ colima подводятся назад: задача на миг «из будущего»
    job = None
    while job is None and time.monotonic() < deadline:
        job = core.post("/api/external-worker/claim", headers=auth, json={
            "worker_id": "nl-ai-bulk-1", "queues": ["external-ai-bulk"], "capabilities": ["openai"],
        }).json()["job"]
    assert job is not None and job["payload"]["only"] == ["scoring"]
    assert [c["id"] for c in job["payload"]["criteria"]] == [c["id"] for c in business]
    monkeypatch.setattr(external_ai, "make_client", lambda offline=False: OfflineAIClient())
    result = external_ai.process_payload(job["payload"])
    assert set(result["articles"][0]) == {"article_id", "errors", "scoring"}

    done = core.post(f"/api/external-worker/jobs/{job['id']}/complete", headers=auth,
                     json={"lease_token": job["lease_token"], "result": result})

    assert done.status_code == 200
    assert _stored_score(first) == ("business", scoring_profiles.criteria_snapshot(business))
    capsys.readouterr()
    _cli("enqueue-rescore", "--month", "2026-09")
    assert "N=1" in capsys.readouterr().out   # пересчитанная статья выпала из выборки


def test_enqueue_rescore_refuses_what_it_must_not_do(isolated_db, monkeypatch):
    _freeze_feed_window(monkeypatch)
    seed_default_scoring_criteria()
    _give_score(_feed_article(1), repository.list_enabled_scoring_criteria(), snapshot=None)
    monkeypatch.setattr("oiltech_digest.config.EXTERNAL_WORKERS_ENABLED", False)

    with pytest.raises(SystemExit, match="внешний контур"):
        _cli("enqueue-rescore", "--month", "2026-09", "--no-dry-run")
    with pytest.raises(SystemExit, match="окна ленты"):
        _cli("enqueue-rescore", "--month", "2026-07")
    with pytest.raises(SystemExit, match="не подключён"):
        _cli("enqueue-rescore", "--profile", "tech_radar", "--month", "2026-09")
    with connection.get_connection() as conn:
        assert conn.execute("SELECT count(*) FROM background_jobs").fetchone()[0] == 0
