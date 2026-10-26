"""Экзамен агента «Отбор» (#133, спека #128), шов 2 — команда экзамена на подставной модели.

Пакет выгружает otbor-expert-pack на изолированной базе, разметка — как у эксперта (otbor_exam_support): 116 строк,
из них взято экспертом 63. Решение машины — по группе манифеста, как в разборе разметки 23.10.
"""

from collections import defaultdict
import json

import pytest

from tests.otbor_exam_support import FULL, LUNA, MINI, NANO, always_take, oracle, part, run_exam, write_rows
from tests.otbor_exam_support import exam_pack  # noqa: F401 — фикстура
from tests.otbor_pack_support import pack  # noqa: F401 — фикстура

FEED = ("feed_rejected", "feed_accepted")


def test_an_agent_that_answers_like_the_expert_passes_with_the_gate_chain_and_constant_alongside(exam_pack,
                                                                                                  monkeypatch):
    model, report = run_exam(monkeypatch, exam_pack["markup"], {LUNA: oracle(exam_pack)})

    exam = part(report, exam_pack)
    assert len(model.calls) == len(exam) * 3  # три прогона на строку экзамена
    assert {call["model"] for call in model.calls} == {LUNA}
    assert report["decision"]["model"] == LUNA
    [agent] = report["models"]
    decision = agent["scores"]["decision"]

    feed = [one for one in exam if one["group"] in FEED]
    taken = [one for one in feed if one["codes"]["decision"] == "accept"]
    refused = [one for one in feed if one["codes"]["decision"] == "reject"]
    gate = decision["feed_random"]
    assert (gate["accept_rows"], gate["reject_rows"]) == (len(taken), len(refused))
    assert gate["agent"]["accept"] == gate["agent"]["reject"] == gate["agent"]["balanced"] == 1.0
    # Гейт из взятых экспертом взял только принятые им, из отклонённых экспертом отклонил только отклонённые им.
    assert gate["control"]["accept"] == pytest.approx(sum(one["group"] == "feed_accepted" for one in taken) / len(taken))
    assert gate["control"]["reject"] == pytest.approx(
        sum(one["group"] == "feed_rejected" for one in refused) / len(refused))
    assert gate["constant"]["always_accept"] == pytest.approx(len(taken) / len(feed))

    # Цепочка — на строках с решением машины: без материалов коллег и без страниц-списков.
    machine = [one for one in exam if one["group"] != "materials" and one["id"] not in exam_pack["rows"]["list_pages"]]
    chain = decision["chain"]
    assert chain["accept_rows"] + chain["reject_rows"] == len(machine)
    refused_by_expert = [one for one in machine if one["codes"]["decision"] == "reject"]
    assert chain["control"]["reject"] == pytest.approx(
        sum(one["group"] in ("feed_rejected", "radar_filtered") for one in refused_by_expert) / len(refused_by_expert))

    # Судья на радаре — только класс «брать»: «не брать» у радара отдельно не сравнивается (решение 29).
    assert [check["name"] for check in agent["scores"]["rule"]] == [
        "feed_random:accept", "feed_random:reject", "radar:accept", "chain:accept", "chain:reject", "balanced"]
    assert all(check["passed"] for check in agent["scores"]["rule"])
    assert agent["passed"] is True and agent["failed"] == []


def test_the_cheapest_model_that_passes_is_chosen_and_the_next_is_tried_only_after_a_failure(exam_pack,
                                                                                            monkeypatch):
    markup = exam_pack["markup"]
    model, report = run_exam(monkeypatch, markup, {NANO: always_take, MINI: oracle(exam_pack)})

    nano, mini = report["models"]
    assert (nano["model"], mini["model"]) == (NANO, MINI)
    # «Всегда брать» берёт всё взятое экспертом и ничего не отклоняет: класс «не брать» хуже гейта и цепочки.
    assert nano["scores"]["decision"]["feed_random"]["agent"]["accept"] == 1.0
    assert nano["scores"]["decision"]["feed_random"]["agent"]["reject"] == 0.0
    assert {"feed_random:reject", "chain:reject", "balanced"} <= set(nano["failed"])
    assert mini["passed"] is True
    assert report["decision"]["model"] == MINI

    # Не прошла ни одна — решения нет, причины в отчёте.
    _, report = run_exam(monkeypatch, markup, {NANO: always_take, MINI: always_take, FULL: always_take})
    assert report["decision"]["model"] is None
    assert [one["model"] for one in report["models"]] == [NANO, MINI, FULL]
    assert "сбалансированная" in report["decision"]["reason"]

    # Лестница — по цене, в каком порядке модели ни назови: прошла самая дешёвая — до mini не доходит.
    model, report = run_exam(monkeypatch, markup, {LUNA: oracle(exam_pack), MINI: oracle(exam_pack)},
                             "--models", f"{MINI},{LUNA}")
    assert report["decision"]["model"] == LUNA and {call["model"] for call in model.calls} == {LUNA}


