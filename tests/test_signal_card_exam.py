"""Экзамен агента «Карточка сигнала» (#98, спека #93), шов 4 — команда экзамена на подставной модели.

Пакет эксперту (#94) — манифест и таблица разметки — собирается во временной папке вне репозитория
(tests/signal_card_support.py): таблица — тем же писателем, что у пакета, разметка — как её вернул
эксперт. Состав — как у
настоящего набора: 24 полезных и 6 шумных статей ленты, 15 технологий и 5 бизнес-сигналов радара; деление
по долям — настройка 7 / 2 / 5 / 1, экзамен 17 / 4 / 10 / 4 (наибольшие остатки, проверка разметки 18.10).
"""

from collections import Counter
import io
import json

import pytest

from oiltech_digest import cli, signal_card, signal_card_pack
from oiltech_digest.db import repository
from tests.signal_card_support import (EXAM_COUNTS, MINI, NANO, STRATA, ExamModel, answer, by_stratum, pack_rows,
                                       radar_finding, row, run_exam, strata, write_pack)


def test_exam_scores_the_agent_against_the_markup_with_the_constant_alongside(tmp_path, monkeypatch, strata):
    # Шумные статьи эксперт пометил «нет связи», технологии радара — «пилот»; агент технологии назвал так же.
    markup = write_pack(tmp_path, pack_rows(noise={"Связь с нефтесервисом": "нет связи"},
                                             technology={"Тип события": "пилот"}))
    agent = by_stratum(strata, technology=answer(event_type="пилот"))

    model, report = run_exam(monkeypatch, markup, {NANO: agent})

    assert {call["model"] for call in model.calls} == {NANO}
    assert len(model.calls) == 35 * 3  # 35 сигналов экзамена, по три прогона
    assert report["decision"]["model"] == NANO
    [nano] = report["models"]
    metrics = nano["lenient"]["overall"]
    # Связь с нефтесервисом с #132 ставит «Отбор»: её нет ни в вопросе к модели, ни в метриках и порогах экзамена.
    assert "oilfield_relevance" not in metrics
    assert all("oilfield_relevance" not in call["input"] + call["instructions"] for call in model.calls)
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

    # Лестница — по цене, в каком порядке модели ни назови: прошла nano — до mini не доходит.
    model, report = run_exam(monkeypatch, markup, {NANO: lambda title: answer(), MINI: lambda title: answer()},
                             "--models", f"{MINI},{NANO}")
    assert report["decision"]["model"] == NANO and {call["model"] for call in model.calls} == {NANO}


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
    # Константа — самая частая компания эксперта (одна из пяти у полезных, по 17 строк) у каждого сигнала.
    assert metrics["companies_precision"]["constant"] == pytest.approx(17 / 35)
    assert metrics["companies_recall"]["constant"] == pytest.approx(17 / 99)


FORMS = ("Atlas Energy Solutions Inc., Iraya Energies Sdn Bhd and Petroleum Development Oman (PDO) signed a contract "
         "on 24 September 2026. ПАО «Татнефть» и ПАО «Газпром» в сделке не участвуют.")


def test_legal_form_and_abbreviation_do_not_make_another_company(tmp_path, monkeypatch, strata):
    # Форма собственности — не часть имени, как у ядра (word_forms); «Имя (АББР)» — правило экзамена: имя и его
    # аббревиатура.
    markup = write_pack(tmp_path, pack_rows(
        useful={"text": FORMS,
                "Компании-участники": "Atlas Energy Solutions; Iraya Energies; Petroleum Development Oman"},
        technology={"text": FORMS, "Компании-участники": "ПАО «Газпром»"}))
    spelled = [{"name": "Atlas Energy Solutions Inc.", "quote": "Atlas Energy Solutions Inc., Iraya Energies Sdn Bhd"},
               {"name": "Iraya Energies Sdn Bhd", "quote": "Iraya Energies Sdn Bhd and Petroleum Development Oman"},
               {"name": "Petroleum Development Oman (PDO)",
                "quote": "Petroleum Development Oman (PDO) signed a contract"}]
    other = [{"name": "ПАО «Татнефть»", "quote": "ПАО «Татнефть» и ПАО «Газпром» в сделке не участвуют"}]

    _, report = run_exam(monkeypatch, markup, {NANO: by_stratum(strata, useful=answer(companies=spelled),
                                                                technology=answer(companies=other))})

    by_part = report["models"][0]["strict"]["by_stratum"]
    useful, technology = by_part["useful"]["companies_precision"], by_part["technology"]["companies_precision"]
    # 17 полезных строк экзамена × 3 прогона × 3 компании — все те же, что у эксперта.
    assert (useful["named"], useful["matched"]) == (17 * 3 * 3, 17 * 3 * 3)
    # Общая форма собственности компанию не называет: «ПАО «Татнефть»» — не «ПАО «Газпром»».
    assert (technology["named"], technology["matched"]) == (10 * 3, 0)


