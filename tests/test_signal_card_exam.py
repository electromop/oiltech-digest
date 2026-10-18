"""Экзамен агента «Карточка сигнала» (#98, спека #93), шов 4 — команда экзамена на подставной модели.

Пакет эксперту (#94) — манифест и таблица разметки — собирается здесь же, во временной папке вне
репозитория: таблица — тем же писателем, что у пакета, разметка — как её вернул эксперт. Состав — как у
настоящего набора: 24 полезных и 6 шумных статей ленты, 15 технологий и 5 бизнес-сигналов радара; деление
по долям — настройка 7 / 2 / 5 / 1, экзамен 17 / 4 / 10 / 4 (наибольшие остатки, проверка разметки 18.10).
"""

from datetime import date
import hashlib
import io
import json

import openpyxl
import pytest

from oiltech_digest import cli, signal_card, signal_card_pack
from oiltech_digest.db import repository
from oiltech_digest.processing.openai_client import AIResponse
from tests.signal_card_support import DRILLING, answer, radar_finding

NANO, MINI = "gpt-5.4-nano", "gpt-5.4-mini"
STRATA = {"useful": 24, "noise": 6, "technology": 15, "business": 5}
EXAM_COUNTS = {"useful": 17, "noise": 4, "technology": 10, "business": 4}
TEXT = "SLB подписала контракт с Saudi Aramco 24 сентября 2026 года на бурение 40 скважин."
MARKUP_NAME = "signal_card_markup_completed.xlsx"


def label(**overrides):
    """Разметка эксперта одной строки — колонки таблицы пакета (signal_card_pack.MARKUP_COLUMNS)."""
    return {
        "Дата события": date(2026, 9, 24), "Источник даты события": "из текста", "Компании-участники": "SLB",
        "Тип события": "контракт", "Регион": "Ближний Восток", "Класс технологии": "неприменимо",
        "Зрелость": "неприменимо", "Связь с нефтесервисом": "прямая",
        "Применение в нефтесервисе": "Бурение скважин", "Почему важно нефтесервису": "Крупный контракт",
        "Комментарий эксперта": "", **overrides,
    }


def row(number, stratum, *, title=None, text=TEXT, published="2026-09-25", publisher="Нефтегаз",
        publisher_type="Media", **labels):
    radar = stratum in ("technology", "business")
    return {
        "id": f"{'signal' if radar else 'article'}:{number}", "origin": "radar" if radar else "feed",
        "stratum": stratum, "signal_id": str(number) if radar else None,
        "article_id": None if radar else str(number), "evidence_id": str(number) if radar else None,
        "url": f"https://pack.example/{number}", "category": "технология" if stratum == "technology" else "бизнес",
        "theme": DRILLING, "title": title or f"Сигнал {number}", "text": text, "published": published,
        "publisher": publisher, "publisher_type": "" if radar else publisher_type,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text_chars": len(text),
        "override_note": None, "overrides": [], "labels": label(**labels),
    }


def pack_rows(**by_stratum):
    """50 строк набора; by_stratum — поправки разметки целой доли (её строки на экзамене — по долям)."""
    rows, number = [], 0
    for stratum, count in STRATA.items():
        for _ in range(count):
            number += 1
            rows.append(row(number, stratum, **by_stratum.get(stratum, {})))
    return rows


