"""ОС коллег по технологическому радару доходит до обучения агента (решение владельца 28.09).

Проверяется на настоящей базе: какой пример увидит судья и какая подсказка поиска
останется, если коллега оценил карточку, поменял оценку, выбрал её в дайджест или
назвал дублем.
"""

from oiltech_digest import signal_feedback
from oiltech_digest.db import repository

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


def _active(memory_type, signal_id):
    with repository.get_connection() as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT subject FROM signal_agent_memory WHERE memory_type = %s AND status = 'active'"
                " AND facts_json->>'signal_id' = %s ORDER BY id",
                (memory_type, str(signal_id)),
            )
        ]


def test_new_verdict_of_a_colleague_replaces_the_previous_one(isolated_db):
    user = _user()
    signal = _signal("s1", "Роботизированная буровая установка", url="https://example.com/rig")

    signal_feedback.store_signal_feedback(
        {"signal_id": signal, "signal_title": "Роботизированная буровая установка", "verdict": "approved",
         "reason": "есть внедрение", "comment": ""},
        user_id=user,
    )
    result = signal_feedback.store_signal_feedback(
        {"signal_id": signal, "signal_title": "Роботизированная буровая установка", "verdict": "reject",
         "reason": "обзор без нового факта", "comment": ""},
        user_id=user,
    )

    assert result["superseded"] >= 1
    assert _active("signal_verdict", signal) == ["reject"]
    # После отказа поиск больше не учится на этой карточке.
    assert _active("signal_query_hint", signal) == []


def test_duplicate_verdict_hides_the_card_although_it_was_reviewed(isolated_db):
    user = _user()
    main = _signal("main", "ZenaTech: дроны для инспекции", url="https://example.com/a")
    dup = _signal("dup", "ZenaTech дроны для инспекций трубопроводов", url="https://example.com/b")

    result = signal_feedback.store_signal_feedback(
        {"signal_id": dup, "signal_title": "ZenaTech дроны", "verdict": "merge_duplicate",
         "duplicate_of_signal_id": main, "reason": "то же событие", "comment": ""},
        user_id=user,
    )

    assert result["merged"] is True
    listed = {row["id"] for row in repository.list_signals(limit=50)}
    assert dup not in listed and main in listed
    # Ссылки дубля видны в главной карточке.
    assert {row["source_url"] for row in repository.list_signal_evidence(main)} == {
        "https://example.com/a",
        "https://example.com/b",
    }


def test_wrong_block_teaches_the_judge_what_not_to_bring(isolated_db):
    user = _user()
    signal = _signal("biz", "Сделка M&A сервисной компании", url="https://example.com/deal")

    signal_feedback.store_signal_feedback(
        {"signal_id": signal, "signal_title": "Сделка M&A сервисной компании", "verdict": "не тот блок",
         "reason": "это бизнес, не технология", "comment": ""},
        user_id=user,
    )

    assert _active("signal_verdict", signal) == ["wrong_block"]
    block = signal_feedback.feedback_prompt_block()
    rejected = block.split("feedback_rejected_examples", 1)[1]
    assert "wrong_block (это бизнес-сигнал, а не технология" in rejected
    assert "Сделка M&A сервисной компании" in rejected


def test_rejected_examples_reach_the_judge_even_behind_many_approvals(isolated_db):
    for index in range(130):
        repository.upsert_signal_agent_memory(
            memory_key=f"approved-{index}", memory_type="signal_verdict", subject="approved", score=90,
            facts={"signal_title": f"Одобренный сигнал {index}", "reason": "хорошо"},
        )
    repository.upsert_signal_agent_memory(
        memory_key="reject-1", memory_type="signal_verdict", subject="reject", score=-80,
        facts={"signal_title": "Школьный проект по литию", "reason": "шум"},
    )

    block = signal_feedback.feedback_prompt_block()
    snapshot = signal_feedback.memory_snapshot_rows()

    assert "Школьный проект по литию" in block.split("feedback_rejected_examples", 1)[1]
    assert any(row["subject"] == "reject" for row in snapshot["signal_verdict"])
    # Судье на воркере без базы — тот же набор, что и на ядре.
    with signal_feedback.use_memory_snapshot(snapshot):
        assert "Школьный проект по литию" in signal_feedback.feedback_prompt_block()


def test_duplicates_do_not_crowd_real_rejections_out_of_the_judge_prompt(isolated_db):
    # Память прода 28.09: «Дубль» числится среди отказов, но с весом +85, и 11 дублей
    # занимали все 8 мест раздела — ни один из 16 настоящих отказов (−80) судья не видел.
    for subject, score, count in (("approved", 90, 12), ("merge_duplicate", 85, 11),
                                  ("reject", -80, 10), ("wrong_domain", -80, 3), ("too_generic", -80, 3)):
        for index in range(count):
            repository.upsert_signal_agent_memory(
                memory_key=f"{subject}-{index}", memory_type="signal_verdict", subject=subject, score=score,
                facts={"signal_title": f"{subject} {index}", "reason": "разметка"},
            )

    snapshot = signal_feedback.memory_snapshot_rows()
    for block in (signal_feedback.feedback_prompt_block(), None):
        if block is None:
            with signal_feedback.use_memory_snapshot(snapshot):
                block = signal_feedback.feedback_prompt_block()
        rejected = block.split("feedback_rejected_examples", 1)[1].split("feedback_", 1)[0]
        verdicts = [line.split("verdict=", 1)[1].split(" ", 1)[0] for line in rejected.splitlines() if "verdict=" in line]
        assert len(verdicts) == signal_feedback.PROMPT_NEGATIVE_EXAMPLES
        assert set(verdicts) <= {"reject", "wrong_domain", "too_generic"}


