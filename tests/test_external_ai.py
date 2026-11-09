import pytest

from oiltech_digest.processing import external_ai


def test_external_ai_process_payload_offline_returns_structured_result():
    """Пакет статей ленты (снятие старой цепочки 08.11): только суть и перевод заголовка, тегов и критериев в пакете
    нет. Гейта, тега и балла в итоге нет: решает «Отбор», тему и баллы ставит «Карточка»."""
    payload = {
        "offline": True,
        "articles": [
            {
                "id": 1,
                "title": "Directional drilling automation",
                "url": "https://example.com/a",
                "language": "en",
                "raw_text": "Automation improves drilling efficiency and well construction quality.",
                "source_name": "Example",
                "source_category": "Drilling",
                "otbor_decision": "accept",
            }
        ],
    }

    result = external_ai.process_payload(payload)

    assert result["external_ai"] is True
    assert result["stats"] == {"processed": 1, "summary": 1, "translated": 1, "errors": 0}
    article = result["articles"][0]
    assert article["article_id"] == 1
    assert article["summary"]["summary"]
    assert article["translation"]["title_ru"]                # иностранный заголовок переведён
    assert set(article) == {"article_id", "errors", "summary", "translation"}


def test_external_ai_process_payload_heartbeats_per_article():
    article = {
        "id": 1,
        "title": "Directional drilling automation",
        "url": "https://example.com/a",
        "language": "en",
        "raw_text": "Automation improves drilling efficiency and well construction quality.",
        "source_name": "Example",
        "source_category": "Drilling",
    }
    payload = {
        "offline": True,
        "articles": [dict(article, id=1), dict(article, id=2), dict(article, id=3)],
    }
    beats = []
    # Колбэк, который один раз бросает: обработка не должна падать (heartbeat защищён).
    def heartbeat(done=None):
        beats.append(1)
        if len(beats) == 2:
            raise RuntimeError("transient heartbeat failure")

    result = external_ai.process_payload(payload, heartbeat=heartbeat)

    assert len(beats) == 3              # по разу на каждую из 3 статей
    assert result["stats"]["processed"] == 3   # сбой heartbeat не прервал батч


def test_heartbeat_that_cannot_take_the_result_fails_loudly():
    """23.09: циклы ИИ передают heartbeat итог на границе шага (его отдаст воркер, если шаг
    зависнет на остановке). Колбэк старой сигнатуры проглоченным TypeError тихо отключил бы
    и остановку, и отзыв аренды: класс 24.07. Поэтому это громкая ошибка."""
    payload = {
        "offline": True,
        "articles": [{"id": 1, "title": "Drilling", "url": "https://example.com/a", "raw_text": "drilling"}],
    }

    with pytest.raises(TypeError):
        external_ai.process_payload(payload, heartbeat=lambda: None)
    with pytest.raises(TypeError):
        external_ai.process_translate_payload({"articles": payload["articles"]}, heartbeat=lambda: None)


def test_external_ai_apply_process_result_calls_repository(monkeypatch):
    calls = []

    monkeypatch.setattr(external_ai.repository, "upsert_article_card", lambda *args: calls.append(("summary", args)))
    monkeypatch.setattr(external_ai.repository, "set_article_title_ru", lambda *args: calls.append(("translation", args)))
    monkeypatch.setattr(external_ai.repository, "insert_ai_run", lambda rec: calls.append(("run", rec["stage"])))
    monkeypatch.setattr(external_ai.repository, "get_articles_by_ids", lambda ids, **kwargs: [])

    stats = external_ai.apply_process_result(
        {
            "external_ai": True,
            "articles": [
                {
                    "article_id": 1,
                    "summary": {"summary": "Short", "model": "offline", "provider": "offline"},
                    "translation": {"title_ru": "Короткий заголовок", "model": "offline", "provider": "offline"},
                }
            ],
        }
    )

    assert stats == {"articles": 1, "summary": 1, "translation": 1, "errors": 0}
    assert [item[0] for item in calls] == ["summary", "run", "translation", "run"]


SPUD_ARTICLE = {
    "id": 1,
    "title": "Global Land Drilling Rigs Tracker",
    "raw_text": "In Egypt, NDC 9 spudded the vertical well T-200. The tracker adds more granular field data.",
    "language": "en",
}


