"""Второй вызов «Карточки»: баллы (#132, спека #128), шов 1 — статья сбора → «Отбор» → суть → основной вызов →
второй вызов на подставной модели через настоящий клиент воркера NL → ядро → API витрин (tests/card_pipeline_support.py).
Профиль — по категории; «Свежесть» считает код из даты события в тексте; итог по весам, ступень и «Решение платформы»
считает код; «не тот блок» перезапускает только второй вызов.
"""

import re

import pytest

from oiltech_digest import api, cli, contract, signal_scores
from oiltech_digest.db import repository
from oiltech_digest.otbor_pack_format import load_rubrics
from oiltech_digest.processing.openai_client import AIClientError
from oiltech_digest.signal_research import UNCONFIRMED_LINK
from tests.card_pipeline_support import (SCORES_MODEL, ScoresModel, accepted, answer, card_answer, even, fill_cards,
                                         pipeline, profile_names, run_scores, scores)  # noqa: F401 — фикстура
from tests.otbor_support import JUSTIFICATION, api_send, reject, showcase
from tests.otbor_support import contour  # noqa: F401 — фикстура
from tests.signal_card_support import admin, api_get, radar_finding, run_worker, user, worker  # noqa: F401 — фикстуры

CONTRACT = "Контракт Weatherford в Омане"
CONTRACT_TEXT = "Weatherford получила контракт на бурение 274 скважин в Омане. Контракт подписан 24 сентября 2026 года."
TWIN = "Цифровой двойник бурения"
TWIN_TEXT = "Equinor 10 октября 2026 года запустила цифровой двойник бурения скважин на шельфе."
WHY = "Крупный контракт на буровые услуги"
FRESHNESS = "Свежесть"
MATURITY = "Зрелость / доказательность"


def _title(call):
    return next(line.split(":", 1)[1].strip() for line in call["input"].splitlines() if line.startswith("Заголовок:"))


def _criteria(call):
    return call["schema"]["schema"]["properties"]["scores"]["items"]["properties"]["criterion"]["enum"]


def _items(card):
    return {item["name"]: item for item in card["score_items_json"]}


def _tech(monkeypatch, cards, *, title=TWIN, text=TWIN_TEXT):
    """Технологические статьи прошли «Отбор», суть и основной вызов «Карточки» (cards: заголовок → ответ карточки)."""
    accepted(monkeypatch, {name: answer(industry="oilfield_services", category="technology") for name in cards},
             {name: f"{text} ({name})" for name in cards})
    fill_cards(monkeypatch, cards)


