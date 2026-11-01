"""Справочники «Отбора» новой версии и таблица конфликтов шагов (#133, спека #128).

Таблица конфликтов шагов (otbor_answer.DECISION_STEPS) опирается на коды справочника: новая версия, сменившая код
причины, правит таблицу в том же изменении. Проверка — через контур (шов 1: воркер «Отбора» на подставной модели,
запись ядром): каждая причина справочника проходит без пометки «спорное», а причина без строки таблицы — конфликт
шагов, и решение остаётся спорным. Правила справочников утверждает владелец (решение 29 журнала гриля, 23.10).
"""

from __future__ import annotations

from oiltech_digest import otbor
from oiltech_digest.db import connection, feed_signals
from oiltech_digest.otbor_answer import ACCEPT, DECISION_STEPS, MIXED_EVENTS, REJECT, codes
from oiltech_digest.otbor_pack_format import load_dictionaries
from tests.otbor_support import answer, article, decide, reject
from tests.otbor_support import contour  # noqa: F401 — фикстура
from tests.signal_card_support import user  # noqa: F401 — фикстура

NEW_REASON = "listing_page"


def _decided(article_id):
    with connection.get_connection() as conn:
        return conn.execute("SELECT otbor_decision, reject_reason, otbor_disputed FROM signals WHERE signal_key = %s",
                            (f"{feed_signals.FEED_KEY_PREFIX}{article_id}",)).fetchone()


def test_every_reason_passes_the_contour_and_a_reason_missing_from_the_table_is_disputed(user, contour, monkeypatch):
    current = load_dictionaries()
    # Новая версия справочника с причиной, которую таблицу конфликтов забыли дополнить.
    extended = {**current, "reject_reason": {**current["reject_reason"], "values": [
        *current["reject_reason"]["values"],
        {"code": NEW_REASON, "label": "Страница-список", "rule": "Лента нескольких публикаций.", "examples": []}]}}
    monkeypatch.setattr(otbor, "load_dictionaries", lambda: extended)
    answers, articles = {}, {}
    for code in codes(extended, "reject_reason"):
        if code == MIXED_EVENTS:
            continue  # только у находки с двумя и более ссылками (#131)
        # Отрасль и связь, которые строка таблицы допускает (нет строки — любые: тогда конфликт даст сама таблица).
        industries, relevances = DECISION_STEPS.get(code, (None, None))
        industry = sorted(industries or codes(extended, "industry"))[0]
        relevance = sorted(relevances or {"none"})[0]
        title = f"Отказ: {code}"
        articles[code] = article(title, f"Публикация, которую «Отбор» отклоняет по причине {code}.")
        answers[title] = reject(code, industry=industry, oilfield_relevance=relevance)

    decide(monkeypatch, answers)

    for code in codes(current, "reject_reason"):
        if code != MIXED_EVENTS:
            assert _decided(articles[code]) == (REJECT, code, None), code
    decision, reason, disputed = _decided(articles[NEW_REASON])
    assert (decision, reason) == (REJECT, NEW_REASON)
    assert "причины «Страница-список» нет в таблице конфликтов шагов" in disputed


def test_new_version_carries_the_rules_the_owner_approved():
    # Правки эксперта, принятые владельцем 23.10 (решение 29): граница прямой связи шире, переносимая — и из смежных
    # операций нефтегаза, «разные события» — только по содержательным ссылкам.
    dictionaries = load_dictionaries()
    rule = {value["code"]: value["rule"] for block in ("oilfield_relevance", "reject_reason")
            for value in dictionaries[block]["values"]}
    assert "ремонтных и обеспечивающих работ на нефтегазовом объекте" in rule["direct"]
    assert "программу работ заказчика" in rule["direct"] and "названное в тексте" in rule["direct"]
    assert "смежных операций нефтегаза (переработка, транспорт, хранение)" in rule["transferable"]
    assert "заказ работ или услуг для самого нефтегазового объекта" in rule["transferable"]
    assert "двумя и более ссылками" in rule["mixed_events"] and "общую страницу" in rule["mixed_events"]


def test_no_example_teaches_to_reject_by_the_headline():
    # Связь решает содержание, а не заголовок: заметка о ценах с названной в тексте остановкой промыслов — прямая.
    # Поэтому заголовок о ценах нефти не стоит примером ни в одном списке.
    examples = [example for block in load_dictionaries().values() if isinstance(block, dict)
                for value in block.get("values") or [] for example in value.get("examples") or []]
    assert examples and not any(example.startswith(("Нефть дешевеет", "Котировки нефти")) for example in examples)


def test_every_occasion_names_its_link_from_the_relevance_codes():
    # Список поводов публикации (решение владельца 26.10): правило связи — таблица в справочнике, а не абзац подсказки.
    dictionaries = load_dictionaries()
    relevances = set(codes(dictionaries, "oilfield_relevance"))
    occasions = dictionaries["occasion"]["values"]
    assert occasions and all(value["relevances"] and set(value["relevances"]) <= relevances for value in occasions)
    # Каждая связь достижима, а у «прямой» и «нет связи» есть поводы, которые дают только её.
    assert {code for value in occasions for code in value["relevances"]} == relevances
    assert {"direct", "none"} <= {value["relevances"][0] for value in occasions if len(value["relevances"]) == 1}


def test_an_occasion_with_one_link_overrides_the_models_link_through_one_retry(user, contour, monkeypatch):
    deal = article("Слияние операторов", "Две нефтедобывающие компании объявили о слиянии.")
    stubborn = article("Покупка доли", "Компания купила долю в нефтеперерабатывающем активе.")
    # Модель узнала сделку, но связь поставила по заголовку: «нет связи» — повод это не допускает.
    by_headline = reject("oil_gas_no_link", occasion="asset_deal")
    model = decide(monkeypatch, {"Слияние операторов": [by_headline, answer(occasion="asset_deal")],
                                 "Покупка доли": [by_headline, by_headline]})

    retried = [call["input"] for call in model.calls if call["input"].startswith("Заголовок: Слияние")]
    conflict = "повод публикации «Сделка (M&A)» даёт связь «прямая», а не «нет связи»"
    assert len(retried) == 2 and conflict in retried[1]
    assert _decided(deal) == (ACCEPT, None, None)
    decision, reason, disputed = _decided(stubborn)
    assert (decision, reason) == (REJECT, "oil_gas_no_link")
    assert conflict in disputed

