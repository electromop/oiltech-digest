"""Экзамен «Карточки» (#133, спека #128), шов 2 — команды: выгрузка с ядра на изолированной базе, отказы до оплаты,
вход второго вызова такой же, как у ядра, сбои, сухой прогон, пересчёт без вызовов, полная таблица и читаемый отчёт.
Пакет и разметка — card_scores_exam_support."""

import json
import re

import pytest

from oiltech_digest import card_scores_exam, card_scores_exam_calls, cli, config, signal_card, signal_scores
from oiltech_digest.db import marks, repository
from oiltech_digest.otbor_pack_format import MANIFEST_NAME, load_rubrics
from tests.card_scores_exam_support import (CARD, CONTROL_NAME, LUNA, MINI, SUMMARY, ScoresModel, add_strong,
                                            approve_scores, oracle_card, oracle_scores, part_rows, run_exam)
from tests.card_scores_exam_support import scores_pack  # noqa: F401 — фикстура
from tests.otbor_exam_support import ROWS_NAME, write_rows
from tests.otbor_pack_support import pack  # noqa: F401 — фикстура
from tests.otbor_pack_support import radar

DASHES = re.compile("[–—]")


def test_core_export_gives_criteria_tags_control_and_strong_signals_beyond_the_pack(pack, capsys):
    corpus, items = pack["corpus"], pack["items"]
    strong = add_strong(corpus)
    with repository.get_connection() as conn:
        # Тег ленты бывает подтегом: контроль — его корневая тематика.
        drilling = conn.execute("SELECT id FROM tags WHERE name = 'Бурение'").fetchone()[0]
        child = conn.execute("INSERT INTO tags (name, parent_id) VALUES ('Долота', %s) RETURNING id",
                             (drilling,)).fetchone()[0]
        feed = next(item for item in items if item["group"] == "feed_accepted")
        conn.execute("UPDATE article_tags SET tag_id = %s WHERE article_id = %s", (child, feed["article_id"]))
        conn.execute("UPDATE article_scores SET total_score = 81.5 WHERE article_id = %s", (feed["article_id"],))
        conn.commit()
    # Эталон заказчика в памяти радара: ссылка находки — среди его ссылок (адрес записан иначе).
    reference = radar("reference-hit", links=1)
    repository.upsert_signal_agent_memory(
        memory_key="signal_verdict:customer_reference:x", memory_type="signal_verdict", subject="strong_signal",
        score=100, facts={"origin": "customer_reference", "source_url": "http://radar0.example.com/reference-hit/",
                          "signal_title": "Эталон"})
    # Не идут: выбранное «в дайджест» из пакета, «другое», без текста издателя, дубль.
    in_pack = next(item for item in items if item["group"] == "radar_links")
    other = radar("digest-other", links=1, category="other")
    no_text = radar("digest-no-text", links=1, fetched=False)
    duplicate = radar("digest-duplicate", links=1)
    for signal in (in_pack["signal_id"], other, no_text, duplicate):
        marks.set_signal_mark(signal, status="digest")
    with repository.get_connection() as conn:
        conn.execute("UPDATE signals SET merged_into_signal_id = %s WHERE id = %s", (reference, duplicate))
        conn.commit()
    capsys.readouterr()

    cli.main(["card-scores-exam-control", "--manifest", str(pack["out"] / MANIFEST_NAME)])

    data = json.loads(capsys.readouterr().out)
    assert {profile: sum(one["weight"] for one in criteria) for profile, criteria in data["criteria"].items()} == {
        "business": 100.0, "tech_radar": 100.0}
    assert {tag["name"] for tag in data["tags"] if tag["parent_id"] is None} >= {"Бурение", "Добыча"}
    assert data["rows"][feed["id"]] == {"origin": "feed", "theme": "Бурение", "score": 81.5}
    judged = next(item for item in items if item["group"] == "radar_filtered")
    assert data["rows"][judged["id"]] == {"origin": "radar", "theme": "Бурение", "score": 70.0}
    rejected = next(item for item in items if item["group"] == "feed_rejected")
    assert data["rows"][rejected["id"]] == {"origin": "feed", "theme": None, "score": None}  # гейт отклонил: тега нет
    assert not any(number.startswith("material:") for number in data["rows"])

    found = {one["id"]: one for one in data["strong"]}
    assert set(found) == {f"article:{strong['digest_article']}", f"signal:{strong['top_signal']}",
                          f"signal:{reference}"}
    assert found[f"article:{strong['digest_article']}"]["sources"] == ["digest"]
    assert found[f"article:{strong['digest_article']}"]["category"] == "business"  # статья ленты без своего сигнала
    assert found[f"signal:{strong['top_signal']}"]["sources"] == ["top"]
    assert found[f"signal:{reference}"]["category"] == "technology"
    # Публикация — по правилам пакета: текст издателя, у находки поиска сайт из ссылки и нет даты модели поиска.
    top = found[f"signal:{strong['top_signal']}"]
    assert top["publisher"] == "radar0.example.com" and top["published"] is None and "Page text" in top["text"]
    assert data["skipped"] == {"in_pack": 1, "other": 1, "no_text": 1}


