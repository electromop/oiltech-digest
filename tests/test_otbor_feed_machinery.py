"""Агент «Отбор» для ленты (#130, спека #128), шов 1 — прежняя механика ленты при «Отборе»: агент включается,
прежняя релевантность (гейт, стоп-слова, перепроверка) для его статей выключается; что остаётся от механики ленты
(лента статей, обработка, перепечатки, счётчики, пересчёт после полного текста), читает его решение из сигнала
(решение владельца 23.10). Флаг выключили — снова работает гейт (tests/otbor_support.py).
"""

import pytest

from oiltech_digest import api, background_jobs, cli, otbor
from oiltech_digest.db import analytics, repository
from oiltech_digest.processing import external_ai, fulltext_recompute
from oiltech_digest.processing.openai_client import AIClientError
from oiltech_digest.processing.seed import seed_default_scoring_criteria
from tests.otbor_support import OtborModel, answer, api_send, article, decide, reject, showcase
from tests.otbor_support import contour  # noqa: F401 — фикстура
from tests.signal_card_support import DRILLING, admin, api_get, run_worker, tag, user  # noqa: F401 — фикстуры


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
    # Статью решил гейт: прежний сбой «Отбора» снят, у карточки, как у всех, «В дайджест».
    card = showcase(user)["Ждёт решения"]
    assert (card["otbor_failure"], card["otbor_decision"]) == (None, None)
    api_send("PATCH", f"/api/signals/{card['id']}", user, {"selected_for_digest": True})
    # Решённое гейтом «Отбор» не пересматривает и командой с номером (проверка на реальных данных, #134, пишет
    # свои решения отдельно).
    monkeypatch.setattr(api.config, "OTBOR_ENABLED", True)
    capsys.readouterr()
    cli.main(["enqueue-otbor", "--ids", str(waiting)])
    assert "решает не «Отбор» — пропущены" in capsys.readouterr().out
    assert len(api_get("/api/jobs", admin, kind="otbor")) == 1


def test_manual_gate_command_leaves_otbor_articles_alone(user, contour, monkeypatch):
    # Агент включается, прежняя релевантность для его статей выключается (владелец, 23.10): и ручная команда гейта.
    seed_default_scoring_criteria()
    tag(DRILLING)
    article("Программа бурения Equinor", "Equinor начнёт бурение 12 скважин на шельфе Норвегии в 2027 году.")
    article("Ждёт решения", "Equinor испытает робота-бурильщика на шельфе в 2027 году.")
    decide(monkeypatch, {"Программа бурения Equinor": answer(), "Ждёт решения": AIClientError("timeout")})
    _process_feed()

    cli.main(["relevance", "--offline"])

    [row] = api_get("/api/articles", user)
    assert (row["title"], row["relevant"]) == ("Программа бурения Equinor", None)
    monkeypatch.setattr(api.config, "OTBOR_ENABLED", False)
    assert _feed(user) == {"Программа бурения Equinor"}  # ждущую «Отбора» гейт не решал


def test_rerun_of_otbor_does_not_hide_an_article_chosen_for_the_digest(user, contour, monkeypatch, capsys):
    # «Выбранное «в дайджест» не прячет ничто», «повторный прогон «Отбора» решения людей не перезаписывает» (спека).
    chosen = article("Пилот робота Equinor", "Equinor испытала робота-бурильщика на платформе в Северном море.")
    raced = article("Контракт Weatherford", "Weatherford получила контракт на бурение 274 скважин в Омане.")
    decide(monkeypatch, {"Пилот робота Equinor": answer(industry="oilfield_services", category="technology"),
                         "Контракт Weatherford": answer(industry="oilfield_services")})
    radar_card = showcase(user, "technology")["Пилот робота Equinor"]
    api_send("PATCH", f"/api/signals/{radar_card['id']}", user, {"selected_for_digest": True})

    # Команда с номером выбранное в дайджест не пересматривает.
    capsys.readouterr()
    cli.main(["enqueue-otbor", "--ids", str(chosen)])
    assert "выбраны в дайджест" in capsys.readouterr().out
    # Задачу поставили до отметки: её «не брать» решение выбранной статьи не меняет.
    cli.main(["enqueue-otbor", "--ids", str(raced)])
    business_card = showcase(user)["Контракт Weatherford"]
    api_send("PATCH", f"/api/signals/{business_card['id']}", user, {"selected_for_digest": True})
    monkeypatch.setattr(otbor, "make_client", lambda: OtborModel({"Контракт Weatherford": reject("corporate_promo")}))
    run_worker("external-ai")

    assert showcase(user)["Контракт Weatherford"]["otbor_decision"] == "accept"
    assert {item["title"] for item in api_get("/api/digest-content", user)["news"]} == {
        "Пилот робота Equinor", "Контракт Weatherford"}


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