def test_scores_follow_the_profile_of_the_category_with_only_the_agreed_input(user, pipeline, monkeypatch):
    accepted(monkeypatch, {CONTRACT: answer(industry="oilfield_services"),
                           TWIN: answer(industry="oilfield_services", category="technology")},
             {CONTRACT: CONTRACT_TEXT, TWIN: TWIN_TEXT})
    fill_cards(monkeypatch, {CONTRACT: card_answer(why_important=WHY),
                             TWIN: card_answer(event_date="2026-10-10", maturity="пилот/ОПИ")})
    summary = showcase(user, "technology")[TWIN]["summary"]

    model = run_scores(monkeypatch, {CONTRACT: scores(even("business", 70)),
                                     TWIN: scores(even("tech_radar", 70), named_customer=False)})

    calls = {_title(call): call for call in model.calls}
    assert {call["model"] for call in model.calls} == {SCORES_MODEL}
    # Профиль — по категории: у бизнеса пять критериев бизнеса, у технологии — радара без «Свежести»: её считает код.
    assert _criteria(calls[CONTRACT]) == profile_names("business")
    assert _criteria(calls[TWIN]) == [name for name in profile_names("tech_radar") if name != FRESHNESS]
    assert "named_customer" in calls[TWIN]["schema"]["schema"]["properties"]
    assert "named_customer" not in calls[CONTRACT]["schema"]["schema"]["properties"]
    header = calls[TWIN]["input"].split("Текст публикации:", 1)[0]
    for line in ("Категория: технология", "Отрасль: нефтесервис", "Связь с нефтесервисом: прямая",
                 "Дата события из текста: 10.10.2026", "Стадия зрелости: пилот/ОПИ", f"Суть: {summary}"):
        assert line in header, line
    # Рассуждений других агентов во входе нет: ни решения и обоснования «Отбора», ни «почему важно» «Карточки».
    for call in model.calls:
        assert JUSTIFICATION not in call["input"] and "accept" not in call["input"] and WHY not in call["input"]
    # В инструкции линейки критериев и правила эксперта; весов нет: итог по весам считает код.
    rubrics = {one["name"]: one for one in load_rubrics()["profiles"]["tech_radar"]}
    instructions = calls[TWIN]["instructions"]
    assert rubrics["Технологическая новизна"]["bands"]["strong"]["rule"] in instructions
    assert rubrics[FRESHNESS]["bands"]["strong"]["rule"] not in instructions
    assert "сумма договора — масштаб" in instructions.lower() and not re.search(r"\bвес", instructions.lower())

    contract_card, twin = showcase(user)[CONTRACT], showcase(user, "technology")[TWIN]
    assert (contract_card["score_profile"], twin["score_profile"]) == ("business", "tech_radar")
    assert list(_items(contract_card)) == profile_names("business")
    assert (float(contract_card["score"]), contract_card["score_band"]) == (70.0, "useful")
    # «Свежесть» — кодом: событие 10.10, публикация 20.10, 10 дней — 100 − 10 × 20/30.
    fresh = _items(twin)[FRESHNESS]
    assert fresh["ai_score"] == pytest.approx(93.33, abs=0.01) and fresh["excluded"] is None
    assert float(twin["score"]) == pytest.approx((70 * 90 + 93.33 * 10) / 100, abs=0.01)
    recipe = twin["scores_recipe"]
    assert (recipe["model"], recipe["prompt_version"], recipe["rubrics_version"]) == (
        f"{SCORES_MODEL}-2026-03-17", signal_scores.PROMPT_VERSION, load_rubrics()["version"])
    steps = [step for step in twin["decision_log"] if step["stage"] == "«Карточка», баллы"]
    assert len(steps) == 1 and "полезный" in steps[0]["text"]


def test_model_gets_the_ruler_of_a_criterion_not_its_screen_description(user, pipeline, monkeypatch):
    # Описание критерия на экране «Скоринг» — черновик заказчика: у «Достоверности» там «свежесть события», а линейка
    # (утверждённая правка эксперта) возраст оценивать запрещает. Модель получает линейку.
    accepted(monkeypatch, {CONTRACT: answer()}, {CONTRACT: CONTRACT_TEXT})
    fill_cards(monkeypatch, {CONTRACT: card_answer()})

    model = run_scores(monkeypatch, {CONTRACT: scores(even("business", 70))})

    instructions = model.calls[0]["instructions"]
    assert "свежесть события" not in instructions
    assert "Возраст события здесь не оценивается" in instructions
    assert "В «почему такой балл» масштаб и эффект пишутся раздельно" in instructions


