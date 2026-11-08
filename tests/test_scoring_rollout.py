"""Репетиция выката ADR 0002 на данных, как на проде 29.09 (ревью PR #82).

Критерии: активны «Ценность для нефтесервиса» 30, «Бизнес-эффект» 25, «Зрелость / доказательность»
15, «Применимость» 15, «Новизна публикации» 15; выключены три критерия старого сида. Баллы — до
профилей, как их пишет старый код. Шаги — файлы scripts/g/ и команды CLI, в порядке выката.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from oiltech_digest.db import connection
from oiltech_digest.processing.seed import seed_default_scoring_criteria

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts" / "g"

# (id, имя, вес, активен, порядок) — как на проде 29.09.
PROD = [
    (1, "Технологическая новизна", 35, False, 10),
    (2, "Применимость для РФ и зрелых активов", 30, False, 20),
    (3, "Бизнес-эффект", 25, True, 2),
    (4, "Достоверность и зрелость сигнала", 10, False, 40),
    (789, "Ценность для нефтесервиса", 30, True, 1),
    (791, "Зрелость / доказательность", 15, True, 3),
    (792, "Применимость", 15, True, 4),
    (793, "Новизна публикации", 15, True, 5),
]
PROD_ACTIVE = [789, 3, 791, 792, 793]


def _run_script(name: str) -> None:
    """Файл из scripts/g — так, как его выполнит владелец: целиком, со своими BEGIN/COMMIT."""
    with connection.get_connection() as conn:
        conn.commit()  # тестовое подключение открыло транзакцию своим SET search_path
        conn.autocommit = True
        conn.execute((SCRIPTS / name).read_text(encoding="utf-8"))


def _to_schema_before_profiles() -> None:
    """Схема базы на ae2ada1: без профилей, имя критерия уникально глобально."""
    with connection.get_connection() as conn:
        conn.execute("DROP INDEX IF EXISTS idx_scoring_criteria_profile_name")
        conn.execute("ALTER TABLE scoring_criteria DROP CONSTRAINT IF EXISTS scoring_criteria_profile_check")
        conn.execute("ALTER TABLE scoring_criteria DROP COLUMN IF EXISTS profile")
        conn.execute("ALTER TABLE article_scores DROP COLUMN IF EXISTS profile")
        conn.execute("ALTER TABLE article_scores DROP COLUMN IF EXISTS criteria_snapshot")
        conn.execute("CREATE UNIQUE INDEX idx_scoring_criteria_name ON scoring_criteria(name)")
        conn.commit()


def _prod_like_data() -> dict[int, int]:
    """Схема ae2ada1, критерии прода и баллы до профилей: три статьи сентября, одна августа."""
    _to_schema_before_profiles()
    articles = {}
    with connection.get_connection() as conn:
        for criterion_id, name, weight, enabled, order in PROD:
            conn.execute(
                "INSERT INTO scoring_criteria (id, name, description, weight, enabled, sort_order, keywords_json, "
                "keywords_en_json) OVERRIDING SYSTEM VALUE VALUES (%s, %s, 'текст прода', %s, %s, %s, "
                "'[\"пилот\"]', '[]')",
                (criterion_id, name, weight, enabled, order),
            )
        conn.execute("SELECT setval(pg_get_serial_sequence('scoring_criteria', 'id'), 800)")
        source_id = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('Лента', 'News', 'https://feed.example', TRUE, 'rss') RETURNING id"
        ).fetchone()[0]
        for n, published in ((1, "2026-09-10 12:00+00"), (2, "2026-09-11 12:00+00"),
                             (3, "2026-09-12 12:00+00"), (4, "2026-08-10 12:00+00")):
            article_id = conn.execute(
                "INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language) "
                "VALUES (%s, %s, %s, %s, %s, 'x', 'ru') RETURNING id",
                (source_id, f"Статья {n}", f"https://feed.example/{n}", published, published),
            ).fetchone()[0]
            conn.execute("INSERT INTO article_cards (article_id, summary, relevant) VALUES (%s, 'Суть', TRUE)",
                         (article_id,))
            score_id = conn.execute(
                "INSERT INTO article_scores (article_id, model, total_score, score_label, explanation) "
                "VALUES (%s, 'gpt', 60, 'Средняя', 'e') RETURNING id", (article_id,),
            ).fetchone()[0]
            for criterion_id in PROD_ACTIVE:
                conn.execute(
                    "INSERT INTO article_score_items (article_score_id, criterion_id, keyword_score, ai_score, "
                    "final_score, rationale) VALUES (%s, %s, 0, 60, 60, 'r')", (score_id, criterion_id),
                )
            articles[n] = int(article_id)
        conn.execute("INSERT INTO tags (name, enabled, sort_order) VALUES ('Бурение', TRUE, 1)")
        conn.commit()
    return articles


def _criteria(profile: str | None = None, *, enabled_only: bool = False) -> list[tuple]:
    where, params = [], []
    if profile:
        where.append("profile = %s")
        params.append(profile)
    if enabled_only:
        where.append("enabled")
    sql = "SELECT id, name, weight::float, enabled FROM scoring_criteria"
    if where:
        sql += " WHERE " + " AND ".join(where)
    with connection.get_connection() as conn:
        return conn.execute(sql + " ORDER BY id", params).fetchall()


def _indexes() -> set[str]:
    with connection.get_connection() as conn:
        return {row[0] for row in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() AND tablename = 'scoring_criteria'")}


def _cli(*argv: str) -> None:
    from oiltech_digest import cli

    args = cli.build_parser().parse_args(list(argv))
    args.func(args)


def _to_seeded_profiles() -> dict[int, int]:
    """Данные как на проде, шаги 1 и 3 выката и сид радара."""
    articles = _prod_like_data()
    _run_script("scoring-profiles-before-deploy.sql")
    _run_script("scoring-profiles-after-deploy.sql")
    seed_default_scoring_criteria()
    return articles


def test_code_rollback_after_seed_gives_old_code_one_set_and_its_index(isolated_db):
    """Откат «а» ранбука: после сида прежний код читал бы критерии без фильтра профиля — оба набора,
    сумма 200, — а его «Сохранить» (ON CONFLICT (name)) падало бы без глобального индекса имён.
    После файла отката — один набор с суммой 100 и индекс на месте; повтор безопасен."""
    _to_seeded_profiles()
    _cli("apply-scoring-preset", "--profile", "business", "--preset", "viktor", "--apply")

    _run_script("scoring-profiles-rollback.sql")
    _run_script("scoring-profiles-rollback.sql")

    assert _criteria("tech_radar") == []
    assert "idx_scoring_criteria_name" in _indexes()
    with connection.get_connection() as conn:
        # Так читает и пишет критерии прежний код: без профиля и по глобальному имени.
        weights = [float(row[0]) for row in conn.execute("SELECT weight FROM scoring_criteria WHERE enabled = TRUE")]
        conn.execute("INSERT INTO scoring_criteria (name, weight, enabled, sort_order) VALUES ('Новый', 0, TRUE, 90) "
                     "ON CONFLICT (name) DO UPDATE SET weight = EXCLUDED.weight")
        conn.commit()
    assert len(weights) == 5 and sum(weights) == 100


def test_preset_rollback_printed_by_the_dry_run_brings_back_the_previous_set(isolated_db, capsys):
    """Откат «б»: сухой прогон пресета печатает id прежнего набора и готовый UPDATE — на данных как
    на проде это 789, 3, 791, 792, 793. После записи и этого UPDATE активен прежний набор."""
    _to_seeded_profiles()
    tech = _criteria("tech_radar")
    _cli("apply-scoring-preset", "--profile", "business", "--preset", "viktor")
    rollback = capsys.readouterr().out.split("откат после --apply: ", 1)[1].splitlines()[0]
    assert rollback == ("UPDATE scoring_criteria SET enabled = id IN (789, 3, 791, 792, 793), updated_at = now() "
                        "WHERE profile = 'business';")
    _cli("apply-scoring-preset", "--profile", "business", "--preset", "viktor", "--apply")

    with connection.get_connection() as conn:
        conn.execute(rollback)
        conn.commit()

    active = _criteria("business", enabled_only=True)
    assert sorted(row[0] for row in active) == sorted(PROD_ACTIVE) and sum(row[2] for row in active) == 100
    assert _criteria("tech_radar") == tech


def _new_columns() -> set[str]:
    with connection.get_connection() as conn:
        return {row[0] for row in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name IN ('scoring_criteria', 'article_scores') AND column_name IN ('profile', 'criteria_snapshot')")}


def test_step1_lock_timeout_is_atomic_and_retryable(isolated_db):
    """Шаг 1, когда таблицу держит чужая транзакция: отказ по lock_timeout через 5 с, а не очередь
    впереди ленты; всё — одна транзакция, так что и уже прошедший ALTER article_scores откатан."""
    _prod_like_data()
    holder = connection.get_connection()
    try:
        holder.execute("SELECT count(*) FROM scoring_criteria").fetchone()   # AccessShare до конца транзакции
        with pytest.raises(psycopg.errors.LockNotAvailable):
            _run_script("scoring-profiles-before-deploy.sql")
    finally:
        holder.rollback()
        holder.close()

    assert _new_columns() == set()
    _run_script("scoring-profiles-before-deploy.sql")
    assert _new_columns() == {"profile", "criteria_snapshot"}