def test_split_is_fixed_at_the_first_run_and_instruction_examples_stay_in_tuning(exam_pack, monkeypatch):
    out, rows = exam_pack["out"], exam_pack["rows"]
    _, tuning = run_exam(monkeypatch, exam_pack["markup"], {NANO: oracle(exam_pack)},
                         "--part", "tuning", "--runs", "1", "--models", NANO)

    split = json.loads((out / "otbor_exam_split.json").read_text(encoding="utf-8"))
    assert sorted(split["tuning"] + split["exam"]) == sorted(item["id"] for item in exam_pack["items"])
    # Строки, которые стоят примерами в инструкции, — только в настройке: иначе модель видит ответ в инструкции.
    assert set(rows["in_instruction"]) <= set(split["tuning"])
    # Доли «группа × решение эксперта», страницы-списки — своя доля; доля от двух строк — в обеих частях.
    shares = defaultdict(lambda: {"tuning": 0, "exam": 0})
    for name in ("tuning", "exam"):
        for one in part({"split": split}, exam_pack, name):
            stratum = "list_page" if one["id"] in rows["list_pages"] else f"{one['group']}:{one['codes']['decision']}"
            shares[stratum][name] += 1
    assert all(counts["tuning"] and counts["exam"] for counts in shares.values()
               if counts["tuning"] + counts["exam"] >= 2), dict(shares)
    # Четверть от 116 по долям (наибольшие остатки) — 29, и ещё по строке трёх долей из двух строк (старые события,
    # страницы-списки, материалы), которым по четверти не досталось ни одной.
    assert len(split["tuning"]) == 32
    assert tuning["decision"]["model"] is None  # настройка модель не выбирает

    # Тот же набор — то же деление, даже если зерно манифеста сменилось: деление записано при первом прогоне.
    manifest_path = out / "otbor_markup_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(json.dumps({**manifest, "seed": 1}, ensure_ascii=False), encoding="utf-8")
    _, exam = run_exam(monkeypatch, exam_pack["markup"], {NANO: oracle(exam_pack)})
    assert exam["split"] == {"tuning": split["tuning"], "exam": split["exam"]}

    # Строка экзамена, ставшая примером инструкции, — отказ до вызовов: инструкцию правят только по настройке.
    write_rows(out, {**rows, "in_instruction": [*rows["in_instruction"], split["exam"][0]]})
    with pytest.raises(SystemExit, match="примеры инструкции"):
        run_exam(monkeypatch, exam_pack["markup"], {NANO: oracle(exam_pack)})


def test_list_pages_are_out_of_the_decision_metric_and_counted_apart(exam_pack, monkeypatch):
    pages = exam_pack["rows"]["list_pages"]
    # Эксперт взял страницы-списки фоном, модель их отклоняет: метрика решения от этого не меняется (решение 29).
    refuse = {number: {"decision": "reject", "reject_reason": "corporate_promo"} for number in pages}

    _, report = run_exam(monkeypatch, exam_pack["markup"], {NANO: oracle(exam_pack, refuse)})

    [nano] = report["models"]
    scores = nano["scores"]
    assert scores["decision"]["all"]["agent"]["accept"] == scores["decision"]["all"]["agent"]["reject"] == 1.0
    in_exam = [number for number in pages if number in report["split"]["exam"]]
    assert len(in_exam) == 1  # доля из двух строк: одна в настройке, одна на экзамене
    assert scores["list_pages"] == {"rows": 1, "expert_accept": 1, "agent_accept": 0.0,
                                    "by_row": {in_exam[0]: ["reject"] * 3}}
    assert nano["passed"] is True


