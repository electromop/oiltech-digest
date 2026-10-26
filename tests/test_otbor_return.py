"""Возврат отклонённого человеком (#132, спека #128), шов 1 — человек вписывает поля «Отбора» (отрасль, связь и
применение, категорию), публикация идёт по остатку конвейера (суть, оба вызова «Карточки») и получает пометку «спорное:
„Отбор“ не взял бы» с причиной; повтор «Отбора» решение человека не перезаписывает (tests/card_pipeline_support.py).
"""

from oiltech_digest import cli, otbor
from oiltech_digest.db import repository
from oiltech_digest.otbor_pack_format import load_dictionaries
from tests.card_pipeline_support import (accepted, answer, card_answer, even, fill_cards, pipeline, process_feed,  # noqa: F401
                                         run_scores, scores)
from tests.otbor_support import OtborModel, api_request, api_send, reject, showcase
from tests.otbor_support import contour  # noqa: F401 — фикстура
from tests.signal_card_support import admin, api_get, run_worker, user  # noqa: F401 — фикстуры

SALES = "Сбыт топлива Лукойла"
SALES_TEXT = "Лукойл заключил контракт на бурение 12 скважин и расширил сбыт топлива на северных АЗС."
SALES_REASON = "Нефтегаз без связи с нефтесервисом"


def _rejected(monkeypatch, admin):
    ids, model = accepted(monkeypatch, {SALES: reject("oil_gas_no_link")}, {SALES: SALES_TEXT})
    [card] = [card for card in api_get("/api/signals", admin, section="all") if card["title"] == SALES]
    return ids[SALES], card["id"], model


def _return(admin, signal_id, **overrides):
    body = {"industry": "oil_gas", "oilfield_relevance": "direct", "oilfield_application": "",
            "signal_category": "business", **overrides}
    return api_request("POST", f"/api/signals/{signal_id}/return", admin, body)


def test_returned_publication_goes_through_the_rest_of_the_pipeline_marked_disputed(user, admin, pipeline,
                                                                                    monkeypatch):
    article_id, signal_id, otbor_model = _rejected(monkeypatch, admin)
    assert SALES not in showcase(user)

    response = _return(admin, signal_id)

    assert response.status_code == 200, response.text
    card = showcase(user)[SALES]
    assert (card["otbor_decision"], card["industry"], card["oilfield_relevance"], card["signal_category"]) == (
        "accept", "oil_gas", "direct", "business")
    assert {card["card_set_by"][key] for key in ("industry", "oilfield_relevance", "signal_category",
                                                 "otbor_decision")} == {"human"}
    assert card["otbor_disputed"] == f"«Отбор» не взял бы: {SALES_REASON}"
    assert card["reject_reason"] is None and card["maturity"] == "watch"
    assert any(step["stage"] == "Человек" and "Вернул" in step["text"] for step in card["decision_log"])
    assert SALES not in {one["title"] for one in api_get("/api/signals", admin, section="all")}

    # Остаток конвейера: суть (обработка ленты), основной вызов «Карточки», баллы; «Решение платформы» — из ступени.
    process_feed()
    fill_cards(monkeypatch, {SALES: card_answer()})
    run_scores(monkeypatch, {SALES: scores(even("business", 85))})
    card = showcase(user)[SALES]
    assert card["summary"] and card["card_recipe"] is not None
    assert (card["score_band"], card["maturity"]) == ("strong", "shortlist")
    assert card["otbor_disputed"] == f"«Отбор» не взял бы: {SALES_REASON}"

    # Повтор «Отбора» решение человека не перезаписывает: ни названной командой, ни запоздалым итогом задачи.
    calls = len(otbor_model.calls)
    cli.main(["enqueue-otbor", "--ids", str(article_id)])
    run_worker("external-ai")
    assert len(otbor_model.calls) == calls
    monkeypatch.setattr(otbor, "make_client", lambda: OtborModel({SALES: reject("oil_gas_no_link")}))
    otbor.enqueue([article_id], trigger="manual")
    run_worker("external-ai")
    card = showcase(user)[SALES]
    assert (card["otbor_decision"], card["card_set_by"]["otbor_decision"]) == ("accept", "human")
    assert card["decision_log"][-1]["text"].startswith("Решение человека: не изменено")


