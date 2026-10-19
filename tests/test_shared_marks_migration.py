"""Перенос личных отметок и черновиков в общие (#111): шов команд на сервере.

Основа — учётка эксперта: её отметки и черновики становятся общими; проверочные отметки
других учёток не переносятся. Перенос сам сверяет состав выпуска закрытых месяцев до и после
и при расхождении ничего не пишет; повторный запуск ничего не меняет; сверка после переноса —
только чтением. Здесь же — команды сборки выпуска (digest-content, digest-save).

Часы окна — 19.10.2026: октябрь открыт, сентябрь и август закрыты.
"""

from __future__ import annotations

import json

import pytest

from oiltech_digest import cli, feed_window, signal_feedback
from oiltech_digest.db import connection, marks, repository
from oiltech_digest.db.marks_migration import check_shared_marks, migrate_shared_marks
from tests.digest_user_path_support import SEPT, add_article, add_signal, issue_key, msk, utc
from tests.digest_user_path_support import issue  # noqa: F401 — фикстура выпуска


def _state(conn, user_id: int, table: str, column: str, row_id: int, status: str) -> None:
    conn.execute(
        f"INSERT INTO {table} (user_id, {column}, status) VALUES (%s, %s, %s)", (user_id, row_id, status),
    )


def _draft(conn, user_id: int | None, month: str, article_ids: list[int], *, title: str = "Черновик",
           updated_at=None) -> None:
    digest_id = conn.execute(
        "INSERT INTO monthly_digests (user_id, month, title, status, updated_at)"
        " VALUES (%s, %s, %s, 'draft', COALESCE(%s, now())) RETURNING id",
        (user_id, month, title, updated_at),
    ).fetchone()[0]
    for order, article_id in enumerate(article_ids, start=1):
        conn.execute(
            "INSERT INTO monthly_digest_items (digest_id, article_id, sort_order) VALUES (%s, %s, %s)",
            (digest_id, article_id, order),
        )


def _snapshot() -> dict:
    with connection.get_connection() as conn:
        return {
            "article_marks": conn.execute(
                "SELECT article_id, status, set_by_user_id, updated_at FROM article_marks ORDER BY 1").fetchall(),
            "signal_marks": conn.execute(
                "SELECT signal_id, status, set_by_user_id, updated_at FROM signal_marks ORDER BY 1").fetchall(),
            "drafts": conn.execute(
                "SELECT d.month, d.title, d.updated_at, array_agg(i.article_id ORDER BY i.sort_order)"
                " FROM monthly_digests d LEFT JOIN monthly_digest_items i ON i.digest_id = d.id"
                " WHERE d.user_id IS NULL GROUP BY d.id ORDER BY 1").fetchall(),
        }


@pytest.fixture()
def base(isolated_db, monkeypatch):
    """Эксперт (основа) и проверочная учётка; сентябрь закрыт, в нём выпуск эксперта."""
    monkeypatch.setattr(feed_window, "_now", lambda: msk(2026, 10, 19, 12, 0))
    expert = int(repository.create_user("expert@example.test", "long-enough-password", "user")["id"])
    tester = int(repository.create_user("tester@example.test", "long-enough-password", "user")["id"])
    signal = add_signal("sep", first_seen=msk(2026, 9, 12, 0, 20))
    test_signal = add_signal("sep-test", first_seen=msk(2026, 9, 13, 0, 20))
    with connection.get_connection() as conn:
        source = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('World Oil', 'Media', 'https://news.example.org', TRUE, 'request') RETURNING id"
        ).fetchone()[0]
        a = {
            slug: add_article(conn, source, slug, published=published, score=score)
            for slug, published, score in (
                ("hi", utc(2026, 9, 10, 12), 88), ("mid", utc(2026, 9, 11, 12), 64),
                ("noise", utc(2026, 9, 12, 12), 30), ("test", utc(2026, 9, 14, 12), 70),
                ("aug", utc(2026, 8, 14, 12), 70), ("oct", utc(2026, 10, 14, 12), 70),
            )
        }
        for slug, status in (("hi", "digest"), ("mid", "digest"), ("noise", "noise"), ("oct", "digest")):
            _state(conn, expert, "user_article_states", "article_id", a[slug], status)
        _state(conn, expert, "user_signal_states", "signal_id", signal, "digest")
        # Проверочные отметки: не переносятся и в выпуски не попадают.
        _state(conn, tester, "user_article_states", "article_id", a["test"], "digest")
        _state(conn, tester, "user_article_states", "article_id", a["noise"], "digest")
        _state(conn, tester, "user_signal_states", "signal_id", test_signal, "digest")
        _draft(conn, expert, "2026-09", [a["mid"], a["hi"]], updated_at=utc(2026, 10, 2, 9))
        _draft(conn, tester, "2026-09", [a["test"]])
        # Общий черновик месяца, у которого нет черновика основы, — не трогается.
        _draft(conn, None, "2026-08", [a["aug"]], title="Август, общий")
        conn.commit()
    return {"expert": expert, "tester": tester, "a": a, "signal": signal, "test_signal": test_signal}