def test_freshness_is_left_out_without_an_event_date_in_the_text_or_with_a_month_across_bands(
        user, pipeline, monkeypatch):
    _tech(monkeypatch, {
        # Даты в тексте нет: дата публикации вместо неё не подставляется.
        "Без даты": card_answer(event_date="", event_date_source="по дате публикации", maturity="пилот/ОПИ"),
        # Сентябрь к 20.10: от 20 до 49 дней — «сильный» или «полезный», ступень не понять.
        "Сентябрь": card_answer(event_date="2026-09", maturity="пилот/ОПИ"),
        # Август к 20.10: от 50 до 80 дней — одна ступень, балл по середине периода (65 дней).
        "Август": card_answer(event_date="2026-08", maturity="пилот/ОПИ"),
    })

    run_scores(monkeypatch, {title: scores(even("tech_radar", 70), named_customer=False)
                             for title in ("Без даты", "Сентябрь", "Август")})

    cards = showcase(user, "technology")
    for title, why in (("Без даты", "нет даты события в тексте"), ("Сентябрь", "разные ступени")):
        fresh = _items(cards[title])[FRESHNESS]
        assert fresh["ai_score"] is None and why in fresh["excluded"], title
        # Веса остальных пересчитаны: без «Свежести» итог — 70, а не 63.
        assert float(cards[title]["score"]) == 70.0, title
    august = _items(cards["Август"])[FRESHNESS]
    assert august["excluded"] is None
    assert august["ai_score"] == pytest.approx(79 - (65 - 31) * 14 / 59, abs=0.01)


def test_maturity_score_stays_in_the_band_of_the_card_stage(user, pipeline, monkeypatch):
    _tech(monkeypatch, {
        "Прототип": card_answer(maturity="прототип"),
        "Запуск без заказчика": card_answer(maturity="коммерческое внедрение"),
        "Внедрение у заказчика": card_answer(maturity="коммерческое внедрение"),
        "Без стадии": card_answer(maturity="неприменимо"),
    })

    run_scores(monkeypatch, {
        "Прототип": scores(even("tech_radar", 60, **{MATURITY: 90}), named_customer=True),
        # Запуск продукта без названного заказчика — 40-64 и при стадии «коммерческое внедрение».
        "Запуск без заказчика": scores(even("tech_radar", 60, **{MATURITY: 85}), named_customer=False),
        # Внедрение у названного заказчика — 80 и выше.
        "Внедрение у заказчика": scores(even("tech_radar", 60, **{MATURITY: 70}), named_customer=True),
        "Без стадии": scores(even("tech_radar", 60, **{MATURITY: 30}), named_customer=False),
    })

    cards = showcase(user, "technology")
    assert {title: _items(cards[title])[MATURITY]["ai_score"] for title in cards} == {
        "Прототип": 64, "Запуск без заказчика": 64, "Внедрение у заказчика": 80, "Без стадии": 30}


def test_keywords_do_not_lift_maturity_above_the_band_of_the_card_stage(user, pipeline, monkeypatch):
    # Ключевые слова подмешиваются в балл критерия (вариант экзамена), но «Зрелость» остаётся в ступени стадии.
    with repository.get_connection() as conn:
        conn.execute("UPDATE scoring_criteria SET keywords_json = '[\"цифровой\", \"двойник\", \"бурения\"]'::jsonb "
                     "WHERE profile = 'tech_radar' AND name = %s", (MATURITY,))
        conn.commit()
    _tech(monkeypatch, {"Прототип": card_answer(maturity="прототип")})

    run_scores(monkeypatch, {"Прототип": scores(even("tech_radar", 64), named_customer=False)})

    maturity = _items(showcase(user, "technology")["Прототип"])[MATURITY]
    assert maturity["keyword_score"] > 64 and maturity["final_score"] == 64


