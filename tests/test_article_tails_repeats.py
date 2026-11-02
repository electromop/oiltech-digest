"""Правило повторов (#140): хвост, который есть у других статей источника, отрезается при записи."""

import argparse
from datetime import datetime, timedelta, timezone
import random

import pytest

from oiltech_digest import cli
from oiltech_digest.db import connection, repository
from oiltech_digest.ingestion import article_fetcher, body_repair, external_fetch, normalize

SITE_BLOCK = ("Подписывайтесь на наши новости в социальных сетях и оставайтесь в курсе событий отрасли, "
              "редакция ждёт ваши письма и отзывы каждый рабочий день с девяти до шести часов")
SYLLABLES = "ка ро ти ну ве ло ма зу ге шо ля пи ру да ме сы бо фа ке ню хо ца жи тэ вы".split()


def _sentence(rng: random.Random, words: int = 12) -> str:
    return " ".join("".join(rng.choices(SYLLABLES, k=3)) for _ in range(words)).capitalize() + "."


def _body(seed: int, sentences: int = 14) -> str:
    """Текст без случайных совпадений с другими статьями."""
    rng = random.Random(seed)
    return " ".join(_sentence(rng) for _ in range(sentences))


@pytest.fixture()
def source_id(isolated_db):
    return _new_source("Источник")


def _new_source(name: str) -> int:
    with connection.get_connection() as conn:
        row = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy, category) "
            "VALUES (%s, 'News', %s, TRUE, 'request', 'международные') RETURNING id",
            (name, f"https://{abs(hash(name))}.example.com/"),
        ).fetchone()
        conn.commit()
    return int(row[0])


def _record(source_id: int, number: int, text: str, title: str | None = None) -> dict:
    title = title or " ".join(text.split()[:6]).rstrip(".")  # заголовок есть в начале текста, как на сайтах
    url = f"https://example.com/{source_id}/{number}"
    return {"source_id": source_id, "title": title, "url": url, "published_at": datetime.now(timezone.utc),
            "raw_text": text, "text_truncated": False, "language": "ru",
            "content_hash": normalize.compute_content_hash(title, url), "image_url": None}


def _insert(source_id: int, number: int, text: str, title: str | None = None) -> bool:
    return repository.insert_article(_record(source_id, number, text, title))


def _stored(source_id: int, number: int) -> dict:
    with connection.get_connection() as conn:
        row = conn.execute(
            "SELECT id, raw_text, text_tail, body_hash FROM articles WHERE url = %s",
            (f"https://example.com/{source_id}/{number}",),
        ).fetchone()
    return {"id": row[0], "raw_text": row[1], "text_tail": row[2], "body_hash": row[3]}


def test_block_at_the_end_is_cut_once_two_other_articles_have_it(source_id):
    texts = {number: f"{_body(number)} {SITE_BLOCK}" for number in (1, 2, 3)}

    for number, text in texts.items():
        assert _insert(source_id, number, text)

    # У первых двух ещё нет двух других статей с этим блоком: их срежет перечистка.
    assert _stored(source_id, 1) == {**_stored(source_id, 1), "raw_text": texts[1], "text_tail": None}
    assert _stored(source_id, 2) == {**_stored(source_id, 2), "raw_text": texts[2], "text_tail": None}
    third = _stored(source_id, 3)
    assert third["raw_text"] == _body(3)
    assert third["text_tail"] == f" {SITE_BLOCK}"


def test_article_without_repeats_is_stored_as_is(source_id):
    for number in (1, 2, 3, 4):
        assert _insert(source_id, number, _body(number))

    stored = _stored(source_id, 4)
    assert stored["raw_text"] == _body(4) and stored["text_tail"] is None


def test_quote_shared_in_the_middle_of_the_text_is_not_a_tail(source_id):
    quote = _sentence(random.Random(77), 30)

    for number in (1, 2, 3):
        assert _insert(source_id, number, f"{_body(number)} {quote} {_body(number + 100)}")

    stored = _stored(source_id, 3)
    assert stored["text_tail"] is None and quote in stored["raw_text"]


