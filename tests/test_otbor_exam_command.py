"""Экзамен агента «Отбор» (#133, спека #128), шов 2 — команда: сухой прогон и отказы до оплаты, контроль с ядра
(размеры страт и категория судьи), оценка на весь поток ленты и цена, полная таблица и устаревшие модели, пересчёт без
вызовов модели, читаемый отчёт и вход агента такой же, как у ядра. Пакет и разметка — otbor_exam_support."""

import json
import re

import pytest

from oiltech_digest import cli, config, otbor, otbor_exam
from oiltech_digest.otbor_pack_format import MANIFEST_NAME, load_dictionaries
from tests.otbor_exam_support import (FULL, LUNA, MINI, NANO, ROWS_NAME, ExamModel, always_take, oracle, part,
                                      run_exam, write_rows)
from tests.otbor_exam_support import exam_pack  # noqa: F401 — фикстура
from tests.otbor_pack_support import fill
from tests.otbor_pack_support import pack  # noqa: F401 — фикстура

DASHES = re.compile("[–—]")


def test_dry_run_prints_the_split_calls_and_price_and_writes_nothing(exam_pack, monkeypatch, capsys):
    out = exam_pack["out"]
    before = sorted(path.name for path in out.iterdir())
    model = ExamModel({})
    monkeypatch.setattr(otbor, "make_client", lambda: model)
    # Цена входа — ноль, выхода — круглая и в прежнем порядке: в цене остаётся только оценка ответа (600 токенов
    # на вызов).
    prices = {"gpt-6-luna": 0.5, "gpt-5.6-luna": 0.8, NANO: 1.0, MINI: 4.0, FULL: 10.0}
    for name, price in prices.items():
        monkeypatch.setitem(config.OPENAI_MODEL_PRICES, name, (0.0, price))

    cli.main(["otbor-exam", "--markup", str(exam_pack["markup"]), "--dry-run"])

    printed = capsys.readouterr().out
    assert model.calls == []
    assert sorted(path.name for path in out.iterdir()) == before  # ни деления, ни отчёта
    assert "деление новое, запишется при первом прогоне" in printed
    assert "настройка 32:" in printed and "экзамен 84:" in printed and "страницы-списки 1" in printed
    assert "Утверждено: утверждено владельцем" in printed
    assert "публикаций 84, прогонов на публикацию 3" in printed
    # Лестница по умолчанию — кандидаты спеки и самые дешёвые из новых, по цене.
    ladder = [line.split(":")[0].strip() for line in printed.splitlines() if ": вызовов 252" in line]
    assert ladder == list(prices)
    assert "gpt-6-luna: вызовов 252" in printed and "≈ $0.076" in printed  # 252 × 600 токенов × $0,5 за 1 млн
    assert f"{NANO}: вызовов 252" in printed and "≈ $0.151" in printed
    assert f"{FULL}: вызовов 252" in printed and "≈ $1.51" in printed
    assert "Не больше: вызовов 1260, ≈ $2.46" in printed
    assert not DASHES.search(printed)


