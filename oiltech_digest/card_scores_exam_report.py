"""Отчёт экзамена «Карточки» (#133): JSON с ответами и метриками и читаемый Markdown рядом, вне репозитория.

Сверху таблица качества и цены по моделям второго вызова (по ней владелец решает, с чем включать), рядом то, с чем
модель сравнивается без её ответов: константа, эксперт сам с собой, контроль с ядра. Ниже тема и атрибуты основного
вызова и по модели: оба варианта ключевых слов, ступень эксперта против ступени модели (точность «сильного» — это
«кандидаты» «Решения платформы»), ошибка по критериям, итог модели по оценке эксперта (по этим цифрам пересматривают
границы ступеней, если совпадение низкое: решение 30 журнала гриля). Текст отчёта читает владелец: без длинного и
среднего тире.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from oiltech_digest import card_scores_exam_calls, card_scores_exam_control, otbor_exam
from oiltech_digest.card_scores_exam_score import BAND_RULE_RU, EXTRA, THRESHOLDS, VARIANTS
from oiltech_digest.otbor_exam_score import FEED_GROUPS
from oiltech_digest.otbor_pack_format import CATEGORY_PROFILE, EXPERT_SCALE, PROFILE_RU
from oiltech_digest.signal_scoring import BAND_LABELS, BAND_ORDER

REPORT_PREFIX = "card_scores_exam_report_"
PART_RU = {"exam": "экзамен", "tuning": "настройка"}
VARIANT_RU = {"keywords": "с ключевыми словами", "plain": "без ключевых слов"}
_FAILED_RU = {"band": "ступень", "order": "порядок"}
_RULES_RU = {"thresholds": "пороги", "band_rule": "правило ступени", "bands": "границы ступеней",
             "rows": "строки экзамена"}
_RATING_RU = {**{value: short for value, short, _ in EXPERT_SCALE}, EXTRA: "Сильный сверх пакета"}
_FILES = (("rows", otbor_exam.ROWS_NAME), ("split", otbor_exam.SPLIT_NAME), ("control", card_scores_exam_control.NAME),
          ("summaries", card_scores_exam_calls.SUMMARIES_NAME), ("cards", card_scores_exam_calls.CARDS_NAME))
_SPEND_RU = {"summary": "суть", "card": "основной вызов"}


def reason(decision: str | None, tried: list[dict[str, Any]]) -> str:
    """Почему решение экзамена такое; сама модель в decision["model"]."""
    if decision:
        return "самая дешёвая из прошедших ступень и порядок"
    passed = [one["model"] for one in tried if one["passed"]]
    if passed:
        return "прошли только устаревшие модели (" + ", ".join(passed) + "): их экзамен не выбирает"
    return "ни одна модель не прошла: " + "; ".join(f"{one['model']}: {failed_ru(one['failed'])}" for one in tried)


def failed_ru(names: list[str]) -> str:
    return ", ".join(_FAILED_RU.get(name, name) for name in names)


def files(folder: Path) -> dict[str, dict[str, str] | None]:
    """Отметки файлов рядом с манифестом: строки экзамена, деление, выгрузка с ядра, суть, основной вызов."""
    return {name: {"file": str(folder / file), "sha256": hashlib.sha256((folder / file).read_bytes()).hexdigest()}
            if (folder / file).exists() else None for name, file in _FILES}


def month(exam: dict[str, Any]) -> dict[str, Any] | None:
    """Сколько сигналов в месяц пойдёт на второй вызов из ленты: доля бизнеса и технологии среди взятого экспертом на
    случайных статьях пакета, взвешенная размерами страт окна (выгрузка otbor-exam-control). Нет размеров — None."""
    strata = (exam.get("control") or {}).get("strata")
    feed = [item for item in exam["items"] if item["group"] in FEED_GROUPS]
    counts = Counter(item["group"] for item in feed)
    if not strata or not all(counts[group] and int(strata.get(group) or 0) > 0 for group in FEED_GROUPS):
        return None
    scored = sum(int(strata[item["group"]]) / counts[item["group"]] for item in feed
                 if item["labels"]["decision"] == "accept" and item["labels"]["category"] in CATEGORY_PROFILE)
    return {"feed": sum(int(strata[group]) for group in FEED_GROUPS), "signals": round(scored)}


def write(folder: Path, report: dict[str, Any]) -> tuple[Path, Path]:
    """Отчёт рядом с манифестом; имя по времени записи, прежние отчёты не перезаписываются."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path, number = folder / f"{REPORT_PREFIX}{stamp}.json", 1
    while path.exists():
        number += 1
        path = folder / f"{REPORT_PREFIX}{stamp}_{number}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    readable = path.with_suffix(".md")
    readable.write_text(render(report), encoding="utf-8")
    return path, readable