def test_one_company_with_and_without_legal_form_is_stable_between_runs(tmp_path, monkeypatch, strata):
    markup = write_pack(tmp_path, pack_rows(useful={"text": FORMS, "Компании-участники": "Atlas Energy Solutions"}))
    runs = Counter()

    def agent(title):
        if strata[title] != "useful":
            return answer()
        runs[title] += 1
        name = "Atlas Energy Solutions Inc." if runs[title] == 1 else "Atlas Energy Solutions"
        return answer(companies=[{"name": name, "quote": "Atlas Energy Solutions Inc., Iraya Energies Sdn Bhd"}])

    _, report = run_exam(monkeypatch, markup, {NANO: agent}, "--models", NANO)

    # Устойчивость считает компанию по ключу, а ключ — как сравнение: форма собственности его не меняет.
    assert report["models"][0]["strict"]["by_stratum"]["useful"]["companies_precision"]["stability"] == 1.0


BRACKETS = ("Бета (ПАО) и Дельта (США) подписали контракт 24 сентября 2026 года. "
            "Омега Нефть-Восток (Омега Нефть) начала бурение.")


def test_a_bracket_is_an_abbreviation_only_when_it_is_one_capital_word(tmp_path, monkeypatch, strata):
    # «Имя (АББР)» — правило экзамена, ядро его не знает: аббревиатура — одно слово заглавными, не форма
    # собственности, и аббревиатуры между собой не сравниваются. Иначе общая скобка («(ПАО)», «(США)») или
    # материнская компания в скобке делали бы чужие компании одной.
    markup = write_pack(tmp_path, pack_rows(
        useful={"text": BRACKETS, "Компании-участники": "Альфа (ПАО); Гамма (США); Омега Нефть"},
        technology={"text": BRACKETS, "Компании-участники": "Дельта"}))
    others = [{"name": "Бета (ПАО)", "quote": "Бета (ПАО) и Дельта (США) подписали"},
              {"name": "Дельта (США)", "quote": "Дельта (США) подписали контракт"},
              {"name": "Омега Нефть-Восток (Омега Нефть)", "quote": "Омега Нефть-Восток (Омега Нефть) начала бурение"}]
    delta = [{"name": "Дельта (США)", "quote": "Дельта (США) подписали контракт"}]

    _, report = run_exam(monkeypatch, markup, {NANO: by_stratum(strata, useful=answer(companies=others),
                                                                technology=answer(companies=delta))}, "--models", NANO)

    by_part = report["models"][0]["strict"]["by_stratum"]
    useful, technology = by_part["useful"]["companies_precision"], by_part["technology"]["companies_precision"]
    assert (useful["named"], useful["matched"]) == (17 * 3 * 3, 0)
    # Одно заглавное слово в скобке не делает компанию другой: «Дельта (США)» — это «Дельта».
    assert (technology["named"], technology["matched"]) == (10 * 3, 10 * 3)


ROSNEFTEGAZ = "Роснефтегаз и Сибнефть подписали контракт 24 сентября 2026 года; Роснефть и Сибур в сделке не участвуют."