def _example(signal_id: int, user_id: int) -> None:
    """Пример «выбран в дайджест» до #111: у каждой учётки свой ключ."""
    repository.upsert_signal_agent_memory(
        memory_key=f"digest-selection:{signal_id}:{user_id}", memory_type="signal_verdict", subject="strong_signal",
        score=90, facts={"signal_id": signal_id, "origin": signal_feedback.DIGEST_SELECTION_ORIGIN, "user_id": user_id},
    )


def _active_examples() -> list[tuple[int, int]]:
    with connection.get_connection() as conn:
        return [
            (int(signal_id), int(user_id)) for signal_id, user_id in conn.execute(
                "SELECT facts_json->>'signal_id', facts_json->>'user_id' FROM signal_agent_memory"
                " WHERE status = 'active' AND facts_json->>'origin' = %s ORDER BY 1",
                (signal_feedback.DIGEST_SELECTION_ORIGIN,),
            )
        ]


def test_dry_run_reports_what_moves_and_writes_nothing(base):
    report = migrate_shared_marks(base["expert"])

    assert report["ok"] and not report["written"] and not report["already_done"]
    assert report["articles"]["by_status"] == {"digest": 3, "noise": 1}
    assert report["articles"]["inserted"] == 4
    assert report["signals"]["inserted"] == 1
    assert report["skipped"] == {
        "articles": {"rows": 2, "users": 1}, "signals": {"rows": 1, "users": 1}, "drafts": {"rows": 1, "users": 1},
    }
    assert report["drafts_copied"] == ["2026-09"]
    assert [(m["month"], m["match"]) for m in report["months"]] == [("2026-08", True), ("2026-09", True)]
    snap = _snapshot()
    assert snap["article_marks"] == [] and snap["signal_marks"] == []
    assert [(row[0], row[3]) for row in snap["drafts"]] == [("2026-08", [base["a"]["aug"]])]
    with connection.get_connection() as conn:
        assert conn.execute("SELECT count(*) FROM shared_marks_migration").fetchone()[0] == 0


def test_write_makes_the_expert_state_shared_and_keeps_closed_issues(base):
    a = base["a"]
    report = migrate_shared_marks(base["expert"], write=True)

    assert report["ok"] and report["written"]
    snap = _snapshot()
    assert [(row[0], row[1], row[2]) for row in snap["article_marks"]] == sorted(
        (a[slug], status, base["expert"])
        for slug, status in (("hi", "digest"), ("mid", "digest"), ("noise", "noise"), ("oct", "digest"))
    )
    assert [(row[0], row[1], row[2]) for row in snap["signal_marks"]] == [(base["signal"], "digest", base["expert"])]
    assert [(row[0], row[1], row[3]) for row in snap["drafts"]] == [
        ("2026-08", "Август, общий", [a["aug"]]),
        ("2026-09", "Черновик", [a["mid"], a["hi"]]),
    ]
    # Состав закрытого сентября — прежний выпуск эксперта, без проверочных отметок.
    assert repository.issue_composition("2026-09") == {
        "article_ids": sorted([a["hi"], a["mid"]]), "signal_ids": [base["signal"]],
    }