def test_the_help_names_every_model_of_the_default_ladder(capsys):
    with pytest.raises(SystemExit):
        cli.main(["card-scores-exam", "--help"])
    printed = "".join(capsys.readouterr().out.split())  # перенос строк справки рвёт имена на дефисе
    assert [model for model in card_scores_exam.MODELS if model not in printed] == []


def test_rescore_marks_a_new_core_export_and_refuses_one_without_core_scores(scores_pack, monkeypatch):
    run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)})
    [source] = sorted(scores_pack["out"].glob("card_scores_exam_report_*.json"))
    path = scores_pack["out"] / CONTROL_NAME
    control = json.loads(path.read_text(encoding="utf-8"))
    # Выгрузку сняли заново: у строки другой балл ядра, порог ступени при пересчёте другой — отчёт это говорит.
    number = next(key for key, one in control["rows"].items() if one["score"] is not None)
    rows = {**control["rows"], number: {**control["rows"][number], "score": 10}}
    path.write_text(json.dumps({**control, "rows": rows}, ensure_ascii=False), encoding="utf-8")
    _, rescored = run_exam(monkeypatch, scores_pack, {}, "--rescore", str(source))
    assert rescored["control_changed"] is True
    readable = sorted(scores_pack["out"].glob("card_scores_exam_report_*.md"))[-1].read_text(encoding="utf-8")
    assert "Выгрузка с ядра изменилась после исходного прогона" in readable
    # Выгрузка без единого балла ядра: порог не посчитать, пересчёт отказывает.
    empty = {key: {**one, "score": None} for key, one in control["rows"].items()}
    path.write_text(json.dumps({**control, "rows": empty}, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit, match="нет нынешнего балла ядра"):
        cli.main(["card-scores-exam", "--markup", str(scores_pack["markup"]), "--rescore", str(source),
                  "--card-markup", str(scores_pack["card_markup"])])


def test_the_exam_refuses_before_any_paid_call(scores_pack, monkeypatch):
    out = scores_pack["out"]
    model = ScoresModel(oracle_card(scores_pack), {LUNA: oracle_scores(scores_pack)})
    for module in (card_scores_exam_calls, signal_card, signal_scores):
        monkeypatch.setattr(module, "make_client", lambda: model)

    def refused(match, *args, card_model=CARD):
        with pytest.raises(SystemExit, match=match):
            cli.main(["card-scores-exam", "--markup", str(scores_pack["markup"]), "--models", LUNA,
                      "--card-markup", str(scores_pack["card_markup"]),
                      *(["--card-model", card_model] if card_model else []), *args])

    # Экзамен — три прогона на строку; меньше — только на настройке.
    refused("--runs", "--runs", "1")
    # Модель без своей строки в таблице цен: лестница по цене и расход солгали бы.
    refused("цены gpt-7-mega нет", "--models", "gpt-7-mega")
    # Экзамен — только на утверждённых правилах: без отметки и с отметкой прежних версий он не идёт.
    approve_scores(out, approved=None)
    refused("approved_scores")
    approve_scores(out, approved="утверждено: карточка 2026-10-01.1, баллы 2026-10-01.1, линейки 2026-10-01.1")
    refused("не называет текущие правила")
    # Пороги — часть утверждения: отметка с нынешними версиями, но без порогов экзамен не пускает.
    approve_scores(out, approved=(f"утверждено: карточка {signal_card.PROMPT_VERSION}, баллы "
                                  f"{signal_scores.PROMPT_VERSION}, линейки {load_rubrics()['version']}"))
    refused("пороги: границы итога")
    approve_scores(out)
    # Границы итога — тоже часть утверждения: отметка при других границах экзамен не пускает.
    monkeypatch.setattr(card_scores_exam.config, "SIGNAL_SCORE_BANDS", (74, 61, 40))
    refused("границы итога 74/61/40")
    monkeypatch.setattr(card_scores_exam.config, "SIGNAL_SCORE_BANDS", (80, 65, 40))
    # Деление и строки экзамена утверждает владелец: без отметки «Отбора» прогона нет, даже настройки.
    rows = json.loads((out / ROWS_NAME).read_text(encoding="utf-8"))
    write_rows(out, {key: rows[key] for key in ("list_pages", "in_instruction")}, approved=None)
    refused("approved", "--part", "tuning", "--runs", "1")
    (out / ROWS_NAME).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    # Модель основного вызова и сути — со ставкой в таблице цен; без модели основного вызова тема и дата не те.
    refused("цены gpt-9-card нет", card_model="gpt-9-card")
    monkeypatch.setattr(card_scores_exam.config, "SIGNAL_CARD_MODEL", "")
    refused("--card-model", card_model=None)
    # Выгрузка с ядра: без неё весов и тегов нет; с испорченной — итог исказился бы.
    control = json.loads((out / CONTROL_NAME).read_text(encoding="utf-8"))
    (out / CONTROL_NAME).unlink()
    refused("card-scores-exam-control")
    broken = {**control, "criteria": {**control["criteria"], "business": control["criteria"]["business"][:-1]}}
    (out / CONTROL_NAME).write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
    refused("сумма весов профиля business")
    (out / CONTROL_NAME).write_text(json.dumps({**control, "rows": {"signal:999999": {}}}, ensure_ascii=False),
                                    encoding="utf-8")
    refused("signal:999999")
    # Балл контроля не числом упал бы уже после оплаченных вызовов: проверка — при загрузке набора.
    number = next(iter(control["rows"]))
    (out / CONTROL_NAME).write_text(json.dumps({**control, "rows": {**control["rows"], number: {"score": "n/a"}}},
                                               ensure_ascii=False), encoding="utf-8")
    refused("число или null")
    # Порог ступени — нынешние баллы ядра. Выгрузка, где их нет ни у одной строки, прошла бы проверку, лестница
    # оплатилась бы вся, и каждая модель «не прошла» бы ступень: отказ до вызовов, и на сухом прогоне тоже.
    empty = {key: {**one, "score": None} for key, one in control["rows"].items()}
    (out / CONTROL_NAME).write_text(json.dumps({**control, "rows": empty}, ensure_ascii=False), encoding="utf-8")
    refused("нет нынешнего балла ядра")
    refused("нет нынешнего балла ядра", "--dry-run")
    assert model.calls == []
    assert not list(out.glob("card_scores_exam_*"))[1:]  # только выгрузка с ядра, ни сути, ни отчёта


def test_the_second_call_gets_the_core_input_with_the_expert_fields_and_the_main_call_of_the_same_run(scores_pack,
                                                                                                    monkeypatch):
    _, first = run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)})
    number = next(one for one in part_rows(first, scores_pack) if scores_pack["expert"][one]["profile"] == "business")
    title = scores_pack["expert"][number]["title"]
    # Дата и зрелость основного вызова — свои у каждого прогона: второй вызов прогона получает свои.
    dates = {}

    def card(found):
        dates[found] = dates.get(found, 0) + 1
        answer = oracle_card(scores_pack)(found)
        if found == title:
            answer.update({"event_date": f"2026-09-{20 + dates[found]}", "event_date_source": "из текста",
                           "maturity": "пилот/ОПИ"})
        return answer
    (scores_pack["out"] / "card_scores_exam_cards.json").unlink()

    model, _ = run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)}, card=card)

    calls = [call for call in model.of("signal_scores", LUNA) if call["title"] == title]
    assert len(calls) == 3
    for run, call in enumerate(calls, start=1):
        assert f"Дата события из текста: {20 + run}.09.2026" in call["input"]
        assert "Стадия зрелости: пилот/ОПИ" in call["input"]
        # Категория, отрасль и связь — эксперта (подписи справочника), применения он не писал; суть — «Сути».
        assert "Категория: бизнес" in call["input"] and "Отрасль: нефтегаз" in call["input"]
        assert "Связь с нефтесервисом: прямая" in call["input"] and "Применение" not in call["input"]
        assert f"Суть: Суть публикации: {title}" in call["input"]
    assert model.of("article_summary") == []  # суть уже лежала рядом с манифестом
    card_call = next(call for call in model.of("signal_card") if call["title"] == title)
    # Основной вызов: суть и категория, 13 тем и кандидаты по ключам — как у ядра (signal_card.payload_signal).
    assert "Категория: бизнес" in card_call["input"] and "Темы-кандидаты по ключам:" in card_call["input"]
    assert "Бурение" in card_call["instructions"]