def test_a_name_that_only_begins_like_the_experts_is_another_company(tmp_path, monkeypatch, strata):
    # Общее начало слова — не падеж: «Роснефть» — не «Роснефтегаз», «Сибур» — не «Сибнефть».
    markup = write_pack(tmp_path, pack_rows(business={"text": ROSNEFTEGAZ,
                                                      "Компании-участники": "Роснефтегаз; Сибнефть"}))
    wrong = [{"name": "Роснефть", "quote": "Роснефть и Сибур в сделке не участвуют"},
             {"name": "Сибур", "quote": "Роснефть и Сибур в сделке не участвуют"}]

    _, report = run_exam(monkeypatch, markup, {NANO: by_stratum(strata, business=answer(companies=wrong))})

    business = report["models"][0]["strict"]["by_stratum"]["business"]
    # Обе дошли до карточки (названы в своей цитате), но это не компании эксперта.
    assert (business["companies_precision"]["named"], business["companies_precision"]["matched"]) == (4 * 3 * 2, 0)


SHORT_NAMES = "BP и H&P подписали контракт на бурение 24 сентября 2026 года."


def test_short_company_names_named_in_the_text_are_not_invented(tmp_path, monkeypatch, strata):
    # У коротких имён нет слов от трёх букв — их ищут в тексте целиком; иначе верная карточка «выдумывала» бы BP.
    markup = write_pack(tmp_path, pack_rows(business={"text": SHORT_NAMES,
                                                      "Компании-участники": "BP; Helmerich & Payne"}))
    named = [{"name": "BP", "quote": "BP и H&P подписали контракт"}, {"name": "H&P", "quote": "BP и H&P подписали"}]

    _, report = run_exam(monkeypatch, markup, {NANO: by_stratum(strata, business=answer(companies=named))})

    business = report["models"][0]["strict"]["by_stratum"]["business"]
    assert (business["invented_companies"]["agent"], business["companies_recall"]["matched"]) == (0, 4 * 3 * 2)


GAZPROM_NEFT_ONLY = "Газпром нефть начала бурение на Приобском месторождении 24 сентября 2026 года."
GAZPROM_AND_GAZPROM_NEFT = "Газпром и Газпром нефть подписали соглашение 24 сентября 2026 года."


def test_exam_looks_for_a_company_in_the_text_by_the_core_rule(tmp_path, monkeypatch, strata):
    # Компанию в тексте экзамен ищет по правилу ядра: длинное имя справочника закрывает короткое. Где «Газпром»
    # только внутри «Газпром нефть», цитата-обрывок «Газпром» до карточки не доходит — выдуманной нет. Где он стоит
    # и отдельно, он в карточке, и экзамен его не закрывает: строже карточки экзамен не мерит.
    markup = write_pack(tmp_path, pack_rows(
        business={"text": GAZPROM_NEFT_ONLY, "Компании-участники": "Газпром нефть"},
        technology={"text": GAZPROM_AND_GAZPROM_NEFT, "Компании-участники": "Газпром; Газпром нефть"}))
    cut = [{"name": "Газпром", "quote": "Газпром"},
           {"name": "Газпром нефть", "quote": "Газпром нефть начала бурение"}]
    both = [{"name": "Газпром", "quote": "Газпром"}, {"name": "Газпром нефть", "quote": "Газпром нефть подписали"}]

    _, report = run_exam(monkeypatch, markup, {NANO: by_stratum(strata, business=answer(companies=cut),
                                                                technology=answer(companies=both))})

    shares = report["models"][0]["strict"]["by_stratum"]
    business, technology = shares["business"], shares["technology"]
    assert (business["invented_companies"]["agent"], business["companies_precision"]["named"],
            business["companies_recall"]["matched"]) == (0, 4 * 3, 4 * 3)
    assert (technology["invented_companies"]["agent"], technology["companies_recall"]["matched"]) == (0, 10 * 3 * 2)


