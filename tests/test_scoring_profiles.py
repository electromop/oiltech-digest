"""Два профиля скоринга (сессия G, ADR 0002): «Бизнес-сигналы» и «Технологический радар».

Главное, что держат эти тесты: после выката лента считается тем же набором, что и до него.
Все прежние критерии — профиль business, конвейер видит только его (сумма 100, а не 200),
а сид не добавляет ни строки в профиль, где уже есть хоть один критерий.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from oiltech_digest import api, scoring_profiles
from oiltech_digest.db import connection, repository
from oiltech_digest.processing import external_ai, pipeline
from oiltech_digest.processing.seed import DEFAULT_SCORING_PROFILES, seed_default_scoring_criteria

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts" / "g"


def _criterion(conn, name: str, weight: float, *, profile: str | None = None, enabled: bool = True,
               sort_order: int = 1) -> int:
    if profile is None:  # как пишет код до профилей: колонку не называет
        return conn.execute(
            "INSERT INTO scoring_criteria (name, weight, enabled, sort_order) VALUES (%s, %s, %s, %s) RETURNING id",
            (name, weight, enabled, sort_order),
        ).fetchone()[0]
    return conn.execute(
        "INSERT INTO scoring_criteria (name, weight, enabled, sort_order, profile) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (name, weight, enabled, sort_order, profile),
    ).fetchone()[0]


def _rows(profile: str) -> list[tuple]:
    with connection.get_connection() as conn:
        return conn.execute(
            "SELECT name, weight::float, enabled FROM scoring_criteria WHERE profile = %s ORDER BY sort_order, id",
            (profile,),
        ).fetchall()


def _indexes() -> set[str]:
    with connection.get_connection() as conn:
        return {row[0] for row in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() AND tablename = 'scoring_criteria'"
        )}


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


def _admin_client(conn) -> TestClient:
    user_id = conn.execute(
        "INSERT INTO users (email, password_salt, password_hash, role) "
        "VALUES ('profiles@example.com', 'salt', 'hash', 'admin') RETURNING id"
    ).fetchone()[0]
    conn.commit()
    user = {"id": user_id, "email": "profiles@example.com", "role": "admin"}
    api.app.dependency_overrides[api.require_admin] = lambda: user
    api.app.dependency_overrides[api.require_user] = lambda: user
    return TestClient(api.app)


# --- Схема: только добавления ----------------------------------------------------------------


def test_live_database_keeps_its_criteria_as_business_through_every_rollout_step(isolated_db):
    """Порядок выката ADR 0002 на базе со схемой ae2ada1: SQL до выката → код → SQL после.

    Каждый шаг повторяем — второй прогон не падает. Прежние строки (и выключенные) становятся
    business без UPDATE, старый код между шагами пишет по-старому (ON CONFLICT (name)), а
    init-db после всех шагов не возвращает глобальный индекс имён."""
    _to_schema_before_profiles()
    with connection.get_connection() as conn:
        live = [_criterion(conn, f"Прод {n}", w, sort_order=n) for n, w in enumerate((30, 25, 20, 15, 10), 1)]
        _criterion(conn, "Технологическая новизна", 35, enabled=False, sort_order=9)
        conn.commit()

    _run_script("scoring-profiles-before-deploy.sql")
    _run_script("scoring-profiles-before-deploy.sql")

    with connection.get_connection() as conn:
        assert conn.execute("SELECT DISTINCT profile FROM scoring_criteria").fetchall() == [("business",)]
        # Старый код до выката: сид и «Сохранить» целятся в глобальный индекс имён.
        conn.execute(
            "INSERT INTO scoring_criteria (name, description, weight, keywords_json, keywords_en_json, enabled, sort_order) "
            "VALUES ('Технологическая новизна', 'из сида', 35, '[]', '[]', TRUE, 10) "
            "ON CONFLICT (name) DO UPDATE SET description = COALESCE(scoring_criteria.description, EXCLUDED.description)"
        )
        added = conn.execute(
            "INSERT INTO scoring_criteria (name, weight, enabled, sort_order) VALUES ('Новый с экрана', 0, TRUE, 60) "
            "ON CONFLICT (name) DO UPDATE SET weight = EXCLUDED.weight, enabled = TRUE RETURNING id"
        ).fetchone()[0]
        conn.commit()
    assert _indexes() >= {"idx_scoring_criteria_name", "idx_scoring_criteria_profile_name"}
    # Новый код видит ровно прежний набор ленты (выключенная строка старого сида — нет).
    enabled = repository.list_enabled_scoring_criteria()
    assert [int(row["id"]) for row in enabled] == [*live, added]
    assert sum(float(row["weight"]) for row in enabled) == 100

    _run_script("scoring-profiles-after-deploy.sql")
    _run_script("scoring-profiles-after-deploy.sql")
    connection.init_db()
    connection.init_db()

    assert "idx_scoring_criteria_name" not in _indexes()
    assert "idx_scoring_criteria_profile_name" in _indexes()
    with connection.get_connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE scoring_criteria SET profile = 'digest' WHERE id = %s", (live[0],))


def test_readonly_check_after_rollout_runs_and_changes_nothing(isolated_db):
    seed_default_scoring_criteria()
    before = _rows("business") + _rows("tech_radar")

    _run_script("scoring-profiles-check.sql")

    assert _rows("business") + _rows("tech_radar") == before


# --- Кто что видит ----------------------------------------------------------------------------


def test_pipeline_and_nl_batch_see_only_business_with_sum_100(isolated_db, monkeypatch):
    seed_default_scoring_criteria()

    criteria = repository.list_enabled_scoring_criteria()

    assert {row["profile"] for row in criteria} == {"business"}
    assert sum(float(row["weight"]) for row in criteria) == 100   # а не 200: профиль Б не в ленте
    pipeline._validate_weights(criteria)
    payload = external_ai.build_process_articles_payload({"limit": 5})
    assert [c["id"] for c in payload["criteria"]] == [row["id"] for row in criteria]
    assert {c["profile"] for c in payload["criteria"]} == {"business"}   # профиль едет воркеру в снимке
    tech = repository.list_enabled_scoring_criteria("tech_radar")
    assert [row[0] for row in _rows("tech_radar")] == [row["name"] for row in tech]
    with pytest.raises(ValueError, match="профиль"):
        repository.list_enabled_scoring_criteria("digest")


def test_saving_business_does_not_disable_tech_radar(isolated_db):
    seed_default_scoring_criteria()
    tech_before = _rows("tech_radar")
    business = repository.list_enabled_scoring_criteria()

    repository.save_scoring_criteria([{**row, "weight": 100 if n == 0 else 0} for n, row in enumerate(business[:2])])

    assert _rows("tech_radar") == tech_before
    assert [row[2] for row in _rows("business")] == [True, True, False, False, False]


def test_saving_refuses_a_criterion_of_the_other_profile(isolated_db):
    """id из чужой вкладки не переезжает в этот профиль и не правится из него."""
    seed_default_scoring_criteria()
    tech = repository.list_enabled_scoring_criteria("tech_radar")

    with pytest.raises(ValueError, match="профил"):
        repository.save_scoring_criteria([{**tech[0], "weight": 100}], profile="business")

    assert [row[2] for row in _rows("business")] == [True] * 5
    assert _rows("tech_radar")[0][1] == float(tech[0]["weight"])


def test_same_name_lives_in_both_profiles(isolated_db):
    """«Технологическая новизна» — критерий профиля Б и старого сида: уникальность — в профиле."""
    with connection.get_connection() as conn:
        _criterion(conn, "Технологическая новизна", 100, profile="business")
        conn.commit()

    repository.save_scoring_criteria([{"name": "Технологическая новизна", "weight": 100}], profile="tech_radar")

    assert _rows("business") == [("Технологическая новизна", 100.0, True)]
    assert _rows("tech_radar") == [("Технологическая новизна", 100.0, True)]


def test_duplicate_names_in_a_profile_are_a_readable_refusal(isolated_db):
    with connection.get_connection() as conn:
        first = _criterion(conn, "Один", 50, profile="business")
        second = _criterion(conn, "Два", 50, profile="business", sort_order=2)
        conn.commit()

    with pytest.raises(ValueError, match="имен"):
        repository.save_scoring_criteria([{"id": first, "name": "Один", "weight": 50},
                                          {"id": second, "name": "Один", "weight": 50}])

    assert _rows("business") == [("Один", 50.0, True), ("Два", 50.0, True)]


def test_delete_checks_the_sum_of_its_own_profile(isolated_db):
    """До профилей удаление считало сумму всех активных — с профилем Б она 200, и любое
    удаление отклонялось бы, а сломать сумму профиля можно было бы из соседнего."""
    seed_default_scoring_criteria()
    with connection.get_connection() as conn:
        spare = _criterion(conn, "Нулевой", 0, profile="business", sort_order=99)
        conn.commit()
    business = repository.list_enabled_scoring_criteria()
    tech = repository.list_enabled_scoring_criteria("tech_radar")

    repository.delete_scoring_criterion(spare)       # в business останется ровно 100
    with pytest.raises(ValueError, match="70"):
        repository.delete_scoring_criterion(int(business[0]["id"]))   # 100 − 30
    with pytest.raises(ValueError, match="75"):
        repository.delete_scoring_criterion(int(tech[1]["id"]))       # 100 − 25

    assert len(repository.list_enabled_scoring_criteria()) == 5
    assert len(repository.list_enabled_scoring_criteria("tech_radar")) == 5


# --- Сид ---------------------------------------------------------------------------------------


def test_seed_on_non_empty_profile_adds_nothing(isolated_db):
    """Сид запускается сам в bootstrap (docker-compose, docker-scheduler.sh без SKIP_BOOTSTRAP).

    На проде профиль business непуст: добавь сид в него свой набор, сумма стала бы 200, и
    оценка встала бы на локальных путях и раздулась бы на NL. Переименованный заказчиком
    критерий сид тоже не воскрешает: профиль непуст — не трогаем ничего, и выключенное тоже."""
    with connection.get_connection() as conn:
        _criterion(conn, "Бизнес-эффект (правка заказчика)", 100, profile="business")
        _criterion(conn, "Потенциальный бизнес-эффект", 25, profile="business", enabled=False, sort_order=2)
        conn.commit()

    first = seed_default_scoring_criteria()
    second = seed_default_scoring_criteria()

    assert _rows("business") == [("Бизнес-эффект (правка заказчика)", 100.0, True),
                                 ("Потенциальный бизнес-эффект", 25.0, False)]
    assert first["business"]["added"] == 0 and second["business"]["added"] == 0
    assert first["tech_radar"]["added"] == 5 and second["tech_radar"]["added"] == 0
    assert sum(row[1] for row in _rows("tech_radar")) == 100 and len(_rows("tech_radar")) == 5


def test_seed_fills_empty_profiles_with_the_customer_sets(isolated_db):
    stats = seed_default_scoring_criteria()

    assert stats["business"]["added"] == 5 and stats["tech_radar"]["added"] == 5
    for profile, records in DEFAULT_SCORING_PROFILES.items():
        assert [(row[0], row[1]) for row in _rows(profile)] == [(r["name"], float(r["weight"])) for r in records]
        assert sum(r["weight"] for r in records) == 100


def test_seed_leaves_no_half_profile_when_the_old_name_index_is_still_there(isolated_db):
    """Шаг выката пропущен: сид раньше DROP INDEX idx_scoring_criteria_name. «Технологическая
    новизна» профиля Б упрётся в строку старого сида — профиль не должен остаться из одного
    критерия (следующий сид счёл бы его заведённым), а ошибка — назвать шаг."""
    with connection.get_connection() as conn:
        conn.execute("CREATE UNIQUE INDEX idx_scoring_criteria_name ON scoring_criteria(name)")
        _criterion(conn, "Технологическая новизна", 100, profile="business")
        conn.commit()

    with pytest.raises(RuntimeError, match="idx_scoring_criteria_name"):
        seed_default_scoring_criteria()

    assert _rows("tech_radar") == []
    assert _rows("business") == [("Технологическая новизна", 100.0, True)]


# --- API без параметра профиля — как старый экран ----------------------------------------------


def test_old_screen_without_profile_reads_and_saves_business(isolated_db):
    seed_default_scoring_criteria()
    tech_before = _rows("tech_radar")
    with connection.get_connection() as conn:
        client = _admin_client(conn)
    try:
        listed = client.get("/api/scoring-criteria").json()
        assert [item["name"] for item in listed] == [r["name"] for r in DEFAULT_SCORING_PROFILES["business"]]

        saved = client.put("/api/scoring-criteria", json=[{**item, "weight": 100 if n == 0 else 0}
                                                          for n, item in enumerate(listed)])
        assert saved.status_code == 200, saved.text
    finally:
        api.app.dependency_overrides.clear()

    assert _rows("tech_radar") == tech_before


# --- Снимок критериев в балле ------------------------------------------------------------------


def _article() -> int:
    with connection.get_connection() as conn:
        source_id = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('S', 'News', 'https://s.example', TRUE, 'rss') RETURNING id"
        ).fetchone()[0]
        article_id = conn.execute(
            "INSERT INTO articles (source_id, title, url, collected_at, raw_text, language) "
            "VALUES (%s, 'T', 'https://s.example/1', now(), 'x', 'ru') RETURNING id",
            (source_id,),
        ).fetchone()[0]
        conn.commit()
    return int(article_id)


def _stored_score(article_id: int) -> tuple:
    with connection.get_connection() as conn:
        return conn.execute(
            "SELECT profile, criteria_snapshot FROM article_scores WHERE article_id = %s", (article_id,)
        ).fetchone()


def _worker_scoring(criteria: list[dict], **extra) -> dict:
    return {
        "total_score": 70, "score_label": "Выше средней", "explanation": "e", "model": "gpt", "provider": "openai",
        "items": [{"criterion_id": int(c["id"]), "ai_score": 70, "keyword_score": 0, "final_score": 70,
                   "rationale": "r"} for c in criteria],
        **extra,
    }


def test_score_carries_profile_and_snapshot_of_the_criteria_it_was_computed_with():
    criteria = [
        {"id": 1, "name": "А", "weight": 60, "description": "d", "keywords_json": ["x"], "keywords_en_json": [],
         "profile": "business"},
        {"id": 2, "name": "Б", "weight": 40, "description": "", "keywords_json": [], "keywords_en_json": [],
         "profile": "business"},
    ]

    result = pipeline.normalize_score_payload(
        {"title": "t", "raw_text": "x"}, criteria,
        {"items": [{"criterion_id": 1, "ai_score": 50}, {"criterion_id": 2, "ai_score": 100}]},
    )

    assert result["profile"] == "business"
    snapshot = result["criteria_snapshot"]
    assert [(s["id"], s["name"], s["weight"]) for s in snapshot] == [(1, "А", 60.0), (2, "Б", 40.0)]
    assert [s["text_hash"] for s in snapshot] == [scoring_profiles.criterion_text_hash(c) for c in criteria]
    # Итог складывается из подпунктов по весам снимка — по ним его и можно пересчитать.
    finals = {item["criterion_id"]: item["final_score"] for item in result["items"]}
    assert result["total_score"] == round(sum(finals[s["id"]] * s["weight"] / 100 for s in snapshot), 2)


def test_text_hash_ignores_weight_but_not_what_the_model_and_keywords_see():
    base = {"id": 1, "name": "А", "weight": 60, "description": "d", "keywords_json": ["x"], "keywords_en_json": ["y"]}
    text_hash = scoring_profiles.criterion_text_hash

    assert text_hash(base) == text_hash({**base, "weight": 10}) == text_hash({**base, "id": 7})
    assert text_hash({**base, "description": None}) == text_hash({**base, "description": ""})
    for field, value in (("name", "Б"), ("description", "e"), ("keywords_json", ["z"]), ("keywords_en_json", [])):
        assert text_hash(base) != text_hash({**base, field: value}), field


def test_worker_sends_profile_and_snapshot_with_the_score(monkeypatch):
    """Воркер NL базы не видит: профиль и снимок он берёт из критериев пакета — тех, что ядро
    положило в него при выдаче. Контракт тот же (номер 1): старое ядро лишние поля пропустит."""
    from oiltech_digest.processing.openai_client import OfflineAIClient

    criteria = [{"id": 20, "name": "Значимость", "weight": 100, "description": "", "profile": "business",
                 "keywords_json": [], "keywords_en_json": ["drilling"]}]
    monkeypatch.setattr(external_ai, "make_client", lambda offline: OfflineAIClient())

    result = external_ai.process_payload({
        "offline": True, "criteria": criteria,
        "articles": [{"id": 1, "title": "Drilling automation", "raw_text": "drilling", "language": "en"}],
        "tags": [{"id": 10, "name": "Бурение", "keywords_json": [], "keywords_en_json": ["drilling"]}],
    })

    scoring = result["articles"][0]["scoring"]
    assert scoring["profile"] == "business"
    assert scoring["criteria_snapshot"] == scoring_profiles.criteria_snapshot(criteria)


def test_old_nl_result_without_snapshot_gets_one_from_the_core(isolated_db):
    """NL пересобирают после ядра: итог старой сборки — без профиля и снимка. Ядро строит их
    по id подпунктов — балл всё равно знает, каким профилем и набором посчитан."""
    seed_default_scoring_criteria()
    business = repository.list_enabled_scoring_criteria()
    article_id = _article()

    external_ai.apply_process_result({"articles": [{"article_id": article_id, "scoring": _worker_scoring(business)}]})

    profile, snapshot = _stored_score(article_id)
    assert profile == "business"
    assert snapshot == scoring_profiles.criteria_snapshot(business)


def test_new_nl_snapshot_is_written_as_sent(isolated_db):
    """Снимок воркера — критерии на момент выдачи задачи, то есть то, что видела модель."""
    seed_default_scoring_criteria()
    business = repository.list_enabled_scoring_criteria()
    sent = scoring_profiles.criteria_snapshot(business)
    sent[0]["weight"] = 35.0   # к записи итога вес успели поправить на экране
    article_id = _article()

    external_ai.apply_process_result({"articles": [{
        "article_id": article_id,
        "scoring": _worker_scoring(business, profile="business", criteria_snapshot=sent),
    }]})

    assert _stored_score(article_id) == ("business", sent)


def test_malformed_provenance_from_worker_is_rebuilt_by_the_core(isolated_db):
    seed_default_scoring_criteria()
    business = repository.list_enabled_scoring_criteria()
    article_id = _article()
    broken = [{"id": "x"}], [{"id": 999999, "name": "чужой", "weight": 100, "text_hash": "h"}], "не список"

    for snapshot in broken:
        external_ai.apply_process_result({"articles": [{
            "article_id": article_id,
            "scoring": _worker_scoring(business, profile="digest", criteria_snapshot=snapshot),
        }]})
        assert _stored_score(article_id) == ("business", scoring_profiles.criteria_snapshot(business)), snapshot


def test_article_breakdown_shows_the_weight_it_was_scored_with(isolated_db):
    """Вес в разбивке статьи — из снимка: текущий вес после правки на экране дал бы
    разбивку, которая не складывается в итог. У балла до профилей снимка нет — вес текущий."""
    with connection.get_connection() as conn:
        _criterion(conn, "А", 60, profile="business")
        _criterion(conn, "Б", 40, profile="business", sort_order=2)
        conn.commit()
    rows = repository.list_enabled_scoring_criteria()
    scored, legacy = _article(), None
    with connection.get_connection() as conn:
        source_id = conn.execute("SELECT source_id FROM articles WHERE id = %s", (scored,)).fetchone()[0]
        legacy = conn.execute(
            "INSERT INTO articles (source_id, title, url, collected_at, raw_text, language) "
            "VALUES (%s, 'L', 'https://s.example/2', now(), 'x', 'ru') RETURNING id", (source_id,),
        ).fetchone()[0]
        conn.commit()
    items = [{"criterion_id": int(c["id"]), "ai_score": 70, "keyword_score": 0, "final_score": 70} for c in rows]
    repository.replace_article_score(scored, 70, "Выше средней", "e", items, "gpt", profile="business",
                                     criteria_snapshot=scoring_profiles.criteria_snapshot(rows))
    repository.replace_article_score(legacy, 70, "Выше средней", "e", items, "gpt")
    with connection.get_connection() as conn:
        conn.execute("UPDATE article_scores SET criteria_snapshot = NULL, profile = NULL WHERE article_id = %s", (legacy,))
        conn.commit()

    repository.save_scoring_criteria([{**rows[0], "weight": 30}, {**rows[1], "weight": 70}])

    with connection.get_connection() as conn:
        breakdown = api._score_items_by_article(conn, [scored, legacy])
    assert [item["weight"] for item in breakdown[scored]] == [60.0, 40.0]
    assert [item["weight"] for item in breakdown[legacy]] == [30.0, 70.0]