def test_a_failed_call_is_retried_once_and_a_lost_answer_is_a_miss(scores_pack, monkeypatch):
    _, first = run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)})
    exam = part_rows(first, scores_pack)
    flaky, broken, cardless = exam[0], exam[1], exam[2]
    titles = {scores_pack["expert"][number]["title"]: number for number in (flaky, broken, cardless)}
    attempts = {}

    def card(title):
        if titles.get(title) == cardless:
            return RuntimeError("основной вызов не ответил")
        return oracle_card(scores_pack)(title)

    def scores(title, criteria, run):
        number = scores_pack["titles"][title]
        attempts[number] = attempts.get(number, 0) + 1
        if number == flaky and attempts[number] % 2 == 1:
            return RuntimeError("сеть")  # каждый первый ответ прогона — сбой, повтор отвечает
        if number == broken:
            return RuntimeError("сеть")
        return oracle_scores(scores_pack)(title, criteria, run)
    (scores_pack["out"] / "card_scores_exam_cards.json").unlink()

    model, report = run_exam(monkeypatch, scores_pack, {LUNA: scores}, card=card)

    # Основной вызов без ответа — повтор, затем без карточки: второй вызов ему не делается (как на ядре).
    assert len([call for call in model.of("signal_card") if titles.get(call["title"]) == cardless]) == 6
    assert attempts.get(cardless) is None
    assert attempts[flaky] == 6 and attempts[broken] == 6
    answers = report["models"][0]["scores"]["answers"]
    assert answers["failures"] == 3 * 2  # «сломанная» и «без карточки» — промах в каждом прогоне
    lost = [one for one in report["models"][0]["answers"] if one["id"] in (broken, cardless)]
    assert {one["attempts"] for one in lost if one["id"] == cardless} == {0}
    assert {one["attempts"] for one in lost if one["id"] == broken} == {2}
    assert report["theme"]["agent"] == pytest.approx((len(exam) - 1) / len(exam))