def test_the_exam_refuses_before_any_paid_call(exam_pack, monkeypatch):
    out, markup = exam_pack["out"], exam_pack["markup"]
    model = ExamModel({NANO: oracle(exam_pack)})
    monkeypatch.setattr(otbor, "make_client", lambda: model)

    def refused(match, *args):
        with pytest.raises(SystemExit, match=match):
            cli.main(["otbor-exam", "--markup", str(markup), *args])

    # Экзамен — три прогона на строку (спека); меньше — только на настройке.
    for runs in ("0", "1"):
        refused("--runs", "--runs", runs)
    # Модель без своей строки в таблице цен: лестница по цене и расход в отчёте солгали бы.
    refused("цены gpt-7-mega нет", "--models", f"gpt-7-mega,{NANO}")
    # Без отметки утверждения прогона нет: первый же прогон, даже настройки, фиксирует деление.
    lists = {key: exam_pack["rows"][key] for key in ("list_pages", "in_instruction")}
    write_rows(out, lists, approved=None)
    refused("approved", "--part", "tuning", "--runs", "1", "--models", NANO)
    assert not (out / "otbor_exam_split.json").exists()
    # Экзамен — только на утверждённых правилах: отметка о прежних инструкции и справочниках его не пускает.
    write_rows(out, lists, approved="утверждено владельцем: инструкция 2026-10-01.1, справочники 2026-10-01.1")
    refused("не называет текущие правила", "--models", NANO)
    assert not (out / "otbor_exam_split.json").exists()
    write_rows(out, lists)
    # Строки экзамена — решение владельца: без файла, с битым файлом и с чужими строками экзамен не идёт.
    rows = json.loads((out / ROWS_NAME).read_text(encoding="utf-8"))
    (out / ROWS_NAME).unlink()
    refused(ROWS_NAME)
    (out / ROWS_NAME).write_text(json.dumps({"list_pages": "signal:1"}), encoding="utf-8")
    refused("нужны списки")
    write_rows(out, {**rows, "list_pages": ["signal:999999"]})
    refused("signal:999999")
    write_rows(out, rows)
    # Битая выгрузка с ядра — до вызовов модели.
    (out / "otbor_exam_control.json").write_text('{"strata": {"feed_rejected": "много"}}', encoding="utf-8")
    refused("otbor_exam_control.json")
    (out / "otbor_exam_control.json").unlink()
    # Разметка с нарушениями — сначала otbor-markup-check.
    number = next(item["id"] for item in exam_pack["items"] if item["group"] == "feed_accepted")
    broken = fill(out, {number: {"Брать или нет": None}}, name="otbor_markup_broken.xlsx")
    with pytest.raises(SystemExit, match="нарушениями"):
        cli.main(["otbor-exam", "--markup", str(broken), "--manifest", str(out / MANIFEST_NAME)])
    # Коды справочников не те, из которых выбирал эксперт: метки несравнимы.
    current = load_dictionaries()
    extra = {**current, "reject_reason": {**current["reject_reason"], "values": [
        *current["reject_reason"]["values"], {"code": "listing_page", "label": "Страница-список", "rule": "",
                                              "examples": []}]}}
    monkeypatch.setattr(otbor_exam, "load_dictionaries", lambda: extra)
    refused("коды справочников")
    assert model.calls == []
    assert not list(out.glob("otbor_exam_report_*"))


def test_core_control_counts_the_feed_strata_of_the_pack_window_and_judged_categories(pack, capsys):
    capsys.readouterr()

    cli.main(["otbor-exam-control", "--manifest", str(pack["out"] / MANIFEST_NAME)])

    data = json.loads(capsys.readouterr().out)
    # Кандидаты выборки в окне пакета: в синтетике их ровно по квоте — 50 отклонённых и 30 принятых гейтом (статьи до
    # окна, без решения гейта, с архивного источника, страницы блокировки, перепечатки и прежние строки — не в счёт).
    assert data["strata"] == {"feed_rejected": 50, "feed_accepted": 30}
    radar = {item["id"]: item for item in pack["items"] if item["origin"] == "radar"}
    assert set(data["rows"]) == set(radar)
    categories = {one["signal_category"] for one in data["rows"].values()}
    assert categories == {"technology", "business", "other"}  # судья ставил все три (otbor_pack_support.radar)
    review = next(number for number, item in radar.items() if item.get("kind") == "review")
    assert data["rows"][review]["signal_category"] == "business"


