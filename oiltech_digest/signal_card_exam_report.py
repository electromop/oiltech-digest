"""Читаемый отчёт экзамена агента «Карточка сигнала» (#98): Markdown рядом с JSON, вне репозитория.

Пороги — по отчёту с допусками, строгий — для сравнения, константа и устойчивость — рядом. Доли набора,
базовая линия без ИИ и судья радара — справочно: решения о модели они не меняют.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from oiltech_digest.signal_card_exam_score import THRESHOLDS
from oiltech_digest.signal_card_pack import STRATUM_RU

# Строка таблицы: метрика отчёта → подпись; порог — из THRESHOLDS счёта или правило без числа (спека, поправки 18.10).
_ROWS = (
    ("companies_precision", "Компании — точность (по упоминаниям)"),
    ("companies_recall", "Компании — полнота (по упоминаниям)"),
    ("region", "Регион"),
    ("event_type", "Тип события"),
    ("technology_class", "Класс технологии"),
    ("maturity", "Зрелость"),
    ("event_date", "Дата события — доля ошибок"),
    ("event_date_source", "Источник даты (где дата события ≠ дате публикации)"),
    ("invented_companies", "Выдуманные компании"),
    ("invented_dates", "Выдуманные даты"),
)
_RULES = {"event_date": "не больше константы", "invented_companies": "0", "invented_dates": "0"}
PART_RU = {"exam": "экзамен", "tuning": "настройка"}
_COUNTS = ("invented_companies", "invented_dates")
REPORT_PREFIX = "signal_card_exam_report_"


def reason(decision: str | None, tried: list[dict[str, Any]]) -> str:
    """Почему решение экзамена такое; сама модель — в decision["model"]."""
    if decision:
        return "самая дешёвая из прошедших пороги"
    return "ни одна модель не прошла пороги: " + "; ".join(
        f"{one['model']} — {', '.join(one['lenient']['failed'])}" for one in tried)


def write(folder: Path, report: dict[str, Any]) -> tuple[Path, Path]:
    """Отчёт рядом с манифестом: JSON с ответами и метриками и читаемый .md; имя — по времени записи, прежние
    отчёты не перезаписываются."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path, number = folder / f"{REPORT_PREFIX}{stamp}.json", 1
    while path.exists():
        number += 1
        path = folder / f"{REPORT_PREFIX}{stamp}_{number}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    readable = path.with_suffix(".md")
    readable.write_text(render(report), encoding="utf-8")
    return path, readable


def read(path: Path) -> dict[str, Any]:
    """Сохранённый отчёт — для пересчёта без вызовов модели. Файл лежит на диске и его правят руками: устройство
    проверяется до счёта, битый отчёт — ValueError с именем файла, а не трасса посреди пересчёта."""
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name}: не JSON ({exc})") from exc
    problems = _problems(saved)
    if problems:
        raise ValueError(f"{path.name}: не отчёт экзамена — " + "; ".join(problems))
    return saved


def _problems(saved: Any) -> list[str]:
    if not isinstance(saved, dict):
        return ["нужен объект"]
    split, part = saved.get("split"), saved.get("part")
    problems = [] if part in PART_RU else ["часть — exam или tuning"]
    if not (isinstance(split, dict) and all(isinstance(split.get(key), list) for key in PART_RU)):
        problems.append("деление — списки tuning и exam")
    if not (isinstance(saved.get("runs"), int) and saved["runs"] >= 1):
        problems.append("прогонов на сигнал — число от 1")
    problems += [f"нет {key}" for key in ("reasoning", "prompt_version", "dictionaries_version")
                 if not isinstance(saved.get(key), str)]
    problems += [f"{key} — без sha256" for key in ("manifest", "markup")
                 if not (isinstance(saved.get(key), dict) and isinstance(saved[key].get("sha256"), str))]
    ids = set(split[part]) if not problems and isinstance(split, dict) else set()
    models = saved.get("models")
    if not (isinstance(models, list) and models and all(_model_entry(one, ids) for one in models)):
        problems.append("модели — с именем, ценой и ответами (прогон, строка своей части, токены, попытки)")
    return problems


def _model_entry(one: Any, ids: set[str]) -> bool:
    return (isinstance(one, dict) and isinstance(one.get("model"), str) and isinstance(one.get("answers"), list)
            and all(isinstance(one.get(key), (int, float)) for key in ("calls", "input_tokens", "output_tokens",
                                                                       "cost_usd"))
            and all(isinstance(answer, dict) and answer.get("id") in ids and isinstance(answer.get("run"), int)
                    and all(isinstance(answer.get(key), int) for key in ("input_tokens", "output_tokens", "attempts"))
                    for answer in one["answers"]))


def render(report: dict[str, Any]) -> str:
    part = PART_RU[report["part"]]
    signals = len(report["split"][report["part"]])
    lines = [
        f"# Экзамен агента «Карточка сигнала» — {report['created_at']}", "",
        f"Решение: {_decision(report['decision'])}", "",
        *([f"Пересчёт отчёта {report['rescored_from']} без вызовов модели: ответы те же, счёт — по нынешним "
           "проверкам ядра, допускам и алиасам; цена — исходного прогона.", ""] if report.get("rescored_from") else []),
        *(["Разметка изменилась после исходного прогона: счёт — по новой разметке.", ""]
          if report.get("markup_changed") else []),
        *([f"Исходный прогон остановился на {report['original_decision']}: модели дороже не пробовались — для "
           "решения нужен прогон.", ""] if report.get("original_decision") and not report["decision"]["model"] else []),
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
    return f"**{decision['model']}** — {decision['reason']}" if decision["model"] else decision["reason"]


def _threshold(key: str) -> str:
    return f"≥ {THRESHOLDS[key]:.0%}" if key in THRESHOLDS else _RULES.get(key, "—")


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
    for key, title in _ROWS:
        row = lenient[key]
        form = _count if key in _COUNTS else _share
        mark = {True: "✓", False: "✗"}.get(row.get("passed"), "")
        lines.append(f"| {title} | {_threshold(key)} | {form(row['agent'])} | {form(strict[key]['agent'])} | "
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
    for key, title in _ROWS:
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
    for key, title in (("event_date", "Дата события — доля ошибок"),):
        lines.append(f"| {title} | {_share(lenient[key]['agent'])} | {_share(strict[key]['agent'])} | "
                     f"{_share(lenient[key]['constant'])} |")
    return lines + [""]


def _share(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _count(value: int | None) -> str:
    return "—" if value is None else str(value)
