"""Единое хранилище сигналов (#95, ADR 0003): статья, принятая отбором, — сигнал «бизнес»
происхождения «лента»; перепечатка — публикация сигнала главной статьи; радар при этом
выдаёт ровно то же, что раньше. Шов — API списка сигналов на изолированной базе.
"""

from datetime import date, datetime, timedelta, timezone
import json

import pytest
from fastapi.testclient import TestClient

from oiltech_digest import api
from oiltech_digest.db import feed_signals, repository
from oiltech_digest.db import marks
from oiltech_digest.ingestion.normalize import url_key
from oiltech_digest.processing import external_ai, reprints
from tests.signal_card_support import before_otbor

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


def _article(n, *, collected=None, title=None, source="Лента"):
    # Сбор «сейчас»: окна радара (неделя итогов, 30 дней снимка дедупа) считаются от текущего
    # дня, и сигнал ленты должен в них попадать в любой день прогона.
    collected = collected or datetime.now(timezone.utc)
    with repository.get_connection() as conn:
        row = conn.execute("SELECT id FROM sources WHERE name = %s", (source,)).fetchone()
        if row is None:
            row = conn.execute(
                "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
                "VALUES (%s, 'News', %s, TRUE, 'rss') RETURNING id",
                (source, f"https://{n}.source.example" if source != "Лента" else "https://feed.example"),
            ).fetchone()
        article_id = conn.execute(
            "INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language) "
            "VALUES (%s, %s, %s, %s, %s, 'текст', 'en') RETURNING id",
            (row[0], title or f"Article {n}", f"https://feed.example/{n}", collected, collected),
        ).fetchone()[0]
        conn.commit()
    return int(article_id)


SNAPSHOT = [{"id": 1, "name": "к", "weight": 100, "text_hash": "h"}]


def _process(article_id, *, relevant=True, summary="Суть статьи", title_ru=None, theme=DRILLING, score=70.0):
    """Статья до «Отбора»: вердикт гейта, тег и балл прежней обработки (история, сняты 08.11), затем суть и перевод,
    итог воркера, как его применяет ядро."""
    if not relevant:
        _reject_by_gate(article_id)
        return
    before_otbor(article_id, tag_id=_tag(theme), score=score, snapshot=SNAPSHOT)
    external_ai.apply_process_result({"articles": [{
        "article_id": article_id, "summary": {"summary": summary, "model": "m"},
        "translation": {"title_ru": title_ru or f"Статья {article_id}", "model": "m"}}]})


def _reject_by_gate(article_id):
    """Вердикт гейта «не брать» (история: так статью снимала перепроверка гейтом до 08.11); мост синхронизирован, как
    после записи вердикта."""
    before_otbor(article_id, relevant=False)
    feed_signals.sync_quietly([article_id])


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


def _finding(key, title, url, *, company=""):
    """Находка поиска радара: тема с одной находкой, как её отдаёт воркер ядру (_finding_candidate): заголовок
    страницы и ссылка, черновая компания модели поиска лежит в ссылке."""
    signal = {"title": title, "theme": DRILLING, "summary": "", "companies": [], "maturity": "watch", "score": 0,
              "signal_key": key, "evidence_count": 1,
              "evidence": [{"source_url": url, "title": title, "raw_payload": {"research_title": title,
                                                                              "company": company}}]}
    return {"topic": DRILLING, "candidates": [{"signal": signal}]}


def _discovery_config():
    from oiltech_digest import signal_discovery

    return signal_discovery.SignalDiscoveryConfig(offline=False, dry_run=False, persist_training_examples=False)


