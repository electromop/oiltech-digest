"""Перенос личных отметок и черновиков в общие (#111, ADR 0004).

Основа общего состояния — одна учётка (на запуске — учётка эксперта): её отметки статей и
карточек радара и её черновики выпуска становятся общими. Отметки и черновики других учёток —
проверочные: не переносятся, команда печатает их число. Личные таблицы остаются архивом.

Всё — в одной транзакции. Перед записью — состав выпуска каждого закрытого месяца по прежнему
правилу (отметки и черновик учётки-основы), после записи в той же транзакции — по новому
(repository.issue_composition). Расхождение — откат, ничего не записано. Пробный прогон
(по умолчанию) делает то же и откатывает в конце: печатает, что перенёс бы, и сверку.

Повторный запуск ничего не меняет: общая отметка, которая уже есть, не перезаписывается, а
общий черновик заменяется черновиком основы, только если он старше его. Общий черновик месяца,
у которого нет черновика основы, не трогается.
"""

from __future__ import annotations

from typing import Any

from oiltech_digest import feed_window
from oiltech_digest.db import connection, repository
from oiltech_digest.feed_window import period_month_sql, signal_month_sql


def _legacy_composition(conn, month: str, base_user_id: int) -> dict[str, list[int]]:
    """Состав выпуска месяца по прежнему правилу для учётки: её отметки и её черновик, а нет
    его — общий черновик (прежний get_monthly_digest(month, user_id))."""
    base = int(base_user_id)
    row = conn.execute(
        """
        SELECT COALESCE(array_agg(i.article_id ORDER BY i.sort_order, i.id), '{}')
        FROM monthly_digest_items i
        WHERE i.digest_id = (
          SELECT d.id FROM monthly_digests d
          WHERE d.month = %s AND (d.user_id = %s OR d.user_id IS NULL)
          ORDER BY (d.user_id IS NOT NULL) DESC, d.updated_at DESC
          LIMIT 1
        )
        """,
        (month, base),
    ).fetchone()
    return repository.issue_composition_by_rule(
        conn, month,
        article_selected_sql=(
            "EXISTS (SELECT 1 FROM user_article_states u WHERE u.article_id = a.id"
            f" AND u.user_id = {base} AND u.status = 'digest')"
        ),
        signal_selected_sql=(
            "EXISTS (SELECT 1 FROM user_signal_states u WHERE u.signal_id = sig.id"
            f" AND u.user_id = {base} AND u.status = 'digest')"
        ),
        draft_article_ids=[int(article_id) for article_id in row[0]],
    )


def _closed_months(conn, base_user_id: int, window: feed_window.FeedWindow) -> list[str]:
    """Закрытые месяцы, где у основы или в общем состоянии есть выбор «в дайджест» или черновик."""
    rows = conn.execute(
        f"""
        SELECT {period_month_sql('a')} FROM user_article_states u JOIN articles a ON a.id = u.article_id
        WHERE u.user_id = %(base)s AND u.status = 'digest'
        UNION
        SELECT {signal_month_sql('s')} FROM user_signal_states u JOIN signals s ON s.id = u.signal_id
        WHERE u.user_id = %(base)s AND u.status = 'digest'
        UNION
        SELECT {period_month_sql('a')} FROM article_marks m JOIN articles a ON a.id = m.article_id
        WHERE m.status = 'digest'
        UNION
        SELECT {signal_month_sql('s')} FROM signal_marks m JOIN signals s ON s.id = m.signal_id
        WHERE m.status = 'digest'
        UNION
        SELECT month FROM monthly_digests WHERE user_id = %(base)s OR user_id IS NULL
        """,
        {"base": int(base_user_id)},
    ).fetchall()
    return sorted(month for (month,) in rows if month and not window.is_open(month))


def _count_by_status(conn, table: str, base_user_id: int) -> dict[str, int]:
    rows = conn.execute(
        f"SELECT status, COUNT(*) FROM {table} WHERE user_id = %s GROUP BY status ORDER BY status",
        (int(base_user_id),),
    ).fetchall()
    return {status: int(count) for status, count in rows}


def _skipped(conn, table: str, base_user_id: int) -> dict[str, int]:
    rows, users = conn.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT user_id) FROM {table} WHERE user_id <> %s",
        (int(base_user_id),),
    ).fetchone()
    return {"rows": int(rows), "users": int(users)}


