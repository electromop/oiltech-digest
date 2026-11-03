"""Проверка «Отбора» на реальных данных (#134, спека #128), шов 2: команды тени, отчёта и переключения. Статьи сбора,
решённые гейтом до «Отбора», проходят «Отбор» на воркере NL с подставной моделью; решения пишутся рядом, витрины не
меняются; переключение после флага переносит решения в витрины (tests/otbor_support.py).
"""

from datetime import date, datetime, timedelta, timezone
import json

import pytest

from oiltech_digest import api, cli, otbor, otbor_shadow
from oiltech_digest.db import marks, otbor_shadow_store, repository
from oiltech_digest.processing import external_ai
from oiltech_digest.processing.openai_client import AIClientError
from tests.card_pipeline_support import (MARKET, SCORES_MODEL, card_answer, even, fill_cards,  # noqa: F401
                                         pipeline, process_feed, run_scores, scores)
from tests.otbor_support import MODEL, OtborModel, answer, article, reject, showcase
from tests.otbor_support import contour  # noqa: F401 — фикстура
from tests.signal_card_support import (FakeModel, admin, api_get, radar_finding, run_worker,  # noqa: F401
                                       user)

SINCE = (date.today() - timedelta(days=14)).isoformat()


@pytest.fixture()
def before_flag(monkeypatch):
    """Прод до переключения: ИИ на внешнем воркере, полоса пересчётов есть, «Отбор» выключен, модель задана."""
    monkeypatch.setattr(api.config, "EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr(api.config, "AI_EXECUTION_REGION", "external")
    monkeypatch.setattr(api.config, "AI_BULK_LANE_ENABLED", True)
    monkeypatch.setattr(api.config, "OTBOR_ENABLED", False)
    monkeypatch.setattr(api.config, "OTBOR_MODEL", MODEL)
    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))


def _gate(article_id, relevant):
    """Решение гейта ленты, как его пишет ядро из итога обработки (до «Отбора»)."""
    external_ai.apply_process_result({"articles": [{
        "article_id": article_id, "relevance": {"relevant": relevant, "reason": "r", "model": "m"},
    }]})


def _gated(title, text, relevant, **kwargs):
    article_id = article(title, text, **kwargs)
    _gate(article_id, relevant)
    return article_id


def _collected(article_id, days_ago):
    with repository.get_connection() as conn:
        conn.execute("UPDATE articles SET collected_at = now() - make_interval(days => %s) WHERE id = %s",
                     (days_ago, article_id))
        conn.commit()


def test_dry_run_counts_window_articles_and_price_and_queues_nothing(user, before_flag, capsys):
    _gated("Контракт Weatherford", "Weatherford получила контракт на бурение 274 скважин в Омане.", True)
    _gated("Котировки нефти", "Нефть дешевеет после роста запасов нефти в США.", False)
    _collected(_gated("Старый контракт", "Halliburton получила контракт в Кувейте.", True), 30)
    marks.set_article_mark(_gated("В выпуске", "SLB подписала контракт с Saudi Aramco.", True), status="digest")
    _gated("Закрытый месяц", "Baker Hughes поставит компрессоры в Катар.", True,
           published=datetime(2026, 1, 15, 9, tzinfo=timezone.utc))
    capsys.readouterr()

    cli.main(["otbor-shadow", "--since", SINCE])

    out = capsys.readouterr().out
    # Окно: две статьи, решённые гейтом; старше окна, выбранная «в дайджест» и закрытый месяц не идут.
    assert "статей 2" in out
    assert "$" in out and MODEL in out
    assert "сухой прогон" in out
    assert run_worker("external-ai-bulk") == []


def _feed(user):
    """Лента статей (/api/articles): заголовки видимых статей."""
    return {row["title"] for row in api_get("/api/articles", user)}


