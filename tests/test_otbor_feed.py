"""Агент «Отбор» для ленты (#130, спека #128, ADR 0005), шов 1 — контур целиком: статья сбора → шаг
планировщика → воркер NL на подставной модели → ядро → API витрин и разделов отклонённого
(tests/otbor_support.py).
"""

import itertools

from fastapi.testclient import TestClient
import pytest

from oiltech_digest import api, background_jobs, cli, signal_card
from oiltech_digest.db import analytics, repository
from oiltech_digest.signal_research import UNCONFIRMED_LINK
from oiltech_digest.otbor_pack_format import load_dictionaries
from oiltech_digest.processing import external_ai, fulltext_recompute
from oiltech_digest.processing.openai_client import AIClientError
from oiltech_digest.processing.seed import seed_default_scoring_criteria
from tests.otbor_support import JUSTIFICATION, answer, article, decide, reject, showcase
from tests.otbor_support import contour  # noqa: F401 — фикстура
from tests.signal_card_support import (DRILLING, FakeModel, admin, api_get, radar_finding,  # noqa: F401 — фикстуры
                                       run_worker, tag, user)
from tests.signal_card_support import answer as card_answer

OTBOR_FIELDS = ("industry", "oilfield_relevance", "decision", "reject_reason", "category")


def test_feed_article_accepted_by_otbor_is_in_the_business_feed_with_its_decision(user, contour, monkeypatch):
    article("Программа бурения Equinor", "Equinor начнёт бурение 12 скважин на шельфе Норвегии в 2027 году.")

    model = decide(monkeypatch, {"Программа бурения Equinor": answer()})

    card = showcase(user)["Программа бурения Equinor"]
    assert (card["origin"], card["otbor_decision"], card["industry"], card["oilfield_relevance"],
            card["signal_category"], card["reject_reason"]) == ("feed", "accept", "oil_gas", "direct", "business", None)
    assert card["verdict_reason"] == JUSTIFICATION
    assert {card["card_set_by"][key] for key in ("industry", "oilfield_relevance", "signal_category")} == {"otbor"}
    assert [step["stage"] for step in card["decision_log"]] == ["«Отбор»"]
    [call] = model.calls
    # Вход модели — публикация, справочники и приёмщик ОС; происхождения в нём нет: его использует только код.
    header = call["input"].split("Текст публикации:", 1)[0]
    assert [line.split(":", 1)[0] for line in header.strip().splitlines()] == [
        "Заголовок", "Источник", "Язык", "Дата публикации", "Дата поступления",
        "Похожие сигналы, оценённые людьми", "Оценки людей текстом"]
    assert "Похожие сигналы, оценённые людьми: нет" in call["input"]
    assert "происхожд" not in (call["instructions"] + call["input"]).lower()
    for word in ("лента", "радар", "поиск", "feed", "radar"):
        assert word not in call["input"].lower()
    # Справочники «Отбора» — в каждом вызове: в инструкции и в схеме ответа.
    dictionaries = load_dictionaries()
    properties = call["schema"]["schema"]["properties"]
    assert list(properties) == ["industry", "oilfield_relevance", "oilfield_application", "decision",
                                "reject_reason", "justification", "category"]
    for field in OTBOR_FIELDS:
        codes = [value["code"] for value in dictionaries[field]["values"]]
        assert set(codes) <= set(properties[field]["enum"])
        assert all(code in call["instructions"] for code in codes)


def _feed(user):
    """Лента статей (/api/articles): заголовки видимых статей."""
    return {row["title"] for row in api_get("/api/articles", user)}


def _gate(article_id, relevant):
    """Решение гейта ленты — как его пишет ядро из итога обработки (до «Отбора»)."""
    external_ai.apply_process_result({"articles": [{
        "article_id": article_id, "relevance": {"relevant": relevant, "reason": "r", "model": "m"},
    }]})