def test_dry_run_prints_rows_calls_and_price_and_writes_nothing(scores_pack, monkeypatch, capsys):
    out = scores_pack["out"]
    before = sorted(path.name for path in out.iterdir())
    model = ScoresModel(oracle_card(scores_pack), {})
    for module in (card_scores_exam_calls, signal_card, signal_scores):
        monkeypatch.setattr(module, "make_client", lambda: model)
    # Цена входа — ноль, выхода — круглая: в цене остаётся только оценка ответа (600 токенов на вызов).
    for name, price in {SUMMARY: 1.0, CARD: 2.0, LUNA: 0.5, MINI: 4.0}.items():
        monkeypatch.setitem(config.OPENAI_MODEL_PRICES, name, (0.0, price))
    capsys.readouterr()

    cli.main(["card-scores-exam", "--markup", str(scores_pack["markup"]), "--dry-run", "--models", f"{MINI},{LUNA}",
              "--card-model", CARD, "--summary-model", SUMMARY])

    printed = capsys.readouterr().out
    assert model.calls == [] and sorted(path.name for path in out.iterdir()) == before  # ни деления, ни сути
    exam = [number for number in scores_pack["expert"]]  # строки пакета с баллами, деление ещё не записано
    assert "деление новое, запишется при первом прогоне" in printed
    assert "«сильных сверх пакета» 2" in printed and "Утверждено: утверждено владельцем: карточка" in printed
    rows = int(re.search(r"Часть «экзамен»: строк пакета (\d+)", printed).group(1)) + 2
    assert rows <= len(exam) + 2
    assert f"Суть ({SUMMARY}): вызовов {rows}" in printed
    assert f"Основной вызов ({CARD}): вызовов {rows * 3}" in printed
    ladder = [line.split(":")[0].strip() for line in printed.splitlines() if f": вызовов {rows * 3}," in line
              and line.startswith("  ")]
    assert ladder == [LUNA, MINI]  # лестница по цене
    price = rows * 3 * 600 / 1_000_000
    assert f"≈ ${price * 0.5:.3f}" in printed and f"≈ ${price * 4.0:.3f}" in printed
    assert not DASHES.search(printed)


