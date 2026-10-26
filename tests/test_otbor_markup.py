"""Загрузка разметки эксперта для «Отбора» (#129, шов 2 спеки #128): проверка целостности и отчёт о нарушениях.

Шов — команда `otbor-markup-check` по пакету, который выгрузила `otbor-expert-pack` на изолированной базе
(синтетика — otbor_pack_support)."""

from __future__ import annotations

import json
import shutil

import pytest

from oiltech_digest import otbor_markup, otbor_pack_format as pack_format
from tests.otbor_pack_support import BUSINESS, RUBRICS, TAKEN, TECH, business, fill, rejected, run_check, technology
from tests.otbor_pack_support import pack  # noqa: F401 — фикстура


def by_the_rules(items):
    """Разметка по правилам: отказы, бизнес, технологии, «другое» без баллов, «разные события» у находки со ссылками."""
    answers = {}
    for number, item in enumerate(items):
        answers[item["id"]] = [rejected(), business(), technology(),
                               {**TAKEN, "Категория": "другое", "Почему такой балл": None}][number % 4]
    links = next(item["id"] for item in items if item["group"] == "radar_links")
    answers[links] = rejected(**{"Причина отказа": "Разные события в одной находке", "Отрасль": "нефтесервис"})
    # «Свежесть» — число, если дата события в тексте есть.
    first_tech = next(item_id for item_id, answer in answers.items() if answer.get("Категория") == "технология")
    answers[first_tech] = technology(**{TECH[-1]: 90})
    return answers


def test_a_markup_by_the_rules_passes(pack, capsys):
    path = fill(pack["out"], by_the_rules(pack["items"]))

    run_check("--markup", str(path))

    printed = capsys.readouterr().out
    assert "строк 116, нарушений 0" in printed
    assert "Утверждение: «да» 0, правки 0, без решения 18" in printed
    # Строки с метками в кодах справочников — вход экзамена (#133).
    labels = {item["id"]: item for item in otbor_markup.check(path)["items"]}
    answers = by_the_rules(pack["items"])
    refused = next(item_id for item_id, answer in answers.items()
                   if answer.get("Причина отказа") == "Нефтегаз без связи с нефтесервисом")
    taken = next(item_id for item_id, answer in answers.items() if answer.get("Категория") == "бизнес"
                 and answer["Брать или нет"] == "брать")
    assert {key: labels[refused][key] for key in ("decision", "reject_reason", "industry", "oilfield_relevance",
                                                  "category", "overall")} == {
        "decision": "reject", "reject_reason": "oil_gas_no_link", "industry": "oil_gas", "oilfield_relevance": "none",
        "category": None, "overall": "wrong_domain"}
    assert labels[taken]["scores"] == {**{one["name"]: 70 for one in RUBRICS["business"]},
                                       **{one["name"]: None for one in RUBRICS["tech_radar"]}}
    assert labels[taken]["theme"] == "Бурение" and labels[taken]["why"] == TAKEN["Почему такой балл"]


def test_every_violation_is_reported_at_once_and_the_check_fails(pack, capsys):
    items = pack["items"]
    answers = by_the_rules(items)
    feed = [item["id"] for item in items if item["group"] in ("feed_rejected", "feed_accepted")]
    (outside, no_reason, reason_taken, mixed_single, score_missing, score_bad, both_profiles, no_why, no_theme,
     scored_rejected, fresh_empty, blank, other_scored, taken_uncategorized, changed_text, dropped,
     doubled) = feed[:17]
    answers.update({
        outside: rejected(**{"Отрасль": "энергетика"}),
        no_reason: rejected(**{"Причина отказа": None}),
        reason_taken: business(**{"Причина отказа": "Старое событие"}),
        mixed_single: rejected(**{"Причина отказа": "Разные события в одной находке"}),
        score_missing: business(**{BUSINESS[2]: None}),
        score_bad: business(**{BUSINESS[0]: 120, BUSINESS[1]: "высокий"}),
        both_profiles: technology(**{BUSINESS[0]: 50}),
        no_why: business(**{"Почему такой балл": None}),
        no_theme: business(**{"Тема": None, "Общая оценка": None}),
        scored_rejected: rejected(**{BUSINESS[0]: 40}),
        fresh_empty: technology(**{TECH[-1]: None}),
        blank: {"Брать или нет": None, "Отрасль": None, "Связь с нефтесервисом": None, "Категория": None},
        other_scored: {**TAKEN, "Категория": "другое", TECH[0]: 60},
        # Категория выбирает витрину: у взятой строки она обязательна, у отклонённой — нет.
        taken_uncategorized: {**TAKEN, "Категория": None},
    })

    def edit(book, sheet, header):
        rows = {row[0].value: row for row in sheet.iter_rows(min_row=2)}
        rows[changed_text][header.index("Текст публикации")].value = "Текст, который поправил эксперт"
        sheet.delete_rows(rows[dropped][0].row)
        sheet.append(["article:999999", "Лишняя строка", "текст"])
        sheet.append([cell.value for cell in rows[doubled]])

    path = fill(pack["out"], answers, edit=edit)

    with pytest.raises(SystemExit) as failed:
        run_check("--markup", str(path))

    printed = capsys.readouterr().out
    expected = [
        f"{outside}: «Отрасль»: «энергетика» нет в списке",
        f"{no_reason}: «Причина отказа» не заполнена",
        f"{reason_taken}: у строки «брать» не должно быть причины отказа",
        f"{mixed_single}: «Разные события в одной находке» только у строки со ссылками находки",
        f"{score_missing}: «{BUSINESS[2]}» не заполнена",
        f"{score_bad}: «{BUSINESS[0]}»: «120» не целое число от 0 до 100",
        f"{score_bad}: «{BUSINESS[1]}»: «высокий» не целое число от 0 до 100",
        f"{both_profiles}: баллы не той категории: «{BUSINESS[0]}»",
        f"{no_why}: «Почему такой балл» не заполнена",
        f"{no_theme}: «Тема» не заполнена",
        f"{no_theme}: «Общая оценка» не заполнена",
        f"{scored_rejected}: баллы у строки «не брать»",
        f"{fresh_empty}: «{TECH[-1]}» не заполнена",
        f"{blank}: «Брать или нет» не заполнена",
        f"{blank}: «Отрасль» не заполнена",
        f"{blank}: «Связь с нефтесервисом» не заполнена",
        f"{other_scored}: баллы у категории «другое»",
        f"{taken_uncategorized}: «Категория» не заполнена",
        f"{changed_text}: текст публикации не тот, что в манифесте",
        f"{dropped}: строки нет в таблице",
        "article:999999: строки нет в манифесте",
        f"{doubled}: номер повторяется",
    ]
    for line in expected:
        assert line in printed, line
    assert f"нарушений {len(expected)}" in printed
    assert "нарушений" in str(failed.value)


