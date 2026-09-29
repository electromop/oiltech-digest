"""Смена набора критериев профиля командой apply-scoring-preset (решение владельца 29.09).

Сид непустой профиль не трогает — набор на проде меняется только этой командой и явно:
по умолчанию сухой прогон «до / после», с --apply — одной транзакцией. Критерии не удаляются:
на них ссылаются подпункты старых баллов.
"""

from __future__ import annotations

import pytest

from oiltech_digest import api
from oiltech_digest.db import connection, repository
from oiltech_digest.processing.seed import seed_default_scoring_criteria
from oiltech_digest.scoring_profiles import SCORING_PRESETS

VIKTOR_BUSINESS = [
    ("Стратегическая значимость для нефтесервиса", 30.0),
    ("Потенциальный бизнес-эффект", 25.0),
    ("Рыночная возможность / изменение рынка", 20.0),
    ("Практическая применимость", 15.0),
    ("Достоверность и актуальность", 10.0),
]


def _cli(*argv: str) -> None:
    from oiltech_digest import cli

    args = cli.build_parser().parse_args(list(argv))
    args.func(args)


def _rows(profile: str) -> list[tuple]:
    """Строки профиля целиком, с updated_at: «ничего не записано» — значит, не тронута ни одна."""
    with connection.get_connection() as conn:
        return conn.execute(
            "SELECT id, name, description, weight::float, enabled, sort_order, keywords_json, updated_at "
            "FROM scoring_criteria WHERE profile = %s ORDER BY id",
            (profile,),
        ).fetchall()


def _active(profile: str) -> list[tuple[str, float]]:
    with connection.get_connection() as conn:
        return conn.execute(
            "SELECT name, weight::float FROM scoring_criteria WHERE profile = %s AND enabled ORDER BY sort_order, id",
            (profile,),
        ).fetchall()


def _business_before_the_decision() -> dict[str, int]:
    """Профиль business со своим набором (сумма 100) и выключенными старыми критериями, набор
    радара — от сида; статья, оценённая нынешним набором business."""
    with connection.get_connection() as conn:
        ids = {}
        for order, (name, weight, enabled) in enumerate((
            ("Свой 1", 30, True), ("Свой 2", 25, True), ("Свой 3", 15, True), ("Свой 4", 15, True),
            ("Свой 5", 15, True), ("Потенциальный бизнес-эффект", 25, False), ("Старый", 35, False),
        ), 1):
            ids[name] = conn.execute(
                "INSERT INTO scoring_criteria (profile, name, description, weight, enabled, sort_order, keywords_json) "
                "VALUES ('business', %s, 'свой текст', %s, %s, %s, '[\"пилот\"]') RETURNING id",
                (name, weight, enabled, order),
            ).fetchone()[0]
        source_id = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('S', 'News', 'https://s.example', TRUE, 'rss') RETURNING id"
        ).fetchone()[0]
        ids["article"] = conn.execute(
            "INSERT INTO articles (source_id, title, url, collected_at, raw_text, language) "
            "VALUES (%s, 'T', 'https://s.example/1', now(), 'x', 'ru') RETURNING id",
            (source_id,),
        ).fetchone()[0]
        conn.commit()
    seed_default_scoring_criteria()   # business непуст — сид заводит только радар
    items = [{"criterion_id": ids[f"Свой {n}"], "ai_score": 70, "keyword_score": 0, "final_score": 70}
             for n in range(1, 6)]
    repository.replace_article_score(ids["article"], 70, "Выше средней", "e", items, "gpt")
    return ids


def test_dry_run_shows_before_and_after_and_writes_nothing(isolated_db, capsys):
    _business_before_the_decision()
    business, tech = _rows("business"), _rows("tech_radar")

    _cli("apply-scoring-preset", "--profile", "business", "--preset", "viktor")

    out = capsys.readouterr().out
    assert "сухой прогон" in out
    assert "до (активны, сумма 100): Свой 1 30; Свой 2 25; Свой 3 15; Свой 4 15; Свой 5 15" in out
    assert ("после (сумма 100): Стратегическая значимость для нефтесервиса 30; Потенциальный бизнес-эффект 25; "
            "Рыночная возможность / изменение рынка 20; Практическая применимость 15; "
            "Достоверность и актуальность 10") in out
    assert "выключатся (не удаляются: подпункты старых баллов остаются целыми): Свой 1, Свой 2, Свой 3, Свой 4, Свой 5" in out
    assert ("заведутся: Стратегическая значимость для нефтесервиса, Рыночная возможность / изменение рынка, "
            "Практическая применимость, Достоверность и актуальность") in out
    assert "включатся: Потенциальный бизнес-эффект" in out
    assert _rows("business") == business and _rows("tech_radar") == tech


