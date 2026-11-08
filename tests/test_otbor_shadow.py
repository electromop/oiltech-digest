"""Проверка «Отбора» на реальных данных (#134, спека #128), шов 2: команды проверки, отчёта и переключения. Статьи
сбора, решённые гейтом до «Отбора», проходят «Отбор» на воркере NL с подставной моделью; решения пишутся рядом,
витрины не меняются; переключение после флага переносит решения в витрины (tests/otbor_support.py).
"""

from datetime import date, datetime, timedelta, timezone
import json

import pytest

from oiltech_digest import api, cli, feed_window, otbor
from oiltech_digest.db import marks, repository
from oiltech_digest.otbor_pack_format import load_dictionaries
from oiltech_digest.processing import external_ai
from oiltech_digest.processing.openai_client import AIClientError
from tests.card_pipeline_support import (MARKET, SCORES_MODEL, card_answer, even, fill_cards,  # noqa: F401
                                         pipeline, process_feed, run_scores, scores)
from tests.otbor_support import MODEL, OtborModel, answer, api_send, article, reject, showcase
from tests.otbor_support import contour  # noqa: F401 — фикстура
from tests.signal_card_support import FakeModel, admin, api_get, run_worker, user  # noqa: F401 — фикстуры

SINCE = (date.today() - timedelta(days=14)).isoformat()
# Окно ленты на день проверки: открыты октябрь и ноябрь (в тестах смена месяца 5-го, conftest). Статьи сбора тестов
# опубликованы в октябре (otbor_support.article): без заморозки окна тесты зависели бы от сегодняшней даты.
CHECK_DAY = datetime(2026, 11, 3, 12, tzinfo=feed_window.MSK)


@pytest.fixture(autouse=True)
def check_day(monkeypatch):
    monkeypatch.setattr(feed_window, "_now", lambda: CHECK_DAY)


@pytest.fixture()
def before_flag(monkeypatch):
    """Прод до переключения (#134, история): ИИ на внешнем воркере, полоса пересчётов есть, модель «Отбора» задана;
    статьи окна решены гейтом до «Отбора» (_gate). Флага у «Отбора» с 08.11 нет."""
    monkeypatch.setattr(api.config, "EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr(api.config, "AI_EXECUTION_REGION", "external")
    monkeypatch.setattr(api.config, "AI_BULK_LANE_ENABLED", True)
    monkeypatch.setattr(api.config, "OTBOR_MODEL", MODEL)
    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))


def _flag_on(monkeypatch):
    """День переключения: «Карточка» включена, оба её вызова настроены (флаг «Отбора» снят 08.11)."""
    monkeypatch.setattr(api.config, "SIGNAL_CARD_ENABLED", True)
    monkeypatch.setattr(api.config, "SIGNAL_CARD_MODEL", MODEL)
    monkeypatch.setattr(api.config, "SIGNAL_SCORES_MODEL", SCORES_MODEL)


def _gate(article_id, relevant):
    """Статья, собранная до «Отбора» (путь статьи пуст), и вердикт гейта ленты в её карточке, как его записала прежняя
    обработка (история: гейт снят 08.11); мост «лента → сигнал» синхронизирован, как после записи вердикта."""
    with repository.get_connection() as conn:
        conn.execute("UPDATE articles SET selector = NULL WHERE id = %s", (article_id,))
        conn.execute("INSERT INTO article_cards (article_id, relevant, relevance_reason, relevance_model, status) "
                     "VALUES (%s, %s, 'r', 'm', %s)", (article_id, relevant, "new" if relevant else "rejected"))
        conn.commit()
    repository._sync_feed_signal(article_id)


def _gated(title, text, relevant, **kwargs):
    article_id = article(title, text, **kwargs)
    _gate(article_id, relevant)
    return article_id


def _collected(article_id, days_ago):
    with repository.get_connection() as conn:
        conn.execute("UPDATE articles SET collected_at = now() - make_interval(days => %s) WHERE id = %s",
                     (days_ago, article_id))
        conn.commit()