def test_broken_side_files_and_short_runs_are_refused_before_any_paid_call(tmp_path, monkeypatch, capsys):
    markup = write_pack(tmp_path, pack_rows())
    model = ExamModel({NANO: lambda title: answer(), MINI: lambda title: answer()})
    monkeypatch.setattr(signal_card, "make_client", lambda: model)
    # Файлы рядом с манифестом пишут люди и выгрузка с ядра: битый — отказ с именем файла, до вызовов модели.
    for name, content in (("signal_card_exam_tolerances.json", "[]"),
                          ("signal_card_exam_tolerances.json", '{"article:1": ["SLB"]}'),
                          ("signal_card_exam_judge.json", '{"signal:25": "2026-09-24"}'),
                          ("signal_card_exam_aliases.json", '{"name": "SLB"}')):
        (tmp_path / name).write_text(content, encoding="utf-8")
        with pytest.raises(SystemExit, match=name):
            cli.main(["signal-card-exam", "--markup", str(markup)])
        (tmp_path / name).unlink()
    # Экзамен — три прогона на сигнал (спека); меньше — только на настройке.
    for runs in ("0", "1"):
        with pytest.raises(SystemExit, match="--runs"):
            cli.main(["signal-card-exam", "--markup", str(markup), "--runs", runs])
    cli.main(["signal-card-exam", "--markup", str(markup), "--part", "tuning", "--runs", "1", "--dry-run"])
    assert "сигналов 15, прогонов на сигнал 1" in capsys.readouterr().out
    assert model.calls == []


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
    # Судья: у десяти технологий дата верна, у пяти не названа; у бизнеса — чужая дата. Связь в выгрузке судьи
    # осталась, но экзамен карточки её не сравнивает (#132: связь ставит «Отбор»).
    judge = {number: {"event_date": "2026-09-24" if index < 10 else None if index < 15 else "2026-09-01",
                      "oilfield_relevance": "direct" if index < 15 or index >= 18 else "none"}
             for index, number in enumerate(radar)}
    (tmp_path / "signal_card_exam_judge.json").write_text(json.dumps(judge), encoding="utf-8")

    model, report = run_exam(monkeypatch, markup, {NANO: by_stratum(strata)})

    assert len(model.calls) == 35 * 3  # судья — без вызовов модели
    strict = report["judge"]["strict"]
    assert strict["rows"] == 20
    assert (strict["event_date"]["agent"], strict["event_date"]["constant"]) == (0.5, 1.0)
    assert "oilfield_relevance" not in strict


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


def test_a_date_known_to_the_year_or_month_is_right_when_the_expert_date_falls_in_it(tmp_path, monkeypatch, strata):
    # Дня в тексте нет — «Карточка» пишет только год или месяц (#132), ядро понимает такую дату как период. Экзамен
    # считает её верной, если дата эксперта (24.09.2026) в этом периоде (решение владельца 26.10).
    markup = write_pack(tmp_path, pack_rows())
    agent = by_stratum(strata, business=answer(event_date="2026"), technology=answer(event_date="2026-09"),
                       noise=answer(event_date="2025"))

    _, report = run_exam(monkeypatch, markup, {NANO: agent})

    metrics = report["models"][0]["strict"]["overall"]
    assert metrics["event_date"]["errors"] == 4 * 3  # только шумные: 2025 год, дата эксперта не в нём
    assert metrics["invented_dates"]["agent"] == 0


def test_readable_report_lies_next_to_the_manifest_in_two_views(tmp_path, monkeypatch, strata, capsys):
    markup = write_pack(tmp_path, pack_rows(noise={"Связь с нефтесервисом": "нет связи"}))

    run_exam(monkeypatch, markup, {NANO: by_stratum(strata)})

    [readable] = tmp_path.glob("signal_card_exam_report_*.md")
    assert str(readable) in capsys.readouterr().out
    text = readable.read_text(encoding="utf-8")
    assert "Решение: **gpt-5.4-nano** — самая дешёвая из прошедших пороги" in text
    assert "| Атрибут | Порог | С допусками | Строго | Константа | Устойчивость | |" in text
    assert "Связь с нефтесервисом" not in text
    assert "| Дата события — доля ошибок | не больше константы | 0% | 0% | 100% | 100% | ✓ |" in text
    assert "Допусков нет: отчёт с допусками совпадает со строгим." in text
    # По долям — число строк доли на экзамене, а не ответов (строки × прогоны).
    assert ("| Атрибут | бизнес-сигналов радара (4) | шумных статей ленты (4) | технологий на радаре (10) "
            "| полезных статей ленты (17) |") in text
    assert "## Без ИИ (словарём)" in text