def test_digest_selection_is_a_strong_example_and_unselect_retracts_it(isolated_db):
    user = _user()
    signal = _signal("dig", "Цифровой двойник скважины у оператора", url="https://example.com/twin")
    signal_feedback.store_signal_feedback(
        {"signal_id": signal, "signal_title": "Цифровой двойник скважины у оператора", "verdict": "reject",
         "reason": "рано", "comment": ""},
        user_id=user,
    )

    signal_feedback.learn_from_digest_selection(signal, selected=True, user_id=user)
    # Выбор — свой пример «Сильный сигнал»; вердикт человека из формы он не трогает.
    assert _active("signal_verdict", signal) == ["reject", "strong_signal"]

    signal_feedback.learn_from_digest_selection(signal, selected=False, user_id=user)
    assert _active("signal_verdict", signal) == ["reject"]


def test_card_without_links_is_hidden_and_human_corrections_survive_rediscovery(isolated_db):
    user = _user()
    shown = _signal("shown", "Модельный заголовок", url="https://example.com/shown")
    empty = _signal("empty", "Карточка без ссылок")

    signal_feedback.store_signal_feedback(
        {"signal_id": shown, "signal_title": "Модельный заголовок", "comment": "",
         "corrected_title": "Заголовок от коллеги", "corrected_thesis": "Суть от коллеги"},
        user_id=user,
    )
    # Прогон радара находит ту же карточку снова и переписывает её текст моделью.
    repository.upsert_signal(
        {"signal_key": "shown", "title": "Новый текст модели", "title_ru": "Новый текст модели", "theme": THEME,
         "maturity": "watch", "score": 70, "summary": "Новая суть модели"}
    )

    rows = {row["id"]: row for row in repository.list_signals(limit=50)}
    assert empty not in rows
    assert rows[shown]["title_ru"] == "Заголовок от коллеги"
    assert rows[shown]["summary"] == "Суть от коллеги"
    assert rows[shown]["original_title_ru"] == "Новый текст модели"


def test_rejected_source_candidate_is_not_revived_by_rediscovery(isolated_db):
    url = "https://example.com/newsroom"
    candidate = repository.upsert_source_candidate({"url": url, "status": "new", "discovered_by": "agent"})
    with repository.get_connection() as conn:
        conn.execute("UPDATE source_candidates SET status = 'rejected' WHERE id = %s", (candidate,))
        conn.commit()

    repository.upsert_source_candidate({"url": url, "status": "new", "discovered_by": "agent"})

    with repository.get_connection() as conn:
        status = conn.execute("SELECT status FROM source_candidates WHERE id = %s", (candidate,)).fetchone()[0]
    assert status == "rejected"


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


def test_old_verdict_is_replaced_even_after_the_card_was_retitled(isolated_db):
    # Строка памяти до 28.09: без signal_id, с прежним заголовком — узнаётся по своему отзыву.
    user = _user()
    signal = _signal("old", "Explor получила 18 000 узлов Sercel Accel", url="https://example.com/sercel")
    event = repository.record_signal_feedback_event(
        None, "comment_added", signal_id=signal, user_id=user, verdict="strong_signal", comment="",
    )
    repository.upsert_signal_agent_memory(
        memory_key="legacy-verdict", memory_type="signal_verdict", subject="strong_signal", score=100,
        facts={"signal_title": "Масштабное развертывание узловой сейсмики Sercel Accel", "feedback_event_id": event},
    )

    signal_feedback.store_signal_feedback(
        {"signal_id": signal, "signal_title": "Explor получила 18 000 узлов Sercel Accel", "verdict": "wrong_block",
         "reason": "сделка, не технология", "comment": ""},
        user_id=user,
    )

    with repository.get_connection() as conn:
        status = conn.execute("SELECT status FROM signal_agent_memory WHERE memory_key = 'legacy-verdict'").fetchone()[0]
    assert status == "superseded"
    assert _active("signal_verdict", signal) == ["wrong_block"]


def test_colleague_keeps_digest_example_until_the_last_one_unselects(isolated_db):
    first, second = _user(), int(repository.create_user("second@example.test", "long-enough-password", "user")["id"])
    signal = _signal("both", "Роботизированная обсадка", url="https://example.com/casing")

    signal_feedback.learn_from_digest_selection(signal, selected=True, user_id=first)
    signal_feedback.learn_from_digest_selection(signal, selected=True, user_id=second)
    signal_feedback.learn_from_digest_selection(signal, selected=False, user_id=second)

    # Второй снял отметку, у первого карточка в выпуске — пример остаётся.
    assert _active("signal_verdict", signal) == ["strong_signal"]