def _feed(user):
    """Лента статей (/api/articles): заголовки видимых статей."""
    return {row["title"] for row in api_get("/api/articles", user)}


def _report(capsys):
    """Отчёт проверки (otbor-shadow-report --json)."""
    capsys.readouterr()
    cli.main(["otbor-shadow-report", "--since", SINCE, "--json"])
    return json.loads(capsys.readouterr().out)


def _shadow(monkeypatch, answers):
    """Проверка на реальных данных по окну: задачи и воркер NL с подставной моделью «Отбора»; подставная модель."""
    model = OtborModel(answers)
    monkeypatch.setattr(otbor, "make_client", lambda: model)
    cli.main(["otbor-shadow", "--since", SINCE, "--apply"])
    run_worker("external-ai-bulk")
    return model


def test_dry_run_counts_window_articles_and_price_and_queues_nothing(user, before_flag, capsys):
    _gated("Контракт Weatherford", "Weatherford получила контракт на бурение 274 скважин в Омане.", True)
    _gated("Котировки нефти", "Нефть дешевеет после роста запасов нефти в США.", False)
    _collected(_gated("Старый контракт", "Halliburton получила контракт в Кувейте.", True), 30)
    marks.set_article_mark(_gated("В выпуске", "SLB подписала контракт с Saudi Aramco.", True), status="digest")
    marks.set_article_mark(_gated("С комментарием", "Baker Hughes поставит компрессоры в Катар.", True),
                           analyst_comment="проверить объём")
    api_send("POST", "/api/feedback", user,
             {"article_id": _gated("С отзывом", "Weatherford открыла базу в Кувейте.", False), "usefulness": 2})
    _gated("Закрытый месяц", "Schlumberger открыла центр цементирования в Тюмени.", True,
           published=datetime(2026, 1, 15, 9, tzinfo=timezone.utc))
    capsys.readouterr()

    cli.main(["otbor-shadow", "--since", SINCE])

    out = capsys.readouterr().out
    # Окно: две статьи, решённые гейтом; старше окна, с решением человека (отметка, комментарий, отзыв) и закрытый
    # месяц не идут.
    assert "статей 2" in out
    assert "$" in out and MODEL in out
    assert "сухой прогон" in out
    assert run_worker("external-ai-bulk") == []


def test_apply_writes_decisions_beside_and_leaves_feed_and_showcases_as_they_were(
        user, before_flag, monkeypatch, capsys):
    _gated("Контракт Weatherford", "Weatherford получила контракт на бурение 274 скважин в Омане.", True)
    _gated("Робот-бурильщик", "Equinor испытала робота-бурильщика на платформе.", False)
    assert (_feed(user), set(showcase(user))) == ({"Контракт Weatherford"}, {"Контракт Weatherford"})

    model = _shadow(monkeypatch, {"Контракт Weatherford": reject("corporate_promo"),
                                  "Робот-бурильщик": answer(industry="oilfield_services", category="technology")})

    # Решения «Отбора» рядом с решением гейта: гейт принял, «Отбор» нет, и наоборот.
    report = _report(capsys)
    assert (report["publications"], report["decided"]) == (2, 2)
    assert report["gate"] == {"accept": {"accept": 0, "reject": 1}, "reject": {"accept": 1, "reject": 0}}
    assert report["versions"] == {f"{otbor.PROMPT_VERSION} / {load_dictionaries()['version']}": 2}
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