def _stub_writes(monkeypatch, articles):
    written = {}
    monkeypatch.setattr(external_ai.repository, "get_articles_by_ids", lambda ids, **kwargs: articles(ids))
    monkeypatch.setattr(
        external_ai.repository, "upsert_article_card", lambda article_id, summary, model: written.update(summary=summary)
    )
    monkeypatch.setattr(
        external_ai.repository, "set_article_title_ru", lambda article_id, title_ru: written.update(title_ru=title_ru)
    )
    monkeypatch.setattr(external_ai, "_insert_run", lambda *args, **kwargs: None)
    return written


def test_core_applies_glossary_to_worker_result(monkeypatch):
    """Замечание заказчика 22.09: «спудрил вертикальную скважину», «более granularными».

    Словарь на воркере: его версия кода, NL пересобирает владелец; без прохода на ядре
    правка словаря не дошла бы до новых карточек до пересборки NL.
    """
    written = _stub_writes(monkeypatch, lambda ids: [SPUD_ARTICLE])

    external_ai.apply_process_result(
        {
            "articles": [
                {
                    "article_id": 1,
                    "summary": {
                        "summary": "В Египте NDC 9 спудрил вертикальную скважину T-200; трекер даёт более granularные данные.",
                        "model": "gpt",
                    },
                    "translation": {"title_ru": "NDC 9 спудрил скважину T-200", "model": "gpt"},
                }
            ]
        }
    )

    assert written["summary"] == "В Египте NDC 9 забурил вертикальную скважину T-200; трекер даёт более детальные данные."
    assert written["title_ru"] == "NDC 9 забурил скважину T-200"


def test_core_applies_glossary_to_translation_result(monkeypatch):
    written = _stub_writes(monkeypatch, lambda ids: [SPUD_ARTICLE])

    external_ai.apply_translate_result(
        {"articles": [{"article_id": 1, "translation": {"title_ru": "NDC 9 спудрил скважину T-200", "model": "gpt"}}]}
    )

    assert written["title_ru"] == "NDC 9 забурил скважину T-200"


def test_core_writes_paid_result_as_is_when_glossary_context_fails(monkeypatch):
    def broken(ids):
        raise RuntimeError("база недоступна")

    written = _stub_writes(monkeypatch, broken)

    stats = external_ai.apply_process_result(
        {"articles": [{"article_id": 1, "summary": {"summary": "NDC 9 спудрил скважину.", "model": "gpt"}}]}
    )

    assert stats["summary"] == 1
    assert written["summary"] == "NDC 9 спудрил скважину."


def test_result_of_build_before_the_cleanup_writes_summary_but_bills_every_stage(monkeypatch):
    """Итог сборки NL, выданный до выката снятия (08.11), несёт ответы гейта, тега и балла: ядро их не пишет (гейт,
    передумав, убрал бы статью из ленты, а тег и балл статьи больше никто не читает как своё), но оплаченные вызовы
    учитывает все. С пометкой only, как раньше, пишется запрошенное."""
    writes, runs = [], []
    monkeypatch.setattr(external_ai.repository, "get_articles_by_ids", lambda ids, **kwargs: [])
    monkeypatch.setattr(external_ai.repository, "upsert_article_card", lambda *args: writes.append("summary"))
    monkeypatch.setattr(external_ai.repository, "set_article_title_ru", lambda *args: writes.append("translation"))
    monkeypatch.setattr(external_ai, "_insert_run", lambda article_id, stage, payload, **kwargs: runs.append(stage))
    item = {
        "article_id": 1,
        "relevance": {"relevant": False, "reason": "передумал", "model": "gpt-5.5"},
        "summary": {"summary": "Новая суть", "model": "gpt-5-mini"},
        "translation": {"title_ru": "Новый заголовок", "model": "gpt-5-mini"},
        "tagging": {"tag_id": 10, "confidence": 0.5, "model": "gpt-5-mini"},
        "scoring": {"total_score": 10, "score_label": "Низкая", "items": [], "model": "gpt-5-mini"},
    }

    stats = external_ai.apply_process_result({"articles": [item]})

    assert writes == ["summary", "translation"]
    assert sorted(runs) == ["relevance", "scoring", "summary", "tagging", "translation"]
    assert (stats["summary"], stats["translation"]) == (1, 1)

    writes.clear()
    external_ai.apply_process_result({"articles": [item]}, only=["summary", "tagging", "scoring"])
    assert writes == ["summary"]


