"""Качество источников и дефицит тем считаются по общей отметке (#111).

19–20.10 на проде: оба запроса остались обычными строками, а не f-строками, и обрывок
`{marks.article_marked_sql(...)}` уходил в базу буквально — агент источников падал с
синтаксической ошибкой на каждом прогоне. Тесты планировщика подменяют обе функции
заглушками, поэтому здесь они идут на настоящей базе.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from oiltech_digest.db import connection, marks, repository


def test_source_quality_and_topic_gaps_count_shared_marks(isolated_db):
    now = datetime.now(timezone.utc)
    user_id = int(repository.create_user("analyst@example.test", "long-enough-password")["id"])
    with connection.get_connection() as conn:
        source_id = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('World Oil', 'Media', 'https://news.example.org', TRUE, 'request') RETURNING id"
        ).fetchone()[0]
        parent_id = conn.execute(
            "INSERT INTO tags (name, enabled, sort_order) VALUES ('Технологии', TRUE, 1) RETURNING id"
        ).fetchone()[0]
        tag_id = conn.execute(
            "INSERT INTO tags (parent_id, name, enabled, sort_order) VALUES (%s, 'Бурение', TRUE, 1) RETURNING id",
            (parent_id,),
        ).fetchone()[0]
        ids = {}
        for slug in ("digest", "noise", "duplicate", "plain"):
            article_id = conn.execute(
                "INSERT INTO articles (source_id, title, url, collected_at, raw_text, language) "
                "VALUES (%s, %s, %s, %s, 'Текст.', 'ru') RETURNING id",
                (source_id, f"Материал {slug}", f"https://news.example.org/{slug}", now - timedelta(hours=2)),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO article_cards (article_id, summary, relevant) VALUES (%s, 'Суть.', TRUE)", (article_id,),
            )
            conn.execute(
                "INSERT INTO article_tags (article_id, tag_id, confidence) VALUES (%s, %s, 0.9)", (article_id, tag_id),
            )
            ids[slug] = article_id
        conn.commit()
    for slug in ("digest", "noise", "duplicate"):
        marks.set_article_mark(ids[slug], status=slug, user_id=user_id)
    period_from, period_to = now - timedelta(days=1), now + timedelta(hours=1)

    quality = {row["source_id"]: row for row in repository.compute_source_quality_rows(period_from, period_to)}
    assert (quality[source_id]["digest_count"], quality[source_id]["noise_count"],
            quality[source_id]["duplicate_count"]) == (1, 1, 1)
    gaps = {row["topic"]: row for row in repository.compute_topic_gap_rows(period_from, period_to)}
    assert gaps["Технологии"]["digest_count"] == 1
    assert repository.snapshot_source_quality(period_from, period_to) >= 1


def test_no_sql_fragment_is_left_in_a_plain_string():
    """Сторож того же дефекта: обрывок `{marks.…}` / `{_issue_…}` в строке без префикса f уходит в
    базу буквально. Ищем по всем модулям пакета: строковый литерал, который не часть f-строки."""
    import ast
    from pathlib import Path

    package = Path(repository.__file__).resolve().parents[1]
    left = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        in_fstring = {id(part) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr) for part in node.values}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in in_fstring
                    and ("{marks." in node.value or "{_issue_" in node.value)):
                left.append(f"{path.relative_to(package)}:{node.lineno}")
    assert left == []