def test_duplicate_verdict_spares_card_chosen_for_the_issue(isolated_db):
    user = _user()
    main = _signal("m", "ZenaTech: дроны", url="https://example.com/m")
    chosen = _signal("c", "ZenaTech дроны для инспекций", url="https://example.com/c")
    repository.set_user_signal_status(user, chosen, status="digest")

    result = signal_feedback.store_signal_feedback(
        {"signal_id": chosen, "signal_title": "ZenaTech дроны", "verdict": "merge_duplicate",
         "duplicate_of_signal_id": main, "comment": ""},
        user_id=user,
    )

    # Выбранная в выпуск карточка не исчезает с экрана, оставаясь в выпуске.
    assert result["merged"] is False and result["merge_skipped"] is True
    assert chosen in {row["id"] for row in repository.list_signals(limit=50)}


def test_hidden_duplicate_does_not_enter_the_issue(isolated_db):
    user = _user()
    main = _signal("m2", "Сделка ZenaTech", url="https://example.com/m2")
    dup = _signal("d2", "ZenaTech купила", url="https://example.com/d2")
    repository.set_user_signal_status(user, dup, status="digest")
    repository.mark_signal_merged(dup, main, "судья дедупа", respect_review=True)
    with repository.get_connection() as conn:  # дубль, скрытый раньше отметки в выпуск
        conn.execute("UPDATE signals SET merged_into_signal_id = %s WHERE id = %s", (main, dup))
        conn.commit()

    rows = repository.digest_candidates(user_id=user, min_score=0)

    assert dup not in {row["id"] for row in rows if row.get("item_type") == "signal"}


def test_same_title_on_another_card_is_left_alone(isolated_db):
    user = _user()
    twin_a = _signal("ta", "Автономная буровая установка", url="https://example.com/ta")
    twin_b = _signal("tb", "Автономная буровая установка", url="https://example.com/tb")
    signal_feedback.store_signal_feedback(
        {"signal_id": twin_a, "signal_title": "Автономная буровая установка", "verdict": "approved",
         "reason": "внедрение", "comment": ""},
        user_id=user,
    )

    signal_feedback.store_signal_feedback(
        {"signal_id": twin_b, "signal_title": "Автономная буровая установка", "verdict": "merge_duplicate",
         "duplicate_of_signal_id": twin_a, "comment": ""},
        user_id=user,
    )

    # «Дубль» на второй карточке не гасит одобрение первой с тем же заголовком.
    assert _active("signal_verdict", twin_a) == ["approved"]


def test_digest_selection_leaves_training_examples_for_real_feedback(isolated_db):
    user = _user()
    signal = _signal("tr", "Сейсморазведка без кабеля", url="https://example.com/tr")
    example = repository.create_signal_training_example(
        generation_run_id=None, signal_id=signal, topic=THEME, signal_key="tr", pipeline_verdict="kept",
        input_payload={}, raw_output={}, normalized_output={},
    )
    event = repository.record_signal_feedback_event(None, "added_to_digest", signal_id=signal, user_id=user)

    signal_feedback.learn_from_digest_selection(signal, selected=True, user_id=user, event_id=event)

    with repository.get_connection() as conn:
        linked = conn.execute("SELECT feedback_event_id FROM signal_training_examples WHERE id = %s", (example,)).fetchone()[0]
    assert linked is None


def test_mistaken_duplicate_can_be_undone(isolated_db):
    user = _user()
    main = _signal("um", "Главная", url="https://example.com/um")
    dup = _signal("ud", "Ошибочно дубль", url="https://example.com/ud")
    signal_feedback.store_signal_feedback(
        {"signal_id": dup, "signal_title": "Ошибочно дубль", "verdict": "merge_duplicate",
         "duplicate_of_signal_id": main, "comment": ""},
        user_id=user,
    )

    assert repository.unmerge_signal(dup) is True
    assert dup in {row["id"] for row in repository.list_signals(limit=50)}


def test_cards_with_same_title_and_verdict_keep_separate_memory(isolated_db):
    user = _user()
    first = _signal("same1", "Автоматизация буровой", url="https://example.com/s1")
    second = _signal("same2", "Автоматизация буровой", url="https://example.com/s2")
    for signal in (first, second):
        signal_feedback.store_signal_feedback(
            {"signal_id": signal, "signal_title": "Автоматизация буровой", "verdict": "approved",
             "reason": "внедрение", "comment": ""},
            user_id=user,
        )

    signal_feedback.store_signal_feedback(
        {"signal_id": second, "signal_title": "Автоматизация буровой", "verdict": "reject",
         "reason": "повтор", "comment": ""},
        user_id=user,
    )

    # Отказ по второй карточке не забирает одобрение первой: у каждой своя строка памяти.
    assert _active("signal_verdict", first) == ["approved"]
    assert _active("signal_verdict", second) == ["reject"]