def write_pack(folder, rows):
    """Пакет эксперту так, как он вернулся: манифест рядом с заполненной таблицей."""
    items = [{key: value for key, value in item.items() if key != "labels"} for item in rows]
    manifest = {"created_at": "2026-10-16T07:40:50+00:00", "seed": signal_card_pack.DEFAULT_SEED,
                "dictionaries_version": signal_card_pack.load_dictionaries()["version"],
                "text_limit": signal_card_pack.TEXT_LIMIT, "items": items}
    (folder / signal_card_pack.MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    path = folder / MARKUP_NAME
    signal_card_pack._write_workbook(path, items, signal_card_pack.load_dictionaries())
    book = openpyxl.load_workbook(path)
    sheet = book[signal_card_pack.MARKUP_SHEET]
    header = [cell.value for cell in sheet[1]]
    for number, item in enumerate(rows, start=2):
        for name, value in item["labels"].items():
            sheet.cell(row=number, column=header.index(name) + 1, value=value)
    book.save(path)
    return path


class ExamModel:
    """Подставная модель экзамена: ответ — по модели и заголовку публикации; вызовы запоминает."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None,
                      reasoning_effort=None):
        title = next(line.split(":", 1)[1].strip() for line in user_input.splitlines()
                     if line.startswith("Заголовок:"))
        self.calls.append({"model": model, "title": title, "input": user_input})
        reply = self.answers[model](title)
        if isinstance(reply, Exception):
            raise reply
        return AIResponse(data=reply, model=f"{model}-2026-03-17", input_tokens=3000, output_tokens=600)


def run_exam(monkeypatch, markup, answers, *args):
    model = ExamModel(answers)
    monkeypatch.setattr(signal_card, "make_client", lambda: model)
    cli.main(["signal-card-exam", "--markup", str(markup), *args])
    [report] = sorted(markup.parent.glob("signal_card_exam_report_*.json"))[-1:]
    return model, json.loads(report.read_text(encoding="utf-8"))


def by_stratum(stratum_of, **answers_by_stratum):
    """Ответ модели по доле строки: заголовок «Сигнал N» → доля N-й строки набора."""
    def reply(title):
        return answers_by_stratum.get(stratum_of[title], answer())
    return reply


@pytest.fixture()
def strata():
    return {item["title"]: item["stratum"] for item in pack_rows()}


def test_exam_scores_the_agent_against_the_markup_with_the_constant_alongside(tmp_path, monkeypatch, strata):
    # Шумные статьи эксперт пометил «нет связи», технологии радара — «пилот»; агент ответил так же.
    markup = write_pack(tmp_path, pack_rows(noise={"Связь с нефтесервисом": "нет связи"},
                                             technology={"Тип события": "пилот"}))
    agent = by_stratum(strata, noise=answer(oilfield_relevance="нет связи", oilfield_application=""),
                       technology=answer(event_type="пилот"))

    model, report = run_exam(monkeypatch, markup, {NANO: agent})

    assert {call["model"] for call in model.calls} == {NANO}
    assert len(model.calls) == 35 * 3  # 35 сигналов экзамена, по три прогона
    assert report["decision"]["model"] == NANO
    [nano] = report["models"]
    metrics = nano["lenient"]["overall"]
    # Связь — средняя доля верных по значениям: «прямая» у 31 строки экзамена, «нет связи» у 4.
    assert (metrics["oilfield_relevance"]["agent"], metrics["oilfield_relevance"]["constant"]) == (1.0, 0.5)
    # Самое частое значение типа — «контракт»: 25 из 35 строк экзамена.
    assert metrics["event_type"]["agent"] == 1.0
    assert metrics["event_type"]["constant"] == pytest.approx(25 / 35)
    # Дата события у всех — из текста, на день раньше публикации: константа ошибается в каждой строке.
    assert (metrics["event_date"]["agent"], metrics["event_date"]["constant"]) == (0.0, 1.0)
    assert metrics["event_date"]["passed"] is True
    assert {stratum: nano["lenient"]["by_stratum"][stratum]["event_type"]["n"] for stratum in STRATA} == {
        stratum: count * 3 for stratum, count in EXAM_COUNTS.items()}


def test_exam_takes_the_next_model_only_when_the_cheaper_one_fails(tmp_path, monkeypatch, strata):
    markup = write_pack(tmp_path, pack_rows())
    # nano путает регион у всех сигналов, mini отвечает как эксперт.
    model, report = run_exam(monkeypatch, markup, {NANO: lambda title: answer(region="Европа"),
                                                   MINI: lambda title: answer()})

    assert [one["model"] for one in report["models"]] == [NANO, MINI]
    nano, mini = report["models"]
    assert (nano["passed"], nano["lenient"]["failed"]) == (False, ["region"])
    assert nano["lenient"]["overall"]["region"]["agent"] == 0.0
    assert mini["passed"] is True
    assert report["decision"]["model"] == MINI
    assert [call["model"] for call in model.calls].count(MINI) == 105

    # Не прошла ни одна — решения нет, обе причины в отчёте.
    _, report = run_exam(monkeypatch, markup, {NANO: lambda title: answer(region="Европа"),
                                               MINI: lambda title: answer(event_type="пилот")})
    assert report["decision"]["model"] is None
    assert [one["lenient"]["failed"] for one in report["models"]] == [["region"], ["event_type"]]


def test_split_by_shares_is_fixed_at_the_first_run_and_does_not_change(tmp_path, monkeypatch, strata):
    markup = write_pack(tmp_path, pack_rows())
    _, first = run_exam(monkeypatch, markup, {NANO: lambda title: answer()})
    stratum_of = {item["id"]: item["stratum"] for item in pack_rows()}
    counts = lambda ids: {stratum: [stratum_of[one] for one in ids].count(stratum) for stratum in STRATA}  # noqa: E731
    assert counts(first["split"]["exam"]) == EXAM_COUNTS
    assert counts(first["split"]["tuning"]) == {"useful": 7, "noise": 2, "technology": 5, "business": 1}

    # Зерно манифеста сменилось — новое деление было бы другим, но оно уже записано рядом с манифестом.
    manifest_path = tmp_path / signal_card_pack.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(json.dumps({**manifest, "seed": 1}, ensure_ascii=False), encoding="utf-8")
    model, second = run_exam(monkeypatch, markup, {NANO: lambda title: answer()})
    assert second["split"] == first["split"]

    # Настройка — свои 15 строк, названные модели без лестницы и без решения о модели.
    model, tuning = run_exam(monkeypatch, markup, {NANO: lambda title: answer()}, "--part", "tuning", "--models", NANO)
    assert len(model.calls) == 15 * 3
    assert {call["title"] for call in model.calls} == {
        item["title"] for item in pack_rows() if item["id"] in first["split"]["tuning"]}
    assert tuning["decision"]["model"] is None


def test_dry_run_prints_calls_and_price_and_writes_nothing(tmp_path, monkeypatch, capsys):
    markup = write_pack(tmp_path, pack_rows())
    before = sorted(path.name for path in tmp_path.iterdir())
    model = ExamModel({})
    monkeypatch.setattr(signal_card, "make_client", lambda: model)
    # Цена выхода — круглая, входа — ноль: в цене остаётся только оценка ответа (600 токенов на вызов).
    monkeypatch.setitem(signal_card.config.OPENAI_MODEL_PRICES, NANO, (0.0, 1.0))
    monkeypatch.setitem(signal_card.config.OPENAI_MODEL_PRICES, MINI, (0.0, 4.0))

    cli.main(["signal-card-exam", "--markup", str(markup), "--dry-run"])

    out = capsys.readouterr().out
    assert model.calls == []
    assert sorted(path.name for path in tmp_path.iterdir()) == before  # ни деления, ни отчёта
    assert "сигналов 35, прогонов на сигнал 3" in out
    assert f"{NANO}: вызовов 105" in out and "≈ $0.063" in out  # 105 × 600 токенов × $1 за 1 млн
    assert f"{MINI}: вызовов 105" in out and "≈ $0.252" in out
    assert "Не больше: вызовов 210, ≈ $0.315" in out


def test_thresholds_are_checked_on_the_report_with_tolerances_strict_is_alongside(tmp_path, monkeypatch, strata):
    rows = pack_rows(technology={"Класс технологии": "другое"})
    markup = write_pack(tmp_path, rows)
    # Допуски утверждает владелец: Saudi Aramco законно и участник, и нет; у технологий — второй класс.
    tolerances = {item["id"]: {"companies": ["Saudi Aramco"], "note": "сторона контракта или заказчик?",
                               **({"technology_class": ["датчики и IoT"]} if item["stratum"] == "technology" else {})}
                  for item in rows}
    (tmp_path / "signal_card_exam_tolerances.json").write_text(json.dumps(tolerances, ensure_ascii=False),
                                                               encoding="utf-8")
    both = [{"name": "SLB", "quote": "SLB подписала контракт"}, {"name": "Saudi Aramco", "quote": "контракт с Saudi Aramco"}]
    agent = by_stratum(strata, technology=answer(companies=both, technology_class="датчики и IoT"))

    _, report = run_exam(monkeypatch, markup, {NANO: lambda title: {**agent(title), "companies": both}})

    [nano] = report["models"]
    strict, lenient = nano["strict"], nano["lenient"]
    assert strict["failed"] == ["companies_precision", "technology_class"]
    assert strict["overall"]["companies_precision"]["agent"] == 0.5
    assert strict["overall"]["technology_class"]["agent"] == pytest.approx(25 / 35)
    assert lenient["passed"] is True
    assert (lenient["overall"]["companies_precision"]["agent"], lenient["overall"]["technology_class"]["agent"]) == (
        1.0, 1.0)
    # Константа меряется так же: у технологий «неприменимо» не допускается и с допусками.
    assert lenient["overall"]["technology_class"]["constant"] == pytest.approx(25 / 35)
    assert report["decision"]["model"] == NANO


ALLIANCE = ("Пермского Политеха, SLB и Губкинский университет подписали соглашение. Объединенная "
            "двигателестроительная корпорация и КТК-Р присоединились 24 сентября 2026 года.")
GAZPROM = "Газпром нефть и Газпром подписали контракт 24 сентября 2026 года."
GAZPROMNEFT = "Газпромнефть и Газпром подписали контракт 24 сентября 2026 года."


def test_company_names_are_compared_through_aliases_word_forms_and_spelling(tmp_path, monkeypatch, strata):
    markup = write_pack(tmp_path, pack_rows(
        # Эксперт пишет полное имя, именительный падеж, «ё» и неразрывный дефис — агент берёт имя из текста.
        useful={"text": ALLIANCE, "Компании-участники": "Пермский Политех; Schlumberger; РГУ нефти и газа (НИУ) "
                                                        "имени И. М. Губкина; КТК‑Р; Объединённая "
                                                        "двигателестроительная корпорация"},
        business={"text": GAZPROM, "Компании-участники": "Газпром нефть"},
        technology={"text": GAZPROMNEFT, "Компании-участники": "Газпром"},
        noise={"Компании-участники": "не определено"},
    ))
    (tmp_path / "signal_card_exam_aliases.json").write_text(json.dumps([
        {"name": "РГУ нефти и газа (НИУ) имени И. М. Губкина", "aliases": ["Губкинский университет"]},
    ], ensure_ascii=False), encoding="utf-8")
    alliance = [{"name": "Пермского Политеха", "quote": "Пермского Политеха, SLB и Губкинский университет подписали"},
                {"name": "SLB", "quote": "SLB и Губкинский университет подписали соглашение"},
                {"name": "Губкинский университет", "quote": "Губкинский университет подписали соглашение"},
                {"name": "Объединенная двигателестроительная корпорация",
                 "quote": "Объединенная двигателестроительная корпорация и КТК-Р присоединились"},
                {"name": "КТК-Р", "quote": "КТК-Р присоединились"}]
    agent = by_stratum(strata, useful=answer(companies=alliance), noise=answer(companies=[]),
                       business=answer(companies=[{"name": "Газпром", "quote": "Газпром подписали контракт"}]),
                       technology=answer(companies=[{"name": "Газпромнефть", "quote": "Газпромнефть и Газпром"}]))

    _, report = run_exam(monkeypatch, markup, {NANO: agent})

    metrics = report["models"][0]["strict"]["overall"]
    # На прогон экзамена: у полезных 17 × 5 совпавших; «Газпром» — не «Газпром нефть» (бизнес, 4 × 1) и не
    # «Газпромнефть» (технологии, 10 × 1) — мимо и в точности, и в полноте; у шумных «не определено» — пусто у обоих.
    assert (metrics["companies_precision"]["matched"], metrics["companies_precision"]["named"]) == (85 * 3, 99 * 3)
    assert metrics["companies_recall"]["expected"] == 99 * 3
    assert metrics["companies_precision"]["agent"] == metrics["companies_recall"]["agent"] == pytest.approx(85 / 99)
    # По сигналам: у 17 из 31 сигнала с компаниями всё сошлось, у остальных — ничего.
    assert metrics["companies_precision"]["by_signal"] == pytest.approx(17 / 31)
    assert metrics["companies_recall"]["by_signal"] == pytest.approx(17 / 31)
    assert metrics["invented_companies"]["agent"] == 0


def test_no_ai_baseline_finds_companies_and_region_by_dictionary(tmp_path, monkeypatch, strata):
    robot = "Equinor и SLB испытали робота-бурильщика в Северном море 24 сентября 2026 года."
    record = "Буровики провели рекордную проходку 24 сентября 2026 года."
    markup = write_pack(tmp_path, pack_rows(
        technology={"text": robot, "Компании-участники": "Equinor; SLB", "Регион": "Европа"},
        business={"text": record, "Компании-участники": "не определено", "Регион": "Россия"},
    ))
    agent = by_stratum(strata, business=answer(companies=[], region="Россия"),
                       technology=answer(region="Европа", companies=[
                           {"name": "Equinor", "quote": "Equinor и SLB испытали"},
                           {"name": "SLB", "quote": "SLB испытали робота-бурильщика"}]))

    _, report = run_exam(monkeypatch, markup, {NANO: agent})

    # Словарь находит в тексте SLB и Saudi Aramco (участник по разметке — только SLB), Equinor и SLB; места —
    # Saudi → Ближний Восток, Северное море → Европа; у рекордной проходки места нет — «не определено».
    baseline = report["baseline"]["strict"]
    assert baseline["companies_precision"] == pytest.approx(41 / 62)
    assert baseline["companies_recall"] == 1.0
    assert baseline["region"] == pytest.approx(31 / 35)
    assert report["models"][0]["passed"] is True


def test_radar_judge_is_compared_with_the_expert_on_all_radar_rows_without_model_calls(tmp_path, monkeypatch,
                                                                                      strata):
    rows = pack_rows(business={"Связь с нефтесервисом": "нет связи"})
    markup = write_pack(tmp_path, rows)
    radar = [item["id"] for item in rows if item["origin"] == "radar"]  # 15 технологий, затем 5 бизнес
    # Судья: у десяти технологий дата верна, у пяти не названа; у бизнеса — чужая дата; связь — мимо у двух.
    judge = {number: {"event_date": "2026-09-24" if index < 10 else None if index < 15 else "2026-09-01",
                      "oilfield_relevance": "direct" if index < 15 or index >= 18 else "none"}
             for index, number in enumerate(radar)}
    (tmp_path / "signal_card_exam_judge.json").write_text(json.dumps(judge), encoding="utf-8")

    agent = by_stratum(strata, business=answer(oilfield_relevance="нет связи", oilfield_application=""))
    model, report = run_exam(monkeypatch, markup, {NANO: agent})

    assert len(model.calls) == 35 * 3  # судья — без вызовов модели
    strict = report["judge"]["strict"]
    assert strict["rows"] == 20
    assert (strict["event_date"]["agent"], strict["event_date"]["constant"]) == (0.5, 1.0)
    # «Прямая» — 15 из 15, «нет связи» — 3 из 5: в среднем по значениям 0,8.
    assert strict["oilfield_relevance"]["agent"] == pytest.approx(0.8)
    assert strict["oilfield_relevance"]["by_value"] == {"прямая": 1.0, "нет связи": 0.6}


def test_judge_values_are_exported_from_the_core_by_manifest_number(isolated_db, tmp_path, monkeypatch, capsys):
    robot = radar_finding("robot", "Робот", "Equinor испытала робота.", event_date="2026-09-01",
                          oilfield_relevance="transferable")
    sensor = radar_finding("sensor", "Датчик", "Датчик утечек на трубопроводе.")  # даты и связи судья не назвал
    duplicate = radar_finding("sensor-2", "Датчик, повтор", "Датчик утечек.", oilfield_relevance="direct")
    assert repository.mark_signal_merged(duplicate, sensor, "то же событие")
    rows = [row(1, "useful"), {**row(2, "technology"), "signal_id": str(robot)},
            {**row(3, "technology"), "signal_id": str(sensor)}, {**row(4, "business"), "signal_id": str(duplicate)}]
    manifest = json.dumps({"items": [{key: value for key, value in item.items() if key != "labels"} for item in rows]})
    monkeypatch.setattr("sys.stdin", io.StringIO(manifest))
    capsys.readouterr()

    cli.main(["signal-card-exam-judge", "--manifest", "-"])

    # Номер — строки манифеста: сигнал мог стать дублем, но судил судья именно его находку.
    assert json.loads(capsys.readouterr().out) == {
        "signal:2": {"signal_id": robot, "event_date": "2026-09-01", "oilfield_relevance": "transferable",
                     "set_by": {"event_date": "radar_judge", "oilfield_relevance": "radar_judge"},
                     "merged_into_signal_id": None},
        "signal:3": {"signal_id": sensor, "event_date": None, "oilfield_relevance": None, "set_by": {},
                     "merged_into_signal_id": None},
        "signal:4": {"signal_id": duplicate, "event_date": None, "oilfield_relevance": "direct",
                     "set_by": {"oilfield_relevance": "radar_judge"}, "merged_into_signal_id": sensor},
    }


def test_date_source_is_not_scored_where_the_event_date_is_the_publication_date(tmp_path, monkeypatch, strata):
    # У шумных дата события — день публикации, и эксперт поставил «по дате публикации»; агент назвал ту же
    # дату «из текста» — расхождение источника ничего не значит. У бизнеса агент даты не нашёл.
    markup = write_pack(tmp_path, pack_rows(noise={"published": "2026-09-24",
                                                    "Источник даты события": "по дате публикации"}))
    agent = by_stratum(strata, business=answer(event_date="", event_date_source="по дате публикации"))

    _, report = run_exam(monkeypatch, markup, {NANO: agent})

    metrics = report["models"][0]["strict"]["overall"]
    assert metrics["event_date_source"]["n"] == 31 * 3  # без четырёх шумных
    assert metrics["event_date_source"]["agent"] == pytest.approx(27 / 31)
    assert metrics["event_date_source"]["passed"] is None  # порога у источника даты нет
    assert metrics["event_date"]["errors"] == 4 * 3
    assert metrics["event_date"]["constant_errors"] == 31


def test_readable_report_lies_next_to_the_manifest_in_two_views(tmp_path, monkeypatch, strata, capsys):
    markup = write_pack(tmp_path, pack_rows(noise={"Связь с нефтесервисом": "нет связи"}))
    agent = by_stratum(strata, noise=answer(oilfield_relevance="нет связи", oilfield_application=""))

    run_exam(monkeypatch, markup, {NANO: agent})

    [readable] = tmp_path.glob("signal_card_exam_report_*.md")
    assert str(readable) in capsys.readouterr().out
    text = readable.read_text(encoding="utf-8")
    assert "Решение: **gpt-5.4-nano** — самая дешёвая из прошедших пороги" in text
    assert "| Атрибут | Порог | С допусками | Строго | Константа | Устойчивость | |" in text
    assert "| Связь с нефтесервисом (среднее по значениям) | ≥ 75% | 100% | 100% | 50% | 100% | ✓ |" in text
    assert "| Дата события — доля ошибок | не больше константы | 0% | 0% | 100% | 100% | ✓ |" in text
    assert "Допусков нет: отчёт с допусками совпадает со строгим." in text
    assert "## Без ИИ (словарём)" in text