def _apply_findings(*topics):
    from oiltech_digest import signal_discovery

    return signal_discovery.apply_discovery(_discovery_config(), {"topics": list(topics)})


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

    articles = {n: _article(n) for n in (5, 6, 7)}
    for n, article_id in articles.items():
        _process(article_id, title_ru=f"Бизнес-новость {n}")
    article = _article(8)
    _process(article)
    _reject_by_gate(article)
    feed = {card["origin_article_id"]: card["id"] for card in _business(user) if card["origin"] == "feed"}
    assert sorted(feed) == sorted(articles.values())
    with repository.get_connection() as conn:
        # Отзывы о сигналах ленты (их даст сервис ОС) — не отзывы радара, в том числе о сигнале с
        # общей публикацией: «Обратная связь: N» на карточке радара считает отзывы и по адресу.
        conn.execute("INSERT INTO signal_feedback_events (signal_id, event_type, verdict, source_url) "
                     "VALUES (%s, 'verdict', 'approved', 'https://feed.example/7'), (%s, 'verdict', 'reject', NULL)",
                     (feed[articles[7]], feed[articles[5]]))
        # Сервис ОС сменит категорию сигнала ленты — витрина технологий всё равно «как сейчас»:
        # только находки радара.
        conn.execute("UPDATE signals SET signal_category = 'technology' WHERE id = %s", (feed[articles[6]],))
        conn.commit()

    assert len(_business(user)) == 3  # две статьи ленты (третья теперь «технология») и бизнес-находка радара
    assert _radar_views(user, admin) == before
    assert _rows(_get("/api/signals", user, q="#" + str(shared)))[0]["evidence"][0]["source_url"] == "https://feed.example/7"


def test_radar_finding_with_the_address_of_a_feed_article_is_no_new_card(user):
    # Адрес публикации уникален: ссылку держит кто-то один. Находка с адресом статьи сбора не новый материал
    # (#138): карточки радара нет, в итоге задачи ссылка «уже в сборе».
    article = _article(9)
    with repository.get_connection() as conn:
        # Ключ адреса статьи, как его пишет сбор (insert_article): по нему ядро узнаёт ссылку из сбора.
        conn.execute("UPDATE articles SET url_key = %s WHERE id = %s", (url_key("https://feed.example/9"), article))
        conn.commit()
    _process(article)

    result = _apply_findings(_finding("leak", "Датчик утечек", "https://feed.example/9"))

    assert repository.signal_key_owners(["leak"]) == {}
    assert [item["url"] for item in result["topic_results"][0]["already_collected"]] == ["https://feed.example/9"]
    # Сигнал ленты остаётся в витрине «бизнес» по своей статье, и статья по-прежнему его публикация.
    (feed_card,) = _business(user)
    assert feed_card["origin_article_id"] == article
    assert [item["source_url"] for item in feed_card["evidence"]] == ["https://feed.example/9"]
    assert feed_card["evidence_count"] == 1  # число сходится со списком
    assert repository.refresh_all_signal_evidence_counts() == 0  # и пересчёт его не «чинит»


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
    # Плашка «Отклонённые» одна в обеих лентах (#130, решение владельца 23.10): смешанная бизнес-находка не видна
    # ни в одной витрине и считается в ней.
    assert _rows(_get("/api/signals/summary", admin, showcase="business"))["rejected"] == 1
    assert summary["themes"] == [{"theme": DRILLING, "count": 2}]
    assert _rows(_get("/api/signals/summary", user, showcase="business", min_score=60))["matching"] == 1
    # Прежний просмотр «скрытое правилами радара» (hidden=true) — разбор работы радара: у бизнес-витрины его нет.
    assert _get("/api/signals", user, showcase="business", hidden=True).status_code == 422


def test_feed_signal_follows_resummary_and_the_history_tag_and_score(user):
    """Сигнал ленты повторяет новую суть и заголовок статьи, а у статьи до «Отбора» ещё тему и балл её тега и балла
    (история: тег и балл статьи сняты 08.11, мост берёт их при следующей записи статьи)."""
    article = _article(1)
    _process(article, summary="Первая суть", title_ru="Первый заголовок", score=50)
    before_otbor(article, tag_id=_tag(ECOLOGY), score=88.0, snapshot=SNAPSHOT)

    external_ai.apply_process_result(
        {"articles": [{"article_id": article, "summary": {"summary": "Новая суть", "model": "m"},
                       "translation": {"title_ru": "Новый заголовок", "model": "m"}}]},
        only=["summary", "translation"],
    )

    (card,) = _business(user)
    assert (card["title_ru"], card["summary"], card["theme"], card["score"]) == (
        "Новый заголовок", "Новая суть", ECOLOGY, 88)


def test_feed_signal_leaves_the_showcase_with_its_article(user):
    kept, rejected_later, deleted = _article(1), _article(2), _article(3)
    for article in (kept, rejected_later, deleted):
        _process(article)
    _reject_by_gate(rejected_later)
    with repository.get_connection() as conn:
        conn.execute("UPDATE articles SET pending_deletion = TRUE WHERE id = %s", (deleted,))
        conn.commit()

    assert [card["origin_article_id"] for card in _business(user)] == [kept]