def test_own_closing_after_a_shared_passage_is_not_cut(source_id):
    # Один абзац у нескольких статей ближе к концу, а за ним у этой свой вывод: конец текста не повторяется.
    shared = _sentence(random.Random(77), 60)
    closing = _sentence(random.Random(78), 20)

    for number in (1, 2):
        assert _insert(source_id, number, f"{_body(number)} {shared}")
    assert _insert(source_id, 3, f"{_body(3)} {shared} {closing}")

    stored = _stored(source_id, 3)
    assert stored["text_tail"] is None and stored["raw_text"].endswith(closing)


def test_short_list_of_titles_is_a_tail_though_only_the_oldest_item_repeats(source_id):
    # Три пункта: самый старый есть у двух других статей, два свежих это заголовки статей источника.
    titles = {number: _sentence(random.Random(1000 + number), 8).rstrip(".") for number in range(1, 9)}

    for number in titles:
        items = [titles[older] for older in range(number - 1, max(number - 4, 0), -1)]
        assert _insert(source_id, number, " ".join([_body(number), *items]), title=titles[number])

    stored = _stored(source_id, 8)
    assert stored["raw_text"] == _body(8)
    assert all(titles[older] in stored["text_tail"] for older in (5, 6, 7))


def test_lists_with_items_moving_between_articles_are_cut(source_id):
    # Как на сайтах с блоком соседних новостей: пункт это заголовок другой статьи, новые сверху,
    # старые уходят. Двух самых свежих пунктов ещё нет в концах двух других статей.
    titles = {number: _sentence(random.Random(1000 + number), 9).rstrip(".") for number in range(1, 13)}
    bodies = {number: _body(number) for number in titles}

    for number in titles:
        items = [titles[older] for older in range(number - 1, max(number - 7, 0), -1)]
        assert _insert(source_id, number, " ".join([bodies[number], *items]), title=titles[number])

    for number in range(7, 13):
        stored = _stored(source_id, number)
        assert stored["raw_text"] == bodies[number]
        assert titles[number - 1] in stored["text_tail"] and titles[number - 6] in stored["text_tail"]


def test_tail_longer_than_half_of_the_text_is_not_cut(source_id):
    block = _body(5, sentences=30)

    for number in (1, 2, 3):
        assert _insert(source_id, number, f"{_body(number, sentences=8)} {block}")

    stored = _stored(source_id, 3)
    assert stored["text_tail"] is None and stored["raw_text"].endswith(block)


def test_nothing_is_cut_if_less_than_full_text_threshold_would_stay(source_id):
    # Меньше половины, но и остаток короче порога полного текста (500 знаков).
    block = _body(5, sentences=4)
    bodies = {number: _body(number, sentences=5) for number in (1, 2, 3)}
    assert all(len(body) < 500 < len(body) + len(block) and len(block) < len(body) for body in bodies.values())

    for number, body in bodies.items():
        assert _insert(source_id, number, f"{body} {block}")

    stored = _stored(source_id, 3)
    assert stored["text_tail"] is None and stored["raw_text"].endswith(block)


def test_replacing_text_by_full_text_fetch_cuts_the_tail_and_returns_what_was_stored(source_id):
    for number in (1, 2):
        assert _insert(source_id, number, f"{_body(number)} {SITE_BLOCK}")
    assert _insert(source_id, 3, "Анонс статьи из ленты.")
    article_id = _stored(source_id, 3)["id"]

    written = repository.update_article_full_text(article_id, f"{_body(3)} {SITE_BLOCK}", False, "ok", "lxml")

    stored = _stored(source_id, 3)
    assert stored["raw_text"] == written == _body(3)
    assert stored["text_tail"] == f" {SITE_BLOCK}"


def test_new_text_without_the_block_clears_the_old_tail(source_id):
    for number in (1, 2, 3):
        assert _insert(source_id, number, f"{_body(number)} {SITE_BLOCK}")
    article_id = _stored(source_id, 3)["id"]

    repository.update_article_full_text(article_id, _body(103), False, "ok", "lxml")

    stored = _stored(source_id, 3)
    assert stored["raw_text"] == _body(103) and stored["text_tail"] is None


def test_status_only_update_leaves_text_and_tail_alone(source_id):
    for number in (1, 2, 3):
        assert _insert(source_id, number, f"{_body(number)} {SITE_BLOCK}")
    before = _stored(source_id, 3)

    assert repository.update_article_full_text(before["id"], None, True, "failed", "lxml", error="x") is None

    assert _stored(source_id, 3) == before