def test_new_article_is_hidden_until_otbor_decides_then_shown_by_its_decision(user, contour, monkeypatch):
    article("Программа бурения Equinor", "Equinor начнёт бурение 12 скважин на шельфе Норвегии в 2027 году.")
    article("Котировки нефти", "Нефть дешевеет после роста запасов нефти в США.")
    # До решения «Отбора» новой публикации нет ни в ленте статей, ни в витрине.
    assert (_feed(user), showcase(user)) == (set(), {})

    decide(monkeypatch, {"Программа бурения Equinor": answer(), "Котировки нефти": reject("oil_gas_no_link")})

    assert _feed(user) == {"Программа бурения Equinor"}
    assert set(showcase(user)) == {"Программа бурения Equinor"}


def test_article_decided_before_otbor_lives_by_the_gate_rules(user, contour, monkeypatch):
    monkeypatch.setattr(api.config, "OTBOR_ENABLED", False)
    accepted = article("Контракт Weatherford", "Weatherford получила контракт на бурение 274 скважин в Омане.")
    rejected = article("Прибыль BP", "BP более чем вдвое увеличила чистую прибыль во втором квартале.")
    article("Ещё не проверена", "Газпром нефть пробурила новую скважину на Ямале.")
    _gate(accepted, True)
    _gate(rejected, False)
    monkeypatch.setattr(api.config, "OTBOR_ENABLED", True)

    model = decide(monkeypatch, {})

    # «Отбор» решённое гейтом не трогает; ещё не проверенная гейтом статья видна, как до «Отбора».
    assert model.calls == []
    assert _feed(user) == {"Контракт Weatherford", "Ещё не проверена"}
    assert set(showcase(user)) == {"Контракт Weatherford"}


def _process_feed():
    """Обработка ленты (суть, перевод, тег, балл) — шаг планировщика и воркер NL с офлайн-клиентом."""
    cli.main(["enqueue-process", "--limit", "10", "--offline"])
    [job] = run_worker("external-ai")
    return job


def test_article_accepted_by_otbor_is_processed_without_the_gate_and_stop_words(user, admin, contour, monkeypatch):
    seed_default_scoring_criteria()
    # Стоп-слово темы — смысловой фильтр гейта: принятую «Отбором» статью оно не отклоняет.
    with repository.get_connection() as conn:
        conn.execute("UPDATE tags SET negative_keywords_json = '[\"шельфе\"]'::jsonb WHERE id = %s", (tag(DRILLING),))
        conn.commit()
    article("Программа бурения Equinor", "Equinor начнёт бурение 12 скважин на шельфе Норвегии в 2027 году.")
    article("Ждёт решения", "Equinor испытает робота-бурильщика на шельфе в 2027 году.")
    decide(monkeypatch, {"Программа бурения Equinor": answer(), "Ждёт решения": AIClientError("timeout")})

    job = _process_feed()

    # Обработку проходит только принятая «Отбором»; гейт её не решал, ждущую «Отбора» не трогал никто.
    assert api_get(f"/api/jobs/{job['id']}", admin)["result"]["stats"]["processed"] == 1
    [row] = api_get("/api/articles", user)
    assert (row["title"], row["relevant"], bool(row["summary"])) == ("Программа бурения Equinor", None, True)
    # Мост «лента → сигнал» дописал принятой суть и тему.
    card = showcase(user)["Программа бурения Equinor"]
    assert card["summary"] == row["summary"] and card["theme"] == DRILLING


def test_old_feed_machinery_counts_and_recomputes_by_the_otbor_decision(user, admin, contour, monkeypatch):
    # Решение владельца 23.10: что остаётся от прежней механики ленты (счётчики, пересчёт, тег и балл), читает
    # решение «Отбора» из сигнала.
    seed_default_scoring_criteria()
    tag(DRILLING)
    accepted = article("Программа бурения Equinor", "Equinor начнёт бурение 12 скважин на шельфе Норвегии в 2027 году.")
    rejected = article("Котировки нефти", "Нефть дешевеет после роста запасов нефти в США.")
    decide(monkeypatch, {"Программа бурения Equinor": answer(), "Котировки нефти": reject("oil_gas_no_link")})
    _process_feed()

    # Плитки ленты: принятая обработана.
    assert api_get("/api/stats", user)["processed_articles"] == 1
    # Воронка платформы и месячная аналитика: принятая и отклонённая — по решению «Отбора».
    [platform] = api_get("/api/stats/monthly", admin)["platform"]
    assert (platform["relevant"], platform["rejected"], platform["summarized"]) == (1, 1, 1)
    # Аналитика — без стоимости: та за курсом ходит в сеть ЦБ.
    month = analytics.monthly_analytics(1)["months"][-1]
    assert (month["relevant"], month["rejected"], month["summarized"], month["sources_relevant"]) == (1, 1, 1, 1)
    # Пересчёт после полного текста: суть и балл принятой — по новому телу, отклонённой — ничего.
    monkeypatch.setattr(api.config, "AI_BULK_LANE_ENABLED", True)
    assert fulltext_recompute.enqueue_after_body([accepted, rejected])["articles"] == [accepted]