def test_stream_estimate_category_control_and_monthly_price_come_from_the_core_control(exam_pack, monkeypatch):
    out = exam_pack["out"]
    radar = [item["id"] for item in exam_pack["items"] if item["origin"] == "radar"]
    # Судья у одной взятой находки ошибся категорией (у эксперта «технология»).
    wrong = next(number for number in radar if exam_pack["codes"][number]["decision"] == "accept"
                 and exam_pack["codes"][number]["category"] == "technology"
                 and next(item for item in exam_pack["items"] if item["id"] == number)["group"] == "radar_links")
    rows = {number: {"signal_category": "business" if number == wrong else "technology"} for number in radar}
    (out / "otbor_exam_control.json").write_text(json.dumps({"strata": {"feed_rejected": 2000, "feed_accepted": 3000},
                                                             "rows": rows}), encoding="utf-8")
    monkeypatch.setitem(config.OPENAI_MODEL_PRICES, LUNA, (1.0, 0.0))  # 2000 токенов входа на вызов — $0.002

    _, report = run_exam(monkeypatch, exam_pack["markup"], {LUNA: oracle(exam_pack)})

    [agent] = report["models"]
    exam = part(report, exam_pack)
    feed = [one for one in exam if one["group"] in ("feed_rejected", "feed_accepted")]
    weight = {group: size / sum(one["group"] == group for one in feed)
              for group, size in (("feed_rejected", 2000), ("feed_accepted", 3000))}
    taken = [one for one in feed if one["codes"]["decision"] == "accept"]
    stream = agent["scores"]["stream"]
    assert stream["strata"] == {"feed_rejected": 2000, "feed_accepted": 3000}
    assert stream["agent"]["accept"] == stream["agent"]["reject"] == 1.0
    # Гейт на всём потоке: из взятых экспертом (по весам страт) он взял только принятые им.
    assert stream["gate"]["accept"] == pytest.approx(
        sum(weight[one["group"]] for one in taken if one["group"] == "feed_accepted")
        / sum(weight[one["group"]] for one in taken))
    assert stream["expert_accept"] == pytest.approx(sum(weight[one["group"]] for one in taken) / 5000)
    assert stream["agent_accept"] == pytest.approx(stream["expert_accept"])
    assert stream["gate_accept"] == pytest.approx(0.6)
    # Контроль категории: статьи сбора — «бизнес», находки — судья (одна ошибка у взятых обоими).
    control_rows = [one for one in exam if one["codes"]["decision"] == "accept"
                    and one["group"] in ("feed_accepted", "card_markup", "radar_links")]
    control = agent["scores"]["labels"]["category"]["control"]
    assert control["n"] == len(control_rows)
    assert control["agent"] == pytest.approx((len(control_rows) - (wrong in report["split"]["exam"])) / len(control_rows))
    # Цена: на публикацию и на ленту за 30 дней (5000 публикаций).
    text = next(out.glob("otbor_exam_report_*.md")).read_text(encoding="utf-8")
    assert "| gpt-6-luna | прошла |" in text and "| $0.0020 | $10.00 |" in text


def test_rescore_recomputes_a_saved_report_without_model_calls(exam_pack, monkeypatch):
    markup = exam_pack["markup"]
    _, first = run_exam(monkeypatch, markup, {NANO: always_take, MINI: oracle(exam_pack)})
    [saved] = sorted(markup.parent.glob("otbor_exam_report_*.json"))
    # Владелец добавил страницу-список: метрика решения пересчитывается по тем же ответам.
    rows = exam_pack["rows"]
    page = next(one["id"] for one in part(first, exam_pack) if one["group"] == "radar_links")
    write_rows(markup.parent, {**rows, "list_pages": [*rows["list_pages"], page]})
    model = ExamModel({})
    monkeypatch.setattr(otbor, "make_client", lambda: model)

    cli.main(["otbor-exam", "--markup", str(markup), "--rescore", str(saved)])

    assert model.calls == []
    [second] = [json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(markup.parent.glob("otbor_exam_report_*.json")) if path != saved]
    assert second["rescored_from"] == saved.name
    assert [(one["model"], one["cost_usd"], one["calls"]) for one in second["models"]] == [
        (one["model"], one["cost_usd"], one["calls"]) for one in first["models"]]
    assert second["models"][1]["scores"]["list_pages"]["rows"] == first["models"][1]["scores"]["list_pages"]["rows"] + 1
    assert second["decision"]["model"] == MINI
    # Строки экзамена сменились после того, как ответы увидели: решение по ним — не то, что утверждали.
    assert second["rules_changed"] == ["rows"]
    readable = sorted(markup.parent.glob("otbor_exam_report_*.md"))[-1]
    assert "Правила изменились после исходного прогона" in readable.read_text(encoding="utf-8")

    # Ответы на других справочниках не пересчитываются; модели и прогоны берутся из отчёта.
    current = load_dictionaries()
    monkeypatch.setattr(otbor_exam, "load_dictionaries", lambda: {**current, "version": "2099-01-01.1"})
    with pytest.raises(SystemExit, match="справочниках"):
        cli.main(["otbor-exam", "--markup", str(markup), "--rescore", str(saved)])
    with pytest.raises(SystemExit, match="--rescore"):
        cli.main(["otbor-exam", "--markup", str(markup), "--rescore", str(saved), "--models", NANO])


