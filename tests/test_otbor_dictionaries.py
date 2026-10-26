"""Справочники «Отбора» новой версии и таблица конфликтов шагов (#133, спека #128).

Таблица конфликтов шагов (otbor_answer.DECISION_STEPS) опирается на коды справочника: новая версия, сменившая код
причины, правит таблицу в том же изменении. Проверка — через контур (шов 1: воркер «Отбора» на подставной модели,
запись ядром): каждая причина справочника проходит без пометки «спорное», а причина без строки таблицы — конфликт
шагов, и решение остаётся спорным. Правила справочников утверждает владелец (решение 29 журнала гриля, 23.10).
"""

from __future__ import annotations

from oiltech_digest import otbor
from oiltech_digest.db import connection, feed_signals
from oiltech_digest.otbor_answer import DECISION_STEPS, MIXED_EVENTS, REJECT, codes
from oiltech_digest.otbor_pack_format import load_dictionaries
from tests.otbor_support import article, decide, reject
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


def test_an_example_does_not_teach_to_reject_by_the_headline():
    # Связь решает содержание, а не заголовок: заметка о ценах с названной в тексте остановкой промыслов — прямая.
    # Поэтому заголовок о ценах не стоит примером решения «не брать».
    dictionaries = load_dictionaries()
    refused = next(value for value in dictionaries["decision"]["values"] if value["code"] == REJECT)
    assert not any(example.startswith("Нефть дешевеет") for example in refused["examples"])