def test_feed_signal_is_shown_by_its_article_not_by_radar_quality_rules(user):
    # Смешанные ссылки, «не про нефтесервис», срок события — правила находок поиска. Статью,
    # которую принял отбор, они не прячут (поля заполнит агент карточки, #96).
    article = _article(1)
    _process(article)
    with repository.get_connection() as conn:
        conn.execute("UPDATE signals SET event_date = '2020-01-01', oilfield_relevance = 'none', "
                     "mixed_events = TRUE WHERE origin_article_id = %s", (article,))
        conn.commit()

    assert [card["origin_article_id"] for card in _business(user)] == [article]


def test_bridge_starts_signals_only_for_articles_collected_since_august(user):
    from oiltech_digest.db import feed_signals

    july = _article(1, collected="2026-07-20 12:00+03")
    august = _article(2, collected="2026-08-03 12:00+03")
    _process(july)
    _process(august)
    assert [card["origin_article_id"] for card in _business(user)] == [august]

    # Сигнал, заведённый явной командой, обновляется записями статьи, как любой.
    feed_signals.sync_feed_signals([july])
    repository.upsert_article_card(july, "Новая суть")
    cards = {card["origin_article_id"]: card for card in _business(user)}
    assert cards[july]["summary"] == "Новая суть"


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
    topic = _finding("zena-again", "Покупка Velocity Geomatics компанией ZenaTech", "https://radar.example/zena-2",
                     company="ZenaTech")
    monkeypatch.setattr(signal_discovery, "_radar_client", lambda: _SameEventJudge())

    signal_discovery._dedupe_run(_discovery_config(), {"existing_signals": repository.list_signals_for_dedup()},
                                 [topic], lambda: None)
    _apply_findings(topic)

    (card,) = _business(user)
    assert card["id"] == business
    assert {item["source_url"] for item in card["evidence"]} == {
        "https://radar.example/zena-1", "https://radar.example/zena-2"}


def test_refound_link_of_a_business_card_is_its_repeat_not_a_new_card(user):
    business = _radar_card("biz", "ZenaTech купила Velocity Geomatics", category="business",
                           url="https://radar.example/zena-1")

    _apply_findings(_finding("zena-again", "ZenaTech: сделка", "https://radar.example/zena-1"))

    assert [card["id"] for card in _business(user)] == [business]
    assert _rows(_get("/api/signals", user)) == []


def _historical_article(n, collected, *, relevant=True):
    """Статья, обработанная до моста: карточка и балл есть, сигнала нет."""
    article_id = _article(n, collected=collected)
    with repository.get_connection() as conn:
        conn.execute("INSERT INTO article_cards (article_id, summary, title_ru, relevant) VALUES (%s, 'Суть', %s, %s)",
                     (article_id, f"Статья {n}", relevant))
        conn.commit()
    return article_id


def test_history_backfill_is_a_dry_run_by_default_and_never_doubles(user, capsys):
    from oiltech_digest import cli

    _historical_article(1, "2026-07-20 12:00+03")
    august = _historical_article(2, "2026-08-03 12:00+03")
    october = _historical_article(3, "2026-10-10 12:00+03")
    _historical_article(4, "2026-09-10 12:00+03", relevant=False)

    cli.main(["sync-feed-signals"])
    dry = capsys.readouterr().out
    assert "с 2026-08-01" in dry and "dry_run=True" in dry and "новых сигналов: 2;" in dry
    assert _business(user) == []

    cli.main(["sync-feed-signals", "--apply"])
    cli.main(["sync-feed-signals", "--apply"])

    created = [line for line in capsys.readouterr().out.splitlines() if "новых сигналов" in line]
    assert ["новых сигналов: 2;" in created[0], "новых сигналов: 0;" in created[1]] == [True, True]
    assert sorted(card["origin_article_id"] for card in _business(user)) == [august, october]


