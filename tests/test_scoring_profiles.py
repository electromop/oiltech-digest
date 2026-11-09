"""Два профиля скоринга (сессия G, ADR 0002): «Бизнес-сигналы» и «Технологический радар».

Главное, что держат эти тесты: после выката прежние критерии стали профилем business, список по умолчанию видит
только его (сумма 100, а не 200), а сид не добавляет ни строки в профиль, где уже есть хоть один критерий. Балл
статьи снят 08.11 (баллы ставит второй вызов «Карточки» по профилю категории), его история показывается, как была.
"""

from __future__ import annotations


from psycopg.types.json import Jsonb
import pytest
from fastapi.testclient import TestClient

from oiltech_digest import api, scoring_profiles
from oiltech_digest.db import connection, repository
from oiltech_digest.processing.seed import DEFAULT_SCORING_PROFILES, seed_default_scoring_criteria


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


# --- Кто что видит ----------------------------------------------------------------------------


def test_default_criteria_list_is_business_with_sum_100(isolated_db, monkeypatch):
    seed_default_scoring_criteria()

    criteria = repository.list_enabled_scoring_criteria()

    assert {row["profile"] for row in criteria} == {"business"}
    assert sum(float(row["weight"]) for row in criteria) == 100   # а не 200: профиль Б отдельно
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


def test_api_reads_and_saves_the_profile_of_the_tab(isolated_db):
    seed_default_scoring_criteria()
    business_before = _rows("business")
    with connection.get_connection() as conn:
        client = _admin_client(conn)
    try:
        listed = client.get("/api/scoring-criteria", params={"profile": "tech_radar"}).json()
        assert [item["name"] for item in listed] == [r["name"] for r in DEFAULT_SCORING_PROFILES["tech_radar"]]
        assert {item["profile"] for item in listed} == {"tech_radar"}

        edited = [{**item, "weight": 20} for item in listed]
        saved = client.put("/api/scoring-criteria", params={"profile": "tech_radar"}, json=edited)
        assert saved.status_code == 200, saved.text
        assert saved.json()["profile"] == "tech_radar"
    finally:
        api.app.dependency_overrides.clear()

    assert [row[1] for row in _rows("tech_radar")] == [20.0] * 5
    assert _rows("business") == business_before


def test_api_refuses_unknown_profile_with_400(isolated_db):
    seed_default_scoring_criteria()
    before = _rows("business") + _rows("tech_radar")
    with connection.get_connection() as conn:
        client = _admin_client(conn)
    try:
        read = client.get("/api/scoring-criteria", params={"profile": "digest"})
        write = client.put("/api/scoring-criteria", params={"profile": "digest"},
                           json=[{"name": "Один", "weight": 100}])
    finally:
        api.app.dependency_overrides.clear()

    assert read.status_code == 400 and "профиль" in read.json()["detail"]
    assert write.status_code == 400 and "профиль" in write.json()["detail"]
    assert _rows("business") + _rows("tech_radar") == before


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


def _history_score(article_id: int, items: list[dict], *, profile: str | None = None,
                   snapshot: list[dict] | None = None) -> None:
    """Балл статьи из истории (балл статьи снят 08.11): строка балла с подпунктами, как её записала прежняя оценка."""
    with connection.get_connection() as conn:
        score_id = conn.execute(
            "INSERT INTO article_scores (article_id, model, total_score, score_label, explanation, profile, "
            "criteria_snapshot) VALUES (%s, 'gpt', 70, 'Выше средней', 'e', %s, %s) RETURNING id",
            (article_id, profile, Jsonb(snapshot) if snapshot is not None else None),
        ).fetchone()[0]
        for item in items:
            conn.execute(
                "INSERT INTO article_score_items (article_score_id, criterion_id, keyword_score, ai_score, final_score) "
                "VALUES (%s, %s, %s, %s, %s)",
                (score_id, item["criterion_id"], item["keyword_score"], item["ai_score"], item["final_score"]),
            )
        conn.commit()


def test_text_hash_ignores_weight_but_not_what_the_model_and_keywords_see():
    base = {"id": 1, "name": "А", "weight": 60, "description": "d", "keywords_json": ["x"], "keywords_en_json": ["y"]}
    text_hash = scoring_profiles.criterion_text_hash

    assert text_hash(base) == text_hash({**base, "weight": 10}) == text_hash({**base, "id": 7})
    assert text_hash({**base, "description": None}) == text_hash({**base, "description": ""})
    for field, value in (("name", "Б"), ("description", "e"), ("keywords_json", ["z"]), ("keywords_en_json", [])):
        assert text_hash(base) != text_hash({**base, field: value}), field


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
    _history_score(scored, items, profile="business", snapshot=scoring_profiles.criteria_snapshot(rows))
    _history_score(legacy, items)  # балл до профилей: без снимка

    repository.save_scoring_criteria([{**rows[0], "weight": 30}, {**rows[1], "weight": 70}])

    with connection.get_connection() as conn:
        breakdown = api._score_items_by_article(conn, [scored, legacy])
    assert [item["weight"] for item in breakdown[scored]] == [60.0, 40.0]
    assert [item["weight"] for item in breakdown[legacy]] == [30.0, 70.0]

    # Битый снимок ленту не роняет: вес — текущий.
    for broken in ('[{"id": %d, "weight": "шестьдесят"}]' % rows[0]["id"], '{"id": 1}'):
        with connection.get_connection() as conn:
            conn.execute("UPDATE article_scores SET criteria_snapshot = %s::jsonb WHERE article_id = %s", (broken, scored))
            conn.commit()
            assert [item["weight"] for item in api._score_items_by_article(conn, [scored])[scored]] == [30.0, 70.0]
