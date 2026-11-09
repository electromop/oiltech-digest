"""Качество радара: замечания эксперта заказчика 29.09 и правила показа.

«75% это бизнес-сигналы», «оценки завышены», «в одной карточке могут быть разные темы»,
«нашёл очень старый сигнал». До 08.11 категорию, дату события, смешанные события и связь с
нефтесервисом ставил судья прежней цепочки; его поля в базе остались, и правила показа их читают
(история). Отсеянное лежит карточкой «Отсеяно»: технический отсев поиска и брак судьи прежней цепочки.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from oiltech_digest import config, signal_discovery
from oiltech_digest.db import repository
from oiltech_digest.db import marks

DRILLING = "Бурение, направленное бурение, буровые растворы и буровое оборудование"
MARKET = "Рынок, бизнес-модели, сервисные модели, партнёрства и M&A"


# --- Дата события ----------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "не указано", "2026-13-01", (date.today() + timedelta(days=30)).isoformat()])
def test_unknown_or_future_event_date_is_none(value):
    assert signal_discovery._normalize_event_date(value) is None


# --- Темы поиска и пустой день ---------------------------------------------------------------


def test_every_topic_is_searched_the_market_too_and_an_explicit_topic_picks_one(monkeypatch):
    # Поиск широкий (#138): тема рынка ищется, что брать, решает «Отбор».
    monkeypatch.setattr(signal_discovery, "_radar_topics", lambda: [{"name": DRILLING}, {"name": MARKET}])

    assert [row["name"] for row in signal_discovery._selected_topics(None)] == [DRILLING, MARKET]
    assert [row["name"] for row in signal_discovery._selected_topics("Рынок")] == [MARKET]
    assert signal_discovery.search_topic_names() == [DRILLING, MARKET]


def test_empty_day_without_errors_is_still_ok(monkeypatch):
    monkeypatch.setattr(signal_discovery, "_topic_web_evidence", lambda topic, topic_name, config_, **kwargs: {
        "status": "ok", "provider": "openai_web_search", "research": {"status": "ok", "events": 0}, "evidence": []})
    run_config = signal_discovery.SignalDiscoveryConfig(offline=True, dry_run=True)

    run = signal_discovery.run_discovery(run_config, {"topics": [{"name": DRILLING, "tag_id": 2}], "tags": []})

    assert run["topics"][0]["candidates"] == [] and run["topics"][0]["filtered_findings"] == []


# --- Ядро: запись и видимость (живая база) ---------------------------------------------------


def _store(key, *, title=None, **fields):
    signal = {"signal_key": key, "title": title or key, "title_ru": title or key, "theme": DRILLING,
              "maturity": "watch", "score": 60, **fields}
    signal_id = repository.upsert_signal(signal)
    repository.upsert_signal_evidence(signal_id, {"source_url": f"https://example.com/{key}", "title": key})
    return signal_id


def _visible_ids():
    return {int(row["id"]) for row in repository.list_signals(limit=100)}


def test_radar_hides_business_other_mixed_old_and_archived(isolated_db):
    recent = (date.today() - timedelta(days=10)).isoformat()
    old = (date.today() - timedelta(days=config.SIGNAL_RADAR_MAX_EVENT_AGE_DAYS + 5)).isoformat()
    tech = _store("tech", signal_category="technology", event_date=recent, mixed_events=False)
    legacy = _store("legacy")
    business = _store("business", signal_category="business", event_date=recent)
    other = _store("other", signal_category="other")
    mixed = _store("mixed", signal_category="technology", mixed_events=True, mixed_events_reason="две сделки")
    stale = _store("stale", signal_category="technology", event_date=old)
    archived = _store("archived", signal_category="technology")
    repository.archive_signals([archived], reason="test")

    assert _visible_ids() == {tech, legacy}
    summary = repository.signal_radar_summary()
    assert summary["total"] == 2
    hidden = {business, other, mixed, stale, archived}
    assert hidden.isdisjoint(_visible_ids())


def test_judge_fields_are_stored_and_kept_when_an_old_worker_refinds_the_card(isolated_db):
    signal_id = _store("card", signal_category="technology", event_date="2026-09-10", mixed_events=False,
                       score_profile="tech_radar", score_items=[{"criterion_id": 101, "final_score": 80}],
                       criteria_snapshot=[{"id": 101, "weight": 30}])
    # Старая сборка NL: полей судьи нет: прежние не стираются.
    repository.upsert_signal({"signal_key": "card", "title": "card", "theme": DRILLING, "score": 50})

    with repository.get_connection() as conn:
        row = conn.execute(
            "SELECT signal_category, event_date, mixed_events, score_profile FROM signals WHERE id = %s",
            (signal_id,),
        ).fetchone()
    assert row == ("technology", date(2026, 9, 10), False, None)


def test_archive_selects_early_free_theme_cards_and_is_reversible(isolated_db):
    with repository.get_connection() as conn:
        conn.execute("INSERT INTO tags (name, enabled) VALUES (%s, TRUE)", (DRILLING,))
        conn.commit()
    early = _store("early", title="HSE/цифровые разрешения")
    with repository.get_connection() as conn:
        conn.execute("UPDATE signals SET theme = 'HSE/цифровые разрешения', first_seen_at = '2026-09-13' WHERE id = %s",
                     (early,))
        conn.commit()
    topical = _store("topical")

    rows = repository.archive_signal_candidates(created_before="2026-09-14")

    assert [row["id"] for row in rows] == [early]
    assert rows[0]["reviewed"] is False
    assert repository.archive_signals([early], reason="early-free-theme-2026-09") == 1
    assert _visible_ids() == {topical}
    assert repository.unarchive_signals(reason="early-free-theme-2026-09") == 1
    assert _visible_ids() == {early, topical}


def test_archive_needs_a_selection_rule(isolated_db):
    with pytest.raises(ValueError):
        repository.archive_signal_candidates(free_theme_only=False)


# --- Бюджет ответа и цены моделей ------------------------------------------------------------


@pytest.mark.parametrize("effort, expected", [("minimal", 900), ("low", 2900), ("medium", 6900), ("", 900)])
def test_output_budget_grows_with_reasoning(effort, expected):
    from oiltech_digest.processing.openai_client import output_budget

    assert output_budget(900, effort) == expected


def test_gpt5_has_its_own_price():
    # Без строки gpt-5 считался бы по ставке nano: в ~25 раз дешевле.
    assert config.price_for_model("gpt-5-2025-08-07") == (1.25, 10.0)
    assert config.price_for_model("gpt-5-mini-2025-08-07") == (0.25, 2.0)


# --- Экран: скрытые карточки для админа ------------------------------------------------------


def test_hidden_view_lists_only_hidden_cards_with_a_reason(isolated_db):
    recent = (date.today() - timedelta(days=5)).isoformat()
    _store("tech", signal_category="technology", event_date=recent)
    business = _store("deal", signal_category="business", event_date=recent)
    mixed = _store("mixed", signal_category="technology", mixed_events=True, mixed_events_reason="две сделки SLB")
    archived = _store("old13", signal_category="technology")
    repository.archive_signals([archived], reason="early")

    rows = {int(row["id"]): row["hidden_reason"] for row in repository.list_signals(limit=50, hidden=True)}

    assert rows == {
        business: "бизнес-сигнал, не технология",
        mixed: "ссылки о разных событиях: две сделки SLB",
        archived: "в архиве",
    }
    assert all(row["hidden_reason"] is None for row in repository.list_signals(limit=50))
    summary = repository.signal_radar_summary(hidden=True)
    # Плашка «Отклонённые» (#130, решение владельца 23.10) считает то, чего нет ни в одной витрине: бизнес-находка
    # видна в бизнес-ленте и в неё не идёт.
    assert (summary["total"], summary["rejected"], summary["matching"]) == (1, 2, 3)


def test_filtered_and_hidden_cards_are_for_admin_only(monkeypatch):
    # Решение владельца 05.10: просмотр отсеянного и скрытого: только админу (заказчик: админ).
    from fastapi.testclient import TestClient

    from oiltech_digest import api

    seen = {}
    monkeypatch.setattr(api.repository, "list_signals", lambda **kwargs: seen.update(kwargs) or [])
    monkeypatch.setattr(api.repository, "signal_radar_summary",
                        lambda **kwargs: {"total": 1, "rejected": 3, "matching": 1, "themes": []})
    monkeypatch.setattr(api.repository, "radar_weekly_summary", lambda **kwargs: {
        "days": 7, "on_radar": 2, "filtered": 5, "filtered_reasons": [{"reason": "отсеяно (судья)", "count": 5}],
        "top": [], "awaiting_review": 1, "feedback": 0, "feedback_verdicts": []})
    client = TestClient(api.app)
    try:
        api.app.dependency_overrides[api.require_user] = lambda: {"id": 2, "email": "u@example.com", "role": "user"}
        assert client.get("/api/signals?hidden=true").status_code == 403
        assert client.get("/api/signals/summary?hidden=true").status_code == 403
        # Обычному пользователю и число отсеянных не показываем: ни над списком, ни в итогах недели.
        assert "rejected" not in client.get("/api/signals/summary").json()
        weekly = client.get("/api/signals/weekly").json()
        assert "filtered" not in weekly and "filtered_reasons" not in weekly
        assert weekly["on_radar"] == 2

        api.app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "a@example.com", "role": "admin"}
        assert client.get("/api/signals?hidden=true").status_code == 200
        assert seen["hidden"] is True
        assert client.get("/api/signals/summary").json()["rejected"] == 3
        assert client.get("/api/signals/weekly").json()["filtered"] == 5
    finally:
        api.app.dependency_overrides.pop(api.require_user, None)


# --- Релевантность нефтесервису («релевантности мало», эксперт заказчика, 29.09) --------------------------


def test_radar_hides_cards_without_oilfield_relevance(isolated_db):
    direct = _store("chevron", signal_category="technology", oilfield_relevance="direct",
                    oilfield_application="мониторинг парафина")
    transferable = _store("aurora", signal_category="technology", oilfield_relevance="transferable")
    legacy = _store("old")
    resort = _store("acwa", signal_category="technology", oilfield_relevance="none")

    assert _visible_ids() == {direct, transferable, legacy}
    hidden = {int(row["id"]): row["hidden_reason"] for row in repository.list_signals(limit=20, hidden=True)}
    assert hidden == {resort: "не про нефтесервис"}


def test_relevance_is_kept_when_an_old_worker_refinds_the_card(isolated_db):
    signal_id = _store("card", oilfield_relevance="direct", oilfield_application="ГРП на кустах")
    repository.upsert_signal({"signal_key": "card", "title": "card", "theme": DRILLING, "score": 50})

    with repository.get_connection() as conn:
        row = conn.execute("SELECT oilfield_relevance, oilfield_application FROM signals WHERE id = %s",
                           (signal_id,)).fetchone()
    assert row == ("direct", "ГРП на кустах")


# --- Ревью #89 (02.10): одобренную карточку решение судьи не прячет, выбранную «в дайджест»: ничто


def _choose_for_digest(signal_id):
    user = repository.create_user(f"editor-{signal_id}@example.test", "long-enough-password", "admin")
    marks.set_signal_mark(signal_id, status="digest", user_id=int(user["id"]))


def test_card_in_digest_stays_on_radar_when_refound_with_a_hiding_verdict(isolated_db):
    chosen = _store("chosen", signal_category="technology")
    _choose_for_digest(chosen)
    # Повторная находка: судья передумал по всем правилам сразу: карточка в выпуске всё равно видна,
    # иначе её не снять с выпуска (отметку снимают только с экрана радара).
    old = (date.today() - timedelta(days=config.SIGNAL_RADAR_MAX_EVENT_AGE_DAYS + 5)).isoformat()
    repository.upsert_signal({"signal_key": "chosen", "title": "chosen", "theme": DRILLING, "score": 60,
                              "signal_category": "business", "oilfield_relevance": "none",
                              "mixed_events": True, "event_date": old})

    assert chosen in _visible_ids()
    assert chosen not in {int(row["id"]) for row in repository.list_signals(limit=50, hidden=True)}
    assert repository.signal_radar_summary()["total"] == 1
    # Экран подписывает «Скрыта: …» любую карточку с причиной: у видимой её нет.
    assert {row["hidden_reason"] for row in repository.list_signals(limit=50)} == {None}


def test_card_archived_then_chosen_for_digest_is_visible_without_hidden_label(isolated_db):
    early = _store("early", signal_category="technology")
    repository.archive_signals([early], reason="early-free-theme-2026-09")
    _choose_for_digest(early)  # админ отметил её из «Скрытых»

    rows = {int(row["id"]): row for row in repository.list_signals(limit=50)}

    assert early in rows and rows[early]["hidden_reason"] is None


def test_archive_never_takes_a_card_in_digest(isolated_db):
    chosen = _store("chosen", signal_category="technology")
    _choose_for_digest(chosen)

    assert repository.archive_signal_candidates(ids=[chosen], free_theme_only=False) == []
    assert repository.archive_signals([chosen], reason="test") == 0
    assert chosen in _visible_ids()


def _feedback(signal_id, event_type, verdict=None):
    with repository.get_connection() as conn:
        conn.execute("INSERT INTO signal_feedback_events (signal_id, event_type, verdict) VALUES (%s, %s, %s)",
                     (signal_id, event_type, verdict))
        conn.commit()


def test_judge_does_not_hide_an_approved_card_but_archive_and_age_do(isolated_db):
    old = (date.today() - timedelta(days=config.SIGNAL_RADAR_MAX_EVENT_AGE_DAYS + 5)).isoformat()
    approved = _store("approved", signal_category="business", oilfield_relevance="none")
    archived = _store("archived", signal_category="business")
    stale = _store("stale", signal_category="business", event_date=old)
    plain = _store("plain", signal_category="business")
    for signal_id in (approved, archived, stale):
        _feedback(signal_id, "verdict", "approved")
    repository.archive_signals([archived], reason="test")

    assert _visible_ids() == {approved}
    hidden = {int(row["id"]): row["hidden_reason"] for row in repository.list_signals(limit=50, hidden=True)}
    # Причина, только то, что прячет на самом деле: у одобренной решение судьи не в счёт.
    assert hidden == {
        archived: "в архиве",
        stale: f"событие старше {config.SIGNAL_RADAR_MAX_EVENT_AGE_DAYS} дней",
        plain: "бизнес-сигнал, не технология",
    }


@pytest.mark.parametrize("event_type, verdict", [
    ("comment_added", "wrong_block"),  # «Не тот блок, бизнес-сигнал»: админ учит агента на «Скрытых»
    ("comment_added", "reject"),
    ("comment_added", None),           # комментарий без вердикта
])
def test_rejected_or_commented_hidden_card_stays_off_the_radar(isolated_db, event_type, verdict):
    business = _store("business", signal_category="business")
    _feedback(business, event_type, verdict)

    assert business not in _visible_ids()
    hidden = {int(row["id"]): row["hidden_reason"] for row in repository.list_signals(limit=50, hidden=True)}
    assert hidden == {business: "бизнес-сигнал, не технология"}


def test_later_rejection_overrides_an_earlier_approval(isolated_db):
    card = _store("card", signal_category="business")
    _feedback(card, "verdict", "approved")
    _feedback(card, "verdict", "reject")

    assert card not in _visible_ids()


def test_positive_verdicts_match_the_dedup_ones():
    from oiltech_digest import signal_dedup

    assert set(repository.SIGNAL_POSITIVE_VERDICTS) == signal_dedup._POSITIVE_VERDICTS


# --- Отсеянное: карточкой «Отсеяно», чтобы заказчик просмотрел всё (02.10) ---------------------


def _apply_run(topics):
    config_ = signal_discovery.SignalDiscoveryConfig(offline=False, dry_run=False, persist_training_examples=False)
    return signal_discovery.apply_discovery(config_, {"topics": topics})


def _filtered():
    return {int(row["id"]): row["hidden_reason"] for row in repository.list_signals(limit=50, hidden=True)}


def _judge_rejected(key, title, urls, *, stage="judge", reason="обзор без события"):
    """Брак судьи или ревью пачки прежней цепочки (до 08.11): карточка «Отсеяно» в базе как история, её ссылки
    держит она, а не повторный отсев поиска (этапы идут только вперёд, 17.10)."""
    signal_id = repository.upsert_signal({"signal_key": key, "title": title, "title_ru": title, "theme": DRILLING,
                                          "maturity": "reject", "score": 0, "filter_stage": stage,
                                          "filter_reason": reason})
    for url in urls:
        repository.upsert_signal_evidence(signal_id, {"source_url": url, "title": url})
    repository.refresh_signal_evidence_count(signal_id)
    return signal_id


def test_judge_and_batch_rejects_of_the_old_chain_stay_filtered_with_their_reason(isolated_db):
    judge = _judge_rejected("judge-no", "Обзор рынка бурения", ["https://example.com/judge-no"])
    review = _judge_rejected("review-no", "Реклама вендора", ["https://example.com/review-no"], stage="review",
                             reason="реклама без факта применения")

    reasons = _filtered()
    assert reasons[judge] == "отсеяно (судья): обзор без события"
    assert reasons[review] == "отсеяно (ревью пачки): реклама без факта применения"
    assert _visible_ids() == set()


def test_search_drops_are_kept_with_reason(isolated_db):
    _apply_run([{"topic": DRILLING, "candidates": [], "filtered_findings": [
        {"stage": "search", "title": "ProPetro: контракт на 230 МВт",
         "reason": "страница не открылась, текста издателя нет",
         "url": "https://example.com/propetro", "summary": "контракт", "event_date": "2026-09-20"},
        {"stage": "search", "title": "Событие с битой ссылкой", "reason": "нет ссылки или заголовка", "url": ""},
    ]}])

    reasons = sorted(_filtered().values())
    assert reasons == [
        "отсеяно (поиск): нет ссылки или заголовка",  # без ссылки: тоже видна в разборе
        "отсеяно (поиск): страница не открылась, текста издателя нет",
    ]


def test_filtered_card_does_not_hold_its_link_and_stays_out_of_dedup(isolated_db):
    _apply_run([{"topic": DRILLING, "candidates": [], "filtered_findings": [
        {"stage": "search", "title": "Рано отсеяно", "reason": "дата события вне периода", "url": "https://example.com/x"},
    ]}])
    assert repository.visible_evidence_owners(["https://example.com/x"]) == {}
    assert repository.list_signals_for_dedup() == []

    # Найдено снова и принято: ссылка переходит к принятой карточке.
    accepted = repository.upsert_signal({"signal_key": "accepted", "title": "Принято", "theme": DRILLING,
                                         "signal_category": "technology"})
    repository.upsert_signal_evidence(accepted, {"source_url": "https://example.com/x", "title": "x"})

    assert set(repository.visible_evidence_owners(["https://example.com/x"])) == {"https://example.com/x"}
    assert accepted in _visible_ids()


def test_refiltered_finding_updates_the_same_card(isolated_db):
    finding = {"stage": "search", "title": "Повтор", "reason": "дата вне периода", "url": "https://example.com/r"}
    _apply_run([{"topic": DRILLING, "candidates": [], "filtered_findings": [finding]}])
    _apply_run([{"topic": DRILLING, "candidates": [], "filtered_findings": [finding]}])

    assert len(_filtered()) == 1


def _store_seen(key, url):
    """Карточка на радаре со ссылкой, у которой есть русский заголовок и суть."""
    signal_id = repository.upsert_signal({"signal_key": key, "title": "Seen", "title_ru": "Видно",
                                          "theme": DRILLING, "signal_category": "technology"})
    repository.upsert_signal_evidence(signal_id, {"source_url": url, "title": "Seen", "title_ru": "Видно на радаре",
                                                  "summary_ru": "суть на радаре"})
    return signal_id


def _link(signal_id, url):
    return next(row for row in repository.list_signal_evidence(signal_id) if row["source_url"] == url)


def test_search_drop_of_an_event_on_the_radar_leaves_its_link_alone(isolated_db):
    # Вчера принято и на радаре; сегодня поиск отбросил то же событие (страница не открылась). Ссылка
    # карточки на радаре не теряет русский заголовок и суть, «Отсеяно» не появляется.
    url = "https://example.com/seen"
    seen = _store_seen("url:example.com/seen", url)

    _apply_run([{"topic": DRILLING, "candidates": [], "filtered_findings": [
        {"stage": "search", "title": "Seen again", "reason": "страница не открылась, текста издателя нет", "url": url},
    ]}])

    assert _filtered() == {}
    link = _link(seen, url)
    assert (link["title_ru"], link["summary_ru"]) == ("Видно на радаре", "суть на радаре")


def test_approved_filtered_card_goes_on_the_radar_and_into_dedup(isolated_db):
    card = _judge_rejected("judge-no", "Судья ошибся", ["https://example.com/judge-no"])
    assert card not in _visible_ids() and repository.list_signals_for_dedup() == []

    # Заказчик разобрал «Отсеянные» и одобрил: решение машины одобренную не прячет (ревью #89).
    _feedback(card, "verdict", "approved")

    assert card in _visible_ids()
    assert card not in _filtered()
    assert [row["id"] for row in repository.list_signals_for_dedup()] == [card]


def test_rejected_filtered_card_stays_filtered_but_is_known_to_dedup(isolated_db):
    card = _judge_rejected("judge-no", "Судья прав", ["https://example.com/judge-no"])

    _feedback(card, "verdict", "reject")

    assert _filtered() == {card: "отсеяно (судья): обзор без события"}
    # Отклонённая: главная в своей группе (signal_dedup._rank): повтор события не вернётся.
    assert [(row["id"], row["hidden"]) for row in repository.list_signals_for_dedup()] == [(card, True)]


def _column(signal_id, column):
    with repository.get_connection() as conn:
        return conn.execute(f"SELECT {column} FROM signals WHERE id = %s", (signal_id,)).fetchone()[0]


def _search_drop(title, url, reason="страница не открылась, текста издателя нет"):
    return {"topic": DRILLING, "candidates": [], "filtered_findings": [
        {"stage": "search", "title": title, "reason": reason, "url": url}]}


@pytest.mark.parametrize("rescue", ["approved", "digest"])
def test_filtered_card_without_link_rescued_by_a_human_goes_on_the_radar(isolated_db, rescue):
    # Находка поиска без ссылки, одобренная или выбранная «в дайджест», не пропадает из обоих
    # списков: решение человека выводит её на радар (выбранную иначе не снять с выпуска).
    _apply_run([_search_drop("Без ссылки", "", reason="нет ссылки или заголовка")])
    (card,) = _filtered()
    if rescue == "approved":
        _feedback(card, "verdict", "approved")
    else:
        _choose_for_digest(card)

    assert card in _visible_ids()
    assert card not in _filtered()


def test_filtered_card_that_gave_its_last_link_to_an_accepted_one_becomes_its_duplicate(isolated_db):
    url = "https://example.com/x"
    _apply_run([_search_drop("Рано отсеяно", url)])
    (filtered,) = _filtered()

    # Найдено снова и принято: ссылка у принятой, а пустая копия не висит в «Отсеянных».
    accepted = repository.upsert_signal({"signal_key": "accepted", "title": "Принято", "theme": DRILLING,
                                         "signal_category": "technology"})
    repository.upsert_signal_evidence(accepted, {"source_url": url, "title": "x"})

    assert _filtered() == {}
    assert accepted in _visible_ids()
    # Дубль не удаляется: найден по merged_into_signal_id, причина записана.
    assert (_column(filtered, "merged_into_signal_id"), _column(filtered, "merge_reason")) == (
        accepted, f"найдено снова: ссылку забрала карточка {accepted}")


@pytest.mark.parametrize("stage, reason", [
    ("judge", "отсеяно (судья): обзор без события"),
    ("review", "отсеяно (ревью пачки): реклама без факта применения"),
])
def test_link_rejected_by_the_old_chain_then_dropped_by_the_search_stays_with_the_rejected_card(
        isolated_db, stage, reason):
    # 17.10: судья или ревью пачки забраковали u, потом поиск отсеял ту же u. Повтор забирал ссылку себе, и пустой
    # в «Отсеянных» оставалась забракованная, с лучшей причиной. Этапы идут только вперёд, и с поиском GPT тоже.
    url = "https://example.com/u"
    rejected = _judge_rejected("judge-u", "Событие", [url], stage=stage,
                               reason=reason.split(": ", 1)[1])

    _apply_run([_search_drop("Событие", url, reason="страница не открылась, текста издателя нет")])

    assert _filtered() == {rejected: reason}
    assert [row["source_url"] for row in repository.list_signal_evidence(rejected)] == [url]


def _owner_stages():
    """Чья ссылка: стадия отсева несклеенной хозяйки, «accepted»: у принятой."""
    with repository.get_connection() as conn:
        return dict(conn.execute("SELECT e.source_url, COALESCE(s.filter_stage, 'accepted') FROM signal_evidence e"
                                 " JOIN signals s ON s.id = e.signal_id WHERE s.merged_into_signal_id IS NULL"))


def test_any_order_of_drops_and_accepts_over_old_rejects_leaves_no_empty_copy_and_no_ring(isolated_db):
    # Доказательство «этапы идут только вперёд» (17.10) на нынешнем пути: брак судьи и ревью прежней цепочки лежит в
    # базе историей, а тридцать шагов отсева поиска и приёма в случайном порядке идут над скользящим окном из трёх
    # ссылок: принятая держит свои ссылки, и без окна прогулка застывала бы на первом приёме. После каждого шага: в
    # «Отсеянных» нет пустой копии, ссылка забракованной не ушла к отсеянной поиском, главная любого дубля не склеена
    # (группа в один шаг, кольца нет), ни одна ссылка не у дубля (событие не пропадает из обоих списков), ссылки
    # принятого лежат у видимых карточек.
    rng = random.Random(1017)
    urls = [f"https://example.com/{n}" for n in range(12)]
    _judge_rejected("old-judge", "Событие", urls[1:3])
    _judge_rejected("old-review", "Событие", urls[5:6], stage="review", reason="реклама")
    for step in range(30):
        window = urls[step // 3:step // 3 + 3]
        op = rng.choice(["search", "accept"])
        cluster = rng.choice([window[:1], window[1:2], window[2:], window[:2], window[1:], window])
        key = "c-" + "-".join(url.rsplit("/", 1)[1] for url in cluster)
        before = _owner_stages()
        if op == "search":
            _apply_run([_search_drop("Событие", cluster[0])])
        else:
            accepted = repository.upsert_signal({"signal_key": f"ok-{key}", "title": "Принято", "theme": DRILLING,
                                                 "signal_category": "technology"})
            for url in cluster:
                repository.upsert_signal_evidence(accepted, {"source_url": url, "title": url})
            assert set(repository.visible_evidence_owners(cluster)) == set(cluster), (step, op, cluster)
        backwards = [url for url, stage in _owner_stages().items()
                     if before.get(url) in ("judge", "review") and stage == "search"]
        assert not backwards, (step, op, cluster, backwards)
        with repository.get_connection() as conn:
            groups = conn.execute("SELECT d.id, t.merged_into_signal_id FROM signals d"
                                  " JOIN signals t ON t.id = d.merged_into_signal_id").fetchall()
            links_at_duplicates = conn.execute(
                "SELECT count(*) FROM signal_evidence e JOIN signals s ON s.id = e.signal_id"
                " WHERE s.merged_into_signal_id IS NOT NULL").fetchone()[0]
            empty_copies = [row[0] for row in conn.execute(
                "SELECT s.id FROM signals s WHERE s.filter_stage IS NOT NULL AND s.merged_into_signal_id IS NULL"
                " AND NOT EXISTS (SELECT 1 FROM signal_evidence e WHERE e.signal_id = s.id)")]
        assert not empty_copies, (step, op, cluster, empty_copies)
        assert all(root_merged is None for _, root_merged in groups), (step, op, cluster, groups)
        assert links_at_duplicates == 0, (step, op, cluster)


def test_unknown_filter_stage_from_the_worker_is_stored_as_search(isolated_db):
    _apply_run([{"topic": DRILLING, "candidates": [], "filtered_findings": [
        {"stage": "weird", "title": "Странная стадия", "reason": "причина", "url": "https://example.com/w"}]}])

    assert list(_filtered().values()) == ["отсеяно (поиск): причина"]


def test_recall_counts_only_filtered_cards_a_human_approved(isolated_db):
    _apply_run([_search_drop("SLB MPD на Ямале", "https://example.com/slb")])
    (card,) = _filtered()
    assert repository.radar_cards_for_recall() == []

    _feedback(card, "verdict", "approved")

    assert [(row["id"], row["visible"]) for row in repository.radar_cards_for_recall()] == [(card, True)]


def test_overrated_counts_as_approval(isolated_db):
    from oiltech_digest import signal_dedup

    # «Оценка завышена, сигнал годный, балл слишком высокий»: решение судьи такую не прячет.
    assert "overrated" in repository.SIGNAL_POSITIVE_VERDICTS
    assert "overrated" in signal_dedup._POSITIVE_VERDICTS
    card = _store("card", signal_category="business")
    _feedback(card, "verdict", "overrated")

    assert card in _visible_ids()


# --- Причина и ход решения (03.10) ------------------------------------------------------------


def test_stored_finding_and_search_drop_carry_their_decision_log(isolated_db):
    finding = {"signal": {"title": "SLB внедрила MPD на Ямале", "theme": DRILLING, "summary": "", "companies": [],
                          "maturity": "watch", "score": 0, "signal_key": "ok", "evidence_count": 1,
                          "evidence": [{"source_url": "https://slb.example/mpd",
                                        "title": "SLB внедрила MPD на Ямале"}]}}

    _apply_run([{"topic": DRILLING, "candidates": [finding], "filtered_findings": [
        {"stage": "search", "title": "Контракт ProPetro", "reason": "страница не открылась, текста издателя нет",
         "url": "https://example.com/p", "publisher": "ProPetro", "event_date": "2026-09-20", "technology": ""},
    ]}])

    with repository.get_connection() as conn:
        rows = {key: (log, filter_reason) for key, log, filter_reason in conn.execute(
            "SELECT signal_key, decision_log, filter_reason FROM signals")}
    # Находка: что нашёл поиск; решение и его шаги допишет «Отбор».
    assert rows["ok"] == ([{"stage": "поиск", "text": "Найдено: SLB внедрила MPD на Ямале (slb.example)"}], None)
    search = next(value for key, value in rows.items() if key.startswith("filtered:"))
    assert search == ([
        {"stage": "поиск", "text": "Найдено: Контракт ProPetro (ProPetro, 2026-09-20)"},
        {"stage": "отсев", "text": "Отсеяно (поиск): страница не открылась, текста издателя нет"},
    ], "страница не открылась, текста издателя нет")


def test_decision_log_survives_a_refind_by_an_old_worker(isolated_db):
    signal_id = _store("card", decision_log=[{"stage": "судья", "text": "шаг"}], verdict_reason="причина")
    repository.upsert_signal({"signal_key": "card", "title": "card", "theme": DRILLING, "score": 50})

    with repository.get_connection() as conn:
        row = conn.execute("SELECT verdict_reason, decision_log FROM signals WHERE id = %s", (signal_id,)).fetchone()
    assert row == ("причина", [{"stage": "судья", "text": "шаг"}])