def test_exam_writes_summaries_with_the_summary_agent_model(scores_pack, monkeypatch, capsys):
    """#150: суть экзамена по умолчанию пишет модель «Сути и перевода» (OPENAI_SUMMARY_MODEL), а не общая OPENAI_MODEL:
    второй вызов «Карточки» экзамен проверяет на той сути, что пойдёт на проде."""
    model = ScoresModel(oracle_card(scores_pack), {})
    for module in (card_scores_exam_calls, signal_card, signal_scores):
        monkeypatch.setattr(module, "make_client", lambda: model)
    monkeypatch.setattr(config, "OPENAI_MODEL", SUMMARY)
    monkeypatch.setattr(config, "OPENAI_SUMMARY_MODEL", "gpt-6.1-terra")
    capsys.readouterr()

    cli.main(["card-scores-exam", "--markup", str(scores_pack["markup"]), "--dry-run", "--models", f"{MINI},{LUNA}",
              "--card-model", CARD])

    assert "Суть (gpt-6.1-terra): вызовов" in capsys.readouterr().out


def test_rescore_recomputes_a_saved_report_without_model_calls(scores_pack, monkeypatch):
    _, report = run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)})
    [source] = sorted(scores_pack["out"].glob("card_scores_exam_report_*.json"))
    # Границы ступеней сменились после прогона: ответы те же, ступень по новым границам, правило — с пометкой. При
    # нижней границе «полезного» 71 балл судьи 70 уходит в «наблюдать», балл ленты 77 остаётся «полезным».
    monkeypatch.setattr(config, "SIGNAL_SCORE_BANDS", (95, 71, 40))

    # Рассуждение сменили после прогона: отчёт пересчёта пишет то, на котором получены ответы.
    monkeypatch.setattr(config, "SIGNAL_SCORES_REASONING", "high")

    model, rescored = run_exam(monkeypatch, scores_pack, {}, "--rescore", str(source))

    assert model.calls == []
    assert rescored["rescored_from"] == source.name and rescored["rules_changed"] == ["bands"]
    assert rescored["scores_reasoning"] == report["scores_reasoning"] == "low"
    [luna] = rescored["models"]
    pack = [one for one in rescored["rows"] if one["rating"] != "strong_extra"]
    strong = sum(one["band"] == "strong" for one in pack)
    assert luna["scores"]["plain"]["band"]["agent"] == pytest.approx(1 - strong / len(pack))
    # Порог ступени при пересчёте — нынешние баллы ядра по тем же новым границам: модель с баллами эксперта лучше них.
    control = scores_pack["control"]["rows"]
    controlled = [one for one in pack if (control.get(one["id"]) or {}).get("score") is not None]
    assert {control[one["id"]]["score"] for one in controlled} == {77, 70}
    floor = sum({77: "useful", 70: "watch"}[control[one["id"]]["score"]] == one["band"]
                for one in controlled) / len(controlled)
    # По прежним границам оба балла ядра были «полезными»: порог был бы другим.
    assert floor != pytest.approx(sum(one["band"] == "useful" for one in controlled) / len(controlled))
    assert luna["scores"]["plain"]["band"]["floor"] == pytest.approx(floor)
    assert luna["passed"] is True and rescored["decision"]["model"] == LUNA
    readable = sorted(scores_pack["out"].glob("card_scores_exam_report_*.md"))[-1]
    assert "Пересчёт ничего не оплачивал" in readable.read_text(encoding="utf-8")
    assert luna["cost_usd"] == report["models"][0]["cost_usd"]  # цена исходного прогона
    assert rescored["summaries"] == report["summaries"] and rescored["cards"] == report["cards"]
    # Ответы на других линейках пересчёт не берёт: баллы ставились по другим.
    saved = json.loads(source.read_text(encoding="utf-8"))
    source.write_text(json.dumps({**saved, "rubrics_version": "2026-10-01.1"}, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit, match="линейк"):
        cli.main(["card-scores-exam", "--markup", str(scores_pack["markup"]), "--rescore", str(source),
                  "--card-markup", str(scores_pack["card_markup"])])


def test_tuning_takes_only_pack_rows_needs_no_scores_approval_and_chooses_no_model(scores_pack, monkeypatch):
    approve_scores(scores_pack["out"], approved=None)

    model, report = run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)}, "--part", "tuning",
                             "--runs", "1")

    tuning = part_rows(report, scores_pack, "tuning")
    assert [one["id"] for one in report["rows"]] == tuning  # без «сильных сверх пакета»
    assert len(model.of("signal_scores", LUNA)) == len(tuning)
    assert report["decision"] == {"model": None, "variant": None,
                                  "reason": "настройка: модель выбирает только экзамен"}