def test_same_text_under_another_address_is_still_stopped_by_the_same_body_check(source_id):
    # Статья срезана, но текст, пришедший второй раз целиком, это всё тот же дубль.
    full = f"{_body(3)} {SITE_BLOCK}"
    for number, text in ((1, f"{_body(1)} {SITE_BLOCK}"), (2, f"{_body(2)} {SITE_BLOCK}"), (3, full)):
        assert _insert(source_id, number, text)
    assert _stored(source_id, 3)["text_tail"]

    assert _insert(source_id, 4, full) is False

    article = {"id": 999_999, "source_id": source_id}
    assert article_fetcher._ownership_rejection(article, full[:60], full) == (
        "identical body already stored for another article of this source"
    )


def test_copies_of_one_article_count_as_one(source_id):
    # Скрытая копия не держит рубеж «то же тело», и копий одной статьи может быть две.
    same = f"{_body(1)} {SITE_BLOCK}"
    assert _insert(source_id, 1, same)
    with connection.get_connection() as conn:
        conn.execute("UPDATE articles SET pending_deletion = TRUE WHERE source_id = %s", (source_id,))
        conn.commit()
    assert _insert(source_id, 2, same)

    assert _insert(source_id, 3, f"{_body(3)} {SITE_BLOCK}")

    assert _stored(source_id, 3)["text_tail"] is None


def test_articles_older_than_30_days_do_not_count(source_id):
    for number in (1, 2):
        assert _insert(source_id, number, f"{_body(number)} {SITE_BLOCK}")
    with connection.get_connection() as conn:
        conn.execute("UPDATE articles SET collected_at = %s WHERE source_id = %s",
                     (datetime.now(timezone.utc) - timedelta(days=31), source_id))
        conn.commit()

    assert _insert(source_id, 3, f"{_body(3)} {SITE_BLOCK}")

    assert _stored(source_id, 3)["text_tail"] is None


def test_articles_of_another_source_do_not_count(source_id):
    other = _new_source("Другой источник")
    for number in (1, 2):
        assert _insert(other, number, f"{_body(number)} {SITE_BLOCK}")

    assert _insert(source_id, 3, f"{_body(3)} {SITE_BLOCK}")

    assert _stored(source_id, 3)["text_tail"] is None


def test_tail_of_an_earlier_article_is_compared_after_it_left_the_text(source_id):
    # В сохранённом тексте хвоста уже нет, но следующие статьи сверяются и по нему.
    for number in (1, 2, 3, 4):
        assert _insert(source_id, number, f"{_body(number)} {SITE_BLOCK}")
    with connection.get_connection() as conn:
        conn.execute("UPDATE articles SET raw_text = %s, text_tail = %s WHERE url LIKE '%%/1' OR url LIKE '%%/2'",
                     ("Короткий текст, хвост которого отрезан раньше.", f" {SITE_BLOCK}"))
        conn.commit()

    assert _insert(source_id, 5, f"{_body(5)} {SITE_BLOCK}")

    assert _stored(source_id, 5)["text_tail"] == f" {SITE_BLOCK}"


def test_articles_collected_on_the_worker_go_through_the_same_rule(source_id):
    for number in (1, 2):
        assert _insert(source_id, number, f"{_body(number)} {SITE_BLOCK}")

    external_fetch.apply_scrape_result({"source_id": source_id, "articles": [
        {key: value for key, value in _record(source_id, 3, f"{_body(3)} {SITE_BLOCK}").items() if key != "source_id"},
    ]})

    assert _stored(source_id, 3)["raw_text"] == _body(3)


def test_full_text_fetch_counts_the_gain_by_the_stored_text(source_id, monkeypatch):
    for number in (1, 2):
        assert _insert(source_id, number, f"{_body(number)} {SITE_BLOCK}")
    assert _insert(source_id, 3, "Анонс статьи из ленты.")
    page = f"<html><body><article><p>{_body(3)}</p><p>{SITE_BLOCK}</p></article></body></html>"
    monkeypatch.setattr(article_fetcher, "fetch", lambda url: page)

    stats = article_fetcher.fetch_full_text(limit=1)

    assert stats["updated"] == 1
    assert stats["replaced"] == [(_stored(source_id, 3)["id"], "Анонс статьи из ленты.", _body(3))]