def test_apply_rejects_malformed_only_before_any_write(monkeypatch):
    """payload_json, граница: строка «summary» дала бы множество букв, пустой список: все стадии."""
    import pytest

    writes = []
    monkeypatch.setattr(external_ai.repository, "get_articles_by_ids", lambda ids, **kwargs: [])
    monkeypatch.setattr(external_ai.repository, "upsert_article_card", lambda *args: writes.append(args))
    result = {"articles": [{"article_id": 1, "summary": {"summary": "Суть", "model": "gpt"}}]}

    for bad in ("summary", [], ["summary", "gate"], [1], ["tagging", "scoring"]):
        with pytest.raises(ValueError, match="only"):
            external_ai.apply_process_result(result, only=bad)
    assert writes == []


def test_resummarize_payload_drops_the_old_broken_summary(monkeypatch):
    """Старая суть попадала в промпт (_article_prompt кладёт summary), и модель повторяла брак. Задача пересчёта после
    полного текста, поставленная до снятия тега и балла (суть, тег и балл), выдаётся с одной сутью, а не падает."""
    article = {"id": 7, "title": "Power prices", "summary": "Цены на электроэнergyю выросли.", "raw_text": "x"}
    monkeypatch.setattr(external_ai.repository, "reserve_process_articles", lambda job_id, **kwargs: [7])
    monkeypatch.setattr(external_ai.repository, "get_articles_by_ids", lambda ids, **kwargs: [dict(article)])

    regular = external_ai.build_process_articles_payload({"article_ids": [7]}, job_id=1)
    regen = external_ai.build_process_articles_payload({"article_ids": [7], "only": ["summary", "translation"]}, job_id=2)
    queued_before = external_ai.build_process_articles_payload(
        {"article_ids": [7], "only": ["summary", "tagging", "scoring"]}, job_id=3)
    title_edit = external_ai.build_process_articles_payload({"article_ids": [7], "only": ["translation"]}, job_id=4)

    assert regular["articles"][0]["summary"] == article["summary"]
    assert regen["articles"][0]["summary"] is None
    assert queued_before["articles"][0]["summary"] is None
    # Правка заголовка суть не перегенерирует: заголовок пишется по записанной сути.
    assert title_edit["articles"][0]["summary"] == article["summary"]
    assert (regular.get("only"), regen["only"], queued_before["only"], title_edit["only"]) == (
        None, ["summary", "translation"], ["summary"], ["translation"])
    assert "tags" not in regular and "criteria" not in regular


def test_malformed_only_is_rejected_before_articles_are_reserved(monkeypatch):
    """Ревью 27.09: пометка проверялась только при записи: когда ответ модели уже оплачен."""
    import pytest

    reserved = []
    monkeypatch.setattr(external_ai.repository, "reserve_process_articles", lambda job_id, **kwargs: reserved.append(job_id) or [7])

    with pytest.raises(external_ai.InvalidJobPayload, match="only"):
        external_ai.build_process_articles_payload({"article_ids": [7], "only": "summary"}, job_id=3)
    assert reserved == []


def test_core_passes_only_from_job_payload_to_apply(monkeypatch):
    from oiltech_digest import api

    seen = {}
    monkeypatch.setattr(api.external_ai, "apply_process_result",
                        lambda result, *, job_id=None, only=None: seen.update(only=only, job_id=job_id) or {})
    job = {"kind": "process_articles", "payload_json": {"article_ids": [1], "only": ["summary", "translation"]}}

    api._apply_external_result(job, {"external_ai": True, "articles": []}, 42)

    assert seen == {"only": ["summary", "translation"], "job_id": 42}
    api._apply_external_result({"kind": "process_articles", "payload_json": {}}, {"external_ai": True, "articles": []}, 43)
    assert seen["only"] is None


def _recording_offline(calls: list):
    """Офлайн-модель и список схем, за которые её звали."""
    from oiltech_digest.processing.openai_client import OfflineAIClient

    class Recording(OfflineAIClient):
        def complete_json(self, instructions, user_input, schema, **kwargs):
            calls.append(schema["name"])
            return super().complete_json(instructions, user_input, schema, **kwargs)

    return Recording()