def test_explicit_article_list_does_not_bypass_otbor(user, admin, contour, monkeypatch, capsys):
    # Явный список (ручной импорт с обработкой, перекачка тела): ждущую «Отбора» гейт не решает, а за суть, тег и
    # балл отклонённой модель не платится; принятая «Отбором» обрабатывается без гейта.
    seed_default_scoring_criteria()
    tag(DRILLING)
    waiting = article("Ждёт решения", "Equinor испытает робота-бурильщика на шельфе в 2027 году.")
    rejected = article("Котировки нефти", "Нефть дешевеет после роста запасов нефти в США.")
    accepted = article("Программа бурения Equinor", "Equinor начнёт бурение 12 скважин на шельфе Норвегии в 2027 году.")
    decide(monkeypatch, {"Котировки нефти": reject("oil_gas_no_link"), "Программа бурения Equinor": answer()},
           "--ids", f"{rejected},{accepted}")

    decision = api.network_policy.route_ai_processing()
    background_jobs.enqueue("process_articles", {"article_ids": [waiting, rejected, accepted], "offline": True},
                            queue_name=decision.queue_name, execution_region=decision.execution_region,
                            capability=decision.capability)
    [job] = run_worker("external-ai")
    assert api_get(f"/api/jobs/{job['id']}", admin)["result"]["stats"]["processed"] == 1
    # Местный конвейер — так же.
    cli.main(["process-articles", str(waiting), str(rejected), "--offline"])
    assert "статей=0" in capsys.readouterr().out
    # Ждущую по-прежнему решит «Отбор»: шаг планировщика её берёт.
    cli.main(["enqueue-otbor"])
    [step] = [job for job in api_get("/api/jobs", admin, kind="otbor") if job["payload"]["trigger"] != "manual"]
    assert step["payload"]["article_ids"] == [waiting]


def test_flag_off_keeps_the_old_chain_and_flag_on_runs_the_new_one(user, contour, monkeypatch):
    seed_default_scoring_criteria()
    tag(DRILLING)
    monkeypatch.setattr(api.config, "OTBOR_ENABLED", False)
    article("Старая цепочка", "Weatherford получила контракт на бурение 274 скважин в Омане.")
    model = decide(monkeypatch, {"Старая цепочка": answer()})
    _process_feed()
    monkeypatch.setattr(api.config, "OTBOR_ENABLED", True)
    article("Новая цепочка", "Equinor начнёт бурение 12 скважин на шельфе Норвегии в 2027 году.")
    decide(monkeypatch, {"Новая цепочка": answer()})
    _process_feed()

    # Выключен — статью решает гейт (офлайн-клиент её принимает), «Отбор» не зовётся; включён — наоборот.
    assert model.calls == []
    assert {row["title"]: row["relevant"] for row in api_get("/api/articles", user)} == {
        "Старая цепочка": True, "Новая цепочка": None}
    assert {title: card["otbor_decision"] for title, card in showcase(user).items()} == {
        "Старая цепочка": None, "Новая цепочка": "accept"}


