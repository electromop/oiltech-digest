"""Пробный прогон широкого поиска (#138, спека #128), шов 2: команда radar-search-trial. Сухой прогон по умолчанию
печатает число вызовов и цену и ничего не пишет; запуск делает один широкий прогон через настоящий клиент воркера NL
(подставные поиск GPT, страницы и модель «Отбора»), находки проходят «Отбор», витрины, разделы отклонённого и
сигналы платформы не меняются; отчёт считает материалы, долю уже собранного сбором, решения «Отбора», цену и полноту по
эталону эксперта.
"""

from datetime import date

import pytest

from oiltech_digest import cli, config, otbor, signal_discovery, signal_research
from oiltech_digest.db import repository
from tests.otbor_radar_support import NO_PAGE, SearchModel, _DedupJudge, event, page
from tests.otbor_support import OtborModel, answer, decide, reject, showcase
from tests.otbor_support import contour  # noqa: F401 — фикстура
from tests.signal_card_support import DRILLING, admin, api_get, radar_finding, run_worker, tag, user  # noqa: F401
from tests.test_signal_search_broad import MARKET, collected

PERIOD = ["--since", "2026-10-01", "--until", "2026-10-31"]
DEAL_URL, DEAL = "https://adnoc.example/news/rig-deal", "ADNOC Drilling buys three jack-up rigs for 800 million dollars"
TECH_URL, TECH = "https://slb.example/news/autonomous-rig", "SLB runs an autonomous drilling rig for Aramco in 40 wells"
KNOWN_URL = "https://radar.example/known-card"
COLLECTED_URL = "https://www.upstream.example/news/rig-contract"
INVENTED_URL = "https://invented.example/news/record"


def jobs():
    with repository.get_connection() as conn:
        return conn.execute("SELECT kind, status, payload_json FROM background_jobs ORDER BY id").fetchall()


def radar_job_id():
    with repository.get_connection() as conn:
        [(job_id,)] = conn.execute("SELECT id FROM background_jobs WHERE kind = 'signal_discovery'").fetchall()
    return int(job_id)


def signals_snapshot():
    """Все сигналы: номер, метка пути, решение «Отбора», время последней находки, ссылки."""
    with repository.get_connection() as conn:
        return conn.execute(
            "SELECT s.id, s.selector, s.otbor_decision, s.last_seen_at,"
            " ARRAY(SELECT e.source_url FROM signal_evidence e WHERE e.signal_id = s.id ORDER BY e.id)"
            " FROM signals s ORDER BY s.id").fetchall()


def run_trial(monkeypatch, events, pages, answers, *args, cited=None):
    """radar-search-trial --run за октябрь → воркер NL полосы агентов с подставными поиском и страницами → ядро ставит
    «Отбор» находок → воркер с подставной моделью «Отбора» → ядро. Номер задачи прогона, поиск и модель «Отбора»."""
    search = SearchModel(events, cited)
    monkeypatch.setattr(signal_research, "make_client", lambda offline=False: search)
    monkeypatch.setattr(signal_discovery, "_fetch_full_text",
                        lambda url, fallback_title="", **kwargs: pages.get(url, NO_PAGE))
    monkeypatch.setattr(signal_discovery, "_radar_client", lambda: _DedupJudge(False))
    model = OtborModel(answers)
    monkeypatch.setattr(otbor, "make_client", lambda: model)
    cli.main(["radar-search-trial", *PERIOD, "--otbor-model", "gpt-6-sol", "--run", *args])
    job_id = radar_job_id()
    run_worker("external-agents")
    run_worker("external-ai")
    return job_id, search, model