def test_apply_leaves_exactly_the_preset_active_and_keeps_old_scores_whole(isolated_db):
    ids = _business_before_the_decision()
    tech = _rows("tech_radar")

    _cli("apply-scoring-preset", "--profile", "business", "--preset", "viktor", "--apply")

    assert _active("business") == VIKTOR_BUSINESS
    assert sum(weight for _, weight in _active("business")) == 100
    rows = {row[1]: row for row in _rows("business")}
    reused = rows["Потенциальный бизнес-эффект"]
    assert reused[0] == ids["Потенциальный бизнес-эффект"], "совпавший по имени критерий включён, а не заведён заново"
    assert reused[6] == ["пилот"], "ключевые слова, правленные на экране, набор не стирает"
    assert reused[2] == SCORING_PRESETS["viktor"]["business"][1]["description"]
    assert all(not rows[f"Свой {n}"][4] for n in range(1, 6)), "прежний набор выключен"
    assert len(rows) == 7 + 4, "ничего не удалено: 7 прежних строк и 4 новых"
    assert _rows("tech_radar") == tech
    # Подпункты старого балла ссылаются на прежние критерии — разбивка статьи цела.
    with connection.get_connection() as conn:
        breakdown = api._score_items_by_article(conn, [ids["article"]])[ids["article"]]
    assert [item["name"] for item in breakdown] == [f"Свой {n}" for n in range(1, 6)]


def test_second_apply_changes_nothing(isolated_db, capsys):
    _business_before_the_decision()
    _cli("apply-scoring-preset", "--profile", "business", "--preset", "viktor", "--apply")
    business, tech = _rows("business"), _rows("tech_radar")
    capsys.readouterr()

    _cli("apply-scoring-preset", "--profile", "business", "--preset", "viktor", "--apply")

    assert "изменений нет" in capsys.readouterr().out
    assert _rows("business") == business and _rows("tech_radar") == tech


def test_radar_profile_seeded_with_the_same_preset_is_already_applied(isolated_db, capsys):
    _business_before_the_decision()
    tech = _rows("tech_radar")

    _cli("apply-scoring-preset", "--profile", "tech_radar", "--preset", "viktor", "--apply")

    assert "изменений нет" in capsys.readouterr().out
    assert _rows("tech_radar") == tech


def test_stale_scoring_tab_cannot_revert_the_applied_set(isolated_db):
    """Экран «Скоринг» открыт до apply-scoring-preset, «Сохранить» — после (ревью PR #82). Прежний
    UPDATE по id включал выключенные командой критерии обратно, а «выключить лишнее» гасило набор
    заказчика: ответ 200 и молчаливый откат. Теперь — отказ «список устарел», ничего не записано."""
    _business_before_the_decision()
    with connection.get_connection() as conn:
        user_id = conn.execute("INSERT INTO users (email, password_salt, password_hash, role) "
                               "VALUES ('stale@example.com', 's', 'h', 'admin') RETURNING id").fetchone()[0]
        conn.commit()
    user = {"id": user_id, "email": "stale@example.com", "role": "admin"}
    api.app.dependency_overrides[api.require_admin] = lambda: user
    api.app.dependency_overrides[api.require_user] = lambda: user
    try:
        from fastapi.testclient import TestClient

        client = TestClient(api.app)
        stale = client.get("/api/scoring-criteria").json()   # вкладка открыта до смены набора
        _cli("apply-scoring-preset", "--profile", "business", "--preset", "viktor", "--apply")
        applied = _rows("business")
        stale[0]["keywords_json"] = ["правка заказчика"]
        saved = client.put("/api/scoring-criteria", json=stale)
    finally:
        api.app.dependency_overrides.clear()

    assert saved.status_code == 400 and "устарел" in saved.json()["detail"]
    assert _rows("business") == applied
    assert _active("business") == VIKTOR_BUSINESS


def test_broken_preset_is_refused_before_any_write(isolated_db):
    _business_before_the_decision()
    business = _rows("business")

    with pytest.raises(ValueError, match="100"):
        repository.apply_scoring_preset("business", [{"name": "Один", "weight": 60}], apply=True)
    with pytest.raises(ValueError, match="повтор"):
        repository.apply_scoring_preset("business", [{"name": "Один", "weight": 50}, {"name": "Один", "weight": 50}],
                                        apply=True)

    assert _rows("business") == business