def test_signal_without_scores_shows_no_score_rather_than_zero(user, admin, pipeline, monkeypatch):
    accepted(monkeypatch, {CONTRACT: answer(), "Интервью": answer(category="other"),
                           "Котировки": reject("oil_gas_no_link"), "Не по схеме": {"decision": "accept"}},
             {CONTRACT: CONTRACT_TEXT, "Интервью": "Интервью о подготовке буровых кадров.",
              "Котировки": "Нефть дешевеет после роста запасов в США.",
              "Не по схеме": "SLB подписала контракт с Saudi Aramco на бурение 40 скважин."})
    radar_finding("unconfirmed", "Выдуманная ссылка", "Текст сводки модели поиска.", filter_stage="search",
                  filter_reason=UNCONFIRMED_LINK)
    radar_finding("judged", "Брак судьи", "SLB представила новую систему.", filter_stage="judge",
                  filter_reason="не технология")

    # Второй вызов ещё не прошёл; «другое», отклонённое, сбой «Отбора» и отсеянное поиском не оценивают вовсе: балла
    # нет, а не «0» и «Низкая». У брака судьи балл судьи есть.
    card = showcase(user)[CONTRACT]
    assert (card["unscored"], card["score_label"]) == (True, None)
    removed = {one["title"]: (one["unscored"], one["score_label"]) for one in api_get("/api/signals", admin,
                                                                                    section="all")}
    assert {title: removed[title] for title in ("Интервью", "Котировки", "Не по схеме", "Выдуманная ссылка")} == {
        title: (True, None) for title in ("Интервью", "Котировки", "Не по схеме", "Выдуманная ссылка")}
    assert removed["Брак судьи"][0] is False and removed["Брак судьи"][1]
    fill_cards(monkeypatch, {CONTRACT: card_answer()})
    run_scores(monkeypatch, {CONTRACT: scores(even("business", 70))})
    card = showcase(user)[CONTRACT]
    assert (card["unscored"], card["score_label"]) == (False, "Полезный")


def test_band_and_platform_decision_are_counted_by_code_and_other_gets_neither(user, admin, pipeline, monkeypatch):
    texts = {"Сильный": CONTRACT_TEXT, "Наблюдать": CONTRACT_TEXT + " Работы начнутся в 2027 году.",
             "Слабый": CONTRACT_TEXT + " Сумма не раскрывается.", "Интервью": "Интервью о подготовке буровых кадров."}
    accepted(monkeypatch, {**{title: answer(industry="oilfield_services") for title in texts},
                           "Интервью": answer(industry="oilfield_services", category="other")}, texts)
    fill_cards(monkeypatch, {title: card_answer() for title in texts})

    model = run_scores(monkeypatch, {"Сильный": scores(even("business", 85)), "Наблюдать": scores(even("business", 50)),
                                     "Слабый": scores(even("business", 30))})

    cards = showcase(user)
    assert {title: (cards[title]["score_band"], cards[title]["maturity"]) for title in ("Сильный", "Наблюдать",
                                                                                        "Слабый")} == {
        "Сильный": ("strong", "shortlist"), "Наблюдать": ("watch", "watch"), "Слабый": ("weak", "watch")}
    # «Другое» баллами не оценивается: ни ступени, ни «Решения платформы».
    assert "Интервью" not in {_title(call) for call in model.calls}
    [other] = api_get("/api/signals", admin, section="other")
    assert (other["score_band"], other["maturity"], other["scores_recipe"]) == (None, None, None)


def test_bands_come_from_the_setting_and_both_keyword_variants_are_kept(user, pipeline, monkeypatch):
    monkeypatch.setattr(api.config, "SIGNAL_SCORE_BANDS", (75, 62, 40))
    with repository.get_connection() as conn:
        conn.execute("UPDATE scoring_criteria SET keywords_json = '[\"контракт\", \"скважин\", \"бурение\"]'::jsonb "
                     "WHERE profile = 'business' AND name = 'Рыночная возможность / изменение рынка'")
        conn.commit()
    accepted(monkeypatch, {CONTRACT: answer()}, {CONTRACT: CONTRACT_TEXT})
    fill_cards(monkeypatch, {CONTRACT: card_answer()})

    run_scores(monkeypatch, {CONTRACT: scores(even("business", 76))})

    card = showcase(user)[CONTRACT]
    totals = card["scores_recipe"]["totals"]
    # Ключи «Рыночной возможности» все в тексте: с подмешиванием итог выше, чем без него; в score — вариант настройки.
    assert totals["keywords"] > totals["plain"] == 76.0
    assert float(card["score"]) == pytest.approx(totals["keywords"])
    assert card["score_band"] == "strong"  # 76 при границе «сильного» 75