def _regeneration_payload(only) -> dict:
    return {
        "offline": True,
        "only": only,
        "articles": [
            {"id": 1, "title": "Oil prices rally", "url": "https://e.com/1", "language": "en",
             "raw_text": "Oil prices rally after drilling news.", "source_name": "S", "otbor_decision": "accept"},
            {"id": 2, "title": "Бурение на шельфе", "url": "https://e.ru/2", "language": "ru",
             "raw_text": "Бурение на шельфе Сахалина.", "source_name": "S", "otbor_decision": "accept"},
        ],
    }


def test_worker_runs_only_the_stages_the_core_will_write(monkeypatch):
    """Перегенерация сути (27.09): пометка only приходит воркеру, не запрошенное не зовётся."""
    calls: list = []
    monkeypatch.setattr(external_ai, "make_client", lambda offline=False: _recording_offline(calls))

    result = external_ai.process_payload(_regeneration_payload(["summary", "translation"]))

    assert sorted(calls) == ["article_summary", "article_summary", "article_title_translation"]
    for item in result["articles"]:
        assert set(item) == {"article_id", "errors", "summary", "translation"}, item
    assert result["stats"]["summary"] == 2


def test_worker_drops_retired_stages_from_a_job_queued_before_the_cleanup(monkeypatch):
    """Пометка с гейтом, тегом или баллом (задача поставлена до 08.11): воркер зовёт только то, что от неё осталось."""
    calls: list = []
    monkeypatch.setattr(external_ai, "make_client", lambda offline=False: _recording_offline(calls))

    result = external_ai.process_payload(_regeneration_payload(["relevance", "summary", "tagging", "scoring"]))

    assert calls == ["article_summary", "article_summary"]
    assert all(set(item) == {"article_id", "errors", "summary"} for item in result["articles"])


def test_worker_rejects_malformed_only_before_any_model_call(monkeypatch):
    """payload, граница: строка «summary» дала бы множество букв, и воркер не сделал бы ничего."""
    calls: list = []
    monkeypatch.setattr(external_ai, "make_client", lambda offline=False: _recording_offline(calls))

    with pytest.raises(external_ai.InvalidJobPayload, match="only"):
        external_ai.process_payload(_regeneration_payload("summary"))
    assert calls == []


def test_lease_loss_aborts_batch_instead_of_burning_money():
    """Инцидент 24.07: core отозвал lease (heartbeat 409), но воркер продолжал звать
    OpenAI по каждой статье. Результат уже не примут: деньги в мусор (~$11/час,
    80 минут). Теперь LeaseLost обязан прервать батч на ПЕРВОЙ же статье."""
    calls = {"heartbeats": 0}

    def failing_heartbeat(done=None):
        calls["heartbeats"] += 1
        raise external_ai.LeaseLost("lease lost for job 1181")

    payload = {
        "offline": True,
        "articles": [{"id": i, "title": f"A{i}", "url": f"https://e.ru/{i}",
                      "language": "ru", "raw_text": "бурение скважин", "source_name": "S"}
                     for i in range(1, 6)],
    }

    try:
        external_ai.process_payload(payload, heartbeat=failing_heartbeat)
        raise AssertionError("LeaseLost проглочен — батч продолжил работу и жжёт деньги")
    except external_ai.LeaseLost:
        pass

    assert calls["heartbeats"] == 1, "прервались не на первой статье — часть вызовов уже оплачена"


def test_transient_heartbeat_failure_does_not_abort_batch():
    """Обратная сторона: обычный сбой сети НЕ должен ронять батч: иначе любое моргание
    связи будет терять уже оплаченную работу. Прерываемся только на потере lease."""
    calls = {"heartbeats": 0}

    def flaky_heartbeat(done=None):
        calls["heartbeats"] += 1
        raise ConnectionError("сеть моргнула")

    payload = {
        "offline": True,
        "articles": [{"id": i, "title": f"A{i}", "url": f"https://e.ru/{i}",
                      "language": "ru", "raw_text": "бурение скважин", "source_name": "S"}
                     for i in range(1, 4)],
    }

    result = external_ai.process_payload(payload, heartbeat=flaky_heartbeat)

    assert calls["heartbeats"] == 3, "батч оборвался на временном сбое"
    assert result["stats"]["processed"] == 3