def read(path: Path) -> dict[str, Any]:
    """Сохранённый отчёт для пересчёта без вызовов модели. Файл лежит на диске и его правят руками: устройство
    проверяется до счёта, битый отчёт — ValueError с именем файла, а не трасса посреди пересчёта."""
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name}: не JSON ({exc})") from exc
    problems = _problems(saved)
    if problems:
        raise ValueError(f"{path.name}: не отчёт экзамена «Карточки»: " + "; ".join(problems))
    return saved


def _problems(saved: Any) -> list[str]:
    if not isinstance(saved, dict):
        return ["нужен объект"]
    split, rows, runs = saved.get("split"), saved.get("rows"), saved.get("runs")
    problems = [] if saved.get("part") in PART_RU else ["часть exam или tuning"]
    if not (isinstance(split, dict) and all(isinstance(split.get(key), list) for key in PART_RU)):
        problems.append("деление: списки tuning и exam")
    if not (isinstance(runs, int) and runs >= 1):
        problems.append("прогонов на строку: число от 1")
    problems += [f"нет {key}" for key in ("rubrics_version", "dictionaries_version", "card_model", "summary_model",
                                          "card_prompt_version", "scores_prompt_version", "card_reasoning",
                                          "scores_reasoning")
                 if not isinstance(saved.get(key), str)]
    problems += [f"{key} без sha256" for key in ("manifest", "markup")
                 if not (isinstance(saved.get(key), dict) and isinstance(saved[key].get("sha256"), str))]
    if not (isinstance(rows, list) and rows and all(isinstance(one, dict) and isinstance(one.get("id"), str)
                                                    for one in rows)):
        return problems + ["строки rows с номерами"]
    ids = {one["id"] for one in rows}
    summaries, cards = saved.get("summaries"), (saved.get("cards") or {}).get("answers")
    if not (isinstance(summaries, dict) and all(isinstance(summaries.get(number), str) for number in ids)):
        problems.append("суть summaries у каждой строки")
    if not (isinstance(cards, dict) and all(isinstance(cards.get(number), list) and len(cards[number]) == runs
                                            and all(isinstance(card, dict) for card in cards[number])
                                            for number in ids)):
        problems.append("ответы основного вызова cards.answers у каждой строки по прогонам")
    models = saved.get("models")
    if not (isinstance(models, list) and models and all(_model_entry(one, ids, runs) for one in models)):
        problems.append("модели с именем, ценой и ответами (прогон, строка, вызовы, токены)")
    return problems


def _model_entry(one: Any, ids: set[str], runs: Any) -> bool:
    return (isinstance(one, dict) and isinstance(one.get("model"), str) and isinstance(one.get("answers"), list)
            and all(isinstance(one.get(key), (int, float)) for key in ("calls", "input_tokens", "output_tokens",
                                                                       "cost_usd"))
            and all(isinstance(answer, dict) and answer.get("id") in ids and isinstance(answer.get("run"), int)
                    and 1 <= answer["run"] <= runs
                    and all(isinstance(answer.get(key), int) for key in ("input_tokens", "output_tokens", "attempts"))
                    for answer in one["answers"]))


# --- Читаемый отчёт ------------------------------------------------------------------------------