def test_third_publication_from_dedup_confirms_a_strong_signal(user, contour, pipeline, monkeypatch):
    from tests.otbor_radar_support import event, page, run_radar, summarize
    from tests.otbor_support import decide

    first, second, third = (f"https://drill{number}.example/news/autonomous" for number in (1, 2, 3))
    titles = ("SLB starts autonomous drilling for Aramco in 40 wells", "Aramco and SLB begin autonomous drilling",
              "SLB autonomous drilling reaches 40 wells for Aramco")
    run_radar(monkeypatch, [event(titles[0], first, company="SLB"), event(titles[1], second, company="SLB")],
              {first: page(titles[0]), second: page(titles[1])}, same_event=True)
    decide(monkeypatch, {title: answer(industry="oilfield_services", category="technology") for title in titles})
    summarize(monkeypatch, {title: (title, f"Суть: {title}") for title in titles})
    fill_cards(monkeypatch, {title: card_answer(event_date="2026-10-20", maturity="коммерческое внедрение")
                             for title in titles})
    run_scores(monkeypatch, {title: scores(even("tech_radar", 90), named_customer=True) for title in titles})
    card = next(card for card in api_get("/api/signals", user, showcase="technology") if card["score_band"])
    # Две публикации одного события: сильный — «кандидат».
    assert (card["score_band"], card["maturity"]) == ("strong", "shortlist")

    # «Дедуп» следующего прогона добавил третью публикацию — «подтверждено».
    run_radar(monkeypatch, [event(titles[2], third, company="SLB")], {third: page(titles[2])}, same_event=True)

    after = next(one for one in api_get("/api/signals", user, showcase="technology") if one["id"] == card["id"])
    assert after["maturity"] == "proven"


def test_wrong_block_changes_the_category_at_once_and_reruns_only_the_scores(user, pipeline, monkeypatch):
    accepted(monkeypatch, {CONTRACT: answer(industry="oilfield_services")}, {CONTRACT: CONTRACT_TEXT})
    card_model = fill_cards(monkeypatch, {CONTRACT: card_answer(maturity="пилот/ОПИ")})
    run_scores(monkeypatch, {CONTRACT: scores(even("business", 70))})
    signal_id = showcase(user)[CONTRACT]["id"]
    model = ScoresModel({CONTRACT: scores(even("tech_radar", 85), named_customer=True)})
    monkeypatch.setattr(signal_scores, "make_client", lambda: model)

    api_send("POST", "/api/signals/feedback", user, {"signal_id": signal_id, "verdict": "wrong_block", "comment": ""})

    # Категория — сразу: карточка уже в другой витрине, а баллы до ответа второго вызова «пересчитываются».
    assert CONTRACT not in showcase(user)
    moved = showcase(user, "technology")[CONTRACT]
    assert (moved["signal_category"], moved["card_set_by"]["signal_category"]) == ("technology", "human")
    assert moved["scores_stale_since"] is not None

    run_worker("external-ai")

    rescored = showcase(user, "technology")[CONTRACT]
    assert (rescored["score_profile"], rescored["scores_stale_since"]) == ("tech_radar", None)
    # Профиль радара: «Зрелость» в ступени «пилота» (не выше 79), «Свежесть» — 26 дней с 24.09 по 20.10.
    fresh = 100 - 26 * 20 / 30
    assert float(rescored["score"]) == pytest.approx((85 * 70 + 79 * 20 + fresh * 10) / 100, abs=0.01)
    assert (rescored["score_band"], rescored["maturity"]) == ("strong", "shortlist")
    # Перезапускается только второй вызов: «Отбор», суть и основной вызов «Карточки» не платятся второй раз.
    assert [_title(call) for call in model.calls] == [CONTRACT] and len(card_model.calls) == 1


