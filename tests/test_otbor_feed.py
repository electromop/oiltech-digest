"""Агент «Отбор» для ленты (#130, спека #128, ADR 0005), шов 1 — контур целиком: статья сбора → шаг
планировщика → воркер NL на подставной модели → ядро → API витрин и разделов отклонённого
(tests/otbor_support.py).
"""

from oiltech_digest import api, cli
from oiltech_digest.db import repository
from oiltech_digest.otbor_pack_format import load_dictionaries
from oiltech_digest.processing import external_ai
from oiltech_digest.processing.openai_client import AIClientError
from oiltech_digest.processing.seed import seed_default_scoring_criteria
from tests.otbor_support import JUSTIFICATION, answer, article, decide, reject, showcase
from tests.otbor_support import contour  # noqa: F401 — фикстура
from tests.signal_card_support import DRILLING, admin, api_get, run_worker, tag, user  # noqa: F401 — фикстуры

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


def test_flag_switched_off_returns_the_waiting_article_to_the_gate(user, contour, monkeypatch):
    seed_default_scoring_criteria()
    tag(DRILLING)
    article("Ждёт решения", "Equinor испытает робота-бурильщика на шельфе в 2027 году.")
    decide(monkeypatch, {"Ждёт решения": AIClientError("timeout")})
    assert _feed(user) == set()

    monkeypatch.setattr(api.config, "OTBOR_ENABLED", False)
    _process_feed()

    assert {row["title"]: row["relevant"] for row in api_get("/api/articles", user)} == {"Ждёт решения": True}


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