# ---------------------------------------------------------------------------
#  Перечистка уже собранных: repair-article-bodies
# ---------------------------------------------------------------------------

def _repair(source_id: int, **kwargs) -> dict:
    return body_repair.repair_bodies(
        body_repair.candidate_articles(source_id=source_id, days=30),
        pause_seconds=0, find_tail=body_repair.stored_repeated_tail, **kwargs,
    )


def test_repair_cuts_the_block_from_the_first_articles(source_id):
    texts = {number: f"{_body(number)} {SITE_BLOCK}" for number in (1, 2, 3)}
    for number, text in texts.items():
        assert _insert(source_id, number, text)

    downloads = []
    result = _repair(source_id, apply=True, fetch_fn=downloads.append)

    assert downloads == []
    for number in (1, 2):
        stored = _stored(source_id, number)
        assert stored["raw_text"] == _body(number)
        assert stored["raw_text"] + stored["text_tail"] == texts[number]
        assert stored["body_hash"] == normalize.compute_body_hash(texts[number])
    assert sorted(result["replaced_ids"]) == [_stored(source_id, 1)["id"], _stored(source_id, 2)["id"]]


def test_repair_dry_run_finds_the_tail_and_writes_nothing(source_id, capsys):
    texts = {number: f"{_body(number)} {SITE_BLOCK}" for number in (1, 2, 3)}
    for number, text in texts.items():
        assert _insert(source_id, number, text)
    before = {number: _stored(source_id, number) for number in texts}

    cli.cmd_repair_article_bodies(argparse.Namespace(
        ids="", source_id=None, statuses="", tails=True, all_pages=False, days=30, limit=0, pause=0,
        apply=False, reprocess=True, batch=25, json=False,
    ))

    assert {number: _stored(source_id, number) for number in texts} == before
    printed = capsys.readouterr().out
    assert "replace:tail:tail cut: 2" in printed
    assert f"Источник (id {source_id}): статей 2, знаков {2 * (len(SITE_BLOCK) + 1)}" in printed


def test_repair_goes_to_the_page_when_the_tail_is_longer_than_half(source_id):
    # Комментарии длиннее новости (АНГИ): по повтору резать нельзя, а страница показывает границу.
    comments = _body(5, sentences=30)
    own = {number: _body(number, sentences=8) for number in (1, 2, 3)}
    for number, body in own.items():
        assert _insert(source_id, number, f"{body} {comments}")
    page = (f'<html><body><article><h1>Статья</h1><p>{own[3]}</p>'
            f'<div class="comments"><p>{comments}</p></div></article></body></html>')
    article = next(item for item in body_repair.candidate_articles(source_id=source_id, days=30)
                   if item["id"] == _stored(source_id, 3)["id"])

    result = body_repair.repair_bodies(
        [article], apply=True, pause_seconds=0, fetch_fn=lambda url: page,
        find_tail=body_repair.stored_repeated_tail,
    )

    assert result["stats"] == {"replace:tail:tail cut by page": 1}
    stored = _stored(source_id, 3)
    assert stored["raw_text"] == own[3]
    assert stored["raw_text"] + stored["text_tail"] == f"{own[3]} {comments}"


def test_repair_leaves_the_tail_if_the_page_differs_from_the_stored_text(source_id):
    comments = _body(5, sentences=30)
    for number in (1, 2, 3):
        assert _insert(source_id, number, f"{_body(number, sentences=8)} {comments}")
    page = f'<html><body><article><h1>Статья</h1><p>{_body(55)}</p></article></body></html>'
    before = _stored(source_id, 3)

    result = _repair(source_id, apply=True, fetch_fn=lambda url: page)

    assert result["stats"] == {"skip:tail:page text differs from stored": 3}
    assert _stored(source_id, 3) == before


def test_repair_adds_to_a_tail_cut_earlier(source_id):
    full = f"{_body(3)} {SITE_BLOCK}"
    for number, text in ((1, f"{_body(1)} {SITE_BLOCK}"), (2, f"{_body(2)} {SITE_BLOCK}"), (3, full)):
        assert _insert(source_id, number, text)
    # Вторая часть хвоста появилась у других статей позже: те же слова в конце остатка.
    extra = _body(9, sentences=3)
    with connection.get_connection() as conn:
        conn.execute("UPDATE articles SET raw_text = raw_text || %s WHERE url LIKE '%%/3'", (f" {extra}",))
        conn.commit()
    for number in (4, 5):
        assert _insert(source_id, number, f"{_body(number)} {extra}")

    _repair(source_id, apply=True, fetch_fn=lambda url: None)

    stored = _stored(source_id, 3)
    assert stored["raw_text"] == _body(3)
    assert stored["text_tail"] == f" {extra} {SITE_BLOCK}"