def test_wrong_block_before_the_card_changes_the_category_and_the_scores_come_by_the_new_profile(
        user, pipeline, monkeypatch):
    accepted(monkeypatch, {CONTRACT: answer(industry="oilfield_services")}, {CONTRACT: CONTRACT_TEXT})
    signal_id = showcase(user)[CONTRACT]["id"]

    api_send("POST", "/api/signals/feedback", user, {"signal_id": signal_id, "verdict": "wrong_block", "comment": ""})

    # Карточки ещё нет: категория всё равно сразу другая, а «пересчитывать» нечего — баллов не было.
    moved = showcase(user, "technology")[CONTRACT]
    assert (moved["signal_category"], moved["scores_stale_since"]) == ("technology", None)
    fill_cards(monkeypatch, {CONTRACT: card_answer(maturity="пилот/ОПИ")})
    model = run_scores(monkeypatch, {CONTRACT: scores(even("tech_radar", 70), named_customer=False)})
    assert _criteria(model.calls[0]) == [name for name in profile_names("tech_radar") if name != FRESHNESS]
    assert showcase(user, "technology")[CONTRACT]["score_profile"] == "tech_radar"


def test_scores_model_is_an_explicit_setting_and_its_spend_has_its_own_name(user, admin, pipeline, monkeypatch, capsys):
    accepted(monkeypatch, {CONTRACT: answer()}, {CONTRACT: CONTRACT_TEXT})
    fill_cards(monkeypatch, {CONTRACT: card_answer()})
    monkeypatch.setattr(api.config, "SIGNAL_SCORES_MODEL", "")
    with pytest.raises(SystemExit, match="SIGNAL_SCORES_MODEL"):
        cli.main(["enqueue-signal-scores"])
    monkeypatch.setattr(api.config, "SIGNAL_SCORES_MODEL", SCORES_MODEL)

    run_scores(monkeypatch, {CONTRACT: scores(even("business", 70))})

    report = {row["stage"]: row for row in api_get("/api/reports/ai-cost", admin)}
    assert report["signal_scores"]["runs"] == 1
    # Своя строка и в расходе по месяцам — не в расходе ленты.
    monthly = {row["area"]: row["model"] for row in api_get("/api/stats/monthly", admin, months=1)["ai_cost"]}
    assert monthly["signal_scores"] == f"{SCORES_MODEL}-2026-03-17"


def test_wrong_score_bands_stop_the_step_before_a_paid_call(user, admin, pipeline, monkeypatch):
    # Порядок перепутан или границ не три: ступени исказились бы или запись упала бы после оплаченного вызова.
    accepted(monkeypatch, {CONTRACT: answer()}, {CONTRACT: CONTRACT_TEXT})
    fill_cards(monkeypatch, {CONTRACT: card_answer()})
    for bands in [(65, 80, 40), (80, 65)]:
        monkeypatch.setattr(api.config, "SIGNAL_SCORE_BANDS", bands)
        with pytest.raises(SystemExit, match="SIGNAL_SCORE_BANDS"):
            cli.main(["enqueue-signal-scores"])

    assert api_get("/api/jobs", admin, kind="signal_scores") == []


