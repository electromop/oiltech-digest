"""Экзамен «Карточки» (#133, спека #128), шов 2 — тема основного вызова и баллы второго на подставной модели.

Пакет и разметка — card_scores_exam_support: «Отбор» эксперта как в otbor_exam_support, у взятых бизнес- и
технологических строк — баллы, общая оценка и тема по кругу; выгрузка с ядра — с той же изолированной базы.
"""

import pytest

from oiltech_digest import signal_card, signal_scores
from oiltech_digest.db import repository
from tests.card_scores_exam_support import (CARD, LUNA, MATURITY, MINI, SUMMARY, export_control, flat, oracle_card,
                                            oracle_scores, part_rows, run_exam)
from tests.card_scores_exam_support import scores_pack  # noqa: F401 — фикстура
from tests.otbor_pack_support import THEMES
from tests.otbor_pack_support import pack  # noqa: F401 — фикстура


def test_a_model_that_scores_like_the_expert_passes_and_the_main_call_measures_the_theme(scores_pack, monkeypatch):
    model, report = run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)})

    exam = part_rows(report, scores_pack)
    strong = [one["id"] for one in scores_pack["control"]["strong"]]
    assert len(strong) == 2 and exam
    rows = len(exam) + len(strong)
    # Суть — один раз на строку, основной вызов на модели ядра и второй вызов — по три прогона.
    assert len(model.of("article_summary", SUMMARY)) == rows
    assert len(model.of("signal_card", CARD)) == rows * 3
    assert len(model.of("signal_scores", LUNA)) == rows * 3
    assert {call["model"] for call in model.calls} == {SUMMARY, CARD, LUNA}

    assert report["decision"]["model"] == LUNA
    [luna] = report["models"]
    assert luna["passed"] is True
    for variant in ("keywords", "plain"):
        scores = luna["scores"][variant]
        assert scores["band"]["agent"] == 1.0 and scores["order"]["agent"] == 1.0
    # Тема — у строк пакета (у «сильных сверх пакета» темы эксперта нет), версии инструкций — в отчёте.
    assert report["theme"]["agent"] == 1.0 and report["theme"]["n"] == len(exam) * 3
    assert report["card_prompt_version"] == signal_card.PROMPT_VERSION
    assert report["scores_prompt_version"] == signal_scores.PROMPT_VERSION


def test_the_cheapest_model_that_passes_is_chosen_and_the_main_call_and_summary_are_paid_once(scores_pack,
                                                                                               monkeypatch):
    model, report = run_exam(monkeypatch, scores_pack, {LUNA: flat(60), MINI: oracle_scores(scores_pack)})

    luna, mini = report["models"]
    assert (luna["model"], mini["model"]) == (LUNA, MINI)
    # Один балл всем: итог у всех строк одинаковый — порядок только ничьи (половина), ступень совпала лишь у
    # «наблюдать».
    rows = [one for one in report["rows"]]
    watch = sum(one["band"] == "watch" for one in rows)
    assert luna["scores"]["plain"]["order"]["agent"] == 0.5
    assert luna["scores"]["plain"]["band"]["agent"] == pytest.approx(watch / len(rows))
    assert set(luna["failed"]) == {"band", "order"} and luna["passed"] is False
    assert mini["passed"] is True and report["decision"]["model"] == MINI
    # Основной вызов — один на прогон строки для всех моделей второго вызова: дата и стадия у них одни и те же.
    assert len(model.of("signal_card")) == len(rows) * 3
    assert len(model.of("article_summary")) == len(rows)

    # Повторный запуск суть и основной вызов не оплачивает: они рядом с манифестом.
    model, report = run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)})
    assert model.of("article_summary") == [] and model.of("signal_card") == []
    assert report["decision"]["model"] == LUNA and report["theme"]["agent"] == 1.0

    # Не прошла ни одна: решения нет, причины в отчёте.
    _, report = run_exam(monkeypatch, scores_pack, {LUNA: flat(60), MINI: flat(70)})
    assert report["decision"]["model"] is None
    assert "ступень" in report["decision"]["reason"] and "порядок" in report["decision"]["reason"]