def test_feed_signal_follows_the_telegram_title_repair(user):
    from oiltech_digest.ingestion import telegram_titles

    glued = "Газпрому разрешили не соблюдать требования по числу независимых директоровПрезидент РФ В."
    fixed = "Газпрому разрешили не соблюдать требования по числу независимых директоров"
    with repository.get_connection() as conn:
        channel = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('Канал', 'Telegram', 'https://t.me/s/chan', TRUE, 'telegram') RETURNING id"
        ).fetchone()[0]
        article = conn.execute(
            "INSERT INTO articles (source_id, title, url, collected_at, raw_text, language) "
            "VALUES (%s, %s, 'https://t.me/chan/1', now(), 'текст', 'ru') RETURNING id",
            (channel, glued),
        ).fetchone()[0]
        conn.commit()
    _process(article, title_ru=glued)

    telegram_titles.repair(apply=True, frequency={"директоров": 40, "президент": 300})

    (card,) = _business(user)
    assert (card["title"], card["title_ru"]) == (fixed, fixed)


def test_feed_signals_stay_out_of_the_issue_and_the_radar_context_bundle(user):
    # Выпуск собирается как раньше: статьи — своей выборкой, сигнал ленты был бы их второй копией.
    from oiltech_digest import signal_training

    radar = _radar_card("tech", "Роботизированная буровая", score=70)
    _process(_article(1), score=70)
    (feed_card,) = _business(user)
    for signal_id in (radar, feed_card["id"]):
        marks.set_signal_mark(signal_id, status="digest", user_id=int(user["id"]))
    # Отзыв о сигнале ленты с адресом публикации радара (форма берёт адрес первой ссылки).
    with repository.get_connection() as conn:
        conn.execute("INSERT INTO signal_feedback_events (signal_id, event_type, verdict, source_url) "
                     "VALUES (%s, 'verdict', 'approved', 'https://radar.example/tech')", (feed_card["id"],))
        conn.commit()

    items = repository.digest_candidates(min_score=0)
    assert [row["id"] for row in items if row["item_type"] == "signal"] == [radar]
    bundle = signal_training._build_signal_context_bundle(include_rejected=True)
    assert [row["id"] for row in bundle["signals"]] == [radar]
    assert bundle["signal_feedback_events"] == []


# --- Блок А5 схемы агентов (04.11): сигналы ленты в «Дедупе» радара, перепечатки по русскому заголовку ---------


def _feed_signal_id(article_id):
    with repository.get_connection() as conn:
        return int(conn.execute("SELECT id FROM signals WHERE origin = 'feed' AND origin_article_id = %s",
                                (article_id,)).fetchone()[0])


def _feedback(signal_id, verdict="approved", event_type="comment_added"):
    with repository.get_connection() as conn:
        conn.execute("INSERT INTO signal_feedback_events (signal_id, event_type, verdict) VALUES (%s, %s, %s)",
                     (signal_id, event_type, verdict))
        conn.commit()


def _participants(signal_id, *names):
    with repository.get_connection() as conn:
        conn.execute("UPDATE signals SET participants_json = %s::jsonb WHERE id = %s",
                     (json.dumps([{"name": name, "quote": name, "verified": True} for name in names]), signal_id))
        conn.commit()


def test_dedup_snapshot_of_the_feed_takes_visible_and_reviewed_signals_of_the_search_window(user):
    visible, reviewed, hidden = _article(61), _article(62), _article(63)
    old = _article(64, collected=datetime.now(timezone.utc) - timedelta(days=40))
    for article in (visible, reviewed, hidden, old):
        _process(article)
    for article in (reviewed, hidden):
        _reject_by_gate(article)
    _feedback(_feed_signal_id(reviewed), "reject", event_type="verdict")
    _participants(_feed_signal_id(visible), "Halliburton", "Keystone AS")

    rows = {row["id"]: row for row in repository.list_feed_signals_for_dedup(since=date.today() - timedelta(days=10))}

    # Видимый сигнал окна и разобранный человеком (его повтор не вернётся); скрытый неразобранный и старый — нет.
    assert set(rows) == {_feed_signal_id(visible), _feed_signal_id(reviewed)}
    row = rows[_feed_signal_id(visible)]
    assert (row["origin"], row["companies"], row["hidden"], row["reviewed"], row["fresh"]) == (
        "feed", ["Halliburton", "Keystone AS"], False, False, True)
    assert row["evidence_urls"] == ["https://feed.example/61"]
    assert (rows[_feed_signal_id(reviewed)]["hidden"], rows[_feed_signal_id(reviewed)]["verdict"]) == (True, "reject")
    # Снимок радара ленту не берёт; участники «Карточки» — его компании, если судья их не писал (после «Отбора»).
    radar = _radar_card("radar-keystone", "Halliburton купила Keystone", category="business")
    _participants(radar, "Halliburton")
    [radar_row] = repository.list_signals_for_dedup()
    assert (radar_row["id"], radar_row["origin"], radar_row["companies"]) == (radar, "radar", ["Halliburton"])