def test_flag_switched_off_returns_the_waiting_article_to_the_gate(user, admin, contour, monkeypatch, capsys):
    seed_default_scoring_criteria()
    tag(DRILLING)
    waiting = article("Ждёт решения", "Equinor испытает робота-бурильщика на шельфе в 2027 году.")
    decide(monkeypatch, {"Ждёт решения": AIClientError("timeout")})
    assert _feed(user) == set()

    monkeypatch.setattr(api.config, "OTBOR_ENABLED", False)
    _process_feed()

    assert {row["title"]: row["relevant"] for row in api_get("/api/articles", user)} == {"Ждёт решения": True}
    # Решённое гейтом «Отбор» не пересматривает и командой с номером (проверка на реальных данных, #134, пишет
    # свои решения отдельно).
    monkeypatch.setattr(api.config, "OTBOR_ENABLED", True)
    capsys.readouterr()
    cli.main(["enqueue-otbor", "--ids", str(waiting)])
    assert "решает не «Отбор» — пропущены" in capsys.readouterr().out
    assert len(api_get("/api/jobs", admin, kind="otbor")) == 1


def test_gate_recheck_leaves_otbor_articles_alone(user, contour, monkeypatch):
    # Стоп-слово отклоняет статью в перепроверке гейтом без вызова модели.
    with repository.get_connection() as conn:
        conn.execute("UPDATE tags SET negative_keywords_json = '[\"шельфе\"]'::jsonb WHERE id = %s", (tag(DRILLING),))
        conn.commit()
    monkeypatch.setattr(api.config, "OTBOR_ENABLED", False)
    _gate(article("Старая статья", "Бурение на шельфе Сахалина продолжится."), True)
    monkeypatch.setattr(api.config, "OTBOR_ENABLED", True)
    article("Программа бурения Equinor", "Equinor начнёт бурение 12 скважин на шельфе Норвегии в 2027 году.")
    article("Ждёт решения", "Equinor испытает робота-бурильщика на шельфе в 2027 году.")
    decide(monkeypatch, {"Программа бурения Equinor": answer(), "Ждёт решения": AIClientError("timeout")})

    cli.main(["enqueue-recheck", "--mark"])
    run_worker("external-ai")

    # Перепроверка — механизм гейта: статьи, которые решает или решил «Отбор», она не трогает.
    assert _feed(user) == {"Программа бурения Equinor"}
    monkeypatch.setattr(api.config, "OTBOR_ENABLED", False)
    assert _feed(user) == {"Программа бурения Equinor"}  # ждущая «Отбора» не помечена на удаление


def _sections(admin):
    return {section["key"]: section for section in api_get("/api/signals/sections", admin)["sections"]}


def _status(path, user, **params):
    api.app.dependency_overrides[api.require_user] = lambda: user
    try:
        return TestClient(api.app).get(path, params=params).status_code
    finally:
        api.app.dependency_overrides.pop(api.require_user, None)


def test_showcase_is_chosen_by_category_for_feed_articles_and_other_is_admin_only(user, admin, contour, monkeypatch):
    article("Пилот робота Equinor", "Equinor испытала робота-бурильщика на платформе в Северном море.")
    article("Контракт Weatherford", "Weatherford получила контракт на бурение 274 скважин в Омане.")
    article("Интервью о кадрах", "Интервью о подготовке кадров для буровых бригад.")
    decide(monkeypatch, {"Пилот робота Equinor": answer(industry="oilfield_services", category="technology"),
                         "Контракт Weatherford": answer(industry="oilfield_services"),
                         "Интервью о кадрах": answer(industry="oilfield_services", category="other")})

    # Витрину определяет категория: технологическая статья сбора — на радаре, бизнес — в бизнес-ленте.
    assert set(showcase(user, "technology")) == {"Пилот робота Equinor"}
    assert set(showcase(user, "business")) == {"Контракт Weatherford"}
    # «Другое» — ни в одной витрине, только админу, в разделах.
    assert _sections(admin)["other"]["count"] == 1
    assert [card["title"] for card in api_get("/api/signals", admin, section="other")] == ["Интервью о кадрах"]
    assert _status("/api/signals", user, section="other") == 403
    assert _status("/api/signals/sections", user) == 403