def test_labels_are_scored_where_they_mean_something(exam_pack, monkeypatch):
    _, first = run_exam(monkeypatch, exam_pack["markup"], {NANO: oracle(exam_pack)})
    exam = part(first, exam_pack)
    business = next(one for one in exam if one["codes"]["category"] == "business")
    transferable = next(one for one in exam if one["codes"]["oilfield_relevance"] == "transferable"
                        and one["group"] == "feed_rejected")
    no_link = [one for one in exam if one["codes"]["reject_reason"] == "oil_gas_no_link"]
    changes = {
        business["id"]: {"category": "technology"},  # не та категория у взятой обоими
        # Эксперт взял (перенос), модель отклонила: категории у неё нет — в метрику категории строка не идёт.
        transferable["id"]: {"decision": "reject", "reject_reason": "other_industry_no_transfer",
                             "oilfield_relevance": "none", "oilfield_application": ""},
        no_link[0]["id"]: {"reject_reason": "incident_war_politics"},  # не та причина у отклонённой обоими
        # Эксперт отклонил, модель взяла: причины у неё нет — в метрику причины строка не идёт.
        no_link[1]["id"]: {"decision": "accept", "reject_reason": "none", "oilfield_relevance": "direct"},
    }

    _, report = run_exam(monkeypatch, exam_pack["markup"], {NANO: oracle(exam_pack, changes)})

    labels = report["models"][0]["scores"]["labels"]
    taken_by_both = [one for one in exam if one["codes"]["decision"] == "accept" and one["id"] != transferable["id"]]
    refused_by_both = [one for one in exam if one["codes"]["decision"] == "reject" and one["id"] != no_link[1]["id"]]
    assert labels["category"]["n"] == len(taken_by_both) * 3
    assert labels["category"]["agent"] == pytest.approx((len(taken_by_both) - 1) / len(taken_by_both))
    assert labels["reject_reason"]["n"] == len(refused_by_both) * 3
    assert labels["reject_reason"]["agent"] == pytest.approx((len(refused_by_both) - 1) / len(refused_by_both))
    assert labels["industry"]["agent"] == 1.0
    # Связь — средняя по значениям эксперта: у переносимой и у «нет связи» по одной строке мимо.
    by_value = labels["oilfield_relevance"]["by_value"]
    count = lambda code: sum(one["codes"]["oilfield_relevance"] == code for one in exam)  # noqa: E731
    assert by_value == pytest.approx({"direct": 1.0, "transferable": 1 - 1 / count("transferable"),
                                      "none": 1 - 1 / count("none")})
    # Константа — самое частое значение эксперта на тех же строках.
    assert labels["category"]["constant_value"] == "technology"
    assert labels["category"]["constant"] == pytest.approx(
        sum(one["codes"]["category"] == "technology" for one in exam if one["codes"]["decision"] == "accept")
        / sum(one["codes"]["decision"] == "accept" for one in exam))
    # Контроль категории без выгрузки с ядра — только у статей сбора, взятых гейтом: «статья сбора всегда бизнес».
    assert labels["category"]["control"]["agent"] == 1.0
    assert labels["category"]["passed"] is True and labels["category"]["threshold"] == 0.80


def test_a_failure_is_retried_once_an_answer_off_the_rules_is_a_miss_and_conflicts_are_disputed(exam_pack,
                                                                                                monkeypatch):
    _, first = run_exam(monkeypatch, exam_pack["markup"], {NANO: oracle(exam_pack)})
    flaky, invalid, conflicting = [one for one in part(first, exam_pack) if one["codes"]["decision"] == "accept"][:3]
    failed_once = []

    def answer(title):
        number = exam_pack["titles"][title]
        if number == flaky["id"] and not failed_once:
            failed_once.append(title)
            return RuntimeError("timeout")
        if number == invalid["id"]:
            return {**oracle(exam_pack)(title), "industry": "energy"}  # не из справочника
        if number == conflicting["id"]:
            return {**oracle(exam_pack)(title), "reject_reason": "old_event"}  # «брать» с причиной отказа
        return oracle(exam_pack)(title)

    model, report = run_exam(monkeypatch, exam_pack["markup"], {NANO: answer})

    [nano] = report["models"]
    calls = [call["title"] for call in model.calls]
    title = {one["id"]: one["title"] for one in exam_pack["items"]}
    # Сбой — одна повторная попытка (обе оплачены); ответ не по правилам не повторяется; конфликт шагов — повтор
    # внутри «Отбора», как на воркере.
    assert calls.count(title[flaky["id"]]) == 4
    assert calls.count(title[invalid["id"]]) == 3
    assert calls.count(title[conflicting["id"]]) == 6
    assert nano["calls"] == len(model.calls)
    retried = [one for one in nano["answers"] if one["id"] == flaky["id"]]
    assert [one["attempts"] for one in retried] == [2, 1, 1] and retried[0]["model_calls"] == 2
    answers = nano["scores"]["answers"]
    assert (answers["failures"], answers["disputed"]) == (3, 3)
    # Ответ не по правилам — промах в метрике решения: «брать» у эксперта, решения нет.
    taken = nano["scores"]["decision"]["all"]
    assert taken["agent"]["accept"] == pytest.approx(1 - 1 / taken["accept_rows"])


def test_on_radar_rows_the_take_class_is_compared_with_the_judge(exam_pack, monkeypatch):
    _, first = run_exam(monkeypatch, exam_pack["markup"], {NANO: oracle(exam_pack)})
    radar = [one for one in part(first, exam_pack) if one["origin"] == "radar"
             and one["id"] not in exam_pack["rows"]["list_pages"]]
    taken = [one for one in radar if one["codes"]["decision"] == "accept"]
    # Модель отклоняет все находки, которые эксперт взял: на радаре «брать» хуже судьи.
    refuse = {one["id"]: {"decision": "reject", "reject_reason": "corporate_promo"} for one in taken}

    _, report = run_exam(monkeypatch, exam_pack["markup"], {NANO: oracle(exam_pack, refuse)})

    [nano] = report["models"]
    judge = nano["scores"]["decision"]["radar"]
    # Судья взял неотсеянные находки, а «Отсеянные» отклонил.
    assert judge["control"]["accept"] == pytest.approx(
        sum(one["group"] != "radar_filtered" for one in taken) / len(taken))
    assert judge["agent"]["accept"] == 0.0
    assert "radar:accept" in nano["failed"]
    assert not any(check["name"] == "radar:reject" for check in nano["scores"]["rule"])
