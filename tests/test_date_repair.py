"""Починка даты публикации собранных статей (#139): в план идёт только дата, которая противоречит
адресу статьи, «стало» — дата нового разбора той же страницы; пишется ровно согласованный план."""

from datetime import datetime, timezone

import pytest

from oiltech_digest.ingestion import date_repair
from tests.test_article_page import PGJ_TITLE, PGJ_URL, _pgj_page

WRONG = datetime(2025, 10, 1, tzinfo=timezone.utc)  # атрибут шаблона Pipeline & Gas Journal
SHOWN = datetime(2026, 10, 6, tzinfo=timezone.utc)  # видимая дата той же статьи
# Главная сайта вместо статьи: «сегодняшняя» дата того же месяца сверку с адресом прошла бы.
HOME_PAGE = b"""<html><head><meta property="og:title" content="Pipeline & Gas Journal"></head><body>
<time datetime="10/27/2026">10/27/2026</time>
<p>Weekly natural gas storage rose as mild weather cut heating demand across the region.</p>
<p>Subscribe to the newsletter to receive the latest pipeline construction updates every week.</p>
</body></html>"""


def _article(**fields) -> dict:
    return {"id": 7345917, "title": PGJ_TITLE, "url": PGJ_URL, "published_at": WRONG,
            "collected_at": datetime(2026, 10, 6, 16, 2, tzinfo=timezone.utc), **fields}


def _run(*articles, page=None):
    return date_repair.repair_dates(list(articles), fetch_fn=lambda url: page if page is not None else _pgj_page(),
                                    pause_seconds=0)


def test_date_that_contradicts_the_url_is_planned_as_the_page_date_that_agrees():
    result = _run(_article())

    assert [(item["article_id"], item["old"], item["new"], item["reason"]) for item in result["changes"]] == [
        (7345917, WRONG, SHOWN, "page date agrees with url")]
    assert result["months"] == [{"old_month": "2025-10", "new_month": "2026-10", "count": 1}]


def test_without_an_agreeing_date_on_the_page_the_planned_date_is_empty():
    result = _run(_article(), page=_pgj_page(""))

    assert [(item["new"], item["reason"]) for item in result["changes"]] == [(None, "no page date agrees with url")]
    assert result["months"][0]["new_month"] == "2026-10", "пустая дата: месяц ленты по дате поступления"


@pytest.mark.parametrize(("fields", "reason"), [
    ({"published_at": datetime(2026, 10, 15, tzinfo=timezone.utc)}, "stored date agrees with url"),
    ({"published_at": None}, "no stored date"),
    ({"url": "https://www.pgjonline.com/news/energy-transfer-to-acquire-vaquero-midstream"}, "url has no date"),
])
def test_without_a_provable_defect_the_article_is_shown_but_not_planned(fields, reason):
    # Так смотрят статьи, под которые правил нет (ADNOC, ДРТ и другие «старше поступления»).
    result = _run(_article(**fields))

    assert (result["changed"], result["stats"]) == (0, {f"skip:{reason}": 1})
    shown = result["skipped"][0]
    assert (shown["defect"], shown["page_status"], shown["page"] is not None) == (False, "read", True)


@pytest.mark.parametrize(("page", "reason", "status"), [
    (b"", "page not downloaded", "not downloaded"),
    (HOME_PAGE, "page is not the article", "not the article"),
])
def test_defect_without_a_trustworthy_page_is_reported_not_guessed(page, reason, status):
    result = _run(_article(), page=page)

    assert result["changed"] == 0
    assert [(item["article_id"], item["defect"], item["reason"], item["page_status"], item["page"])
            for item in result["skipped"]] == [(7345917, True, reason, status, None)]


def test_plan_that_is_not_a_dry_run_is_refused(tmp_path):
    for text in ("{}", '{"changes": [{"article_id": 1, "old": "2025-10-01", "new": null}]}', "не json"):
        path = tmp_path / "plan.json"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError, match="не план repair-article-dates"):
            date_repair.load_plan(path)


def _published(article_id: int):
    from oiltech_digest.db import repository

    with repository.get_connection() as conn:
        return conn.execute("SELECT published_at, title, raw_text FROM articles WHERE id = %s",
                            (article_id,)).fetchone()