def test_dry_run_prints_calls_and_price_and_writes_nothing(user, contour, monkeypatch, capsys):
    tag(DRILLING)
    tag(MARKET)

    cli.main(["radar-search-trial", *PERIOD, "--per-topic", "20", "--model", "gpt-6.1-sol", "--otbor-model", "gpt-6-sol"])

    out = capsys.readouterr().out
    # Две темы (с рынком), по одному вызову; «Отбор» — от половины до всего потолка. Цена — по ставкам config: поиск
    # 74 тыс. входа × $2/млн + 5,1 тыс. выхода × $10/млн + 15,6 поиска × $0,01 на тему, верхняя граница растит выход и
    # поиски в 20/12 раза; вызов «Отбора» 4 900 × $2/млн + 600 × $10/млн, с повтором ×1,1.
    assert "[сухой прогон]: тем 2, период с 2026-10-01 по 2026-10-31, поиск gpt-6.1-sol low, до 20" in out
    assert "Вызовов поиска 2 (до 4 с повтором пустого ответа), «Отбора» от 20 до 40" in out
    assert "поиск от 0.71 до 0.99, «Отбор» от 0.35 до 0.7, всего от 1.06 до 1.68" in out
    assert "Ничего не записано" in out
    assert jobs() == [] and signals_snapshot() == []


@pytest.mark.parametrize("args, problem", [
    (["--otbor-model", ""], "модель «Отбора» не задана"),
    (["--model", "gpt-5", "--otbor-model", "gpt-6-sol"], "снимается 11.12.2026"),
    (["--model", "gpt-7-preview", "--otbor-model", "gpt-6-sol"], "нет строки в OPENAI_MODEL_PRICES"),
    (["--otbor-model", "gpt-6-sol", "--since", "2026-11-01", "--until", "2026-10-01"], "период пуст"),
])
def test_trial_is_refused_without_priced_models_or_a_period(user, contour, monkeypatch, args, problem):
    monkeypatch.setattr(config, "OTBOR_MODEL", "")

    with pytest.raises(SystemExit, match=problem):
        cli.main(["radar-search-trial", *PERIOD, *args, "--run"])

    assert jobs() == []


def test_trial_run_passes_findings_through_otbor_and_changes_no_showcase_section_or_platform_signal(
        user, admin, contour, monkeypatch, capsys):
    tag(DRILLING)
    known = radar_finding("known-card", "Equinor tests a drilling robot offshore Norway", "Текст страницы находки")
    collected(COLLECTED_URL)
    showcases_before = (showcase(user, "technology"), showcase(user, "business"))
    sections_before = api_get("/api/signals", admin, section="all")
    signals_before = signals_snapshot()

    job_id, search, model = run_trial(
        monkeypatch,
        [event(DEAL, DEAL_URL), event(TECH, TECH_URL), event("Equinor tests a drilling robot offshore Norway",
                                                            KNOWN_URL),
         event("Upstream: rig contract in Kuwait", COLLECTED_URL), event("Invented drilling record in Oman",
                                                                        INVENTED_URL)],
        {DEAL_URL: page(DEAL), TECH_URL: page(TECH), KNOWN_URL: page("Equinor tests a drilling robot offshore Norway")},
        {DEAL: answer(), TECH: reject("oil_gas_no_link")},
        cited=[DEAL_URL, TECH_URL, KNOWN_URL, COLLECTED_URL])

    # Широкий промпт за октябрь при выключенном флаге «Отбора» на ядре не нужен: прогон его везёт сам.
    [call] = search.calls
    assert call["schema"] == "radar_topic_research_broad" and "Период: с 2026-10-01 по 2026-10-31" in call["input"]
    # «Отбор» решил обе новые находки: та, что уже на платформе, и ссылка сбора в него не идут.
    assert sorted(line.split(":", 1)[1].strip() for one in model.calls for line in one["input"].splitlines()
                  if line.startswith("Заголовок:")) == sorted([DEAL, TECH])
    # Витрины, разделы отклонённого и сигналы платформы — как до прогона.
    assert (showcase(user, "technology"), showcase(user, "business")) == showcases_before
    assert api_get("/api/signals", admin, section="all") == sections_before
    after = {row[0]: row for row in signals_snapshot()}
    for row in signals_before:
        assert after[row[0]] == row
    trial = [row for row in after.values() if row[1] == repository.SIGNAL_SELECTOR_TRIAL]
    assert sorted(row[4] for row in trial) == [[DEAL_URL], [TECH_URL]] and all(row[2] is None for row in trial)
    # Обычный шаг «Отбора» находки пробного прогона не берёт.
    assert decide(monkeypatch, {}).calls == []

    capsys.readouterr()
    cli.main(["radar-search-trial", "--report", str(job_id)])
    out = capsys.readouterr().out
    assert ("Материалов (ссылок после технических проверок) 4, уже в сборе 1 (25%), новых находок 2, сигналов "
            "платформы найдено снова 1") in out
    assert "«Отбор»: решил 2, принял 1 (50%), сбоев 0, ждут решения 0, спорных 0" in out
    assert "Принятое по категориям: business 1" in out and "Причины отказа: oil_gas_no_link 1" in out
    assert known in [row[0] for row in signals_before]