def render(report: dict[str, Any]) -> str:
    rows = report["rows"]
    pack = [row for row in rows if row["rating"] != EXTRA]
    profiles = Counter(PROFILE_RU[row["profile"]].lower() for row in pack)
    skipped = report.get("strong_skipped") or {}
    lines = [
        f"# Экзамен «Карточки»: тема и баллы, {report['created_at']}", "",
        f"Решение: {_decision(report['decision'])}", "",
        *_rescore_notes(report),
        f"Часть: {PART_RU[report['part']]}, строк пакета {len(pack)} ("
        + ", ".join(f"{name} {count}" for name, count in sorted(profiles.items())) + f"), «сильных сверх пакета» "
        f"{len(rows) - len(pack)}" + (" (не вошли: " + ", ".join(
            f"{card_scores_exam_control.SKIPPED.get(key, key)} {value}" for key, value in skipped.items()) + ")"
            if skipped else "") + f"; прогонов на строку {report['runs']}.",
        f"Основной вызов: {report['card_model']}, инструкция {report['card_prompt_version']}, рассуждение "
        f"{report['card_reasoning']}. Второй вызов: инструкция {report['scores_prompt_version']}, линейки "
        f"{report['rubrics_version']}, рассуждение {report['scores_reasoning']}. Суть: {report['summary_model']}.",
        "Вход второго вызова: категория, отрасль и связь эксперта (у «сильных сверх пакета» машинные), применения у "
        "строк пакета нет: эксперт его не размечал.",
        f"Ступени {'/'.join(map(str, report['rules']['bands']))}; в настройке сейчас "
        f"{VARIANT_RU['keywords' if report['keywords_setting'] else 'plain']}. Утверждено: "
        f"{report.get('approved_scores') or 'нет'}.",
        f"Манифест: `{Path(report['manifest']['file']).name}`, разметка: `{Path(report['markup']['file']).name}`.",
        _spent(report), "",
    ]
    lines += _quality_and_price(report)
    lines += _theme(report)
    lines += _attributes(report)
    for model in report["models"]:
        lines += _model(model, report)
    return "\n".join(lines) + "\n"


def _decision(decision: dict[str, Any]) -> str:
    if not decision["model"]:
        return decision["reason"]
    return f"**{decision['model']}**, {VARIANT_RU[decision['variant']]}: {decision['reason']}"


def _rescore_notes(report: dict[str, Any]) -> list[str]:
    lines = []
    if report.get("rescored_from"):
        lines += [f"Пересчёт отчёта {report['rescored_from']} без вызовов модели: ответы те же, счёт по нынешним "
                  "проверке кодом, выгрузке с ядра и порогам; цена исходного прогона.", ""]
    if report.get("markup_changed"):
        lines += ["Разметка изменилась после исходного прогона: счёт по новой разметке.", ""]
    if report.get("control_changed"):
        lines += ["Выгрузка с ядра изменилась после исходного прогона: порог ступени по новой выгрузке.", ""]
    if report.get("rules_changed"):
        lines += ["**Правила изменились после исходного прогона** ("
                  + ", ".join(_RULES_RU.get(name, name) for name in report["rules_changed"])
                  + "): решение по ним не годится без нового утверждения владельцем.", ""]
    if report.get("original_decision") and not report["decision"]["model"]:
        lines += [f"Исходный прогон остановился на {report['original_decision']}: модели дороже не пробовались, для "
                  "решения нужен прогон.", ""]
    return lines


def _spent(report: dict[str, Any]) -> str:
    """Оплата сути и основного вызова: в этом запуске или (у пересчёта) в исходном прогоне."""
    done = [f"{_SPEND_RU[stage]}: вызовов {one['calls']}, {_usd(one['cost_usd'])}"
            for stage, one in (report.get("spend") or {}).items() if stage in _SPEND_RU]
    if report.get("rescored_from"):
        return ("Пересчёт ничего не оплачивал; в исходном прогоне: " + "; ".join(done) + "." if done
                else "Пересчёт ничего не оплачивал.")
    return ("Оплачено в этом запуске: " + "; ".join(done) + "; остальное взято из файлов рядом с манифестом."
            if done else "Суть и основной вызов взяты из файлов рядом с манифестом: в этом запуске не оплачивались.")