def test_reprint_candidates_pair_an_english_article_with_its_russian_retelling(user):
    # Прод 03.11: World Oil и Drilling Contractor об одной сделке — две видимые карточки бизнес-ленты: исходные
    # заголовки сходятся на 0,2 при пороге 0,35, русские заголовки карточек — на 0,33 (основы как у «Дедупа»).
    world_oil = _article(71, title="Halliburton acquires Norway’s Keystone AS to expand digital well construction "
                                   "software", source="World Oil")
    contractor = _article(72, title="Halliburton buys Keystone to link well planning with rig execution",
                          source="Drilling Contractor")
    other = _article(73, title="Shell agrees to sell its stake in North West Shelf to XRG", source="Upstream")
    _process(world_oil, title_ru="Halliburton приобрела норвежскую Keystone для развития цифровых решений "
                                 "по строительству скважин")
    _process(contractor, title_ru="Halliburton купила Keystone для интеграции планирования и строительства скважин")
    _process(other, title_ru="Shell продаст долю в СПГ-проекте North West Shelf компании XRG")

    pairs = {(row["a_id"], row["b_id"]): row["overlap"] for row in reprints.find_candidates(days=14)}

    assert set(pairs) == {(world_oil, contractor)} and float(pairs[(world_oil, contractor)]) == 0.333
    # Без русского заголовка пары нет: правило до 04.11 её не видело.
    assert repository.reprint_candidates(days=14) == []


def test_reprint_keeps_on_screen_the_copy_people_gave_feedback_on(user):
    main, copy, third = _article(81), _article(82), _article(83)
    for article in (main, copy, third):
        _process(article)
    _feedback(_feed_signal_id(copy))

    repository.mark_article_reprint(article_id=copy, primary_id=main, similarity=0.4, reason="одно событие")

    # Как с выбранной «в дайджест»: копия с блоком ОС — главная, прежняя главная спрятана вместе с группой.
    assert sorted(card["origin_article_id"] for card in _business(user)) == [copy, third]
    with repository.get_connection() as conn:
        assert conn.execute("SELECT primary_id, reason FROM article_reprints WHERE article_id = %s",
                            (main,)).fetchone() == (copy, "главная — с обратной связью человека; судья: одно событие")
    # Блок ОС у обеих — не прячем ни одну.
    _feedback(_feed_signal_id(third))
    with pytest.raises(ValueError, match="с обратной связью людей"):
        repository.mark_article_reprint(article_id=third, primary_id=copy, similarity=0.4, reason="одно событие")
    assert sorted(card["origin_article_id"] for card in _business(user)) == [copy, third]


def test_reprint_of_an_article_whose_signal_the_dedup_merged_joins_the_root_card(user):
    radar = _radar_card("radar-root", "Halliburton купила Keystone", category="business")
    main, copy = _article(91), _article(92)
    _process(main)
    _process(copy)
    # «Дедуп» склеил сигнал главной статьи с более ранней карточкой радара, а с сигналом копии — карточку радара.
    child = _radar_card("radar-child", "Keystone: сделка", category="business")
    assert repository.mark_signal_merged(_feed_signal_id(main), radar, "то же событие")
    assert repository.mark_signal_merged(child, _feed_signal_id(copy), "то же событие")

    repository.mark_article_reprint(article_id=copy, primary_id=main, similarity=0.9, reason="одно событие")

    # Группа в один шаг: сигнал перепечатки и склеенная с ним карточка — у корня, их ссылки видны в его карточке.
    with repository.get_connection() as conn:
        merged = dict(conn.execute("SELECT id, merged_into_signal_id FROM signals WHERE id = ANY(%s)",
                                   ([_feed_signal_id(copy), child],)).fetchall())
    assert merged == {_feed_signal_id(copy): radar, child: radar}
    (card,) = _business(user)
    assert card["id"] == radar
    assert {item["source_url"] for item in card["evidence"]} >= {
        "https://feed.example/91", "https://feed.example/92", "https://radar.example/radar-child"}