def test_expert_draft_replaces_an_older_shared_draft_even_if_that_one_is_newer(base):
    """До #111 команда digest-save писала общий черновик (строку без учётки), и на проде такие есть.
    Выпуск эксперта собирался по его черновику, а общий был запасным: черновик эксперта заменяет
    общий своего месяца, даже если общий сохранён позже, — иначе состав разошёлся бы."""
    a = base["a"]
    with connection.get_connection() as conn:
        _draft(conn, None, "2026-09", [a["test"]], title="Сентябрь, старый общий", updated_at=utc(2026, 10, 3, 9))
        _draft(conn, base["expert"], "2026-10", [a["oct"]], updated_at=utc(2026, 10, 5, 9))
        _draft(conn, None, "2026-10", [], title="Октябрь, старый общий", updated_at=utc(2026, 10, 6, 9))
        conn.commit()

    report = migrate_shared_marks(base["expert"], write=True)

    assert report["ok"] and report["drafts_copied"] == ["2026-09", "2026-10"]
    drafts = {row[0]: (row[1], row[3]) for row in _snapshot()["drafts"]}
    assert drafts["2026-09"] == ("Черновик", [a["mid"], a["hi"]])
    assert drafts["2026-10"] == ("Черновик", [a["oct"]])


def test_second_run_changes_nothing(base):
    migrate_shared_marks(base["expert"], write=True)
    before = _snapshot()

    for write in (False, True):
        again = migrate_shared_marks(base["expert"], write=write)
        assert again["already_done"] and again["ok"] and not again["written"]
    assert _snapshot() == before


def test_mismatch_in_a_closed_month_stops_without_writing(base):
    """Подстроенное расхождение: в общих уже стоит «шум» на статье сентябрьского выпуска эксперта
    (общая отметка не перезаписывается) — перенос изменил бы закрытый выпуск и ничего не пишет."""
    with connection.get_connection() as conn:
        conn.execute(
            "INSERT INTO article_marks (article_id, status, set_by_user_id) VALUES (%s, 'noise', %s)",
            (base["a"]["hi"], base["tester"]),
        )
        conn.commit()
    before = _snapshot()

    report = migrate_shared_marks(base["expert"], write=True)

    assert not report["ok"] and not report["written"]
    september = {m["month"]: m for m in report["months"]}["2026-09"]
    assert not september["match"]
    assert september["before"]["article_ids"] == sorted([base["a"]["hi"], base["a"]["mid"]])
    assert september["after"]["article_ids"] == [base["a"]["mid"]]
    assert _snapshot() == before
    with connection.get_connection() as conn:
        assert conn.execute("SELECT count(*) FROM shared_marks_migration").fetchone()[0] == 0


def test_examples_of_other_accounts_go_out_with_their_marks(base):
    """Пример «выбран в дайджест» — один на карточку и только у отмеченной: примеры проверочных
    учёток, чьи отметки не переносятся, гаснут; пример эксперта остаётся."""
    _example(base["signal"], base["expert"])
    _example(base["signal"], base["tester"])
    _example(base["test_signal"], base["tester"])

    assert migrate_shared_marks(base["expert"])["examples_superseded"] == 2
    assert len(_active_examples()) == 3, "пробный прогон ничего не гасит"
    migrate_shared_marks(base["expert"], write=True)
    assert _active_examples() == [(base["signal"], base["expert"])]


