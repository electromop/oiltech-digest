"""Единое хранилище сигналов (#95, ADR 0003): статья, принятая отбором, — сигнал «бизнес»
происхождения «лента»; перепечатка — публикация сигнала главной статьи; радар при этом
выдаёт ровно то же, что раньше. Шов — API списка сигналов на изолированной базе.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from oiltech_digest import api
from oiltech_digest.db import repository
from oiltech_digest.processing import external_ai

DRILLING = "Бурение, заканчивание и внутрискважинные работы"
ECOLOGY = "Экология, промышленная безопасность и охрана труда"


@pytest.fixture()
def user(isolated_db):
    return repository.create_user("unified-store@example.test", "long-enough-password", "user")


@pytest.fixture()
def admin(isolated_db):
    return repository.create_user("unified-store-admin@example.test", "long-enough-password", "admin")


def _get(path, user, **params):
    api.app.dependency_overrides[api.require_user] = lambda: user
    try:
        return TestClient(api.app).get(path, params=params)
    finally:
        api.app.dependency_overrides.pop(api.require_user, None)


def _rows(response):
    assert response.status_code == 200, response.text
    return response.json()


def _tag(name):
    with repository.get_connection() as conn:
        row = conn.execute("SELECT id FROM tags WHERE name = %s AND parent_id IS NULL", (name,)).fetchone()
        if row is None:
            row = conn.execute("INSERT INTO tags (name, enabled) VALUES (%s, TRUE) RETURNING id", (name,)).fetchone()
        conn.commit()
    return int(row[0])


def _article(n, *, collected=None):
    # Сбор «сейчас»: окна радара (неделя итогов, 30 дней снимка дедупа) считаются от текущего
    # дня, и сигнал ленты должен в них попадать в любой день прогона.
    collected = collected or datetime.now(timezone.utc)
    with repository.get_connection() as conn:
        source = conn.execute("SELECT id FROM sources WHERE name = 'Лента'").fetchone()
        if source is None:
            source = conn.execute(
                "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
                "VALUES ('Лента', 'News', 'https://feed.example', TRUE, 'rss') RETURNING id"
            ).fetchone()
        article_id = conn.execute(
            "INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language) "
            "VALUES (%s, %s, %s, %s, %s, 'текст', 'en') RETURNING id",
            (source[0], f"Article {n}", f"https://feed.example/{n}", collected, collected),
        ).fetchone()[0]
        conn.commit()
    return int(article_id)


def _process(article_id, *, relevant=True, summary="Суть статьи", title_ru=None, theme=DRILLING, score=70.0):
    """Итог обработки статьи воркером — так, как его применяет ядро."""
    item = {"article_id": article_id, "relevance": {"relevant": relevant, "reason": "r", "model": "m"}}
    if relevant:
        item["summary"] = {"summary": summary, "model": "m"}
        item["translation"] = {"title_ru": title_ru or f"Статья {article_id}", "model": "m"}
        item["tagging"] = {"tag_id": _tag(theme), "confidence": 0.9, "model": "m"}
        item["scoring"] = {"total_score": score, "score_label": "Выше средней", "explanation": "e", "items": [],
                           "model": "m", "profile": "business",
                           "criteria_snapshot": [{"id": 1, "name": "к", "weight": 100, "text_hash": "h"}]}
    external_ai.apply_process_result({"articles": [item]})


def _business(user, **params):
    return _rows(_get("/api/signals", user, showcase="business", **params))


def test_accepted_article_becomes_a_business_signal_with_the_article_as_its_publication(user):
    accepted = _article(1)
    rejected = _article(2)

    _process(accepted, summary="Halliburton получил контракт", title_ru="Контракт Halliburton", score=72)
    _process(rejected, relevant=False)

    cards = _business(user)
    assert len(cards) == 1
    card = cards[0]
    assert card["origin"] == "feed"
    assert card["signal_category"] == "business"
    assert card["title_ru"] == "Контракт Halliburton"
    assert card["summary"] == "Halliburton получил контракт"
    assert card["theme"] == DRILLING
    assert card["score"] == 72
    assert [(item["source_url"], item["article_id"]) for item in card["evidence"]] == [
        ("https://feed.example/1", accepted)
    ]

    # Повторная обработка той же статьи второй сигнал не создаёт.
    _process(accepted, summary="Halliburton получил контракт", title_ru="Контракт Halliburton", score=72)
    assert [row["id"] for row in _business(user)] == [card["id"]]


def _radar_card(key, title, *, category=None, url=None, score=60, **extra):
    signal_id = repository.upsert_signal({
        "signal_key": key, "title": title, "title_ru": title, "theme": DRILLING, "maturity": "watch",
        "score": score, "signal_category": category, **extra,
    })
    repository.upsert_signal_evidence(signal_id, {"source_url": url or f"https://radar.example/{key}", "title": title})
    repository.refresh_signal_evidence_count(signal_id)
    return signal_id


def _radar_views(user, admin):
    """Всё, что радар отдаёт наружу: список, сводка, «Отсеянные и скрытые», итоги недели,
    снимки для поиска и дедупа, кандидаты в архив, замер полноты."""
    def strip(rows):
        return [{key: value for key, value in row.items() if key not in ("user_status_updated_at",)} for row in rows]
    return {
        "list": strip(_rows(_get("/api/signals", user))),
        "summary": _rows(_get("/api/signals/summary", user)),
        "hidden": strip(_rows(_get("/api/signals", admin, hidden=True))),
        "hidden_summary": _rows(_get("/api/signals/summary", admin, hidden=True)),
        "weekly": _rows(_get("/api/signals/weekly", admin)),
        "dedup": repository.list_signals_for_dedup(),
        "reviewed_urls": sorted(repository.list_reviewed_signal_urls()),
        "archive": repository.archive_signal_candidates(created_before="2100-01-01", free_theme_only=False),
        "recall": sorted(row["id"] for row in repository.radar_cards_for_recall()),
    }


def test_radar_shows_exactly_the_same_with_feed_signals_in_the_store(user, admin):
    _tag(DRILLING)
    _radar_card("tech", "Роботизированная буровая")
    _radar_card("biz", "Сделка ZenaTech", category="business")
    _radar_card("filtered", "Отсеянная находка", filter_stage="judge", filter_reason="нет технологии")
    # Находка радара с адресом, который потом соберёт и лента: ссылка остаётся у радара.
    shared = _radar_card("shared", "Датчик утечек на трубопроводе", url="https://feed.example/7")
    before = _radar_views(user, admin)

    for n in (5, 6, 7):
        _process(_article(n), title_ru=f"Бизнес-новость {n}")
    article = _article(8)
    _process(article)
    repository.set_article_relevance(article, False, "передумал")
    # Отзыв о сигнале ленты (его даст сервис ОС) — не отзыв радара.
    reviewed_feed = next(card for card in _business(user) if card["origin"] == "feed" and card["evidence"])
    with repository.get_connection() as conn:
        conn.execute("INSERT INTO signal_feedback_events (signal_id, event_type, verdict, source_url) "
                     "VALUES (%s, 'verdict', 'approved', %s)",
                     (reviewed_feed["id"], reviewed_feed["evidence"][0]["source_url"]))
        conn.commit()

    assert len(_business(user)) == 4  # три статьи ленты и бизнес-находка радара
    assert _radar_views(user, admin) == before
    assert _rows(_get("/api/signals", user, q="#" + str(shared)))[0]["evidence"][0]["source_url"] == "https://feed.example/7"


def test_radar_finding_with_the_address_of_a_feed_article_is_stored_as_without_the_feed(user):
    # Адрес публикации уникален: ссылку держит кто-то один. Радар ведёт себя так, будто сигналов
    # ленты нет, — находка с адресом статьи ленты становится карточкой радара со своей ссылкой.
    from oiltech_digest import signal_discovery

    article = _article(9)
    _process(article)
    finding = signal_discovery._normalize_signal_payload(
        {"title": "Датчик утечек", "summary": "датчик", "maturity": "shortlist", "score": 70}, DRILLING)
    finding.update({"signal_key": "leak", "evidence_count": 1,
                    "evidence": [{"source_url": "https://feed.example/9", "title": "Leak sensor"}]})
    config = signal_discovery.SignalDiscoveryConfig(offline=False, dry_run=False, persist_training_examples=False)

    signal_discovery.apply_discovery(config, {"topics": [{"topic": DRILLING, "candidates": [
        {"signal": finding, "rejected": False}]}]})

    (card,) = _rows(_get("/api/signals", user))
    assert [item["source_url"] for item in card["evidence"]] == ["https://feed.example/9"]
    # Сигнал ленты остаётся в витрине «бизнес» по своей статье; до агента дедупа — два сигнала.
    assert [row["origin_article_id"] for row in _business(user)] == [article]


def test_business_showcase_has_radar_business_finds_and_its_own_summary(user, admin):
    _tag(DRILLING)
    _radar_card("tech", "Роботизированная буровая")
    radar_business = _radar_card("biz", "Сделка ZenaTech", category="business", score=80)
    _radar_card("biz-mixed", "Две разные сделки", category="business", mixed_events=True)
    _process(_article(1), score=50)

    cards = _business(user)
    assert [(card["id"], card["origin"]) for card in cards][0] == (radar_business, "radar")
    assert sorted(card["origin"] for card in cards) == ["feed", "radar"]
    assert all(card.get("hidden_reason") is None for card in cards)

    summary = _rows(_get("/api/signals/summary", user, showcase="business"))
    assert summary["total"] == 2 and summary["matching"] == 2
    assert _rows(_get("/api/signals/summary", admin, showcase="business"))["hidden"] == 0
    assert summary["themes"] == [{"theme": DRILLING, "count": 2}]
    assert _rows(_get("/api/signals/summary", user, showcase="business", min_score=60))["matching"] == 1
    # Отсеянных у бизнес-витрины нет: это разбор работы радара.
    assert _get("/api/signals", user, showcase="business", hidden=True).status_code == 422


def test_feed_signal_follows_resummary_retagging_and_rescoring(user):
    article = _article(1)
    _process(article, summary="Первая суть", title_ru="Первый заголовок", score=50)

    external_ai.apply_process_result(
        {"articles": [{"article_id": article, "summary": {"summary": "Новая суть", "model": "m"},
                       "translation": {"title_ru": "Новый заголовок", "model": "m"},
                       "relevance": {"relevant": True, "reason": "r"}}]},
        only=["summary", "translation"],
    )
    repository.upsert_article_tag(article, _tag(ECOLOGY), 0.8, "другая тема")
    repository.replace_article_score(article, 88.0, "Высокая", "e", [], "m", profile="business",
                                     criteria_snapshot=[{"id": 1, "name": "к", "weight": 100, "text_hash": "h"}])

    (card,) = _business(user)
    assert (card["title_ru"], card["summary"], card["theme"], card["score"]) == (
        "Новый заголовок", "Новая суть", ECOLOGY, 88)


def test_feed_signal_leaves_the_showcase_with_its_article(user):
    kept, rejected_later, deleted = _article(1), _article(2), _article(3)
    for article in (kept, rejected_later, deleted):
        _process(article)
    repository.set_article_relevance(rejected_later, False, "перепроверка: не нефтесервис")
    with repository.get_connection() as conn:
        conn.execute("UPDATE articles SET pending_deletion = TRUE WHERE id = %s", (deleted,))
        conn.commit()

    assert [card["origin_article_id"] for card in _business(user)] == [kept]


def test_reprint_is_a_publication_of_the_main_article_signal(user):
    main, copy = _article(1), _article(2)
    _process(main, title_ru="Главная", score=70)
    _process(copy, title_ru="Копия", score=60)
    assert len(_business(user)) == 2

    repository.mark_article_reprint(article_id=copy, primary_id=main, similarity=0.9, reason="то же событие")

    (card,) = _business(user)
    assert card["origin_article_id"] == main
    assert [item["article_id"] for item in card["evidence"]] == [main, copy]
    assert card["evidence_count"] == 2
    with repository.get_connection() as conn:
        merged = conn.execute(
            "SELECT merged_into_signal_id FROM signals WHERE origin_article_id = %s", (copy,)
        ).fetchone()[0]
    assert merged == card["id"]

    # Пометку сняли — копия снова свой сигнал со своей публикацией.
    repository.unmark_article_reprint(copy)
    cards = {card["origin_article_id"]: card for card in _business(user)}
    assert set(cards) == {main, copy}
    assert [item["article_id"] for item in cards[main]["evidence"]] == [main]
    assert cards[main]["evidence_count"] == 1
    assert [item["article_id"] for item in cards[copy]["evidence"]] == [copy]


def test_reprint_marked_before_the_main_article_was_accepted_joins_it_later(user):
    main, copy = _article(1), _article(2)
    _process(copy)
    repository.mark_article_reprint(article_id=copy, primary_id=main, similarity=0.9, reason="то же событие")
    assert _business(user) == []  # главная ещё не прошла отбор, копия в ленте скрыта

    _process(main)

    (card,) = _business(user)
    assert sorted(item["article_id"] for item in card["evidence"]) == [main, copy]


class _SameEventJudge:
    """Судья дедупа: любые две карточки — одно событие."""

    def complete_json(self, instructions, prompt, schema, **kwargs):
        from oiltech_digest.processing.openai_client import AIResponse

        assert schema["name"] == "signal_duplicate_verdict"
        return AIResponse(data={"same_event": True, "reason": "та же сделка"}, model="m",
                          input_tokens=1, output_tokens=1)


def test_new_finding_of_the_same_event_sits_in_the_existing_business_card(user, monkeypatch):
    from oiltech_digest import signal_discovery

    business = _radar_card("biz", "ZenaTech купила Velocity Geomatics", category="business",
                           companies=["ZenaTech"], url="https://radar.example/zena-1")
    again = signal_discovery._normalize_signal_payload(
        {"title": "Покупка Velocity Geomatics компанией ZenaTech", "summary": "сделка", "maturity": "shortlist",
         "score": 70, "companies": ["ZenaTech"], "signal_category": "business"},
        DRILLING,
    )
    again.update({"signal_key": "zena-again", "evidence_count": 1,
                  "evidence": [{"source_url": "https://radar.example/zena-2", "title": "ZenaTech buys Velocity"}]})
    topics = [{"topic": DRILLING, "candidates": [{"signal": again, "rejected": False}]}]
    monkeypatch.setattr(signal_discovery, "_radar_client", lambda: _SameEventJudge())
    config = signal_discovery.SignalDiscoveryConfig(offline=False, dry_run=False, persist_training_examples=False)

    signal_discovery._dedupe_run(config, {"existing_signals": repository.list_signals_for_dedup()}, topics, lambda: None)
    signal_discovery.apply_discovery(config, {"topics": topics})

    (card,) = _business(user)
    assert card["id"] == business
    assert {item["source_url"] for item in card["evidence"]} == {
        "https://radar.example/zena-1", "https://radar.example/zena-2"}


def test_refound_link_of_a_business_card_is_its_repeat_not_a_new_card(user):
    from oiltech_digest import signal_discovery

    business = _radar_card("biz", "ZenaTech купила Velocity Geomatics", category="business",
                           url="https://radar.example/zena-1")
    again = signal_discovery._normalize_signal_payload(
        {"title": "ZenaTech: сделка", "summary": "сделка", "maturity": "shortlist", "score": 70}, DRILLING)
    again.update({"signal_key": "zena-again", "evidence_count": 1,
                  "evidence": [{"source_url": "https://radar.example/zena-1", "title": "t"}]})
    config = signal_discovery.SignalDiscoveryConfig(offline=False, dry_run=False, persist_training_examples=False)

    signal_discovery.apply_discovery(config, {"topics": [{"topic": DRILLING, "candidates": [
        {"signal": again, "rejected": False}]}]})

    assert [card["id"] for card in _business(user)] == [business]
    assert _rows(_get("/api/signals", user)) == []


def _historical_article(n, collected, *, relevant=True):
    """Статья, обработанная до моста: карточка и балл есть, сигнала нет."""
    article_id = _article(n, collected=collected)
    with repository.get_connection() as conn:
        conn.execute("INSERT INTO article_cards (article_id, summary, title_ru, relevant) VALUES (%s, 'Суть', %s, %s)",
                     (article_id, f"Статья {n}", relevant))
        conn.execute("DELETE FROM signals WHERE origin_article_id = %s", (article_id,))
        conn.commit()
    return article_id


def test_history_backfill_is_a_dry_run_by_default_and_never_doubles(user, capsys):
    from oiltech_digest.db import feed_signals

    july = _historical_article(1, "2026-07-20 12:00+03")
    august = _historical_article(2, "2026-08-03 12:00+03")
    october = _historical_article(3, "2026-10-10 12:00+03")
    _historical_article(4, "2026-09-10 12:00+03", relevant=False)

    dry = feed_signals.sync_feed_signals(collected_since="2026-08-01", dry_run=True)
    assert (dry["signals_created"], dry["dry_run"]) == (2, True)
    assert _business(user) == []

    feed_signals.sync_feed_signals(collected_since="2026-08-01")
    again = feed_signals.sync_feed_signals(collected_since="2026-08-01")

    assert again["signals_created"] == 0
    assert sorted(card["origin_article_id"] for card in _business(user)) == [august, october]
    assert july not in [card["origin_article_id"] for card in _business(user)]


def test_merge_of_business_duplicates_lists_pairs_and_merges_only_named_ones(user, capsys):
    from oiltech_digest import cli

    older = _radar_card("biz-1", "ZenaTech купила Velocity Geomatics", category="business", companies=["ZenaTech"])
    newer = _radar_card("biz-2", "Покупка Velocity Geomatics: ZenaTech", category="business", companies=["ZenaTech"])
    other = _radar_card("biz-3", "Татнефть строит завод сорбентов", category="business", companies=["Татнефть"])

    cli.main(["merge-business-duplicates"])
    out = capsys.readouterr().out
    assert f"{newer}:{older}" in out and str(other) + ":" not in out
    assert len(_business(user)) == 3  # сухой прогон

    cli.main(["merge-business-duplicates", "--apply", "--pairs", f"{newer}:{older}"])

    (main, single) = sorted(_business(user), key=lambda card: card["id"])
    assert (main["id"], single["id"]) == (older, other)
    assert main["merged_count"] == 1
