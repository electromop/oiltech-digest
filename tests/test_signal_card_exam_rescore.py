"""Пересчёт отчёта экзамена агента «Карточка сигнала» без вызовов модели (#98, PR #121), шов 4 — команда
экзамена на подставной модели. Пакет эксперту и подставная модель — tests/signal_card_support.py.
"""

import json

import pytest

from oiltech_digest import cli, config, signal_card
from tests.signal_card_support import (MINI, NANO, STRATA, answer, by_stratum, pack_rows, run_exam, strata, write_pack)


def _reports(folder):
    return set(folder.glob("signal_card_exam_report_*.json"))


def test_rescore_counts_a_saved_report_again_without_model_calls(tmp_path, monkeypatch, strata, capsys):
    # Допуски, алиасы и правила счёта правят после прогона: пересчёт тех же ответов не платит модели второй раз.
    rows = pack_rows(useful={"Компании-участники": "С.Л.Б. Сервисез"})
    markup = write_pack(tmp_path, rows)
    _, first = run_exam(monkeypatch, markup, {NANO: by_stratum(strata, useful=answer(region="глобально"))},
                        "--models", NANO)
    [saved] = _reports(tmp_path)
    tolerated = {item["id"]: {"region": ["глобально"]} for item in rows if item["stratum"] == "useful"}
    (tmp_path / "signal_card_exam_tolerances.json").write_text(json.dumps(tolerated), encoding="utf-8")
    (tmp_path / "signal_card_exam_aliases.json").write_text(json.dumps(
        [{"name": "С.Л.Б. Сервисез", "aliases": ["SLB"]}], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(signal_card, "make_client", lambda: pytest.fail("пересчёт вызвал модель"))
    monkeypatch.setattr(config, "price_for_model", lambda model: (100.0, 100.0))  # ставки с тех пор поменялись

    cli.main(["signal-card-exam", "--markup", str(markup), "--rescore", str(saved)])

    [again] = _reports(tmp_path) - {saved}
    report = json.loads(again.read_text(encoding="utf-8"))
    assert report["rescored_from"] == saved.name
    [before], [after] = first["models"], report["models"]
    assert after["answers"] == before["answers"]
    assert after["cost_usd"] == before["cost_usd"]  # цена — исходного прогона по его ставкам, новых вызовов нет
    useful = (before["lenient"]["by_stratum"]["useful"], after["lenient"]["by_stratum"]["useful"])
    assert [one["region"]["agent"] for one in useful] == [0.0, 1.0]  # новые допуски
    assert [one["companies_recall"]["matched"] for one in useful] == [0, 17 * 3]  # новые алиасы
    assert str(again.with_suffix(".md")) in capsys.readouterr().out
    assert f"Пересчёт отчёта {saved.name} без вызовов модели" in again.with_suffix(".md").read_text(encoding="utf-8")


def test_rescore_refuses_answers_from_another_pack_or_dictionaries(tmp_path, monkeypatch, strata):
    markup = write_pack(tmp_path, pack_rows())
    run_exam(monkeypatch, markup, {NANO: by_stratum(strata)}, "--models", NANO)
    [saved] = _reports(tmp_path)
    report = json.loads(saved.read_text(encoding="utf-8"))
    # Ответы относятся к текстам своего манифеста и к своей версии справочников: с другими пересчёт неверен.
    for name, broken in (("manifest", {**report, "manifest": {**report["manifest"], "sha256": "0" * 64}}),
                         ("dictionaries", {**report, "dictionaries_version": "v0"})):
        other = tmp_path / f"other_{name}.json"
        other.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(SystemExit, match="манифест" if name == "manifest" else "справочник"):
            cli.main(["signal-card-exam", "--markup", str(markup), "--rescore", str(other)])
    assert _reports(tmp_path) == {saved}


def test_rescore_refuses_a_broken_report_by_name_and_writes_nothing(tmp_path, monkeypatch, strata):
    markup = write_pack(tmp_path, pack_rows())
    run_exam(monkeypatch, markup, {NANO: by_stratum(strata)}, "--models", NANO)
    [saved] = _reports(tmp_path)
    report = json.loads(saved.read_text(encoding="utf-8"))
    [model] = report["models"]
    # Отчёт — файл на диске, его правят руками: битое устройство — отказ с именем файла, а не трасса.
    broken = [{key: value for key, value in report.items() if key != "split"},
              {**report, "manifest": "signal_card_markup_manifest.json"},
              {**report, "models": [{"model": model["model"]}]},
              {**report, "models": [{**model, "answers": [{"run": 1}]}]},
              {**report, "models": [{**model, "answers": [{**model["answers"][0], "id": "article:999"}]}]},
              {key: value for key, value in report.items() if key != "runs"}]
    for number, content in enumerate(broken):
        other = tmp_path / f"broken_{number}.json"
        other.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(SystemExit, match=other.name):
            cli.main(["signal-card-exam", "--markup", str(markup), "--rescore", str(other)])
    assert _reports(tmp_path) == {saved}


def test_rescore_takes_models_runs_and_part_from_the_report(tmp_path, monkeypatch, strata):
    markup = write_pack(tmp_path, pack_rows())
    run_exam(monkeypatch, markup, {NANO: by_stratum(strata)}, "--models", NANO)
    [saved] = _reports(tmp_path)
    for extra in (["--dry-run"], ["--models", MINI], ["--runs", "1"], ["--part", "exam"]):
        with pytest.raises(SystemExit, match="--rescore"):
            cli.main(["signal-card-exam", "--markup", str(markup), "--rescore", str(saved), *extra])
    assert _reports(tmp_path) == {saved}


def test_rescore_says_when_the_markup_changed_and_dearer_models_were_not_tried(tmp_path, monkeypatch, strata):
    markup = write_pack(tmp_path, pack_rows())
    run_exam(monkeypatch, markup, {NANO: by_stratum(strata)}, "--models", f"{NANO},{MINI}")  # nano прошла — стоп
    [saved] = _reports(tmp_path)
    # Эксперт поправил разметку: у всех строк другой регион — nano не проходит, а mini в прогоне не было.
    write_pack(tmp_path, pack_rows(**{stratum: {"Регион": "Европа"} for stratum in STRATA}))
    monkeypatch.setattr(signal_card, "make_client", lambda: pytest.fail("пересчёт вызвал модель"))

    cli.main(["signal-card-exam", "--markup", str(markup), "--rescore", str(saved)])

    [again] = _reports(tmp_path) - {saved}
    report = json.loads(again.read_text(encoding="utf-8"))
    assert (report["decision"]["model"], report["markup_changed"], report["original_decision"]) == (None, True, NANO)
    text = again.with_suffix(".md").read_text(encoding="utf-8")
    assert "Разметка изменилась после исходного прогона" in text
    assert f"Исходный прогон остановился на {NANO}: модели дороже не пробовались" in text