def test_rejected_and_failed_articles_lie_in_sections_by_who_removed_them_with_a_short_reason(
        user, admin, contour, monkeypatch):
    article("Котировки нефти", "Нефть дешевеет после роста запасов нефти в США.")
    article("Модель молчит", "Equinor испытает робота-бурильщика на шельфе в 2027 году.")
    article("Не по схеме", "SLB подписала контракт с Saudi Aramco на бурение 40 скважин.")
    decide(monkeypatch, {"Котировки нефти": reject("oil_gas_no_link"), "Модель молчит": AIClientError("timeout"),
                         "Не по схеме": {"decision": "accept"}})

    sections = _sections(admin)
    reasons = {value["code"]: value["label"] for value in load_dictionaries()["reject_reason"]["values"]}
    # «Отбор» — по разделу на каждую из шести причин, сбой — свои разделы.
    assert {key: (section["who"], section["reason"]) for key, section in sections.items()
            if key.startswith("otbor:")} == {f"otbor:{code}": ("«Отбор»", label) for code, label in reasons.items()}
    assert {key: section["count"] for key, section in sections.items() if section["count"]} == {
        "otbor:oil_gas_no_link": 1, "otbor_failed": 1, "otbor_invalid": 1}
    assert (sections["otbor_failed"]["who"], sections["otbor_failed"]["reason"]) == ("Сбой", "«Отбор» не ответил")
    # Отклонённая статья хранится сигналом с «Решением платформы» = «отклонено» и короткой причиной.
    [rejected] = api_get("/api/signals", admin, section="otbor:oil_gas_no_link")
    assert (rejected["title"], rejected["maturity"], rejected["section_reason"]) == (
        "Котировки нефти", "reject", "Котировки нефти без связи с нефтесервисом")
    assert [card["section_reason"] for card in api_get("/api/signals", admin, section="otbor_failed")] == [
        "модель: timeout"]
    assert [card["section_reason"] for card in api_get("/api/signals", admin, section="otbor_invalid")] == [
        "ответ не по правилам: нет строки industry"]
    assert showcase(user) == {} and showcase(user, "technology") == {}


def test_search_and_before_otbor_sections_hold_findings_hidden_by_the_old_rules(admin, contour):
    radar_finding("unconfirmed", "Выдуманная ссылка", "Текст сводки модели поиска.",
                  filter_stage="search", filter_reason=UNCONFIRMED_LINK)
    radar_finding("judged", "Брак судьи", "SLB представила новую систему.", filter_stage="judge",
                  filter_reason="не технология")

    sections = _sections(admin)
    assert (sections["search_unconfirmed"]["who"], sections["search_unconfirmed"]["reason"],
            sections["search_unconfirmed"]["count"]) == ("Поиск", "Ссылка не подтверждена", 1)
    assert [card["title"] for card in api_get("/api/signals", admin, section="search_unconfirmed")] == [
        "Выдуманная ссылка"]
    [judged] = api_get("/api/signals", admin, section="before_otbor")
    assert (judged["title"], judged["section_reason"]) == ("Брак судьи", "отсеяно (судья): не технология")


def test_one_rejected_list_holds_every_reason_with_the_reason_on_the_card_in_both_feeds(user, admin, contour, monkeypatch):
    # Единая плашка «Отклонённые» (решение владельца 23.10): все причины одним списком, причина в самой карточке,
    # число одно в обеих лентах. Видное в витрине в список не идёт.
    article("Котировки нефти", "Нефть дешевеет после роста запасов нефти в США.")
    article("Модель молчит", "Equinor испытает робота-бурильщика на шельфе в 2027 году.")
    article("Конференция ассоциации", "Ассоциация проведёт конференцию по менеджменту.")
    article("Не по схеме", "SLB подписала контракт с Saudi Aramco на бурение 40 скважин.")
    decide(monkeypatch, {"Котировки нефти": reject("oil_gas_no_link"), "Модель молчит": AIClientError("timeout"),
                         "Конференция ассоциации": answer(category="other"), "Не по схеме": {"decision": "accept"}})
    radar_finding("judged", "Брак судьи", "SLB представила новую систему.", filter_stage="judge",
                  filter_reason="не технология")
    radar_finding("biz", "Halliburton купила разработчика ПО", "Halliburton купила стартап.",
                  signal_category="business")
    assert "Halliburton купила разработчика ПО" in showcase(user)

    reasons = {card["title"]: card["section_reason"] for card in api_get("/api/signals", admin, section="all")}
    assert reasons == {
        "Котировки нефти": "«Отбор» → Нефтегаз без связи с нефтесервисом: Котировки нефти без связи с нефтесервисом",
        "Модель молчит": "Сбой → «Отбор» не ответил: модель: timeout",
        # Причина раздела в короткой причине не повторяется.
        "Не по схеме": "Сбой → Ответ не по правилам: нет строки industry",
        "Конференция ассоциации": f"«Отбор» → Другое: видно только админу: {JUSTIFICATION}",
        "Брак судьи": "До «Отбора» → Скрыто прежними правилами радара: отсеяно (судья): не технология",
    }
    for name in ("technology", "business"):
        assert api_get("/api/signals/summary", admin, showcase=name)["hidden"] == 5
    # Разделы идут группами «кто убрал»: «Другое» — среди разделов «Отбора».
    order = [section["who"] for section in api_get("/api/signals/sections", admin)["sections"]]
    assert [who for who, _ in itertools.groupby(order)] == ["«Отбор»", "Сбой", "Поиск", "До «Отбора»"]


