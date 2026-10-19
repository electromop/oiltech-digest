"""Перенос личных отметок и черновиков в общие (#111): шов команды migrate-shared-marks.

Основа — учётка эксперта: её отметки и черновики становятся общими; проверочные отметки
других учёток не переносятся. Перенос сам сверяет состав выпуска закрытых месяцев до и после
и при расхождении ничего не пишет; повторный запуск ничего не меняет.

Часы окна — 19.10.2026: октябрь открыт, сентябрь и август закрыты.
"""

from __future__ import annotations

import pytest

from oiltech_digest import cli, feed_window
from oiltech_digest.db import connection, repository
from oiltech_digest.db.marks_migration import migrate_shared_marks
from tests.digest_user_path_support import add_article, add_signal, msk, utc


def _state(conn, user_id: int, table: str, column: str, row_id: int, status: str) -> None:
    conn.execute(
        f"INSERT INTO {table} (user_id, {column}, status) VALUES (%s, %s, %s)", (user_id, row_id, status),
    )


def _draft(conn, user_id: int | None, month: str, article_ids: list[int], *, title: str = "Черновик") -> None:
    digest_id = conn.execute(
        "INSERT INTO monthly_digests (user_id, month, title, status) VALUES (%s, %s, %s, 'draft') RETURNING id",
        (user_id, month, title),
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
        _draft(conn, expert, "2026-09", [a["mid"], a["hi"]])
        _draft(conn, tester, "2026-09", [a["test"]])
        # Общий черновик месяца, у которого нет черновика основы, — не трогается.
        _draft(conn, None, "2026-08", [a["aug"]], title="Август, общий")
        conn.commit()
    return {"expert": expert, "tester": tester, "a": a, "signal": signal, "test_signal": test_signal}


def test_dry_run_reports_what_moves_and_writes_nothing(base):
    report = migrate_shared_marks(base["expert"])

    assert report["ok"] and not report["written"]
    assert report["articles"]["by_status"] == {"digest": 3, "noise": 1}
    assert report["articles"]["inserted"] == 4
    assert report["signals"]["inserted"] == 1
    assert report["skipped"]["article_marks"] == {"rows": 2, "users": 1}
    assert report["skipped"]["signal_marks"] == {"rows": 1, "users": 1}
    assert report["skipped"]["drafts"] == {"rows": 1, "users": 1}
    assert report["drafts"] == {"copied": ["2026-09"], "kept": []}
    assert report["checked_months"] == ["2026-08", "2026-09"]
    snap = _snapshot()
    assert snap["article_marks"] == [] and snap["signal_marks"] == []
    assert [(row[0], row[3]) for row in snap["drafts"]] == [("2026-08", [base["a"]["aug"]])]


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


def test_second_run_changes_nothing(base):
    migrate_shared_marks(base["expert"], write=True)
    before = _snapshot()

    again = migrate_shared_marks(base["expert"], write=True)

    assert again["ok"]
    assert again["articles"]["inserted"] == 0 and again["signals"]["inserted"] == 0
    assert again["drafts"] == {"copied": [], "kept": ["2026-09"]}
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
    assert [m["month"] for m in report["mismatches"]] == ["2026-09"]
    assert report["mismatches"][0]["before"]["article_ids"] == sorted([base["a"]["hi"], base["a"]["mid"]])
    assert report["mismatches"][0]["after"]["article_ids"] == [base["a"]["mid"]]
    assert _snapshot() == before


def test_command_prints_the_dry_run_and_fails_on_mismatch(base, capsys):
    cli.main(["migrate-shared-marks", "--base-user-id", str(base["expert"])])
    out = capsys.readouterr().out
    assert "пробный прогон" in out and "сверка сошлась" in out
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