def test_apply_writes_decisions_beside_and_leaves_feed_and_showcases_as_they_were(user, before_flag, monkeypatch):
    accepted = _gated("Контракт Weatherford", "Weatherford получила контракт на бурение 274 скважин в Омане.", True)
    rejected = _gated("Робот-бурильщик", "Equinor испытала робота-бурильщика на платформе.", False)
    assert (_feed(user), set(showcase(user))) == ({"Контракт Weatherford"}, {"Контракт Weatherford"})
    model = OtborModel({"Контракт Weatherford": reject("corporate_promo"),
                        "Робот-бурильщик": answer(industry="oilfield_services", category="technology")})
    monkeypatch.setattr(otbor, "make_client", lambda: model)

    cli.main(["otbor-shadow", "--since", SINCE, "--apply"])
    run_worker("external-ai-bulk")

    # Решения «Отбора» и решение гейта на момент записи — рядом, в таблице проверки.
    rows = {row["article_id"]: row for row in otbor_shadow_store.rows(article_ids=[accepted, rejected])}
    assert (rows[accepted]["decision"], rows[accepted]["values_json"]["reject_reason"],
            rows[accepted]["gate_relevant"], rows[accepted]["trigger"]) == ("reject", "corporate_promo", True, "shadow")
    assert (rows[rejected]["decision"], rows[rejected]["values_json"]["signal_category"],
            rows[rejected]["gate_relevant"]) == ("accept", "technology", False)
    assert rows[accepted]["recipe"]["model"].startswith(MODEL)
    # Лента статей и витрины — как были: решает по-прежнему гейт.
    assert _feed(user) == {"Контракт Weatherford"}
    assert set(showcase(user)) == {"Контракт Weatherford"}
    assert showcase(user)["Контракт Weatherford"]["otbor_decision"] is None
    assert showcase(user, "technology") == {}
    # Расход проверки — своей стадией, не как решения «Отбора».
    stages = {row["stage"] for row in repository.ai_cost_report()}
    assert "otbor_shadow" in stages and "otbor" not in stages
    # Повторный запуск решённое не оплачивает.
    cli.main(["otbor-shadow", "--since", SINCE, "--apply"])
    assert run_worker("external-ai-bulk") == []
    assert len(model.calls) == 2


def test_repeat_run_retries_a_technical_failure_but_not_an_answer_against_the_rules(user, before_flag, monkeypatch):
    silent = _gated("Модель молчит", "Equinor испытает робота-бурильщика на шельфе в 2027 году.", True)
    invalid = _gated("Не по схеме", "SLB подписала контракт с Saudi Aramco на бурение 40 скважин.", True)
    monkeypatch.setattr(otbor, "make_client", lambda: OtborModel({
        "Модель молчит": AIClientError("timeout"), "Не по схеме": {"decision": "accept"}}))
    cli.main(["otbor-shadow", "--since", SINCE, "--apply"])
    run_worker("external-ai-bulk")
    rows = {row["article_id"]: row for row in otbor_shadow_store.rows(article_ids=[silent, invalid])}
    assert (rows[silent]["decision"], rows[silent]["failure"]) == (None, "technical")
    assert (rows[invalid]["decision"], rows[invalid]["failure"]) == (None, "invalid_answer")

    model = OtborModel({"Модель молчит": answer()})
    monkeypatch.setattr(otbor, "make_client", lambda: model)
    cli.main(["otbor-shadow", "--since", SINCE, "--apply"])
    run_worker("external-ai-bulk")

    # Технический сбой повтор добирает; ответ не по правилам не повторяется (решение владельца 23.10).
    assert [call["input"].splitlines()[0] for call in model.calls] == ["Заголовок: Модель молчит"]
    row = otbor_shadow_store.rows(article_ids=[silent])[0]
    assert (row["decision"], row["failure"]) == ("accept", None)
    # Ход решения хранит и сбой прошлой попытки.
    assert row["decision_log"][0]["text"].startswith("Сбой, «Отбор» не ответил")