def test_rules_by_relevance_mixed_links_and_age_do_not_hide_what_otbor_accepted(user, contour, monkeypatch):
    article("Пилот робота Equinor", "Equinor испытала робота-бурильщика на платформе в Северном море.")
    decide(monkeypatch, {"Пилот робота Equinor": answer(industry="oilfield_services", category="technology")})
    # Поля судьи, которые прячут находку радара (смешанные ссылки, давняя дата события), сигналу с решением
    # «Отбора» не мешают: по смыслу его прячет только «Отбор».
    with repository.get_connection() as conn:
        conn.execute("UPDATE signals SET mixed_events = TRUE, event_date = '2024-01-10',"
                     " card_set_by = card_set_by || '{\"event_date\": \"radar_judge\"}'::jsonb")
        conn.commit()

    card = showcase(user, "technology")["Пилот робота Equinor"]
    assert card["hidden_reason"] is None


def test_reprint_never_hides_the_accepted_copy_behind_one_otbor_rejected_or_has_not_decided(user, contour, monkeypatch):
    # Технические правила (перепечатки) — как раньше (спека), но «видна ли главная» решает и «Отбор».
    rejected = article("Weatherford получила контракт на бурение скважин в Омане", "Котировки нефти и прогноз ОПЕК.",
                       source="Нефтегаз")
    accepted = article("Weatherford получила контракт на бурение скважин в Омане до 2027",
                       "Weatherford получила контракт на бурение 274 скважин в Омане.", source="Oil & Gas")
    waiting = article("Weatherford получила контракт на бурение скважин в Омане, итоги",
                      "Weatherford пробурит 274 скважины в Омане.", source="Rigzone")
    decide(monkeypatch, {"Weatherford получила контракт на бурение скважин в Омане": reject("oil_gas_no_link"),
                         "Weatherford получила контракт на бурение скважин в Омане до 2027": answer()}, "--ids",
           f"{rejected},{accepted}")

    # Пар с отклонённой и с ждущей решения нет: модель перепечаток их не увидит.
    assert {(pair["a_id"], pair["b_id"]) for pair in repository.reprint_candidates()} == set()
    # Судья назвал главной отклонённую — пометка не ложится: принятая копия остаётся в витрине.
    for primary in (rejected, waiting):
        with pytest.raises(ValueError, match="не видна в ленте"):
            repository.mark_article_reprint(article_id=accepted, primary_id=primary, similarity=0.8,
                                            reason="одно событие")
    assert set(showcase(user)) == {"Weatherford получила контракт на бурение скважин в Омане до 2027"}


def _request(method, path, user, body):
    api.app.dependency_overrides[api.require_user] = lambda: user
    try:
        return TestClient(api.app).request(method, path, json=body)
    finally:
        api.app.dependency_overrides.pop(api.require_user, None)


def _send(method, path, user, body):
    response = _request(method, path, user, body)
    assert response.status_code == 200, response.text
    return response.json()


