"""Агент «Отбор» для ленты (#130, спека #128), шов 1: прежняя механика ленты при «Отборе». Прежняя релевантность
(гейт, стоп-слова, перепроверка) снята 08.11 вместе с флагом «Отбора»; что осталось от механики ленты (лента статей,
обработка, перепечатки, счётчики, пересчёт после полного текста), читает его решение из сигнала (решение владельца
23.10). Статья, решённая гейтом до «Отбора», живёт по его решению (история).
"""

import pytest

from oiltech_digest import api, background_jobs, cli, otbor
from oiltech_digest.db import analytics, repository
from oiltech_digest.processing import external_ai, fulltext_recompute
from oiltech_digest.processing.openai_client import AIClientError
from oiltech_digest.processing.seed import seed_default_scoring_criteria
from tests.otbor_support import JUSTIFICATION, OtborModel, answer, api_send, article, decide, reject, showcase
from tests.otbor_support import contour, window_of_published  # noqa: F401 (фикстуры)
from tests.signal_card_support import DRILLING, admin, api_get, run_worker, tag, user  # noqa: F401: фикстуры

# Лента статей и отметка «в дайджест» смотрят окно открытых месяцев: оно на день статей (tests/otbor_support.py).
pytestmark = pytest.mark.usefixtures("window_of_published")


def _feed(user):
    """Лента статей (/api/articles): заголовки видимых статей."""
    return {row["title"] for row in api_get("/api/articles", user)}


def _gate(article_id, relevant=None):
    """Статья, собранная до «Отбора» (история: путь статьи пуст), и вердикт гейта ленты в её карточке, как его
    записала прежняя обработка (гейт снят 08.11); relevant=None: гейт её не решал. Мост «лента → сигнал»
    синхронизирован, как после записи вердикта."""
    with repository.get_connection() as conn:
        conn.execute("UPDATE articles SET selector = NULL WHERE id = %s", (article_id,))
        if relevant is not None:
            conn.execute("INSERT INTO article_cards (article_id, relevant, relevance_reason, relevance_model, status) "
                         "VALUES (%s, %s, 'r', 'm', %s)", (article_id, relevant, "new" if relevant else "rejected"))
        conn.commit()
    repository._sync_feed_signal(article_id)


def test_new_article_is_hidden_until_otbor_decides_then_shown_by_its_decision(user, contour, monkeypatch):
    article("Программа бурения Equinor", "Equinor начнёт бурение 12 скважин на шельфе Норвегии в 2027 году.")
    article("Котировки нефти", "Нефть дешевеет после роста запасов нефти в США.")
    # До решения «Отбора» новой публикации нет ни в ленте статей, ни в витрине.
    assert (_feed(user), showcase(user)) == (set(), {})

    decide(monkeypatch, {"Программа бурения Equinor": answer(), "Котировки нефти": reject("oil_gas_no_link")})

    assert _feed(user) == {"Программа бурения Equinor"}
    assert set(showcase(user)) == {"Программа бурения Equinor"}


def test_article_decided_before_otbor_lives_by_the_gate_rules(user, contour, monkeypatch):
    _gate(article("Контракт Weatherford", "Weatherford получила контракт на бурение 274 скважин в Омане."), True)
    _gate(article("Прибыль BP", "BP более чем вдвое увеличила чистую прибыль во втором квартале."), False)
    _gate(article("Ещё не проверена", "Газпром нефть пробурила новую скважину на Ямале."))

    model = decide(monkeypatch, {})

    # «Отбор» решённое гейтом не трогает; ещё не проверенная гейтом статья видна, как до «Отбора».
    assert model.calls == []
    assert _feed(user) == {"Контракт Weatherford", "Ещё не проверена"}
    assert set(showcase(user)) == {"Контракт Weatherford"}


def _process_feed():
    """Обработка ленты (суть и перевод заголовка): шаг планировщика и воркер NL с офлайн-клиентом."""
    cli.main(["enqueue-process", "--limit", "10", "--offline"])
    [job] = run_worker("external-ai")
    return job


def test_article_accepted_by_otbor_is_processed_without_the_gate_and_stop_words(user, admin, contour, monkeypatch):
    seed_default_scoring_criteria()
    # Стоп-слово темы действует только в предфильтре сбора: принятую «Отбором» статью оно не отклоняет.
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
    # Мост «лента → сигнал» дописал принятой суть; тега у статьи нет (снят 08.11): тема до ответа «Карточки»
    # «не классифицировано», балла нет.
    card = showcase(user)["Программа бурения Equinor"]
    assert card["summary"] == row["summary"] and card["theme"] == repository.SYSTEM_TAG_UNCLASSIFIED


def test_summary_of_an_article_gets_the_otbor_category_and_link_but_not_its_decision(user, contour, monkeypatch):
    # «Суть и перевод» (#131): на вход: категория, связь и применение «Отбора», без решения и обоснования.
    seed_default_scoring_criteria()
    tag(DRILLING)
    article("Роботы-бурильщики Equinor", "Equinor испытала робота-бурильщика на платформе в Северном море.")
    decide(monkeypatch, {"Роботы-бурильщики Equinor": answer(
        industry="other_industry", oilfield_relevance="transferable", oilfield_application="Роботизация буровой",
        category="technology")})
    offline = external_ai.make_client(True)
    inputs = []

    class Recording:
        def complete_json(self, instructions, user_input, schema, **kwargs):
            inputs.append((schema["name"], user_input))
            return offline.complete_json(instructions, user_input, schema, **kwargs)

    monkeypatch.setattr(external_ai, "make_client", lambda offline=False: Recording())
    _process_feed()

    [summary] = [text for name, text in inputs if name == "article_summary"]
    assert ("category: technology" in summary and "oilfield_relevance: transferable" in summary
            and "oilfield_application: Роботизация буровой" in summary)
    assert JUSTIFICATION not in summary and "accept" not in summary