def test_report_shows_reject_sections_categories_technology_for_the_radar_and_gate_divergence(
        user, before_flag, monkeypatch, capsys):
    _gated("Контракт Weatherford", "Weatherford получила контракт на бурение 274 скважин в Омане.", True)
    _gated("Котировки нефти", "Нефть дешевеет после роста запасов нефти в США.", True)
    _gated("Робот-бурильщик", "Equinor испытала робота-бурильщика на платформе.", False)
    _gated("Реклама выставки", "Приглашаем на выставку оборудования в Москве.", False)
    _gated("Модель молчит", "Equinor испытает робота-бурильщика на шельфе в 2027 году.", True)
    monkeypatch.setattr(otbor, "make_client", lambda: OtborModel({
        "Контракт Weatherford": answer(industry="oilfield_services"),
        "Котировки нефти": reject("oil_gas_no_link"),
        "Робот-бурильщик": answer(industry="oilfield_services", category="technology"),
        "Реклама выставки": reject("corporate_promo", industry="other_industry"),
        "Модель молчит": AIClientError("timeout")}))
    cli.main(["otbor-shadow", "--since", SINCE, "--apply"])
    run_worker("external-ai-bulk")
    capsys.readouterr()

    cli.main(["otbor-shadow-report", "--since", SINCE, "--json"])

    report = json.loads(capsys.readouterr().out)
    assert (report["publications"], report["decided"], report["failed"]) == (5, 4, {"technical": 1})
    # Гейт против «Отбора»: по строке на решение гейта, по столбцу на решение «Отбора».
    assert report["gate"] == {"accept": {"accept": 1, "reject": 1}, "reject": {"accept": 1, "reject": 1}}
    assert report["categories"] == {"business": 1, "technology": 1}
    assert {row["label"]: row["count"] for row in report["reasons"]} == {
        "Нефтегаз без связи с нефтесервисом": 1, "Корпоративное и реклама": 1}
    # Технологии из сбора пойдут на радар; находок радара за окно нет.
    assert (report["technology_from_feed"], report["radar_now"]) == (1, 0)
    assert [(row["title"], row["reason"]) for row in report["examples"]["gate_accepted_otbor_rejected"]] == [
        ("Котировки нефти", "Нефтегаз без связи с нефтесервисом")]
    assert [(row["title"], row["category"]) for row in report["examples"]["gate_rejected_otbor_accepted"]] == [
        ("Робот-бурильщик", "технология")]

    cli.main(["otbor-shadow-report", "--since", SINCE])
    text = capsys.readouterr().out
    assert "Нефтегаз без связи с нефтесервисом: 1" in text and "Робот-бурильщик" in text


def _shadow(monkeypatch, answers):
    """Проверка на реальных данных по окну: задачи и воркер NL с подставной моделью «Отбора»."""
    monkeypatch.setattr(otbor, "make_client", lambda: OtborModel(answers))
    cli.main(["otbor-shadow", "--since", SINCE, "--apply"])
    run_worker("external-ai-bulk")


def test_switch_refuses_while_the_flag_is_off_and_while_window_articles_have_no_check(
        user, before_flag, monkeypatch, capsys):
    _gated("Контракт Weatherford", "Weatherford получила контракт на бурение 274 скважин в Омане.", True)
    _shadow(monkeypatch, {"Контракт Weatherford": answer(industry="oilfield_services")})

    # Флаг выключен: новое решал бы гейт, а перенесённое уже «Отбор».
    with pytest.raises(SystemExit, match="OTBOR_ENABLED"):
        cli.main(["otbor-switch", "--since", SINCE, "--apply"])

    # Пришла после проверки, но до флага: её решил гейт.
    late = _gated("Пришла до флага", "Halliburton получила контракт в Кувейте.", True)
    monkeypatch.setattr(api.config, "OTBOR_ENABLED", True)
    monkeypatch.setattr(api.config, "SIGNAL_CARD_ENABLED", True)
    monkeypatch.setattr(api.config, "SIGNAL_CARD_MODEL", MODEL)
    monkeypatch.setattr(api.config, "SIGNAL_SCORES_MODEL", SCORES_MODEL)
    capsys.readouterr()
    cli.main(["otbor-switch", "--since", SINCE])
    assert "без решения проверки 1" in capsys.readouterr().out
    # Статья окна, которую решил гейт, без решения проверки: сначала дозапуск проверки.
    with pytest.raises(SystemExit, match="otbor-shadow"):
        cli.main(["otbor-switch", "--since", SINCE, "--apply"])
    assert showcase(user)["Контракт Weatherford"]["otbor_decision"] is None
    assert late in [row["id"] for row in api_get("/api/articles", user)]


