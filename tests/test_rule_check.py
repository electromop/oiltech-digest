"""Автопроверка правил эксперта (#190, спека #187, ADR 0007), шов 2: задача автопроверки через настоящий клиент
воркера NL с подставной моделью.

Каждое предложение правила до решения меряется на эталоне ОС агента и шага (оценки и правки администратора и
согласованные им улучшения): сколько решений с правилом сдвинулось к эталону, от него и сколько без изменений; совет
«согласовать», если к эталону больше, чем от него; меньше порога строк: «мало данных: N». Ответ «без правила» берётся из
принятых решений, если версия инструкции, справочников и набора та же; после смены инструкции считаются оба прогона. У
«Сути и перевода» числа нет: «было / стало» рядом с текстом эксперта; у поиска: повторный поиск по теме с подсказками и
без против ТОП-списка темы. Потолок расходов откладывает проверки до следующего месяца, расход идёт своей стадией. В
первый понедельник месяца весь набор правил каждого агента проверяется против голой инструкции, итог строкой журнала.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from oiltech_digest import feedback_review, feedback_slice, otbor, rule_check, signal_research
from oiltech_digest.db import connection
from oiltech_digest.feed_window import MSK
from oiltech_digest.processing.openai_client import AIResponse
from tests.expert_rules_support import RecordingModel, approved, proposal
from tests.otbor_radar_support import SearchModel, event
from tests.otbor_support import answer, api_request, api_send, article, decide, reject
from tests.card_pipeline_support import pipeline  # noqa: F401: фикстура
from tests.otbor_support import contour  # noqa: F401: фикстура
from tests.signal_card_support import DRILLING, admin, run_worker, tag, user  # noqa: F401: фикстуры

RULE = "Корпоративный пресс-релиз с объёмом работ брать."
SEED = Path(__file__).resolve().parents[1] / "scripts" / "signals" / "expert-rules-before-deploy.sql"


class CheckModel:
    """Подставная модель агента в автопроверке: ответ по заголовку публикации или паре, с правилом (текст правила
    в инструкции) и без. Вызовы запоминает."""

    def __init__(self, answers, *, key="Заголовок:", rule=RULE):
        self.answers = answers
        self.key = key
        self.rule = rule
        self.calls = []

    def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None,
                      reasoning_effort=None):
        with_rule = self.rule in instructions
        self.calls.append({"instructions": instructions, "input": user_input, "with": with_rule})
        name = next(line.split(":", 1)[1].strip() for line in user_input.splitlines() if line.startswith(self.key))
        without, with_ = self.answers[name]
        return AIResponse(data=with_ if with_rule else without, model=f"{model or 'gpt-6-test'}-2026",
                          input_tokens=3000, output_tokens=200)


def _signal(article_id: int) -> int:
    with connection.get_connection() as conn:
        return int(conn.execute("SELECT id FROM signals WHERE origin = 'feed' AND origin_article_id = %s",
                                (article_id,)).fetchone()[0])


def _mark(admin, signal_id: int, **fields) -> dict:
    return api_send("POST", "/api/signals/feedback", admin, {"signal_id": signal_id, "comment": "", **fields})


def _settings(admin, **values) -> None:
    api_send("PUT", "/api/feedback-journal/settings", admin, {"values": values})


def _entry(admin, action_id: int) -> dict:
    rows = api_send("GET", "/api/feedback-journal?limit=200", admin, None)["entries"]
    return next(row for row in rows if row["key"] == f"action:{action_id}")


def _check(monkeypatch, model, action_id: int | None = None) -> None:
    if action_id is not None:
        rule_check.check_proposal(action_id)
    monkeypatch.setattr(rule_check, "make_client", lambda: model)
    run_worker("external-ai")


def _usage(stage: str) -> list[tuple]:
    with connection.get_connection() as conn:
        return conn.execute("SELECT signal_id, cost_usd, recipe FROM ai_processing_runs WHERE stage = %s ORDER BY id",
                            (stage,)).fetchall()


def test_otbor_rule_check_counts_toward_and_away_and_takes_the_answer_without_the_rule_from_accepted_decisions(
        user, admin, contour, monkeypatch):
    texts = {"Отчёт Weatherford о программе бурения": "Weatherford начнёт бурение 40 скважин в Омане в 2027 году.",
             "Пресс-релиз ADNOC о контракте": "ADNOC Drilling получила контракт на 40 скважин на месторождении Хаил.",
             "Итоги квартала SLB": "SLB отчиталась о выручке за третий квартал 2026 года.",
             "Новый офис Halliburton": "Halliburton открыла офис в Дубае и наняла 40 инженеров."}
    ids = {title: article(title, text) for title, text in texts.items()}
    first, second, third, fourth = texts
    decide(monkeypatch, {first: answer(), second: answer(), third: answer(), fourth: reject("corporate_promo")})
    signals = {title: _signal(ids[title]) for title in texts}
    _mark(admin, signals[first], verdict="wrong_domain")
    _mark(admin, signals[second], verdict="strong_signal")
    _mark(admin, signals[third], verdict="approved")
    _mark(admin, signals[fourth], verdict="strong_signal")
    _settings(admin, little_data_rows=2)
    action_id = proposal("otbor", "decision", RULE)

    take, drop = answer(), reject("corporate_promo")
    model = CheckModel({first: (take, drop), second: (take, take), third: (take, drop), fourth: (drop, take)})
    _check(monkeypatch, model, action_id)

    # «Не релевантно» администратора поменяло решение: ответ без правила считается; у остальных он из принятых.
    assert sorted((call["input"].splitlines()[0], call["with"]) for call in model.calls) == sorted(
        [(f"Заголовок: {first}", True), (f"Заголовок: {first}", False), (f"Заголовок: {second}", True),
         (f"Заголовок: {third}", True), (f"Заголовок: {fourth}", True)])
    assert all("Правила эксперта" not in call["instructions"] for call in model.calls if not call["with"])
    check = _entry(admin, action_id)["check"]
    assert (check["status"], check["rows"], check["toward"], check["away"], check["unchanged"]) == (
        "done", 4, 2, 1, 1)
    assert (check["advice"], check["little_data"], check["baseline"]) == (
        "согласовать", False, {"accepted": 3, "computed": 1})
    assert {sample["direction"] for sample in check["samples"]} == {"toward", "away"}
    assert _entry(admin, action_id)["confirm_reasons"] == []
    usage = _usage("rule_check")
    assert {row[0] for row in usage} == set(signals.values())
    assert all(float(row[1]) > 0 and row[2]["agent"] == "otbor" for row in usage)

    # Инструкция «Отбора» сменилась: принятые решения шли по прежней, считаются оба прогона.
    monkeypatch.setattr(otbor, "PROMPT_VERSION", "2026-11-13.test")
    model = CheckModel({first: (take, drop), second: (take, take), third: (take, drop), fourth: (drop, take)})
    response = api_request("POST", f"/api/feedback-journal/proposals/{action_id}/check", admin, None)
    assert response.status_code == 200, response.text
    _check(monkeypatch, model)
    assert len(model.calls) == 8
    assert _entry(admin, action_id)["check"]["baseline"] == {"accepted": 0, "computed": 4}
    approved_rule = api_send("POST", f"/api/feedback-journal/proposals/{action_id}/decision", admin,
                             {"decision": "approve"})
    assert approved_rule["version"] == 1


class DedupJudge:
    """Подставной судья «Дедупа» в автопроверке: одно ли событие у пары (по заголовку карточки A), с правилом и
    без."""

    def __init__(self, answers, rule):
        self.answers, self.rule, self.calls = answers, rule, []

    def complete_json(self, instructions, user_input, schema, **kwargs):
        title = next(line.split(":", 1)[1].strip() for line in user_input.splitlines() if line.startswith("заголовок:"))
        with_rule = self.rule in instructions
        self.calls.append(with_rule)
        return AIResponse(data={"same_event": self.answers[title][with_rule], "reason": "так"}, model="test-dedup",
                          input_tokens=500, output_tokens=40)


def test_dedup_check_on_few_pairs_shows_little_data_and_needs_a_separate_confirmation(user, admin, contour,
                                                                                    monkeypatch):
    titles = ["Контракт ADNOC на 40 скважин", "ADNOC Drilling: 40 скважин Хаил", "Пилот SLB в Омане",
              "Промышленный запуск SLB в Омане"]
    ids = {title: article(title, f"{title}. Подробности события.") for title in titles}
    decide(monkeypatch, {title: answer() for title in titles})
    signals = {title: _signal(ids[title]) for title in titles}
    _mark(admin, signals[titles[0]], verdict="merge_duplicate", duplicate_of_signal_id=signals[titles[1]])
    merged = _mark(admin, signals[titles[2]], verdict="merge_duplicate", duplicate_of_signal_id=signals[titles[3]])
    undone = api_request("POST", f"/api/feedback-journal/actions/{merged['action_id']}/undo", admin, None)
    assert undone.status_code == 200, undone.text
    rule = "Пилот и промышленный запуск: разные события."
    action_id = proposal("dedup", "same_event", rule)

    judge = DedupJudge({titles[0]: {False: False, True: True}, titles[1]: {False: False, True: True},
                        titles[2]: {False: False, True: True}, titles[3]: {False: False, True: True}}, rule)
    _check(monkeypatch, judge, action_id)

    check = _entry(admin, action_id)["check"]
    assert (check["rows"], check["toward"], check["away"], check["advice"], check["little_data"]) == (
        2, 1, 1, "мало данных: 2", True)
    assert sorted(judge.calls) == [False, False, True, True]
    assert _entry(admin, action_id)["confirm_reasons"] == ["мало данных: 2"]
    refused = api_request("POST", f"/api/feedback-journal/proposals/{action_id}/decision", admin,
                          {"decision": "approve"})
    assert (refused.status_code, refused.json()["detail"]) == (409, "нужно отдельное подтверждение: мало данных: 2")


def test_summary_check_shows_before_and_after_next_to_the_expert_text_without_a_count(user, admin, contour,
                                                                                    monkeypatch):
    titles = {"Weatherford wins Oman contract": "Weatherford won a contract to drill 274 wells in Oman.",
              "SLB launches digital twin": "SLB launched a digital twin for drilling in October 2026."}
    ids = {title: article(title, text, language="en") for title, text in titles.items()}
    decide(monkeypatch, {title: answer() for title in titles})
    expert = {"Weatherford wins Oman contract": "Заказчик: оператор в Омане; Weatherford бурит 274 скважины.",
              "SLB launches digital twin": "Оператор получил цифровой двойник бурения от SLB."}
    for title, text in expert.items():
        _mark(admin, _signal(ids[title]), verdict="approved", corrected_thesis=text)
    rule = "Суть начинай с заказчика работ."
    action_id = proposal("summary", "summary", rule)

    model = CheckModel({title: ({"summary": f"Было: {title}"}, {"summary": f"Стало: {title}"}) for title in titles},
                       key="title:", rule=rule)
    _check(monkeypatch, model, action_id)

    check = _entry(admin, action_id)["check"]
    assert (check["status"], check["no_count"], check["toward"], check["away"]) == ("done", True, 0, 0)
    assert check["advice"] == "числа нет: сравните «было / стало» с текстом эксперта"
    assert sorted((one["expert"], one["before"], one["after"]) for one in check["samples"]) == sorted(
        (expert[title], f"Было: {title}", f"Стало: {title}") for title in titles)
    assert _entry(admin, action_id)["confirm_reasons"] == []


def test_search_check_compares_a_repeated_search_with_hints_and_without_against_the_top_list(user, admin, contour,
                                                                                            monkeypatch):
    tag(DRILLING)
    titles = ["Бурение на обсадной колонне в Омане", "Интеллектуальное заканчивание на шельфе",
              "Обычная новость о бурении"]
    ids = {title: article(title, f"{title}. Подробности события.") for title in titles}
    decide(monkeypatch, {title: answer(category="technology") for title in titles})
    signals = {title: _signal(ids[title]) for title in titles}
    with connection.get_connection() as conn:
        conn.execute("UPDATE signals SET theme = %s WHERE id = ANY(%s)", (DRILLING, list(signals.values())))
        conn.commit()
    _mark(admin, signals[titles[0]], verdict="strong_signal")
    _mark(admin, signals[titles[1]], verdict="approved")
    action_id = proposal("search", "topics", "бурение на обсадной колонне", topic=DRILLING)
    with connection.get_connection() as conn:
        urls = dict(conn.execute("SELECT s.id, a.url FROM signals s JOIN articles a ON a.id = s.origin_article_id"
                                 " WHERE s.id = ANY(%s)", (list(signals.values()),)).fetchall())

    class Search(SearchModel):
        def research_json(self, instructions, user_input, schema, **kwargs):
            hinted = "бурение на обсадной колонне" in user_input
            self.events = [event(titles[0], urls[signals[titles[0]]])] if hinted else [
                event(titles[1], urls[signals[titles[1]]])]
            return super().research_json(instructions, user_input, schema, **kwargs)

    search = Search([])
    monkeypatch.setattr(signal_research, "make_client", lambda offline=False: search)
    _check(monkeypatch, None, action_id)

    assert len(search.calls) == 2
    check = _entry(admin, action_id)["check"]
    assert (check["rows"], check["toward"], check["away"], check["unchanged"]) == (2, 1, 1, 0)
    assert (check["advice"], check["little_data"]) == ("мало данных: 2", True)
    assert {(one["label"], one["with"], one["without"]) for one in check["samples"]} == {
        (titles[0], True, False), (titles[1], False, True)}
    assert all(row[0] is None and float(row[1]) > 0 for row in _usage("rule_check"))


def test_ceiling_postpones_checks_until_next_month_and_the_admin_sees_a_warning(user, admin, contour, monkeypatch):
    _settings(admin, monthly_ceiling_usd=1)
    with connection.get_connection() as conn:
        conn.execute("INSERT INTO ai_processing_runs (stage, provider, cost_usd, status) VALUES ('feedback_review',"
                     " 'openai', 2, 'ok')")
        conn.commit()
    action_id = proposal("card", "maturity", "PoC не промышленное внедрение.")

    assert rule_check.check_proposal(action_id)["status"] == "postponed"
    entry = _entry(admin, action_id)
    assert entry["check"]["status"] == "postponed" and "потолок расходов контура" in entry["check"]["note"]
    assert entry["confirm_reasons"] == ["автопроверка отложена потолком расходов"]
    assert "Потолок расходов контура ОС" in api_send("GET", "/api/feedback-journal/overview", admin, None)["warning"]

    # Тот же месяц: проверка ждёт; следующий месяц: шаг планировщика ставит её снова.
    assert rule_check.scheduler_step(datetime.now(MSK), monthly=False)["proposals"] == []
    next_month = (datetime.now(MSK).replace(day=1) + timedelta(days=32)).replace(day=15)
    [requeued] = rule_check.scheduler_step(next_month, monthly=False)["proposals"]
    assert requeued["status"] in ("queued", "done")


def test_first_monday_checks_the_whole_rule_set_of_each_agent_against_the_bare_instruction(user, admin, contour,
                                                                                          monkeypatch):
    titles = ["Контракт ADNOC на 40 скважин", "ADNOC Drilling: 40 скважин Хаил"]
    ids = {title: article(title, f"{title}. Подробности события.") for title in titles}
    decide(monkeypatch, {title: answer() for title in titles})
    _mark(admin, _signal(ids[titles[0]]), verdict="merge_duplicate", duplicate_of_signal_id=_signal(ids[titles[1]]))
    rule = "Один контракт оператора: одно событие."
    approved("dedup", "same_event", rule)
    _settings(admin, slice_weekday="1", slice_time="09:00")
    monday = datetime(2026, 12, 7, 9, 30, tzinfo=MSK)

    monkeypatch.setattr(feedback_slice, "_now", lambda: monday)
    # Срез этого понедельника тоже идёт (отметка администратора новая): его разбор улучшать нечего.
    monkeypatch.setattr(feedback_review, "make_client", lambda: RecordingModel(
        {"feedback_review": {"improvements": []}, "feedback_rules": {"rules": []}}))
    feedback_slice.scheduler_step(monday)
    feedback_slice.scheduler_step(monday)
    judge = DedupJudge({titles[0]: {False: False, True: True}, titles[1]: {False: False, True: True}}, rule)
    monkeypatch.setattr(rule_check, "make_client", lambda: judge)
    run_worker("external-ai")

    rows = api_send("GET", "/api/feedback-journal?limit=200&kind=set_check", admin, None)["entries"]
    [row] = rows
    assert (row["status"], row["set_agent"], row["version"]) == ("done", "dedup", 1)
    assert (row["check"]["mode"], row["check"]["rows"], row["check"]["toward"]) == ("set", 1, 1)
    assert row["check"]["advice"] == "мало данных: 1"


def test_seeded_stage_hints_proposal_is_first_in_the_journal_and_the_scheduler_checks_it(user, admin, contour):
    with connection.get_connection() as conn:
        # Схема теста выбрана в открытой транзакции подключения: сначала её закрыть, скрипт сам ставит BEGIN и COMMIT.
        conn.commit()
        conn.autocommit = True
        conn.execute(SEED.read_text())
        conn.execute(SEED.read_text())
    rows = api_send("GET", "/api/feedback-journal?limit=200&kind=rule_proposal", admin, None)["entries"]
    [seed] = rows
    assert (seed["rule"]["agent"], seed["rule"]["step"], seed["status"]) == ("card", "maturity", "pending")
    assert seed["rule"]["text"].startswith("«expected to contribute» значит ожидаемый, не подтверждённый эффект")
    assert seed["path"][0]["label"].startswith("Предложено: три подсказки толкования")
    assert seed["check"] is None

    rule_check.scheduler_step(datetime.now(MSK), monthly=False)
    assert _entry(admin, seed["id"])["check"]["status"] in ("queued", "done")
    assert rule_check.scheduler_step(datetime.now(MSK), monthly=False)["proposals"] == []


def test_otbor_check_compares_the_reject_reason_named_in_the_noise_mark(user, admin, contour, monkeypatch):
    title = "Реклама нового сервиса Halliburton"
    article_id = article(title, "Halliburton рассказывает о преимуществах своего сервиса без события и цифр.")
    decide(monkeypatch, {title: answer()})
    # «Шум» администратора с причиной справочника: эталон «не брать» с этой причиной («решение и причина», #133).
    _mark(admin, _signal(article_id), verdict="reject", reason="Корпоративное и реклама, события нет")
    _settings(admin, little_data_rows=1)
    action_id = proposal("otbor", "decision", RULE)

    model = CheckModel({title: (answer(), reject("corporate_promo"))})
    _check(monkeypatch, model, action_id)

    check = _entry(admin, action_id)["check"]
    assert (check["rows"], check["toward"], check["away"]) == (1, 1, 0)
    [sample] = check["samples"]
    assert (sample["expected"], sample["before"], sample["after"]) == (
        "reject:corporate_promo", "accept:None", "reject:corporate_promo")
    # Принятое решение той же инструкции: прогон без правила не оплачивается.
    assert [call["with"] for call in model.calls] == [True]


def test_search_run_that_failed_is_not_counted_as_not_found(user, admin, contour, monkeypatch):
    tag(DRILLING)
    title = "Бурение на обсадной колонне в Омане"
    article_id = article(title, f"{title}. Подробности события.")
    decide(monkeypatch, {title: answer(category="technology")})
    signal_id = _signal(article_id)
    with connection.get_connection() as conn:
        conn.execute("UPDATE signals SET theme = %s WHERE id = %s", (DRILLING, signal_id))
        conn.commit()
    _mark(admin, signal_id, verdict="strong_signal")
    action_id = proposal("search", "topics", "бурение на обсадной колонне", topic=DRILLING)

    class Broken(SearchModel):
        def research_json(self, instructions, user_input, schema, **kwargs):
            from oiltech_digest.processing.openai_client import AIClientError

            self.calls.append(user_input)
            if "бурение на обсадной колонне" not in user_input:
                raise AIClientError("поиск недоступен")
            return super().research_json(instructions, user_input, schema, **kwargs)

    monkeypatch.setattr(signal_research, "RESEARCH_RETRY_PAUSE_SECONDS", 0)
    monkeypatch.setattr(signal_research, "make_client", lambda offline=False: Broken([event(title, "https://x.ex/a")]))
    _check(monkeypatch, None, action_id)

    check = _entry(admin, action_id)["check"]
    assert (check["rows"], check["toward"], check["away"], check["little_data"]) == (0, 0, 0, True)
    assert "поиск не ответил" in check["note"]


def test_card_and_scores_checks_measure_the_field_and_the_band_against_accepted_answers(
        user, admin, pipeline, monkeypatch):
    from oiltech_digest.db import feedback_store
    from tests.card_pipeline_support import accepted, card_answer, even, fill_cards, run_scores, scores

    title = "Контракт Weatherford в Омане"
    text = "Weatherford получила контракт на бурение 274 скважин в Омане. SLB подписала контракт 24 сентября 2026."
    ids, _ = accepted(monkeypatch, {title: answer(industry="oilfield_services")}, {title: text})
    fill_cards(monkeypatch, {title: card_answer(maturity="прототип")})
    run_scores(monkeypatch, {title: scores(even("business", 70))})
    signal_id = _signal(ids[title])
    _settings(admin, little_data_rows=1)

    # Согласованное улучшение карточки: стадия «пилот/ОПИ» вместо «прототип» агента (эталон «Карточки»).
    from oiltech_digest import feedback_review

    card = feedback_store.review_cards([signal_id])[signal_id]
    change = feedback_review._change("technology_maturity", {"technology_maturity": "пилот/ОПИ"}, card,
                                     quote="уже пилот", event_ids=[])
    improvement = feedback_store.create_action("signal_proposal", "pending", signal_id=signal_id,
                                               details={"changes": [change]})
    api_send("POST", f"/api/feedback-journal/proposals/{improvement}/decision", admin, {"decision": "approve"})
    stage_rule = proposal("card", "maturity", "Испытание у оператора: пилот/ОПИ.")
    card_model = CheckModel({title: (card_answer(maturity="прототип"), card_answer(maturity="пилот/ОПИ"))},
                            rule="Испытание у оператора")
    from oiltech_digest import signal_card

    monkeypatch.setattr(signal_card, "make_client", lambda: card_model)
    _check(monkeypatch, card_model, stage_rule)
    check = _entry(admin, stage_rule)["check"]
    assert (check["rows"], check["toward"], check["away"], check["baseline"]) == (
        1, 1, 0, {"accepted": 1, "computed": 0})

    # Оценка администратора «Сильный сигнал» при ступени «полезный»: эталон баллов.
    _mark(admin, signal_id, verdict="strong_signal")
    band_rule = proposal("scores", "criteria", "Контракт на сотни скважин: сильный сигнал.", category="business")
    scores_model = CheckModel({title: (scores(even("business", 70)), scores(even("business", 96)))},
                              rule="сотни скважин")
    _check(monkeypatch, scores_model, band_rule)
    check = _entry(admin, band_rule)["check"]
    assert (check["rows"], check["toward"], check["away"]) == (1, 1, 0)
    [sample] = check["samples"]
    assert (sample["expected"], sample["before"], sample["after"]) == ("strong", "useful", "strong")
    assert [call["with"] for call in scores_model.calls] == [True]