def test_old_feed_machinery_counts_and_recomputes_by_the_otbor_decision(user, admin, contour, monkeypatch):
    # Решение владельца 23.10: что остаётся от прежней механики ленты (счётчики, пересчёт после полного текста),
    # читает решение «Отбора» из сигнала.
    seed_default_scoring_criteria()
    tag(DRILLING)
    accepted = article("Программа бурения Equinor", "Equinor начнёт бурение 12 скважин на шельфе Норвегии в 2027 году.")
    rejected = article("Котировки нефти", "Нефть дешевеет после роста запасов нефти в США.")
    decide(monkeypatch, {"Программа бурения Equinor": answer(), "Котировки нефти": reject("oil_gas_no_link")})
    _process_feed()

    # Плитки ленты: принятая обработана.
    assert api_get("/api/stats", user)["processed_articles"] == 1
    # Воронка платформы и месячная аналитика: принятая и отклонённая: по решению «Отбора».
    [platform] = api_get("/api/stats/monthly", admin)["platform"]
    assert (platform["relevant"], platform["rejected"], platform["summarized"]) == (1, 1, 1)
    # Аналитика, без стоимости: та за курсом ходит в сеть ЦБ.
    month = analytics.monthly_analytics(1)["months"][-1]
    assert (month["relevant"], month["rejected"], month["summarized"], month["sources_relevant"]) == (1, 1, 1, 1)
    # Пересчёт после полного текста: суть принятой по новому телу, отклонённой ничего.
    monkeypatch.setattr(api.config, "AI_BULK_LANE_ENABLED", True)
    assert fulltext_recompute.enqueue_after_body([accepted, rejected])["articles"] == [accepted]


def test_explicit_article_list_does_not_bypass_otbor(user, admin, contour, monkeypatch, capsys):
    # Явный список (ручной импорт с обработкой, перекачка тела): ждущую «Отбора» никто, кроме него, не решает, а за
    # суть отклонённой модель не платится; принятая «Отбором» обрабатывается.
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
    # Местный конвейер: так же.
    cli.main(["process-articles", str(waiting), str(rejected), "--offline"])
    assert "статей=0" in capsys.readouterr().out
    # Ждущую по-прежнему решит «Отбор»: шаг планировщика её берёт.
    cli.main(["enqueue-otbor"])
    [step] = [job for job in api_get("/api/jobs", admin, kind="otbor") if job["payload"]["trigger"] != "manual"]
    assert step["payload"]["article_ids"] == [waiting]


def test_otbor_failure_of_an_article_decided_by_the_gate_before_otbor_is_cleared(user, admin, contour, monkeypatch,
                                                                                   capsys):
    """Статья окна переноса #134 живёт на пути гейта (путь пуст, вердикт гейта в карточке). Если «Карточка» вернула её
    в «Отбор», а он дал сбой, сбой снимается следующей записью статьи: она живёт по решению гейта, с «В дайджест»
    и «Карточкой», как все (_CLEAR_GATE_DECIDED_FAILURES). «Отбор» её командой с номером не пересматривает."""
    gated = article("Контракт Weatherford", "Weatherford получила контракт на бурение 274 скважин в Омане.")
    _gate(gated, True)
    with repository.get_connection() as conn:
        conn.execute("UPDATE signals SET otbor_failure = 'technical', otbor_failure_reason = 'timeout' "
                     "WHERE signal_key = %s", (f"feed:{gated}",))
        conn.commit()

    repository.upsert_article_card(gated, "Weatherford пробурит 274 скважины в Омане.", "m")

    card = showcase(user)["Контракт Weatherford"]
    assert (card["otbor_failure"], card["otbor_decision"]) == (None, None)
    api_send("PATCH", f"/api/signals/{card['id']}", user, {"selected_for_digest": True})
    capsys.readouterr()
    cli.main(["enqueue-otbor", "--ids", str(gated)])
    assert "решает не «Отбор» — пропущены" in capsys.readouterr().out
    assert api_get("/api/jobs", admin, kind="otbor") == []


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


def test_reprint_never_hides_the_accepted_copy_behind_one_otbor_rejected_or_has_not_decided(user, contour, monkeypatch):
    # Технические правила (перепечатки): как раньше (спека), но «видна ли главная» решает и «Отбор».
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
    # Судья назвал главной отклонённую, пометка не ложится: принятая копия остаётся в витрине.
    for primary in (rejected, waiting):
        with pytest.raises(ValueError, match="не видна в ленте"):
            repository.mark_article_reprint(article_id=accepted, primary_id=primary, similarity=0.8,
                                            reason="одно событие")
    assert set(showcase(user)) == {"Weatherford получила контракт на бурение скважин в Омане до 2027"}