def test_switch_moves_decisions_into_showcases_and_sends_the_accepted_down_the_rest_of_the_pipeline(
        user, admin, pipeline, monkeypatch, capsys):
    monkeypatch.setattr(api.config, "OTBOR_ENABLED", False)
    monkeypatch.setattr(api.config, "AI_BULK_LANE_ENABLED", True)
    _gated("Контракт Weatherford", "Weatherford получила контракт на бурение 274 скважин в Омане.", True)
    _gated("Котировки нефти", "Нефть дешевеет после роста запасов нефти в США.", True)
    _gated("Робот-бурильщик", "Equinor испытала робота-бурильщика на платформе.", False)
    process_feed()
    # Карточка прогона по истории: до «Отбора», категория ленты «бизнес», темы нет.
    fill_cards(monkeypatch, {"Контракт Weatherford": card_answer(), "Котировки нефти": card_answer()})
    _shadow(monkeypatch, {"Контракт Weatherford": answer(industry="oilfield_services"),
                          "Котировки нефти": reject("oil_gas_no_link"),
                          "Робот-бурильщик": answer(industry="oilfield_services", category="technology")})
    assert set(showcase(user)) == {"Контракт Weatherford", "Котировки нефти"}

    monkeypatch.setattr(api.config, "OTBOR_ENABLED", True)
    capsys.readouterr()
    cli.main(["otbor-switch", "--since", SINCE])
    out = capsys.readouterr().out
    assert "перенести 3: в бизнес-ленту 1, на радар 1, «Другое» 0, отклонено 1" in out
    assert "карточка заново 1" in out and "сухой прогон" in out
    assert set(showcase(user)) == {"Контракт Weatherford", "Котировки нефти"}

    cli.main(["otbor-switch", "--since", SINCE, "--apply"])

    # Витрины — по решениям «Отбора»: отклонённое ушло в раздел, технология из сбора на радаре.
    assert set(showcase(user)) == {"Контракт Weatherford"}
    assert set(showcase(user, "technology")) == {"Робот-бурильщик"}
    [rejected] = api_get("/api/signals", admin, section="otbor:oil_gas_no_link")
    assert rejected["title"] == "Котировки нефти"
    card = showcase(user)["Контракт Weatherford"]
    assert (card["otbor_decision"], card["industry"]) == ("accept", "oilfield_services")
    assert card["decision_log"][-1]["text"].startswith("Перенесено из проверки на реальных данных")
    # Повторный перенос ничего не делает.
    capsys.readouterr()
    cli.main(["otbor-switch", "--since", SINCE, "--apply"])
    assert "перенести 0" in capsys.readouterr().out

    # Баллы ждут новую карточку: старая сделана до решения «Отбора», с категорией ленты и без темы.
    assert run_scores(monkeypatch, {}).calls == []
    assert fill_cards(monkeypatch, {}).calls == []  # карточка заново стоит в полосе пересчётов, не шагом дня
    monkeypatch.setattr("oiltech_digest.signal_card.make_client",
                        lambda: FakeModel({"Контракт Weatherford": card_answer(theme=MARKET)}))
    run_worker("external-ai-bulk")
    card = showcase(user)["Контракт Weatherford"]
    assert (card["theme"], card["card_set_by"]["theme"]) == (MARKET, "agent")
    scored = run_scores(monkeypatch, {"Контракт Weatherford": scores(even("business", 70))})
    assert [call["input"].splitlines()[0] for call in scored.calls] == ["Заголовок: Контракт Weatherford"]


def test_named_findings_of_a_trial_run_are_left_to_their_check_until_their_mark_is_switched(
        user, admin, before_flag, monkeypatch, capsys):
    # Находка пробного прогона поиска (#138): путь «Отбора», решения нет, появилась давно.
    finding = radar_finding("trial-1", "Пилот робота Equinor", "Equinor испытала робота-бурильщика на платформе.")
    with repository.get_connection() as conn:
        conn.execute("UPDATE signals SET selector = 'otbor', created_at = now() - interval '10 days' WHERE id = %s",
                     (finding,))
        conn.commit()
    model = OtborModel({"Пилот робота Equinor": answer(industry="oilfield_services", category="technology")})
    monkeypatch.setattr(otbor, "make_client", lambda: model)

    otbor_shadow.enqueue(signal_ids=[finding], model=MODEL, trigger="search_trial")
    run_worker("external-ai-bulk")

    [row] = otbor_shadow_store.rows(signal_ids=[finding])
    assert (row["decision"], row["trigger"]) == ("accept", "search_trial")
    # С флагом обычный шаг «Отбора» находку с решением проверки не решает и в «„Отбор“ не ответил» не кладёт.
    monkeypatch.setattr(api.config, "OTBOR_ENABLED", True)
    monkeypatch.setattr(api.config, "SIGNAL_CARD_ENABLED", True)
    monkeypatch.setattr(api.config, "SIGNAL_CARD_MODEL", MODEL)
    monkeypatch.setattr(api.config, "SIGNAL_SCORES_MODEL", SCORES_MODEL)
    cli.main(["enqueue-otbor"])
    run_worker("external-ai")
    assert len(model.calls) == 1
    assert api_get("/api/signals", admin, section="all") == []
    # Перенос по умолчанию берёт только проверку #134; пометку пробного прогона — только названную.
    cli.main(["otbor-switch", "--since", SINCE, "--apply"])
    assert showcase(user, "technology") == {}
    cli.main(["otbor-switch", "--since", SINCE, "--triggers", "search_trial", "--apply"])
    assert showcase(user, "technology")["Пилот робота Equinor"]["otbor_decision"] == "accept"