def test_publication_returned_days_after_the_refusal_still_goes_through_the_pipeline(user, admin, pipeline,
                                                                                      monkeypatch):
    # Шаги конвейера берут сигнал по окну дней: отсчёт — от того, как публикация пошла дальше «Отбора» (его решение
    # или человека), а не от заведения сигнала: отказ мог быть неделю назад.
    _, signal_id, _ = _rejected(monkeypatch, admin)
    with repository.get_connection() as conn:
        conn.execute("UPDATE signals SET created_at = now() - interval '10 days' WHERE id = %s", (signal_id,))
        conn.commit()

    assert _return(admin, signal_id).status_code == 200
    process_feed()
    fill_cards(monkeypatch, {SALES: card_answer()})
    run_scores(monkeypatch, {SALES: scores(even("business", 70))})

    card = showcase(user)[SALES]
    assert card["card_recipe"] is not None and card["scores_recipe"] is not None
    assert card["score_band"] == "useful"


def test_return_form_takes_only_the_otbor_fields_by_its_rules_and_only_for_rejected(user, admin, pipeline,
                                                                                   monkeypatch):
    _, signal_id, _ = _rejected(monkeypatch, admin)
    accepted(monkeypatch, {"Программа бурения": answer()}, {"Программа бурения": "Equinor начнёт бурение 12 скважин."})
    accepted_id = showcase(user)["Программа бурения"]["id"]

    refusals = {
        "переносимая без применения": _return(admin, signal_id, oilfield_relevance="transferable"),
        "нефтесервис с переносимой": _return(admin, signal_id, industry="oilfield_services",
                                             oilfield_relevance="transferable", oilfield_application="Роботизация"),
        "без связи": _return(admin, signal_id, oilfield_relevance="none"),
        "код не из справочника": _return(admin, signal_id, signal_category="space"),
    }
    assert {reason: response.status_code for reason, response in refusals.items()} == {
        reason: 422 for reason in refusals}
    assert "применени" in refusals["переносимая без применения"].json()["detail"]
    # Вернуть можно только отклонённое «Отбором»; форма — у админа, как и сами разделы отклонённого.
    assert _return(admin, accepted_id).status_code == 409
    assert _return(user, signal_id).status_code == 403
    assert showcase(user).keys() == {"Программа бурения"}
    # Что вписывать, экран берёт у сервера — по справочнику «Отбора»; связь — только прямая и переносимая.
    choices = api_get("/api/signals/dictionaries", user)["otbor_return"]
    dictionaries = load_dictionaries()
    assert [one["code"] for one in choices["industry"]] == [value["code"] for value in dictionaries["industry"]["values"]]
    assert [one["code"] for one in choices["oilfield_relevance"]] == ["direct", "transferable"]
    assert [one["code"] for one in choices["category"]] == [value["code"] for value in dictionaries["category"]["values"]]


def test_returned_publication_goes_to_the_showcase_of_the_human_category(user, admin, pipeline, monkeypatch):
    _, signal_id, _ = _rejected(monkeypatch, admin)

    api_send("POST", f"/api/signals/{signal_id}/return", admin, {
        "industry": "other_industry", "oilfield_relevance": "transferable",
        "oilfield_application": "Экзоскелеты для работ на буровой", "signal_category": "technology"})

    # Витрину выбирает категория человека: технология — на радаре, в бизнес-ленте её нет.
    card = showcase(user, "technology")[SALES]
    assert (card["industry"], card["oilfield_relevance"], card["oilfield_application"]) == (
        "other_industry", "transferable", "Экзоскелеты для работ на буровой")
    assert SALES not in showcase(user)
