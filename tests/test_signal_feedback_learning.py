"""Отметка ОС администратора на радаре (#188): «Дубль» прячет карточку в главную, правки заголовка и сути лежат поверх
текста модели и переживают повторную находку. Проверяется на настоящей базе путём записи отметки
(signal_feedback.record_feedback), тем же, что у формы. Память агента отметка не пишет (#188).
"""

from oiltech_digest import signal_feedback
from oiltech_digest.db import repository
from oiltech_digest.db import marks

THEME = "Бурение, заканчивание и внутрискважинные работы"


def _user(role="user"):
    return int(repository.create_user(f"colleague-{role}@example.test", "long-enough-password", role)["id"])


def _signal(key, title, *, url=None, score=70):
    signal_id = repository.upsert_signal(
        {"signal_key": key, "title": title, "title_ru": title, "theme": THEME, "maturity": "watch", "score": score}
    )
    if url:
        repository.upsert_signal_evidence(signal_id, {"source_url": url, "title": title})
    return signal_id


def _admin_mark(admin, fields):
    """Отметка администратора: её явные действия применяются сразу."""
    return signal_feedback.record_feedback({"comment": "", **fields}, user_id=admin, author="admin@example.test",
                                       applies=True)


def test_duplicate_verdict_hides_the_card_although_it_was_reviewed(isolated_db):
    admin = _user("admin")
    main = _signal("main", "ZenaTech: дроны для инспекции", url="https://example.com/a")
    dup = _signal("dup", "ZenaTech дроны для инспекций трубопроводов", url="https://example.com/b")
    signal_feedback.record_feedback({"signal_id": dup, "verdict": "approved", "comment": "Разобрана"},
                                user_id=_user(), author="colleague@example.test", applies=False)

    result = _admin_mark(admin, {"signal_id": dup, "signal_title": "ZenaTech дроны", "verdict": "merge_duplicate",
                                 "duplicate_of_signal_id": main, "reason": "то же событие"})

    assert result["merged"] is True
    listed = {row["id"] for row in repository.list_signals(limit=50)}
    assert dup not in listed and main in listed
    # Ссылки дубля видны в главной карточке.
    assert {row["source_url"] for row in repository.list_signal_evidence(main)} == {
        "https://example.com/a",
        "https://example.com/b",
    }


def test_card_without_links_is_hidden_and_human_corrections_survive_rediscovery(isolated_db):
    admin = _user("admin")
    shown = _signal("shown", "Модельный заголовок", url="https://example.com/shown")
    empty = _signal("empty", "Карточка без ссылок")

    _admin_mark(admin, {"signal_id": shown, "signal_title": "Модельный заголовок",
                        "corrected_title": "Заголовок от эксперта", "corrected_thesis": "Суть от эксперта"})
    # Прогон радара находит ту же карточку снова и переписывает её текст моделью.
    repository.upsert_signal(
        {"signal_key": "shown", "title": "Новый текст модели", "title_ru": "Новый текст модели", "theme": THEME,
         "maturity": "watch", "score": 70, "summary": "Новая суть модели"}
    )

    rows = {row["id"]: row for row in repository.list_signals(limit=50)}
    assert empty not in rows
    assert rows[shown]["title_ru"] == "Заголовок от эксперта"
    assert rows[shown]["summary"] == "Суть от эксперта"
    assert rows[shown]["original_title_ru"] == "Новый текст модели"


def test_listing_marks_themes_outside_customer_topics(isolated_db):
    repository.upsert_tag({"parent_id": None, "name": THEME, "name_en": "Drilling", "description": "",
                           "keywords_json": ["бурение"], "keywords_en_json": ["drilling"], "sort_order": 1})
    topic = _signal("topic", "Карточка с тематикой заказчика", url="https://example.com/topic")
    early = repository.upsert_signal(
        {"signal_key": "early", "title": "Ранняя карточка", "title_ru": "Ранняя карточка", "theme": "HSE/бурение",
         "maturity": "watch", "score": 60}
    )
    repository.upsert_signal_evidence(early, {"source_url": "https://example.com/early", "title": "t"})

    rows = {row["id"]: row for row in repository.list_signals(limit=10)}

    assert rows[topic]["theme_is_topic"] is True
    assert rows[early]["theme_is_topic"] is False


def test_duplicate_verdict_spares_card_chosen_for_the_issue(isolated_db):
    admin = _user("admin")
    main = _signal("m", "ZenaTech: дроны", url="https://example.com/m")
    chosen = _signal("c", "ZenaTech дроны для инспекций", url="https://example.com/c")
    marks.set_signal_mark(chosen, status="digest", user_id=admin)

    result = _admin_mark(admin, {"signal_id": chosen, "signal_title": "ZenaTech дроны", "verdict": "merge_duplicate",
                                 "duplicate_of_signal_id": main})

    # Выбранная в выпуск карточка не исчезает с экрана, оставаясь в выпуске; ответ говорит почему.
    assert result["merged"] is False and "выбрана «в дайджест»" in result["merge_refused"]
    assert chosen in {row["id"] for row in repository.list_signals(limit=50)}


def test_hidden_duplicate_does_not_enter_the_issue(isolated_db):
    user = _user()
    main = _signal("m2", "Сделка ZenaTech", url="https://example.com/m2")
    dup = _signal("d2", "ZenaTech купила", url="https://example.com/d2")
    marks.set_signal_mark(dup, status="digest", user_id=user)
    repository.mark_signal_merged(dup, main, "судья дедупа", respect_review=True)
    with repository.get_connection() as conn:  # дубль, скрытый раньше отметки в выпуск
        conn.execute("UPDATE signals SET merged_into_signal_id = %s WHERE id = %s", (main, dup))
        conn.commit()

    rows = repository.digest_candidates(min_score=0)

    assert dup not in {row["id"] for row in rows if row.get("item_type") == "signal"}


def test_mistaken_duplicate_can_be_undone(isolated_db):
    admin = _user("admin")
    main = _signal("um", "Главная", url="https://example.com/um")
    dup = _signal("ud", "Ошибочно дубль", url="https://example.com/ud")
    _admin_mark(admin, {"signal_id": dup, "signal_title": "Ошибочно дубль", "verdict": "merge_duplicate",
                        "duplicate_of_signal_id": main})

    assert repository.unmerge_signal(dup) is True
    assert dup in {row["id"] for row in repository.list_signals(limit=50)}