def test_repair_of_tails_only_can_be_narrowed_to_one_source(source_id, capsys):
    other = _new_source("Другой источник")
    for number in (1, 2, 3):
        assert _insert(source_id, number, f"{_body(number)} {SITE_BLOCK}")
        assert _insert(other, number, f"{_body(number + 50)} {SITE_BLOCK}")

    cli.cmd_repair_article_bodies(argparse.Namespace(
        ids="", source_id=other, statuses="", tails=True, all_pages=False, days=30, limit=0, pause=0,
        apply=True, reprocess=False, batch=25, json=False,
    ))

    assert _stored(other, 1)["text_tail"] and _stored(other, 2)["text_tail"]
    assert _stored(source_id, 1)["text_tail"] is None
    assert f"Другой источник (id {other}): статей 2" in capsys.readouterr().out


def test_repair_does_not_trust_a_page_that_ends_long_before_the_repeated_tail(source_id):
    # Сайт отдаёт анонс: конец страницы есть в сохранённом тексте, но это ещё середина материала.
    comments = _body(5, sentences=30)
    own = {number: _body(number, sentences=8) for number in (1, 2, 3)}
    for number, body in own.items():
        assert _insert(source_id, number, f"{body} {comments}")
    teaser = " ".join(own[3].split()[:40])
    page = f"<html><body><article><h1>Статья</h1><p>{teaser}</p></article></body></html>"
    before = _stored(source_id, 3)
    article = next(item for item in body_repair.candidate_articles(source_id=source_id, days=30)
                   if item["id"] == before["id"])

    result = body_repair.repair_bodies([article], apply=True, pause_seconds=0, fetch_fn=lambda url: page,
                                       find_tail=body_repair.stored_repeated_tail)

    assert result["stats"] == {"skip:tail:page ends before the tail": 1}
    assert _stored(source_id, 3) == before


def test_repair_with_all_pages_sees_a_tail_without_repeats_and_heading(source_id):
    # Свои комментарии под статьёй: повтора нет, заголовка нет, видно только по странице.
    body, comment = _body(1), _body(2, sentences=4)
    assert _insert(source_id, 1, f"{body} {comment}")
    page = (f'<html><body><article><h1>Статья</h1><p>{body}</p><div class="comments"><p>{comment}</p></div>'
            "</article></body></html>")
    articles = body_repair.candidate_articles(source_id=source_id, days=30)

    without = body_repair.repair_bodies(articles, apply=False, pause_seconds=0, fetch_fn=lambda url: page,
                                        find_tail=body_repair.stored_repeated_tail, only_tails=True)
    result = body_repair.repair_bodies(articles, apply=True, pause_seconds=0, fetch_fn=lambda url: page,
                                       find_tail=body_repair.stored_repeated_tail, only_tails=True, all_pages=True)

    assert without["replaced"] == 0
    assert result["stats"] == {"replace:tail:tail cut by page": 1}
    stored = _stored(source_id, 1)
    assert stored["raw_text"] == body and stored["text_tail"] == f" {comment}"


def test_repair_with_all_pages_keeps_the_limits_when_nothing_else_points_at_a_tail(source_id):
    # Без других признаков странице-анонсу не верим: срез больше половины не проходит.
    body = _body(1)
    assert _insert(source_id, 1, body)
    teaser = " ".join(body.split()[:60])
    page = f"<html><body><article><h1>Статья</h1><p>{teaser}</p></article></body></html>"
    articles = body_repair.candidate_articles(source_id=source_id, days=30)

    result = body_repair.repair_bodies(articles, apply=True, pause_seconds=0, fetch_fn=lambda url: page,
                                       find_tail=body_repair.stored_repeated_tail, only_tails=True, all_pages=True)

    assert result["stats"] == {"skip:tail:page ends before the tail": 1}
    assert _stored(source_id, 1)["raw_text"] == body