def test_both_keyword_variants_are_scored_and_the_one_closer_to_the_expert_is_chosen(scores_pack, monkeypatch, capsys):
    # Ключевые слова, которые есть в каждой статье ленты: подмешивание поднимает слабые строки ленты до «наблюдать».
    with repository.get_connection() as conn:
        conn.execute("""UPDATE scoring_criteria SET keywords_json = '["publisher", "body", "title"]'""")
        conn.commit()
    export_control(scores_pack["out"], capsys)

    _, report = run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)})

    [luna] = report["models"]
    keywords, plain = luna["scores"]["keywords"], luna["scores"]["plain"]
    assert plain["band"]["agent"] == 1.0
    assert keywords["band"]["agent"] < plain["band"]["agent"]
    # Нынешняя настройка — с ключевыми словами, но ближе к эксперту вариант без них: его и берёт решение.
    assert report["keywords_setting"] is True
    assert luna["variant"] == "plain" and report["decision"] == {**report["decision"], "model": LUNA,
                                                                  "variant": "plain"}
    weak_feed = [one for one in report["rows"] if one["band"] == "weak" and one["id"].startswith("article:")]
    assert weak_feed and luna["scores"]["totals"]["reject"]["max"] == 25.0  # итоги — варианта решения


def test_the_main_call_theme_is_scored_against_the_expert_with_the_constant_and_core_control(scores_pack,
                                                                                             monkeypatch):
    _, first = run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)})
    assert first["theme"]["agent"] == 1.0
    exam = part_rows(first, scores_pack)
    wrong = exam[:4]
    other = {name: next(theme for theme in THEMES if theme != name) for name in THEMES}
    card = oracle_card(scores_pack, {number: {"theme": other[scores_pack["expert"][number]["theme"]]}
                                     for number in wrong})
    # Ответы основного вызова лежат рядом с манифестом: новые ответы — после их удаления.
    (scores_pack["out"] / "card_scores_exam_cards.json").unlink()

    _, report = run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)}, card=card)

    assert report["theme"]["agent"] == pytest.approx((len(exam) - len(wrong)) / len(exam))
    assert report["theme"]["passed"] is True
    # Константа — самая частая тема эксперта; контроль — тег ленты и тема судьи (в синтетике у всех «Бурение»).
    themes = [scores_pack["expert"][number]["theme"] for number in exam]
    assert report["baseline"]["constant"]["theme"] == pytest.approx(max(map(themes.count, THEMES)) / len(exam))
    controlled = [number for number in exam if scores_pack["control"]["rows"].get(number, {}).get("theme")]
    assert report["baseline"]["control"]["theme_rows"] == len(controlled)
    assert report["baseline"]["control"]["theme"] == pytest.approx(
        sum(scores_pack["expert"][number]["theme"] == "Бурение" for number in controlled) / len(controlled))


def test_criteria_error_counts_what_reaches_the_signal_and_freshness_follows_the_main_call_date(scores_pack,
                                                                                                monkeypatch):
    _, first = run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)})
    exam = part_rows(first, scores_pack)
    business = [number for number in exam if scores_pack["expert"][number]["profile"] == "business"]
    technology = [number for number in exam if scores_pack["expert"][number]["profile"] == "tech_radar"]
    # Модель ставит бизнесу на 5 больше эксперта. У технологии с баллом эксперта выше 39 основной вызов назвал стадию
    # «исследование»: балл «Зрелости» держится ниже 40, сколько бы модель ни поставила. У другой нашёл дату события.
    scores = oracle_scores(scores_pack, {number: scores_pack["expert"][number]["score"] + 5 for number in business})
    research = next(number for number in technology if scores_pack["expert"][number]["score"] > 39)
    dated = next(number for number in technology if number != research)
    card = oracle_card(scores_pack, {research: {"maturity": "исследование"},
                                     dated: {"event_date": "2026-09-20", "event_date_source": "из текста"}})
    (scores_pack["out"] / "card_scores_exam_cards.json").unlink()

    _, report = run_exam(monkeypatch, scores_pack, {LUNA: scores}, card=card)

    chosen = report["models"][0]["scores"]["plain"]
    criteria = chosen["criteria"]
    assert criteria["Потенциальный бизнес-эффект"]["bias"] == pytest.approx(5.0)
    assert criteria["Потенциальный бизнес-эффект"]["mae"] == pytest.approx(5.0)
    assert criteria["Ценность для нефтесервиса"]["mae"] == 0.0
    clamped = 39 - scores_pack["expert"][research]["score"]
    assert criteria[MATURITY]["bias"] == pytest.approx(clamped / len(technology))
    # «Свежесть» считает код по дате основного вызова: у строки с датой она есть, а у эксперта «нет даты».
    assert chosen["freshness"]["n"] == len(technology) * 3
    assert chosen["freshness"]["agreement"] == pytest.approx((len(technology) - 1) / len(technology))