def _copy_drafts(conn, base_user_id: int) -> dict[str, list[str]]:
    copied: list[str] = []
    kept: list[str] = []
    drafts = conn.execute(
        """
        SELECT id, month, title, status, COALESCE(updated_at, created_at, now())
        FROM monthly_digests WHERE user_id = %s ORDER BY month
        """,
        (int(base_user_id),),
    ).fetchall()
    for draft_id, month, title, status, updated_at in drafts:
        shared = conn.execute(
            "SELECT id, COALESCE(updated_at, created_at) FROM monthly_digests WHERE month = %s AND user_id IS NULL",
            (month,),
        ).fetchone()
        if shared is not None and shared[1] is not None and shared[1] >= updated_at:
            kept.append(month)
            continue
        if shared is None:
            shared_id = conn.execute(
                """
                INSERT INTO monthly_digests (user_id, month, title, status, updated_at)
                VALUES (NULL, %s, %s, %s, %s) RETURNING id
                """,
                (month, title, status, updated_at),
            ).fetchone()[0]
        else:
            shared_id = shared[0]
            conn.execute(
                "UPDATE monthly_digests SET title = %s, status = %s, updated_at = %s WHERE id = %s",
                (title, status, updated_at, shared_id),
            )
        items = conn.execute(
            """
            SELECT article_id, section, editor_note FROM monthly_digest_items
            WHERE digest_id = %s ORDER BY sort_order, id
            """,
            (draft_id,),
        ).fetchall()
        repository._replace_monthly_digest_items(
            conn, shared_id,
            [{"article_id": article_id, "section": section, "editor_note": note} for article_id, section, note in items],
        )
        copied.append(month)
    return {"copied": copied, "kept": kept}


def migrate_shared_marks(base_user_id: int, *, write: bool = False,
                         window: feed_window.FeedWindow | None = None) -> dict[str, Any]:
    """Перенести отметки и черновики учётки-основы в общие. По умолчанию — пробный прогон.

    `ok` False — состав закрытого месяца разошёлся, ничего не записано (`mismatches`)."""
    window = window or feed_window.current()
    base = int(base_user_id)
    with connection.get_connection() as conn:
        user = conn.execute("SELECT id, email FROM users WHERE id = %s", (base,)).fetchone()
        if user is None:
            raise ValueError(f"Учётки №{base} нет")
        months = _closed_months(conn, base, window)
        before = {month: _legacy_composition(conn, month, base) for month in months}
        report: dict[str, Any] = {
            "base_user_id": base,
            "write": write,
            "articles": {"by_status": _count_by_status(conn, "user_article_states", base)},
            "signals": {"by_status": _count_by_status(conn, "user_signal_states", base)},
            "skipped": {
                "article_marks": _skipped(conn, "user_article_states", base),
                "signal_marks": _skipped(conn, "user_signal_states", base),
                "drafts": _skipped(conn, "monthly_digests", base),
            },
            "base_drafts": int(conn.execute(
                "SELECT COUNT(*) FROM monthly_digests WHERE user_id = %s", (base,),
            ).fetchone()[0]),
        }
        report["articles"]["inserted"] = conn.execute(
            """
            INSERT INTO article_marks (article_id, status, analyst_comment, set_by_user_id, updated_at)
            SELECT article_id, status, analyst_comment, user_id, COALESCE(updated_at, now())
            FROM user_article_states WHERE user_id = %s
            ON CONFLICT (article_id) DO NOTHING
            """,
            (base,),
        ).rowcount
        report["signals"]["inserted"] = conn.execute(
            """
            INSERT INTO signal_marks (signal_id, status, analyst_comment, set_by_user_id, updated_at)
            SELECT signal_id, status, analyst_comment, user_id, COALESCE(updated_at, now())
            FROM user_signal_states WHERE user_id = %s
            ON CONFLICT (signal_id) DO NOTHING
            """,
            (base,),
        ).rowcount
        report["drafts"] = _copy_drafts(conn, base)
        after = {month: repository.issue_composition(month, conn=conn) for month in months}
        report["checked_months"] = months
        report["mismatches"] = [
            {"month": month, "before": before[month], "after": after[month]}
            for month in months
            if before[month] != after[month]
        ]
        report["ok"] = not report["mismatches"]
        if write and report["ok"]:
            conn.commit()
        else:
            conn.rollback()
        report["written"] = bool(write and report["ok"])
    return report