def test_digest_button_is_one_mechanism_in_both_showcases(user, contour, monkeypatch):
    article("Пилот робота Equinor", "Equinor испытала робота-бурильщика на платформе в Северном море.")
    article("Контракт Weatherford", "Weatherford получила контракт на бурение 274 скважин в Омане.")
    decide(monkeypatch, {"Пилот робота Equinor": answer(industry="oilfield_services", category="technology"),
                         "Контракт Weatherford": answer(industry="oilfield_services")})
    radar_card = showcase(user, "technology")["Пилот робота Equinor"]
    business_card = showcase(user)["Контракт Weatherford"]

    for card in (radar_card, business_card):
        _send("PATCH", f"/api/signals/{card['id']}", user, {"selected_for_digest": True})

    # Кнопка витрины ставит ту же отметку, что у статьи в ленте статей: выпуск берёт статью один раз.
    assert showcase(user, "technology")["Пилот робота Equinor"]["selected_for_digest"] is True
    assert showcase(user)["Контракт Weatherford"]["selected_for_digest"] is True
    assert {row["title"] for row in api_get("/api/articles", user, status="digest")} == {
        "Пилот робота Equinor", "Контракт Weatherford"}
    news = api_get("/api/digest-content", user)["news"]
    assert sorted((item["item_type"], item["title"]) for item in news) == [
        ("article", "Контракт Weatherford"), ("article", "Пилот робота Equinor")]

    _send("PATCH", f"/api/signals/{business_card['id']}", user, {"selected_for_digest": False})
    assert showcase(user)["Контракт Weatherford"]["selected_for_digest"] is False
    assert [item["title"] for item in api_get("/api/digest-content", user)["news"]] == ["Пилот робота Equinor"]


def test_digest_mark_of_a_rejected_or_failed_article_is_refused_by_the_server(user, admin, contour, monkeypatch):
    # Кнопки «В дайджест» у отклонённого и у сбоя на экране нет; запрос в обход экрана сервер тоже не принимает.
    article("Котировки нефти", "Нефть дешевеет после роста запасов нефти в США.")
    article("Модель молчит", "Equinor испытает робота-бурильщика на шельфе в 2027 году.")
    article("Контракт Weatherford", "Weatherford получила контракт на бурение 274 скважин в Омане.")
    decide(monkeypatch, {"Котировки нефти": reject("oil_gas_no_link"), "Модель молчит": AIClientError("timeout"),
                         "Контракт Weatherford": answer(industry="oilfield_services")})
    [rejected] = api_get("/api/signals", admin, section="otbor:oil_gas_no_link")
    [failed] = api_get("/api/signals", admin, section="otbor_failed")
    business_card = showcase(user)["Контракт Weatherford"]

    for card in (rejected, failed):
        response = _request("PATCH", f"/api/signals/{card['id']}", admin, {"selected_for_digest": True})
        assert response.status_code == 409, response.text
        assert "«Отбор»" in response.json()["detail"]
    assert api_get("/api/articles", admin, status="digest") == []
    # У сигнала ленты меняется только отметка «в дайджест» (одна механика на две витрины, решение 23.10): прочие
    # статусы статьи — в ленте статей.
    response = _request("PATCH", f"/api/signals/{business_card['id']}", user, {"status": "noise"})
    assert response.status_code == 422, response.text
    assert showcase(user)["Контракт Weatherford"]["user_status"] != "noise"


def test_feedback_of_a_card_is_a_stored_block_unless_the_radar_judge_made_it_and_it_is_on_the_radar(
        user, admin, contour, monkeypatch):
    # Форма и API ветвятся одинаково: в память судьи радара идёт отзыв только о его находке на радаре. Блок
    # сигнала ленты и находки радара в бизнес-ленте только хранится (решение владельца 22.10: учить агентов будет
    # сервис ОС): «Не тот блок» в бизнес-ленте значит «это технология», а судья прочёл бы «это бизнес».
    article("Пилот робота Equinor", "Equinor испытала робота-бурильщика на платформе в Северном море.")
    decide(monkeypatch, {"Пилот робота Equinor": answer(industry="oilfield_services", category="technology")})
    feed_card = showcase(user, "technology")["Пилот робота Equinor"]
    business_find = radar_finding("biz-1", "Halliburton купила разработчика ПО", "Halliburton купила стартап.",
                                  signal_category="business")

    saved = _send("POST", "/api/signals/feedback", user, {"signal_id": business_find, "verdict": "не тот блок",
                                                          "comment": "Это технология"})
    # Оценка — тем же словарём, что у судьи радара: «не тот блок» → wrong_block, и блок ответа — сохранённый.
    assert (saved["memories"], saved["block"]["verdict"]) == (0, "wrong_block")
    assert api_get("/api/signals/memory", admin) == []
    # Правки карточки и «дубль» — только у находки радара на радаре: у блока их нет, и сервер это говорит.
    response = _request("POST", "/api/signals/feedback", user, {"signal_id": feed_card["id"],
                                                               "corrected_title": "Другой заголовок"})
    assert response.status_code == 422, response.text
    assert showcase(user, "technology")["Пилот робота Equinor"]["title"] == "Пилот робота Equinor"


