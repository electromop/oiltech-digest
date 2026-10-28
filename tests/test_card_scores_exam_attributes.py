"""Экзамен «Карточки» (#133, спека #128), шов 2 — атрибуты основного вызова того же прогона против разметки экзамена
карточки (#98), без новых порогов: только строки его экзаменационной части (на строках настройки #98 правили
инструкцию) и тексты те же, что видел эксперт. Пакет #98 по строкам прежней разметки — card_scores_exam_support.
"""

import pytest

from tests.card_scores_exam_support import LUNA, card_exam_pack, oracle_card, oracle_scores, part_rows, run_exam
from tests.card_scores_exam_support import scores_pack  # noqa: F401 — фикстура
from tests.otbor_pack_support import pack  # noqa: F401 — фикстура


def test_main_call_attributes_are_scored_on_the_card_exam_rows_with_its_thresholds(scores_pack, monkeypatch):
    card_exam = scores_pack["card_exam"]
    card = oracle_card(scores_pack, {number: {"event_type": "контракт"} for number in card_exam})

    _, report = run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)}, card=card)

    attributes = report["attributes"]
    compared = [number for number in part_rows(report, scores_pack) if number in card_exam]
    assert compared and attributes["rows"] == sorted(compared)
    metrics = attributes["lenient"]
    assert metrics["event_type"]["agent"] == 1.0  # тип события основного вызова — как у эксперта #98
    assert metrics["region"]["agent"] == 0.0  # регион основной вызов не определил, у эксперта «Ближний Восток»
    assert metrics["region"]["passed"] is False and metrics["event_type"]["passed"] is True  # пороги #98
    assert attributes["markup"]["file"] == str(scores_pack["card_markup"])


def test_the_exam_needs_the_card_markup_of_the_same_texts_and_tuning_does_not(scores_pack, monkeypatch, tmp_path):
    with pytest.raises(SystemExit, match="--card-markup"):
        run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)}, "--card-markup", "")

    _, report = run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)}, "--part", "tuning", "--runs",
                         "1", "--card-markup", "")
    assert report["attributes"] is None

    first = next(item["id"] for item in scores_pack["items"] if item["group"] == "card_markup")
    markup, _ = card_exam_pack(scores_pack["items"], tmp_path / "other98",
                               text_of={first: "Другой текст публикации. " * 20})
    with pytest.raises(SystemExit, match="текст"):
        run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)}, "--card-markup", str(markup))


def test_the_main_call_country_is_checked_against_the_expert_region_by_the_country_table(scores_pack, monkeypatch):
    # Эксперт #98 страну не размечал, регион у всех его строк «Ближний Восток» (card_exam_pack): страна события
    # основного вызова сверяется с ним по таблице «страна → регион»; страна на стыке регионов называет оба.
    numbers = sorted(scores_pack["card_exam"])
    given = {numbers[0]: ["Норвегия"], numbers[1]: [], numbers[2]: ["Норвегия", "Саудовская Аравия"]}
    countries = {number: given.get(number, ["Турция"]) for number in numbers}
    card = oracle_card(scores_pack, {number: {"event_countries": value} for number, value in countries.items()})

    _, report = run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)}, card=card)

    country = report["attributes"]["country"]
    compared = report["attributes"]["rows"]
    matched = [number for number in compared if countries[number] not in (["Норвегия"], [])]
    assert country["rows"] == len(compared) and country["n"] == 3 * len(compared)
    assert country["agent"] == pytest.approx(len(matched) / len(compared))
    assert country["no_country"] == 3 * sum(countries[number] == [] for number in compared)
    assert sorted(one["id"] for one in country["mismatches"]) == sorted(set(compared) - set(matched))
    assert all(one["region"] == "Ближний Восток" for one in country["mismatches"])
    [readable] = sorted(scores_pack["out"].glob("card_scores_exam_report_*.md"))[-1:]
    assert "Страна события против региона эксперта" in readable.read_text(encoding="utf-8")