def test_answer_off_the_rules_is_not_written_and_the_batch_goes_on(user, admin, pipeline, monkeypatch):
    titles = {"Без критерия": CONTRACT_TEXT, "Дважды": CONTRACT_TEXT + " Вторая фаза.",
              "Не число": CONTRACT_TEXT + " Третья фаза.", "Модель молчит": CONTRACT_TEXT + " Четвёртая фаза.",
              "Дробный": CONTRACT_TEXT + " Шестая фаза.", "Как надо": CONTRACT_TEXT + " Пятая фаза."}
    accepted(monkeypatch, {title: answer() for title in titles}, titles)
    fill_cards(monkeypatch, {title: card_answer() for title in titles})
    names = profile_names("business")

    run_scores(monkeypatch, {
        "Без критерия": scores({name: 70 for name in names[1:]}),
        "Дважды": {"scores": [{"criterion": names[0], "score": 70, "why": "x"}] * 2
                   + [{"criterion": name, "score": 70, "why": "x"} for name in names[1:]]},
        "Не число": scores({**even("business", 70), names[0]: "много"}),
        # Схема просит целое: дробное — не по правилам.
        "Дробный": scores({**even("business", 70), names[0]: 70.5}),
        "Модель молчит": AIClientError("timeout"),
        "Как надо": scores(even("business", 70)),
    })

    cards = showcase(user)
    assert {title: cards[title]["scores_recipe"] is not None for title in titles} == {
        "Без критерия": False, "Дважды": False, "Не число": False, "Модель молчит": False, "Дробный": False,
        "Как надо": True}
    [job] = api_get("/api/jobs", admin, kind="signal_scores")
    errors = {error["signal_id"]: error["error"] for error in api_get(f"/api/jobs/{job['id']}", admin)["result"][
        "applied"]["errors"]}
    assert len(errors) == 5


def test_criteria_the_model_sees_edited_after_the_job_was_issued_are_not_written_over(user, admin, pipeline,
                                                                                       monkeypatch):
    """Критерий переименовали на экране «Скоринг», пока воркер ждал модель: она оценивала прежний, баллы не пишутся и
    приходят со следующей задачей. Чего модель не видит (веса, описание критерия с линейкой), то запись не держит:
    итог по нынешним весам, они и в снимке."""
    accepted(monkeypatch, {CONTRACT: answer()}, {CONTRACT: CONTRACT_TEXT})
    fill_cards(monkeypatch, {CONTRACT: card_answer()})
    first = profile_names("business")[0]

    def edit(change):
        criteria = api_get("/api/scoring-criteria", admin, profile="business")
        change(criteria)
        api_send("PUT", "/api/scoring-criteria?profile=business", admin, criteria)

    def edited(change):
        def reply(schema):
            edit(change)
            return scores(even("business", 70))
        return reply

    def rename(criteria):
        criteria[0]["name"] = f"{first} (уточнено)"

    def unseen(criteria):
        criteria[0]["weight"], criteria[1]["weight"] = criteria[0]["weight"] + 5, criteria[1]["weight"] - 5
        criteria[0]["description"] = f"{criteria[0]['description'] or ''} Уточнено."

    run_scores(monkeypatch, {CONTRACT: edited(rename)})

    assert showcase(user)[CONTRACT]["scores_recipe"] is None
    [job] = api_get("/api/jobs", admin, kind="signal_scores")
    [error] = api_get(f"/api/jobs/{job['id']}", admin)["result"]["applied"]["errors"]
    assert error["error"].startswith("критерии профиля сменились после выдачи задачи")

    edit(lambda criteria: criteria[0].update(name=first))
    run_scores(monkeypatch, {CONTRACT: edited(unseen)})

    card = showcase(user)[CONTRACT]
    weights = {row["name"]: row["weight"] for row in api_get("/api/scoring-criteria", admin, profile="business")}
    assert card["scores_recipe"] is not None
    assert {name: item["weight"] for name, item in _items(card).items()} == weights


def test_old_worker_build_does_not_get_the_scores_job(user, admin, pipeline, monkeypatch):
    accepted(monkeypatch, {CONTRACT: answer()}, {CONTRACT: CONTRACT_TEXT})
    fill_cards(monkeypatch, {CONTRACT: card_answer()})
    cli.main(["enqueue-signal-scores"])

    assert worker("external-ai", contract_number=5).claim() is None

    [job] = api_get("/api/jobs", admin, kind="signal_scores")
    assert f"нужен контракт {contract.CONTRACT}" in repository.get_background_job(job["id"])["error_message"]