def test_readable_report_lies_next_to_the_manifest_with_the_quality_and_price_table(exam_pack, monkeypatch, capsys):
    _, report = run_exam(monkeypatch, exam_pack["markup"], {LUNA: oracle(exam_pack)})

    [readable] = exam_pack["out"].glob("otbor_exam_report_*.md")
    assert str(readable) in capsys.readouterr().out
    text = readable.read_text(encoding="utf-8")
    assert "Решение: **gpt-6-luna**: самая дешёвая из прошедших правило и пороги" in text
    assert "## Качество и цена" in text and "| gpt-6-luna | прошла | 100% |" in text
    assert "| лента, случайные статьи (контроль: гейт): «брать» не хуже контроля | 100% |" in text
    assert "| сбалансированная доля, все строки, не ниже 80% | 100% | 80% | ✓ |" in text
    assert "Страницы-списки (вне метрики решения): строк 1, эксперт взял 1, модель взяла 100% ответов." in text
    assert "Контроля с ядра нет" in text
    assert not DASHES.search(text)


def test_the_exam_gives_the_agent_the_input_the_core_would(pack):
    # Мерится тот вход, что модель получит на ядре: статья ленты и находка со ссылками — из базы через ядро и из
    # манифеста через экзамен.
    article = next(item for item in pack["items"] if item["group"] == "feed_rejected")
    finding = next(item for item in pack["items"] if item["group"] == "radar_links" and len(item["links"]) > 2)
    core = otbor.build_payload({"article_ids": [int(article["article_id"])],
                                "signal_ids": [int(finding["signal_id"])]})["items"]
    core_article, core_finding = core

    assert otbor.model_input(otbor_exam.model_item(article)) == otbor.model_input(core_article)
    assert otbor.model_input(otbor_exam.model_item(finding)) == otbor.model_input(core_finding)


def test_all_models_give_the_full_table_and_a_deprecated_model_is_measured_but_not_chosen(exam_pack, monkeypatch):
    markup = exam_pack["markup"]
    every = {NANO: oracle(exam_pack), MINI: oracle(exam_pack), FULL: oracle(exam_pack)}

    # С --all идут все модели лестницы: полная таблица качества и цены для владельца.
    model, report = run_exam(monkeypatch, markup, every, "--all")
    assert [one["model"] for one in report["models"]] == [NANO, MINI, FULL]
    assert all(one["passed"] for one in report["models"])
    # gpt-5.4-nano прошла, но поставщик пометил её устаревшей: решение — следующая по цене.
    assert report["decision"]["model"] == MINI
    assert report["deprecated"] == {NANO: otbor_exam.DEPRECATED[NANO]}

    # Без --all лестница идёт дальше устаревшей и останавливается на первой прошедшей.
    model, report = run_exam(monkeypatch, markup, every)
    assert [one["model"] for one in report["models"]] == [NANO, MINI]
    assert {call["model"] for call in model.calls} == {NANO, MINI}

    # Прошла только устаревшая — решения нет, причина в отчёте.
    _, report = run_exam(monkeypatch, markup, {NANO: oracle(exam_pack), MINI: always_take})
    assert report["decision"]["model"] is None
    assert "устаревшие" in report["decision"]["reason"]
    text = sorted(markup.parent.glob("otbor_exam_report_*.md"))[-1].read_text(encoding="utf-8")
    assert "gpt-5.4-nano (устаревшая)" in text


def test_parallel_calls_give_the_same_answers_and_scores(exam_pack, monkeypatch):
    markup = exam_pack["markup"]
    _, alone = run_exam(monkeypatch, markup, {LUNA: oracle(exam_pack)})
    model, parallel = run_exam(monkeypatch, markup, {LUNA: oracle(exam_pack)}, "--workers", "4")

    # Вызовы делятся между потоками, а ответы ложатся по строкам, как при одном потоке: счёт тот же.
    assert len(model.calls) == len(parallel["split"]["exam"]) * 3
    assert [(one["run"], one["id"]) for one in parallel["models"][0]["answers"]] == [
        (one["run"], one["id"]) for one in alone["models"][0]["answers"]]
    assert parallel["models"][0]["scores"] == alone["models"][0]["scores"]
    with pytest.raises(SystemExit, match="--workers"):
        cli.main(["otbor-exam", "--markup", str(markup), "--workers", "0", "--dry-run"])