def _quality_and_price(report: dict[str, Any]) -> list[str]:
    month = report.get("month")
    base = report["baseline"]
    lines = ["## Качество и цена", "",
             f"Пороги на строках пакета: ступень {BAND_RULE_RU} ({_share(base['control']['band'], 1)} на "
             f"{base['control']['rows']} строках с баллом ядра), порядок не ниже {THRESHOLDS['order']:.0%} (хотя бы в "
             "одном варианте ключевых слов); вариант модели: ближе к эксперту. "
             "«Сильные сверх пакета» в решение не входят: доля ответов «сильный» у них и порядок вместе с ними рядом.",
             "", "| Модель | Правило | Вариант | Ступень | Там же, где ядро | Порядок | Сверх пакета: «сильный» "
             "| Порядок с ними | Без баллов | Экзамен | На сигнал | Лента за 30 дней |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for model in report["models"]:
        scores = model["scores"][model["variant"]]
        answers = model["scores"]["answers"]
        per_signal = model["cost_usd"] / answers["answers"] if answers["answers"] else None
        deprecated = " (устаревшая)" if model["model"] in (report.get("deprecated") or {}) else ""
        lines.append(
            f"| {model['model']}{deprecated} | {'прошла' if model['passed'] else 'нет: ' + failed_ru(model['failed'])} "
            f"| {VARIANT_RU[model['variant']]} | {_share(scores['band']['agent'], 1)} "
            f"| {_share(scores['band']['same_rows'], 1)} "
            f"| {_share(scores['order']['agent'], 1)} | {_share(scores['band']['extra_strong'])} "
            f"| {_share(scores['order']['with_extra'])} | {answers['failures']} | {_usd(model['cost_usd'])} "
            f"| {_usd(per_signal)} | {_usd(per_signal * month['signals']) if per_signal and month else 'нет'} |")
    control = base["control"]
    lines += ["", f"Без модели, на строках пакета: константа (всегда «{_band_ru(base['constant']['band_value'])}») "
                  f"ступень {_share(base['constant']['band'])}, порядок {_share(base['constant']['order'])}; эксперт "
                  f"сам с собой (итог по весам из его баллов) ступень {_share(base['expert']['band'])}, порядок "
                  f"{_share(base['expert']['order'])}; контроль с ядра (балл ленты и судьи, строк {control['rows']}) "
                  f"ступень {_share(control['band'], 1)}, порядок {_share(control['order'], 1)}.",
              (f"Лента за 30 дней: сигналов с баллами около {month['signals']} из {month['feed']} публикаций "
               "ленты (доля взятого экспертом бизнеса и технологии по размерам страт окна пакета); находки радара и "
               "материалы не учтены." if month else
               "Цены в месяц нет: размеры страт выгружает с ядра `otbor-exam-control`."),
              ""]
    return lines


def _theme(report: dict[str, Any]) -> list[str]:
    found, base = report["theme"], report["baseline"]
    return ["## Тема основного вызова", "",
            f"{report['card_model']}: {_share(found['agent'])} ответов с темой эксперта (порог "
            f"{found['threshold']:.0%}) {_mark(found['passed'])}; константа (всегда "
            f"«{base['constant']['theme_value']}») {_share(base['constant']['theme'])}; контроль (тег ленты и тема "
            f"судьи, строк {base['control']['theme_rows']}) {_share(base['control']['theme'])}.", ""]


_ATTRIBUTES_RU = (("companies_precision", "Компании, точность"), ("companies_recall", "Компании, полнота"),
                  ("region", "Регион"), ("event_type", "Тип события"), ("technology_class", "Класс технологии"),
                  ("maturity", "Зрелость"), ("event_date", "Дата события, доля ошибок"),
                  ("invented_companies", "Выдуманные компании"), ("invented_dates", "Выдуманные даты"))


def _attributes(report: dict[str, Any]) -> list[str]:
    """Атрибуты основного вызова того же прогона против разметки экзамена #98 (с допусками, пороги #98)."""
    found = report.get("attributes")
    if not found:
        return ["Атрибуты основного вызова с разметкой экзамена #98 не сверялись (настройка без `--card-markup`).", ""]
    lines = ["## Атрибуты основного вызова", "",
             f"Против разметки экзамена #98 на строках его экзамена ({len(found['rows'])}), с допусками и по порогам "
             "#98 (новых порогов нет):", "", "| Атрибут | Модель | Константа | |", "|---|---|---|---|"]
    for key, title in _ATTRIBUTES_RU:
        one = found["lenient"].get(key) or {}
        shown = str(one.get("agent")) if key.startswith("invented") else _share(one.get("agent"))
        constant = _share(one["constant"]) if "constant" in one else ""
        lines.append(f"| {title} | {shown} | {constant} | {_mark(one.get('passed'))} |")
    return lines + [""]


def _model(model: dict[str, Any], report: dict[str, Any]) -> list[str]:
    scores = model["scores"]
    answers = scores["answers"]
    chosen = scores[model["variant"]]
    lines = [f"## {model['model']}: " + ("ступень и порядок пройдены" if model["passed"]
                                         else "не прошла: " + failed_ru(model["failed"])), "",
             f"Вызовов {model['calls']}, токенов {model['input_tokens'] // 1000} тыс. входа и "
             f"{model['output_tokens'] // 1000} тыс. выхода, {_usd(model['cost_usd'])}; ответов {answers['answers']}, "
             f"без баллов {answers['failures']}; одна ступень во всех прогонах у {_share(chosen['band']['stability'])} "
             "строк пакета.", "",
             "| Вариант | Ступень | Там же, где ядро | Порядок | Порядок: бизнес | Порядок: технология | |",
             "|---|---|---|---|---|---|---|"]
    for variant in VARIANTS:
        one = scores[variant]
        lines.append(f"| {VARIANT_RU[variant]} | {_share(one['band']['agent'], 1)} "
                     f"| {_share(one['band']['same_rows'], 1)} | {_share(one['order']['agent'], 1)} "
                     f"| {_share(one['order']['by_profile'].get('business'))} "
                     f"| {_share(one['order']['by_profile'].get('tech_radar'))} | {_mark(one['passed'])} |")
    lines += _bands(scores["bands"], model["variant"])
    constant = report["baseline"]["constant"]["criteria"]
    lines += ["", f"По критериям, {VARIANT_RU[model['variant']]} (ошибка: средняя разница с баллом эксперта; сдвиг: "
                  "модель минус эксперт; константа: ошибка ответа «всегда среднее эксперта»):", "",
              "| Критерий | Ошибка | Сдвиг | Константа | Ответов |", "|---|---|---|---|---|"]
    for name, one in sorted(chosen["criteria"].items()):
        lines.append(f"| {name} | {one['mae']:.1f} | {one['bias']:+.1f} | {_number(constant.get(name))} | {one['n']} |")
    freshness = chosen["freshness"]
    lines += ["", f"«Свежесть»: есть ли дата события, как у эксперта, в {_share(freshness['agreement'])} ответов "
                  f"({freshness['n']}).", "",
              "Итог модели по оценке эксперта (среднее по прогонам на строку):", "",
              "| Оценка эксперта | Строк | Меньший | Средний | Больший |", "|---|---|---|---|---|"]
    order = [value for value, _, _ in EXPERT_SCALE] + [EXTRA]
    for rating in sorted(scores["totals"], key=lambda one: order.index(one) if one in order else len(order)):
        one = scores["totals"][rating]
        lines.append(f"| {_RATING_RU.get(rating, rating)} | {one['rows']} | {one['min']:.1f} | {one['mean']:.1f} "
                     f"| {one['max']:.1f} |")
    return lines + [""]


def _bands(bands: dict[str, Any], variant: str) -> list[str]:
    """Ступень эксперта (строки) против ступени модели (колонки) и точность «сильного» («кандидаты»)."""
    columns = [*BAND_ORDER, "none"]
    lines = ["", f"Ступень эксперта против ступени модели, {VARIANT_RU[variant]} (ответов, строки пакета; «нет»: без "
                 f"баллов). «Сильный» даёт «кандидата» «Решения платформы»: эксперт счёл сильными "
                 f"{_share(bands['strong_precision'])} кандидатов модели, модель нашла "
                 f"{_share(bands['strong_recall'])} сильных эксперта.", "",
             "| Эксперт \\ модель | " + " | ".join(_band_ru(band) for band in columns) + " |",
             "|---|" + "---|" * len(columns)]
    for band in BAND_ORDER:
        counts = bands["matrix"].get(band) or {}
        lines.append(f"| {_band_ru(band)} | " + " | ".join(str(counts.get(one, 0)) for one in columns) + " |")
    return lines


def _band_ru(band: str | None) -> str:
    return BAND_LABELS.get(band or "", "нет")


def _mark(passed: bool | None) -> str:
    return "" if passed is None else "✓" if passed else "✗"


def _number(value: float | None) -> str:
    return "нет" if value is None else f"{value:.1f}"


def _share(value: float | None, digits: int = 0) -> str:
    """Доля в процентах; ступень и порядок — с десятыми: рядом с порогом целые проценты прячут «не прошла»."""
    return "нет" if value is None else f"{value:.{digits}%}"


def _usd(value: float | None) -> str:
    if value is None:
        return "нет"
    return f"${value:.2f}" if value >= 1 else f"${value:.4f}" if value < 0.01 else f"${value:.3f}"
