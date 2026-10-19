"""Читаемый отчёт экзамена агента «Карточка сигнала» (#98): Markdown рядом с JSON, вне репозитория.

Пороги — по отчёту с допусками, строгий — для сравнения, константа и устойчивость — рядом. Доли набора,
базовая линия без ИИ и судья радара — справочно: решения о модели они не меняют.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from oiltech_digest.signal_card_pack import STRATUM_RU

# Строка таблицы: метрика отчёта → подпись и порог (как в спеке и поправках 18.10).
_ROWS = (
    ("companies_precision", "Компании — точность (по упоминаниям)", "≥ 90%"),
    ("companies_recall", "Компании — полнота (по упоминаниям)", "≥ 80%"),
    ("region", "Регион", "≥ 85%"),
    ("event_type", "Тип события", "≥ 75%"),
    ("technology_class", "Класс технологии", "≥ 75%"),
    ("maturity", "Зрелость", "≥ 75%"),
    ("oilfield_relevance", "Связь с нефтесервисом (среднее по значениям)", "≥ 75%"),
    ("event_date", "Дата события — доля ошибок", "не больше константы"),
    ("event_date_source", "Источник даты (где дата события ≠ дате публикации)", "—"),
    ("invented_companies", "Выдуманные компании", "0"),
    ("invented_dates", "Выдуманные даты", "0"),
)
_COUNTS = ("invented_companies", "invented_dates")


def render(report: dict[str, Any]) -> str:
    part = "экзамен" if report["part"] == "exam" else "настройка"
    signals = len(report["split"][report["part"]])
    lines = [
        f"# Экзамен агента «Карточка сигнала» — {report['created_at']}", "",
        f"Решение: {_decision(report['decision'])}", "",
        f"Часть: {part} — сигналов {signals}, прогонов на сигнал {report['runs']}; инструкция "
        f"{report['prompt_version']}, справочники {report['dictionaries_version']}, рассуждение {report['reasoning']}.",
        f"Манифест: `{Path(report['manifest']['file']).name}`, разметка: `{Path(report['markup']['file']).name}`.",
        _file_line(report["files"].get("tolerances"), "Допуски",
                   "Допусков нет: отчёт с допусками совпадает со строгим."),
        _file_line(report["files"].get("aliases"), "Алиасы экзамена", "Алиасов экзамена нет."),
        "",
    ]
    for model in report["models"]:
        lines += _model(model, report["runs"])
    lines += _baseline(report["baseline"])
    lines += _judge(report.get("judge"))
    return "\n".join(lines) + "\n"


def _decision(decision: dict[str, Any]) -> str:
    if decision["model"]:
        return f"**{decision['model']}** — " + decision["reason"].split(" — ", 1)[1]
    return decision["reason"]


def _file_line(mark: dict[str, str] | None, title: str, absent: str) -> str:
    return f"{title}: `{Path(mark['file']).name}` (sha256 {mark['sha256'][:12]}…)." if mark else absent


def _model(model: dict[str, Any], runs: int) -> list[str]:
    strict, lenient = model["strict"]["overall"], model["lenient"]["overall"]
    lines = [
        f"## {model['model']} — " + ("пороги пройдены" if model["passed"]
                                     else "не прошла: " + ", ".join(model["lenient"]["failed"])), "",
        f"Вызовов {model['calls']}, токенов {model['input_tokens'] // 1000} тыс. входа и "
        f"{model['output_tokens'] // 1000} тыс. выхода, ${model['cost_usd']:.3f}; без карточки "
        f"{model['without_card']}.", "",
        "| Атрибут | Порог | С допусками | Строго | Константа | Устойчивость | |",
        "|---|---|---|---|---|---|---|",
    ]
    for key, title, threshold in _ROWS:
        row = lenient[key]
        form = _count if key in _COUNTS else _share
        mark = {True: "✓", False: "✗"}.get(row.get("passed"), "")
        lines.append(f"| {title} | {threshold} | {form(row['agent'])} | {form(strict[key]['agent'])} | "
                     f"{_share(row.get('constant'))} | {_share(row.get('stability'))} | {mark} |")
    by_signal = lenient["companies_precision"]["by_signal"], lenient["companies_recall"]["by_signal"]
    lines += [f"| Компании по сигналам — точность / полнота | — | {_share(by_signal[0])} / {_share(by_signal[1])} "
              f"| {_share(strict['companies_precision']['by_signal'])} / "
              f"{_share(strict['companies_recall']['by_signal'])} | — | — | |", ""]
    strata = model["lenient"]["by_stratum"]
    # В скобках — строки доли: у каждой строки ровно runs ответов (без карточки — тоже ответ).
    lines += ["По долям набора (с допусками, справочно — на экзамене в доле от 4 строк):", "",
              "| Атрибут | " + " | ".join(f"{STRATUM_RU.get(stratum, stratum)} ({strata[stratum]['region']['n'] // runs})"
                                         for stratum in strata) + " |",
              "|---|" + "---|" * len(strata)]
    for key, title, _ in _ROWS:
        form = _count if key in _COUNTS else _share
        lines.append(f"| {title} | " + " | ".join(form(strata[stratum][key]["agent"]) for stratum in strata) + " |")
    return lines + [""]


def _baseline(baseline: dict[str, Any]) -> list[str]:
    lines = ["## Без ИИ (словарём)", "", "Компании — справочник алиасов, регион — словарь мест.", "",
             "| | С допусками | Строго |", "|---|---|---|"]
    for key, title in (("companies_precision", "Компании — точность"), ("companies_recall", "Компании — полнота"),
                       ("region", "Регион")):
        lines.append(f"| {title} | {_share(baseline['lenient'][key])} | {_share(baseline['strict'][key])} |")
    return lines + [""]


def _judge(judge: dict[str, Any] | None) -> list[str]:
    if not judge:
        return ["## Судья радара против эксперта", "",
                "Значений судьи нет: выгрузите их с ядра командой `signal-card-exam-judge` рядом с манифестом.", ""]
    lenient, strict = judge["lenient"], judge["strict"]
    lines = ["## Судья радара против эксперта", "",
             f"Радарных строк набора: {lenient['rows']}; без вызовов модели — значения, которые показывает радар.", "",
             "| | С допусками | Строго | Константа |", "|---|---|---|---|"]
    for key, title in (("event_date", "Дата события — доля ошибок"),
                       ("oilfield_relevance", "Связь с нефтесервисом (среднее по значениям)")):
        lines.append(f"| {title} | {_share(lenient[key]['agent'])} | {_share(strict[key]['agent'])} | "
                     f"{_share(lenient[key]['constant'])} |")
    return lines + [""]


def _share(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _count(value: int | None) -> str:
    return "—" if value is None else str(value)