def _source_with(*articles) -> tuple[int, list[int]]:
    """Источник PGJ и статьи (заголовок, адрес, дата публикации), собранные сейчас."""
    from oiltech_digest.db import repository

    with repository.get_connection() as conn:
        source = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('Pipeline & Gas Journal', 'Journal', 'https://www.pgjonline.com', TRUE, 'request') RETURNING id"
        ).fetchone()[0]
        ids = [conn.execute(
            "INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language) "
            "VALUES (%s, %s, %s, %s, now(), 'текст статьи', 'en') RETURNING id",
            (source, *fields),
        ).fetchone()[0] for fields in articles]
        conn.commit()
    return source, ids


def test_dry_run_lists_every_article_and_apply_writes_exactly_the_saved_plan(isolated_db, monkeypatch, capsys,
                                                                              tmp_path):
    from oiltech_digest import cli, feed_window
    from oiltech_digest.db import feed_signals, repository
    from oiltech_digest.ingestion import http_client

    monkeypatch.setattr(feed_window, "_now", lambda: datetime(2026, 10, 28, 12, tzinfo=feed_window.MSK))
    agreeing = datetime(2026, 10, 15, tzinfo=timezone.utc)
    source, (wrong, right) = _source_with((PGJ_TITLE, PGJ_URL, WRONG),
                                          ("Occidental curtails Gulf output", PGJ_URL + "-2", agreeing))
    with repository.get_connection() as conn:
        conn.execute("INSERT INTO article_cards (article_id, summary, title_ru, relevant) "
                     "VALUES (%s, 'Суть', 'Energy Transfer покупает Vaquero', TRUE)", (wrong,))
        conn.commit()
    feed_signals.sync_feed_signals([wrong])  # сигнал ленты: его публикация держит копию даты статьи
    monkeypatch.setattr(http_client, "fetch", lambda url: _pgj_page())
    plan = tmp_path / "plan.json"

    cli.main(["repair-article-dates", "--source-id", str(source), "--pause", "0", "--save-plan", str(plan)])
    dry = capsys.readouterr().out

    assert "в плане=1" in dry and "сухой прогон" in dry
    assert f"  {wrong}: 2025-10-01 → 2026-10-06  {PGJ_URL}" in dry
    assert "месяц ленты 2025-10 → 2026-10 (открыт): 1" in dry
    assert f"  {right}: было 2026-10-15, страница not the article; не в плане (stored date agrees with url)" in dry
    assert _published(wrong)[0] == WRONG, "сухой прогон ничего не пишет"

    # Между сухим прогоном и записью страницы не качаются: пишется ровно согласованный план.
    monkeypatch.setattr(http_client, "fetch", lambda url: pytest.fail(f"запись плана качает {url}"))
    cli.main(["repair-article-dates", "--apply", str(plan)])

    assert "записано=1 копий даты у публикаций сигналов ленты=1" in capsys.readouterr().out
    assert _published(wrong) == (SHOWN, PGJ_TITLE, "текст статьи")
    assert _published(right)[0] == agreeing
    with repository.get_connection() as conn:
        copies = conn.execute("SELECT published_at FROM signal_evidence WHERE article_id = %s", (wrong,)).fetchall()
    assert copies == [(SHOWN,)]


def test_date_changed_after_the_dry_run_is_not_overwritten(isolated_db):
    _, (article,) = _source_with((PGJ_TITLE, PGJ_URL, datetime(2026, 10, 5, tzinfo=timezone.utc)))

    written = date_repair.apply_plan([(article, WRONG, SHOWN)])

    assert (written["written_ids"], written["not_written_ids"]) == ([], [article])
    assert _published(article)[0] == datetime(2026, 10, 5, tzinfo=timezone.utc)


def test_apply_takes_only_the_plan(tmp_path):
    from oiltech_digest import cli

    with pytest.raises(SystemExit, match="--apply пишет план сухого прогона"):
        cli.main(["repair-article-dates", "--apply", str(tmp_path / "plan.json"), "--source-id", "12"])
    with pytest.raises(SystemExit, match="missing.json"):
        cli.main(["repair-article-dates", "--apply", str(tmp_path / "missing.json")])


def test_stored_midnight_utc_a_day_off_the_url_is_a_time_zone_shift_not_a_defect():
    # Выпуск в 17:00 по Калифорнии 13.07 — это полночь UTC 14.07; адрес пишут по местному дню.
    article = _article(url="https://techcrunch.com/2026/07/13/startup-raises/",
                       published_at=datetime(2026, 7, 14, tzinfo=timezone.utc))

    assert _run(article)["stats"] == {"skip:stored date agrees with url": 1}