def test_repeat_run_retries_a_technical_failure_but_not_an_answer_against_the_rules(
        user, before_flag, monkeypatch, capsys):
    _gated("Модель молчит", "Equinor испытает робота-бурильщика на шельфе в 2027 году.", True)
    _gated("Не по схеме", "SLB подписала контракт с Saudi Aramco на бурение 40 скважин.", True)
    _shadow(monkeypatch, {"Модель молчит": AIClientError("timeout"), "Не по схеме": {"decision": "accept"}})
    assert _report(capsys)["failed"] == {"technical": 1, "invalid_answer": 1}

    model = _shadow(monkeypatch, {"Модель молчит": answer()})

    # Технический сбой повтор добирает; ответ не по правилам не повторяется (решение владельца 23.10).
    assert [call["input"].splitlines()[0] for call in model.calls] == ["Заголовок: Модель молчит"]
    report = _report(capsys)
    assert (report["decided"], report["failed"]) == (1, {"invalid_answer": 1})


def test_report_shows_reject_sections_categories_technology_for_the_radar_and_gate_divergence(
        user, before_flag, monkeypatch, capsys):
    _gated("Контракт Weatherford", "Weatherford получила контракт на бурение 274 скважин в Омане.", True)
    _gated("Котировки нефти", "Нефть дешевеет после роста запасов нефти в США.", True)
    _gated("Робот-бурильщик", "Equinor испытала робота-бурильщика на платформе.", False)
    _gated("Реклама выставки", "Приглашаем на выставку оборудования в Москве.", False)
    _gated("Модель молчит", "Equinor испытает робота-бурильщика на шельфе в 2027 году.", True)
    _shadow(monkeypatch, {
        "Контракт Weatherford": answer(industry="oilfield_services"),
        "Котировки нефти": reject("oil_gas_no_link"),
        "Робот-бурильщик": answer(industry="oilfield_services", category="technology"),
        "Реклама выставки": reject("corporate_promo", industry="other_industry"),
        "Модель молчит": AIClientError("timeout")})

    report = _report(capsys)

    assert (report["publications"], report["decided"], report["failed"]) == (5, 4, {"technical": 1})
    # Гейт против «Отбора»: по строке на решение гейта, по столбцу на решение «Отбора».
    assert report["gate"] == {"accept": {"accept": 1, "reject": 1}, "reject": {"accept": 1, "reject": 1}}
    assert report["categories"] == {"business": 1, "technology": 1}
    assert {row["label"]: row["count"] for row in report["reasons"]} == {
        "Нефтегаз без связи с нефтесервисом": 1, "Корпоративное и реклама": 1}
    # Технологии из сбора пойдут на радар; находок радара за окно и до него нет.
    assert (report["technology_from_feed"], report["radar_now"], report["radar_before_per_week"]) == (1, 0, 0)
    assert [(row["title"], row["reason"]) for row in report["examples"]["gate_accepted_otbor_rejected"]] == [
        ("Котировки нефти", "Нефтегаз без связи с нефтесервисом")]
    assert [(row["title"], row["category"]) for row in report["examples"]["gate_rejected_otbor_accepted"]] == [
        ("Робот-бурильщик", "технология")]

    cli.main(["otbor-shadow-report", "--since", SINCE])
    text = capsys.readouterr().out
    assert "Нефтегаз без связи с нефтесервисом: 1" in text and "Робот-бурильщик" in text