def test_all_models_give_the_full_table_and_a_deprecated_model_is_measured_but_not_chosen(scores_pack, monkeypatch):
    nano = "gpt-5.4-nano"

    _, report = run_exam(monkeypatch, scores_pack, {nano: oracle_scores(scores_pack), LUNA: oracle_scores(scores_pack),
                                                    MINI: oracle_scores(scores_pack)}, "--all")

    assert [one["model"] for one in report["models"]] == [LUNA, nano, MINI]  # по цене, все: --all
    assert all(one["passed"] for one in report["models"])
    assert report["decision"]["model"] == LUNA and nano in report["deprecated"]
    _, report = run_exam(monkeypatch, scores_pack, {nano: oracle_scores(scores_pack), MINI: oracle_scores(scores_pack)})
    assert report["decision"]["model"] == MINI  # прошедшая устаревшая не выбирается


def test_parallel_calls_give_the_same_answers_and_scores(scores_pack, monkeypatch):
    _, single = run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)})
    for name in ("card_scores_exam_summaries.json", "card_scores_exam_cards.json"):
        (scores_pack["out"] / name).unlink()

    _, parallel = run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)}, "--workers", "4")

    assert parallel["summaries"] == single["summaries"]
    strip = lambda answers: sorted((one["run"], one["id"], json.dumps(one["answer"], sort_keys=True))  # noqa: E731
                                   for one in answers)
    assert strip(parallel["models"][0]["answers"]) == strip(single["models"][0]["answers"])
    assert parallel["models"][0]["scores"] == single["models"][0]["scores"]


def test_readable_report_has_the_quality_and_price_table_with_the_monthly_feed_estimate(scores_pack, monkeypatch,
                                                                                        capsys):
    # Размеры страт окна пакета — выгрузка otbor-exam-control: 2000 отклонённых и 3000 принятых гейтом статей.
    (scores_pack["out"] / "otbor_exam_control.json").write_text(json.dumps(
        {"strata": {"feed_rejected": 2000, "feed_accepted": 3000}, "rows": {}}), encoding="utf-8")

    _, report = run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)})

    # Эксперт взял бизнес или технологию у 5 из 50 отклонённых и у 20 из 30 принятых гейтом (otbor_exam_support).
    assert report["month"] == {"feed": 5000, "signals": 2000 // 50 * 5 + 3000 // 30 * 20}
    printed = capsys.readouterr().out
    readable = next(line.split(": ", 1)[1] for line in printed.splitlines() if line.startswith("Отчёт: "))
    text = open(readable, encoding="utf-8").read()
    assert "## Качество и цена" in text and f"| {LUNA} | прошла |" in text
    assert "сигналов с баллами около 2200 из 5000" in text
    per_signal = report["models"][0]["cost_usd"] / report["models"][0]["scores"]["answers"]["answers"]
    assert f"${per_signal * 2200:.2f}" in text
    assert "## Тема основного вызова" in text and "эксперт сам с собой" in text
    assert not DASHES.search(text)


def test_a_changed_summary_instruction_asks_for_a_new_summary(scores_pack, monkeypatch):
    run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)})
    monkeypatch.setattr(card_scores_exam_calls, "SUMMARY_INSTRUCTIONS", "Новая инструкция сути")

    model, report = run_exam(monkeypatch, scores_pack, {LUNA: oracle_scores(scores_pack)})

    # Суть по старой инструкции не годится. Основной вызов переспрашивается, только если суть стала другой: его
    # отпечаток — по его же запросу (подставная модель пишет ту же суть).
    assert len(model.of("article_summary")) == len(report["rows"])
    assert model.of("signal_card") == []
