from __future__ import annotations

from oiltech_digest.db import connection
from oiltech_digest.processing import mixed_script


def _card(conn, source_id, url, title, title_ru, summary):
    article_id = conn.execute(
        "INSERT INTO articles (source_id, title, url, raw_text, language, content_hash) "
        "VALUES (%s, %s, %s, 'текст', 'ru', %s) RETURNING id",
        (source_id, title, url, url),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO article_cards (article_id, title_ru, summary) VALUES (%s, %s, %s)",
        (article_id, title_ru, summary),
    )
    return article_id


def _seed(conn):
    source_id = conn.execute(
        "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
        "VALUES ('S', 'News', 'https://example.com', TRUE, 'rss') RETURNING id"
    ).fetchone()[0]
    return {
        "twins": _card(conn, source_id, "https://example.com/1", "Title", "Компания вхoдит в топ", "Суть совместно сExxonMobil."),
        "half": _card(conn, source_id, "https://example.com/2", "Power prices", "Цены растут", "Цены на электроэнergyю выросли."),
        "title": _card(conn, source_id, "https://example.com/3", "Permian output", "Добыча в Пермian растёт", "Чисто."),
        "source": _card(conn, source_id, "https://example.com/4", "Биrol сказал", "Биrol сказал", "Чисто."),
        "clean": _card(conn, source_id, "https://example.com/5", "Clean", "Чисто", "Чисто."),
    }


def test_scripts_only_repair_fixes_twins_and_glue_and_writes_only_changed_fields(isolated_db):
    with connection.get_connection() as conn:
        ids = _seed(conn)
        conn.commit()

    dry = mixed_script.repair_cards(apply=False)
    assert {(c["article_id"], c["field"]) for c in dry["changes"]} == {(ids["twins"], "title_ru"), (ids["twins"], "summary")}

    mixed_script.repair_cards(apply=True)

    with connection.get_connection() as conn:
        cards = {row[0]: row[1:] for row in conn.execute("SELECT article_id, title_ru, summary FROM article_cards")}
    assert cards[ids["twins"]] == ("Компания входит в топ", "Суть совместно с ExxonMobil.")
    assert cards[ids["half"]] == ("Цены растут", "Цены на электроэнergyю выросли.")  # полуперевод — не наша починка


def test_resummarize_selection_separates_summary_title_and_source_defects(isolated_db):
    with connection.get_connection() as conn:
        ids = _seed(conn)
        conn.commit()

    selection = mixed_script.resummarize_selection()

    assert selection == {"summary": [ids["half"]], "title": [ids["title"]], "source_title": [ids["source"]]}


def test_explicit_articles_are_regenerated_without_the_script_check(isolated_db):
    with connection.get_connection() as conn:
        ids = _seed(conn)
        conn.commit()

    selection = mixed_script.resummarize_selection([ids["clean"], ids["half"]])

    assert sorted(selection["summary"]) == sorted([ids["clean"], ids["half"]])