def test_the_check_needs_the_pack_the_markup_came_from(pack, tmp_path):
    path = fill(pack["out"], {})
    alone = tmp_path / "alone"
    alone.mkdir()
    shutil.copy(path, alone / path.name)

    with pytest.raises(SystemExit) as refused:
        run_check("--markup", str(alone / path.name))
    assert "манифест" in str(refused.value)

    with pytest.raises(SystemExit) as refused:
        run_check("--markup", str(path), "--manifest", str(pack["corpus"]["card"]["manifest"]))
    assert "не манифест пакета «Отбора»" in str(refused.value)

    # Манифест поправили руками: проверка говорит, чего в нём нет, а не падает на середине.
    broken = json.loads((pack["out"] / pack_format.MANIFEST_NAME).read_text(encoding="utf-8"))
    del broken["lists"]
    broken["items"][0].pop("text_sha256")
    (tmp_path / "broken.json").write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit) as refused:
        run_check("--markup", str(path), "--manifest", str(tmp_path / "broken.json"))
    assert "испорчен" in str(refused.value) and "lists" in str(refused.value) and "items" in str(refused.value)

    # Не таблица xlsx: отказ команды, а не трассировка.
    (tmp_path / "notes.xlsx").write_text("не таблица", encoding="utf-8")
    with pytest.raises(SystemExit) as refused:
        run_check("--markup", str(tmp_path / "notes.xlsx"), "--manifest", str(pack["out"] / pack_format.MANIFEST_NAME))
    assert "не таблица xlsx" in str(refused.value)

    def without_sheet(book, sheet, header):
        del book[pack_format.MARKUP_SHEET]

    with pytest.raises(SystemExit) as refused:
        run_check("--markup", str(fill(pack["out"], {}, name="no_sheet.xlsx", edit=without_sheet)))
    assert f"нет листа «{pack_format.MARKUP_SHEET}»" in str(refused.value)

    def without_column(book, sheet, header):
        sheet.delete_cols(header.index("Категория") + 1)

    with pytest.raises(SystemExit) as refused:
        run_check("--markup", str(fill(pack["out"], {}, name="no_column.xlsx", edit=without_column)))
    assert "нет колонок Категория" in str(refused.value)


def test_expert_decisions_on_the_rules_are_summarized(pack, capsys):
    def decide(book, sheet, header):
        for name, changed in ((pack_format.APPROVAL_SHEET, {"Старое событие": "убрать"}),
                              (pack_format.RUBRICS_SHEET, {"Свежесть": "своя правка: от даты события до сегодня"})):
            approval = book[name]
            columns = [cell.value for cell in approval[1]]
            for row in approval.iter_rows(min_row=2):
                row[columns.index("Решение")].value = changed.get(row[1].value, "да")

    path = fill(pack["out"], by_the_rules(pack["items"]), edit=decide)

    run_check("--markup", str(path))

    printed = capsys.readouterr().out
    assert "Утверждение: «да» 16, правки 2, без решения 0" in printed
    assert "Старое событие: убрать" in printed
    assert "Свежесть: своя правка: от даты события до сегодня" in printed


def test_dictionaries_changed_since_the_pack_are_named(pack, capsys, monkeypatch):
    path = fill(pack["out"], by_the_rules(pack["items"]))
    current = pack_format.load_dictionaries()
    monkeypatch.setattr(pack_format, "load_dictionaries", lambda: {**current, "version": "2099-01-01.1"})

    run_check("--markup", str(path))

    printed = capsys.readouterr().out
    assert f"справочники пакета {current['version']}, сейчас 2099-01-01.1" in printed