def test_switch_moves_decisions_into_showcases_and_sends_the_accepted_down_the_rest_of_the_pipeline(
        user, admin, pipeline, monkeypatch, capsys):
    monkeypatch.setattr(api.config, "AI_BULK_LANE_ENABLED", True)
    ids = {title: _gated(title, text, relevant) for title, text, relevant in (
        ("Контракт Weatherford", "Weatherford получила контракт на бурение 274 скважин в Омане.", True),
        ("Котировки нефти", "Нефть дешевеет после роста запасов нефти в США.", True),
        ("Робот-бурильщик", "Equinor испытала робота-бурильщика на платформе.", False))}
    process_feed()
    # Карточка прогона по истории: до «Отбора», категория ленты «бизнес», темы нет.
    fill_cards(monkeypatch, {"Контракт Weatherford": card_answer(), "Котировки нефти": card_answer()})
    _shadow(monkeypatch, {"Контракт Weatherford": answer(industry="oilfield_services"),
                          "Котировки нефти": reject("oil_gas_no_link"),
                          "Робот-бурильщик": answer(industry="oilfield_services", category="technology")})
    assert set(showcase(user)) == {"Контракт Weatherford", "Котировки нефти"}

    capsys.readouterr()
    cli.main(["otbor-switch", "--since", SINCE])
    out = capsys.readouterr().out
    assert "перенести 3: в бизнес-ленту 1, на радар 1, «Другое» 0, отклонено 1" in out
    assert "карточка заново 1" in out and "сухой прогон" in out
    # Поимённый список для дампа и согласования.
    cli.main(["otbor-switch", "--since", SINCE, "--json"])
    assert json.loads(capsys.readouterr().out)["ids"] == {
        "business": [ids["Контракт Weatherford"]], "technology": [ids["Робот-бурильщик"]], "other": [],
        "rejected": [ids["Котировки нефти"]]}
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


def test_switch_leaves_alone_an_article_with_a_human_decision_or_a_closed_month_after_the_check(
        user, admin, before_flag, monkeypatch, capsys):
    commented = _gated("Контракт Weatherford", "Weatherford получила контракт на бурение 274 скважин в Омане.", True,
                       published=datetime(2026, 11, 2, 9, tzinfo=timezone.utc))
    _gated("Котировки нефти", "Нефть дешевеет после роста запасов нефти в США.", True)  # опубликована в октябре
    _shadow(monkeypatch, {"Контракт Weatherford": reject("corporate_promo"),
                          "Котировки нефти": reject("oil_gas_no_link")})
    # После проверки человек оставил отзыв к ноябрьской статье, а октябрь закрылся (с 5-го открыт только ноябрь).
    api_send("POST", "/api/feedback", user, {"article_id": commented, "usefulness": 5})
    monkeypatch.setattr(feed_window, "_now", lambda: datetime(2026, 11, 6, 12, tzinfo=feed_window.MSK))
    _flag_on(monkeypatch)
    capsys.readouterr()

    cli.main(["otbor-switch", "--since", SINCE, "--apply"])

    # Решения людей и закрытый месяц перенос не трогает: обе статьи живут по решению гейта, отказов «Отбора» нет.
    out = capsys.readouterr().out
    assert "перенести 0" in out and "после проверки 2" in out
    assert showcase(user)["Контракт Weatherford"]["otbor_decision"] is None
    assert api_get("/api/signals", admin, section="all") == []


def test_switch_refuses_while_checks_run_and_while_window_articles_have_no_check(
        user, before_flag, monkeypatch, capsys):
    _gated("Контракт Weatherford", "Weatherford получила контракт на бурение 274 скважин в Омане.", True)
    _shadow(monkeypatch, {"Контракт Weatherford": answer(industry="oilfield_services")})

    # Пришла после проверки, но до переключения: её решил гейт (флага у «Отбора» с 08.11 нет).
    late = _gated("Пришла до переключения", "Halliburton получила контракт в Кувейте.", True)
    _flag_on(monkeypatch)
    capsys.readouterr()
    cli.main(["otbor-switch", "--since", SINCE])
    assert "без решения проверки 1" in capsys.readouterr().out
    # Статья окна, которую решил гейт, без решения проверки: сначала дозапуск проверки.
    with pytest.raises(SystemExit, match="otbor-shadow"):
        cli.main(["otbor-switch", "--since", SINCE, "--apply"])
    # Дозапуск поставлен, задачи ещё идут: перенос ждёт их.
    cli.main(["otbor-shadow", "--since", SINCE, "--apply"])
    with pytest.raises(SystemExit, match="идут задачи проверки"):
        cli.main(["otbor-switch", "--since", SINCE, "--apply"])
    assert showcase(user)["Контракт Weatherford"]["otbor_decision"] is None
    assert late in [row["id"] for row in api_get("/api/articles", user)]