def test_feedback_blocks_are_listed_on_the_card_in_both_showcases_and_change_nothing(user, admin, contour, monkeypatch):
    article("Пилот робота Equinor", "Equinor испытала робота-бурильщика на платформе в Северном море.")
    article("Котировки нефти", "Нефть дешевеет после роста запасов нефти в США.")
    decide(monkeypatch, {"Пилот робота Equinor": answer(industry="oilfield_services", category="technology"),
                         "Котировки нефти": reject("oil_gas_no_link")})
    card = showcase(user, "technology")["Пилот робота Equinor"]
    [rejected] = api_get("/api/signals", admin, section="otbor:oil_gas_no_link")

    saved = _send("POST", "/api/signals/feedback", user, {"signal_id": card["id"], "verdict": "reject",
                                                          "reason": "шум", "comment": "Рано, только испытание"})
    # Ответ несёт сохранённый блок: экран добавляет его в список, не перечитывая витрину.
    assert {key: saved["block"][key] for key in ("author", "verdict", "reason", "comment")} == {
        "author": user["email"], "verdict": "reject", "reason": "шум", "comment": "Рано, только испытание"}
    _send("POST", "/api/signals/feedback", admin, {"signal_id": card["id"], "verdict": "strong_signal",
                                                   "comment": "Берём в выпуск"})
    _send("POST", "/api/signals/feedback", admin, {"signal_id": rejected["id"], "verdict": "approved"})

    # Каждый добавляет свой блок, чужие не перезаписаны; блоки видны списком на карточке.
    blocks = showcase(user, "technology")["Пилот робота Equinor"]["feedback_blocks"]
    assert [(block["author"], block["verdict"], block["reason"], block["comment"]) for block in blocks] == [
        (user["email"], "reject", "шум", "Рано, только испытание"),
        (admin["email"], "strong_signal", None, "Берём в выпуск")]
    # На отбор и показ блоки не влияют: отказ человека не прячет, одобрение не возвращает отклонённое.
    assert "Пилот робота Equinor" in showcase(user, "technology")
    assert "Котировки нефти" not in showcase(user) and "Котировки нефти" not in showcase(user, "technology")
    # Блок сигнала ленты только хранится: память судьи радара из него не растёт.
    assert api_get("/api/signals/memory", admin) == []


def test_only_what_otbor_accepted_goes_on_to_the_signal_card(user, contour, monkeypatch):
    monkeypatch.setattr(api.config, "SIGNAL_CARD_ENABLED", True)
    monkeypatch.setattr(api.config, "SIGNAL_CARD_MODEL", "gpt-5.4-nano")
    article("Программа бурения Equinor", "Equinor начнёт бурение 12 скважин на шельфе Норвегии в 2027 году.")
    article("Котировки нефти", "Нефть дешевеет после роста запасов нефти в США.")
    article("Модель молчит", "Equinor испытает робота-бурильщика на шельфе в 2027 году.")
    decide(monkeypatch, {"Программа бурения Equinor": answer(), "Котировки нефти": reject("oil_gas_no_link"),
                         "Модель молчит": AIClientError("timeout")})
    card_model = FakeModel({"Программа бурения Equinor": card_answer()})
    monkeypatch.setattr(signal_card, "make_client", lambda: card_model)

    cli.main(["enqueue-signal-cards"])
    run_worker("external-ai")

    # Дальше «Отбора» идёт только принятое: за карточку отклонённого и не решённого не платим.
    assert [call["input"].splitlines()[0] for call in card_model.calls] == ["Заголовок: Программа бурения Equinor"]
