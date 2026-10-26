"""Экзамен «Карточки» (#133, спека #128), шов 2 — атрибуты основного вызова того же прогона против разметки экзамена
карточки (#98), без новых порогов: только строки его экзаменационной части (на строках настройки #98 правили
инструкцию) и тексты те же, что видел эксперт. Пакет #98 для строк прежней разметки — signal_card_support.write_pack.
"""

import json

import pytest

from oiltech_digest import signal_card_exam
from tests.card_scores_exam_support import LUNA, oracle_card, oracle_scores, part_rows, run_exam
from tests.card_scores_exam_support import scores_pack  # noqa: F401 — фикстура
from tests.otbor_pack_support import pack  # noqa: F401 — фикстура
from tests.signal_card_support import row, write_pack


def card_exam_pack(scores_pack, folder, *, tuning=1, text_of=None):
    """Пакет экзамена #98 по строкам прежней разметки пакета «Отбора»: те же номера и тексты; эксперт #98 поставил
    всем «контракт» и «Ближний Восток». Деление #98 — первые tuning строк в настройке."""
    text_of = text_of or {}
    card = [item for item in scores_pack["items"] if item["group"] == "card_markup"]
    rows = []
    for item in card:
        number = int(item["id"].split(":")[1])
        built = row(number, "technology" if item["origin"] == "radar" else "useful", title=item["title"],
                    text=text_of.get(item["id"], item["text"]), published=item["published"] or "2026-09-30")
        rows.append({**built, "id": item["id"]})
    folder.mkdir()
    markup = write_pack(folder, rows)
    ids = [one["id"] for one in rows]
    (folder / signal_card_exam.SPLIT_NAME).write_text(json.dumps({"tuning": ids[:tuning], "exam": ids[tuning:]}),
                                                      encoding="utf-8")
    return markup, ids[tuning:]


def test_main_call_attributes_are_scored_on_the_card_exam_rows_with_its_thresholds(scores_pack, monkeypatch,
                                                                                   tmp_path):
    markup, card_exam = card_exam_pack(scores_pack, tmp_path / "card98")
    card = oracle_card(scores_pack, {number: {"event_type": "контракт"} for number in card_exam})

    _, report = run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)}, "--card-markup", str(markup),
                         card=card)

    attributes = report["attributes"]
    compared = [number for number in part_rows(report, scores_pack) if number in card_exam]
    assert compared and attributes["rows"] == sorted(compared)
    metrics = attributes["lenient"]
    assert metrics["event_type"]["agent"] == 1.0  # тип события основного вызова — как у эксперта #98
    assert metrics["region"]["agent"] == 0.0  # регион основной вызов не определил, у эксперта «Ближний Восток»
    assert metrics["region"]["passed"] is False and metrics["event_type"]["passed"] is True  # пороги #98
    assert attributes["markup"]["file"] == str(markup)


def test_without_the_card_markup_there_is_no_attribute_check_and_another_text_is_refused(scores_pack, monkeypatch,
                                                                                          tmp_path):
    _, report = run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)})
    assert report["attributes"] is None

    first = next(item["id"] for item in scores_pack["items"] if item["group"] == "card_markup")
    markup, _ = card_exam_pack(scores_pack, tmp_path / "other98", text_of={first: "Другой текст публикации. " * 20})
    with pytest.raises(SystemExit, match="текст"):
        run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)}, "--card-markup", str(markup))