def test_next_radar_run_finds_the_trial_event_as_a_new_finding(user, contour, monkeypatch):
    tag(DRILLING)
    run_trial(monkeypatch, [event(DEAL, DEAL_URL)], {DEAL_URL: page(DEAL)}, {DEAL: answer()})

    # Обычный прогон радара: находка пробного прогона не держит ни ключ, ни ссылку, ни место в снимке дедупа.
    from tests.otbor_radar_support import run_radar
    run_radar(monkeypatch, [event(DEAL, DEAL_URL)], {DEAL_URL: page(DEAL)})

    with repository.get_connection() as conn:
        rows = conn.execute("SELECT selector, ARRAY(SELECT source_url FROM signal_evidence e WHERE e.signal_id = s.id)"
                            " FROM signals s ORDER BY id").fetchall()
    assert rows == [(repository.SIGNAL_SELECTOR_TRIAL, []), ("otbor", [DEAL_URL])]


def test_report_measures_recall_against_the_expert_reference(user, contour, monkeypatch, capsys, tmp_path):
    import openpyxl

    tag(DRILLING)
    collected(COLLECTED_URL)
    job_id, _, _ = run_trial(monkeypatch, [event(DEAL, DEAL_URL)], {DEAL_URL: page(DEAL)}, {DEAL: answer()})
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Октябрь_2026"
    sheet.append(["Сигнал / технология", "Ссылка"])
    sheet.append(["ADNOC Drilling — покупка трёх СПБУ", DEAL_URL])
    sheet.append(["Upstream — контракт на буровую в Кувейте", COLLECTED_URL])
    sheet.append(["Halliburton Octiv — автоматизация ГРП", "https://halliburton.example/news/octiv"])
    reference = tmp_path / "reference.xlsx"
    book.save(reference)
    capsys.readouterr()

    cli.main(["radar-search-trial", "--report", str(job_id), "--reference", str(reference)])

    out = capsys.readouterr().out
    assert "Полнота по эталону: поиск 1 из 3 (33%), сбор 1, поиск или сбор 2 (67%)" in out
    assert "+ только поиск: ADNOC Drilling — покупка трёх СПБУ" in out
    assert "- не нашёл никто: Halliburton Octiv — автоматизация ГРП" in out


def test_trial_report_refuses_a_job_that_is_not_a_trial(user, contour, monkeypatch):
    tag(DRILLING)
    from tests.otbor_radar_support import run_radar
    run_radar(monkeypatch, [event(DEAL, DEAL_URL)], {DEAL_URL: page(DEAL)})

    with pytest.raises(SystemExit, match="не пробный прогон"):
        cli.main(["radar-search-trial", "--report", str(radar_job_id())])


def test_dates_of_the_period_are_whole_days(user, contour):
    tag(DRILLING)
    from oiltech_digest import signal_search_trial

    trial = signal_search_trial.plan(since=date(2026, 10, 1), until=date(2026, 10, 31), otbor_model="gpt-6-sol")

    # research_period отсчитывает days до period_end: 30 дней до 31.10 — с 01.10.
    assert (trial["days"], trial["until"]) == (30, "2026-10-31")