def test_check_after_the_switch_reads_only_the_months_closed_at_the_switch(base, monkeypatch):
    """Сверка на проде — только чтением и по месяцам, закрытым на момент переноса: октябрь закроется
    позже, его отметки ставят уже все, и с замороженным архивом эксперта он сойтись не обязан."""
    with pytest.raises(ValueError, match="ещё не сделан"):
        check_shared_marks()
    migrate_shared_marks(base["expert"], write=True)
    # Сентябрь закрыт, в API был бы 409; здесь — подделка «правки задним числом», её сверка ловит.
    marks.set_article_mark(base["a"]["hi"], status="noise", user_id=base["tester"])
    assert not check_shared_marks()["ok"]
    marks.set_article_mark(base["a"]["hi"], status="digest", user_id=base["tester"])

    october = add_article_in("oct2", utc(2026, 10, 20, 12))
    marks.set_article_mark(october, status="digest", user_id=base["tester"])
    monkeypatch.setattr(feed_window, "_now", lambda: msk(2026, 11, 12, 12, 0))  # октябрь закрыт

    report = check_shared_marks()
    assert report["ok"] and report["open_from"] == "2026-10"
    assert [(m["month"], len(m["after"]["signal_ids"])) for m in report["months"]] == [("2026-08", 0), ("2026-09", 1)]
    assert migrate_shared_marks(base["expert"], write=True)["already_done"]


def add_article_in(slug: str, published) -> int:
    with connection.get_connection() as conn:
        source = conn.execute("SELECT id FROM sources ORDER BY id LIMIT 1").fetchone()[0]
        article_id = add_article(conn, source, slug, published=published)
        conn.commit()
    return article_id


def test_command_prints_the_dry_run_and_fails_on_mismatch(base, capsys):
    with pytest.raises(SystemExit) as not_yet:
        cli.main(["migrate-shared-marks", "--check"])
    assert not_yet.value.code == 1 and "ещё не сделан" in capsys.readouterr().out

    cli.main(["migrate-shared-marks", "--base-user-id", str(base["expert"])])
    out = capsys.readouterr().out
    assert "пробный прогон" in out and "сверка сошлась" in out
    assert "2026-09: статей 2, карточек радара 1 — совпадает с прежним выпуском" in out
    assert "не переносятся (проверочные других учёток): отметок статей 2 у 1 учёток" in out

    with connection.get_connection() as conn:
        conn.execute(
            "INSERT INTO signal_marks (signal_id, status) VALUES (%s, 'digest')", (base["test_signal"],),
        )
        conn.commit()
    with pytest.raises(SystemExit) as stopped:
        cli.main(["migrate-shared-marks", "--base-user-id", str(base["expert"]), "--write"])
    assert stopped.value.code == 1
    assert "ОСТАНОВЛЕНО" in capsys.readouterr().out
    with connection.get_connection() as conn:
        assert conn.execute("SELECT count(*) FROM article_marks").fetchone()[0] == 0


def test_server_commands_build_the_same_issue_as_the_screen(issue, tmp_path):
    """digest-content и digest-save на сервере собирают тот же выпуск, что экран (#111): без
    учётки, по общим отметкам и общему черновику, с умолчаниями экрана."""
    analyst, colleague, a, s = issue["analyst"], issue["colleague"], issue["a"], issue["s"]
    analyst.mark(a["low"])
    colleague.mark(a["hi"])
    colleague.mark_signal(s["sep"])
    assert analyst.save(SEPT, [a["low"], a["hi"]]).status_code == 200

    out = tmp_path / "digest.json"
    cli.main(["digest-content", SEPT, "--output", str(out)])
    by_command = [issue_key(item) for item in json.loads(out.read_text(encoding="utf-8"))["news"]]
    assert by_command == analyst.issue() == [("article", a["low"]), ("article", a["hi"]), ("signal", s["sep"])]
    assert repository.issue_composition(SEPT) == {"article_ids": sorted([a["low"], a["hi"]]), "signal_ids": [s["sep"]]}

    cli.main(["digest-save", SEPT])
    saved = colleague.get(f"/api/monthly-digests/{SEPT}").json()
    assert [item["article_id"] for item in saved["items"]] == [a["low"], a["hi"]]
