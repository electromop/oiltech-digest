"""Отчёт экзамена агента «Отбор» (#133): JSON с ответами и метриками и читаемый Markdown рядом, вне репозитория.

Сверху таблица качества и цены по моделям (её владелец смотрит, решая, с чем включать), ниже по модели: правило
прохождения, решение по срезам с контролем и константой, остальные метки с порогами, страницы-списки, оценка на весь
поток ленты. Текст отчёта читает владелец: без длинного и среднего тире.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from oiltech_digest.otbor_exam_score import BALANCED_MIN, SLICES, THRESHOLDS

REPORT_PREFIX = "otbor_exam_report_"
PART_RU = {"exam": "экзамен", "tuning": "настройка"}
_CLASS_RU = {"accept": "«брать»", "reject": "«не брать»"}
# Проваленная проверка — коротко, для таблицы и строки решения; в JSON остаются коды.
_FAILED_RU = {"feed_random": "лента", "radar": "радар", "chain": "цепочка", "balanced": "сбалансированная",
              "industry": "отрасль", "oilfield_relevance": "связь", "reject_reason": "причина", "category": "категория"}
_RULES_RU = {"thresholds": "пороги меток", "balanced_min": "граница сбалансированной доли",
             "rule": "правило прохождения", "rows": "строки экзамена (страницы-списки, примеры инструкции)"}
_LABELS_RU = (("industry", "Отрасль", "все строки"),
              ("oilfield_relevance", "Связь (среднее по значениям)", "все строки"),
              ("reject_reason", "Причина отказа", "«не брать» у эксперта и модели"),
              ("category", "Категория", "«брать» у эксперта и модели"))


def reason(decision: str | None, tried: list[dict[str, Any]]) -> str:
    """Почему решение экзамена такое; сама модель в decision["model"]."""
    if decision:
        return "самая дешёвая из прошедших правило и пороги"
    passed = [one["model"] for one in tried if one["passed"]]
    if passed:
        return "прошли только устаревшие модели (" + ", ".join(passed) + "): их экзамен не выбирает"
    return "ни одна модель не прошла: " + "; ".join(f"{one['model']}: {failed_ru(one['failed'])}" for one in tried)


def failed_ru(names: list[str]) -> str:
    """Проваленные проверки по-русски: «лента «не брать»», «отрасль»."""
    shown = []
    for name in names:
        part, _, kind = name.partition(":")
        shown.append(_FAILED_RU.get(part, part) + (f" {_CLASS_RU.get(kind, kind)}" if kind else ""))
    return ", ".join(shown)


def write(folder: Path, report: dict[str, Any]) -> tuple[Path, Path]:
    """Отчёт рядом с манифестом; имя по времени записи, прежние отчёты не перезаписываются."""
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
    """Сохранённый отчёт для пересчёта без вызовов модели. Файл лежит на диске и его правят руками: устройство
    проверяется до счёта, битый отчёт — ValueError с именем файла, а не трасса посреди пересчёта."""
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name}: не JSON ({exc})") from exc
    problems = _problems(saved)
    if problems:
        raise ValueError(f"{path.name}: не отчёт экзамена «Отбора»: " + "; ".join(problems))
    return saved


def _problems(saved: Any) -> list[str]:
    if not isinstance(saved, dict):
        return ["нужен объект"]
    split, part = saved.get("split"), saved.get("part")
    problems = [] if part in PART_RU else ["часть exam или tuning"]
    if not (isinstance(split, dict) and all(isinstance(split.get(key), list) for key in PART_RU)):
        problems.append("деление: списки tuning и exam")
    if not (isinstance(saved.get("runs"), int) and saved["runs"] >= 1):
        problems.append("прогонов на строку: число от 1")
    problems += [f"нет {key}" for key in ("reasoning", "prompt_version", "dictionaries_version")
                 if not isinstance(saved.get(key), str)]
    problems += [f"{key} без sha256" for key in ("manifest", "markup")
                 if not (isinstance(saved.get(key), dict) and isinstance(saved[key].get("sha256"), str))]
    ids = set(split[part]) if not problems else set()
    models = saved.get("models")
    if not (isinstance(models, list) and models and all(_model_entry(one, ids) for one in models)):
        problems.append("модели с именем, ценой и ответами (прогон, строка своей части, вызовы, токены)")
    return problems


def _model_entry(one: Any, ids: set[str]) -> bool:
    return (isinstance(one, dict) and isinstance(one.get("model"), str) and isinstance(one.get("answers"), list)
            and all(isinstance(one.get(key), (int, float)) for key in ("calls", "input_tokens", "output_tokens",
                                                                       "cost_usd"))
            and all(isinstance(answer, dict) and answer.get("id") in ids and isinstance(answer.get("run"), int)
                    and isinstance(answer.get("calls"), list) and isinstance(answer.get("model", 0), (str, type(None)))
                    and all(isinstance(answer.get(key), int) for key in ("input_tokens", "output_tokens", "attempts",
                                                                         "model_calls"))
                    for answer in one["answers"]))


# --- Читаемый отчёт ------------------------------------------------------------------------------


def render(report: dict[str, Any]) -> str:
    part = PART_RU[report["part"]]
    rows = report["rows_file"]
    lines = [
        f"# Экзамен агента «Отбор», {report['created_at']}", "",
        f"Решение: {_decision(report['decision'])}", "",
        *([f"Пересчёт отчёта {report['rescored_from']} без вызовов модели: ответы те же, счёт по нынешним проверке "
           "кодом, строкам экзамена и порогам; цена исходного прогона.", ""] if report.get("rescored_from") else []),
        *(["Разметка изменилась после исходного прогона: счёт по новой разметке.", ""]
          if report.get("markup_changed") else []),
        *(["**Правила изменились после исходного прогона** ("
           + ", ".join(_RULES_RU.get(name, name) for name in report["rules_changed"])
           + "): решение по ним не годится без нового утверждения владельцем.", ""]
          if report.get("rules_changed") else []),
        *([f"Исходный прогон остановился на {report['original_decision']}: модели дороже не пробовались, для решения "
           "нужен прогон.", ""] if report.get("original_decision") and not report["decision"]["model"] else []),
        f"Часть: {part}, публикаций {report['publications']}, прогонов на публикацию {report['runs']}; инструкция "
        f"{report['prompt_version']}, справочники {report['dictionaries_version']} (у эксперта "
        f"{report['pack_versions'].get('dictionaries_version', 'не записано')}), рассуждение {report['reasoning']}.",
        f"Манифест: `{Path(report['manifest']['file']).name}`, разметка: `{Path(report['markup']['file']).name}`.",
        f"Строки экзамена: страниц-списков {len(rows['list_pages'])} (вне метрики решения), строк-примеров инструкции "
        f"{len(rows['in_instruction'])} (только в настройке). Утверждено: {report.get('approved') or 'нет'}.",
        ("Контроль с ядра есть: размеры страт ленты и категория судьи у строк радара." if report.get("strata") else
         "Контроля с ядра нет (команда `otbor-exam-control` на ядре): нет контроля категории у строк радара, оценки "
         "на весь поток ленты и цены в месяц."),
        *([f"Устаревшие модели (экзамен меряет, но не выбирает): "
           + "; ".join(f"{model}: {why}" for model, why in report["deprecated"].items()) + "."]
          if report.get("deprecated") else []), "",
    ]
    lines += _quality_and_price(report)
    for model in report["models"]:
        lines += _model(model, report)
    return "\n".join(lines) + "\n"


def _decision(decision: dict[str, Any]) -> str:
    return f"**{decision['model']}**: {decision['reason']}" if decision["model"] else decision["reason"]


def _quality_and_price(report: dict[str, Any]) -> list[str]:
    """Таблица качества и цены: одна строка на модель, цена на публикацию и на ленту за 30 дней (размер страт)."""
    feed = sum(report["strata"].values()) if report.get("strata") else None
    lines = ["## Качество и цена", "",
             "| Модель | Правило и пороги | Сбалансированная | Лента «брать» / «не брать» (гейт) | Отрасль | Связь "
             "| Причина | Категория | Спорных | Без ответа | Экзамен | На публикацию | Лента за 30 дней |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for model in report["models"]:
        scores = model["scores"]
        gate = scores["decision"]["feed_random"]
        answers = scores["answers"]
        per_publication = model["cost_usd"] / answers["answers"] if answers["answers"] else None
        lines.append(
            f"| {model['model']}{' (устаревшая)' if model['model'] in (report.get('deprecated') or {}) else ''} "
            f"| {'прошла' if model['passed'] else 'нет: ' + failed_ru(model['failed'])} "
            f"| {_share(scores['decision']['all']['agent']['balanced'])} "
            f"| {_share(gate['agent']['accept'])} / {_share(gate['agent']['reject'])} "
            f"({_share((gate['control'] or {}).get('accept'))} / {_share((gate['control'] or {}).get('reject'))}) "
            + "".join(f"| {_share(scores['labels'][key]['agent'])} " for key, _, _ in _LABELS_RU)
            + f"| {answers['disputed']} | {answers['failures']} | {_usd(model['cost_usd'])} "
            f"| {_usd(per_publication)} | {_usd(per_publication * feed) if per_publication and feed else 'нет'} |")
    return lines + ["", "Доли по прогонам; гейт на тех же строках. Лента за 30 дней: публикаций ленты в окне пакета "
                    + (f"{feed}" if feed else "нет (контроля с ядра нет)") + ", находки радара и материалы не учтены.",
                    ""]


def _model(model: dict[str, Any], report: dict[str, Any]) -> list[str]:
    scores = model["scores"]
    answers = scores["answers"]
    lines = [f"## {model['model']}: " + ("правило и пороги пройдены" if model["passed"]
                                         else "не прошла: " + failed_ru(model["failed"])), "",
             f"Вызовов {model['calls']}, токенов {model['input_tokens'] // 1000} тыс. входа и "
             f"{model['output_tokens'] // 1000} тыс. выхода, {_usd(model['cost_usd'])}; ответов {answers['answers']}, "
             f"без ответа {answers['failures']}, спорных {answers['disputed']}; одно решение во всех прогонах у "
             f"{_share(answers['stability'])} публикаций.", "",
             "Правило прохождения (решение 29):", "", "| Проверка | Модель | Граница | |", "|---|---|---|---|"]
    for check in scores["rule"]:
        title = (f"{SLICES[check['slice']][0]}: {_CLASS_RU[check['class']]} не хуже контроля" if check["class"]
                 else f"сбалансированная доля, все строки, не ниже {BALANCED_MIN:.0%}")
        lines.append(f"| {title} | {_share(check['agent'])} | {_share(check['bound'])} | {_mark(check['passed'])} |")
    lines += ["", "Решение «брать или нет» по срезам (модель / контроль; константа: доля верных у «всегда брать»):", "",
              "| Срез | Строк («брать» / «не брать») | «Брать» | «Не брать» | Сбалансированная | Точность «брать» "
              "| «Всегда брать» |", "|---|---|---|---|---|---|---|"]
    for name, (title, _) in SLICES.items():
        part = scores["decision"][name]
        if not part["rows"]:
            continue
        agent, control = part["agent"], part["control"] or {}
        lines.append(f"| {title} | {part['rows']} ({part['accept_rows']} / {part['reject_rows']}) "
                     + "".join(f"| {_share(agent[key])} / {_share(control.get(key))} "
                               for key in ("accept", "reject", "balanced", "precision_accept"))
                     + f"| {_share(part['constant']['always_accept'])} |")
    lines += ["", "Остальные метки (пороги: черновик разбора разметки 23.10, раздел 3.4, утверждает владелец):", "",
              "| Метка | Где | Порог | Модель | Константа | Контроль | |", "|---|---|---|---|---|---|---|"]
    for key, title, where in _LABELS_RU:
        row = scores["labels"][key]
        control = row.get("control")
        lines.append(f"| {title} | {where} | не ниже {THRESHOLDS[key]:.0%} | {_share(row['agent'])} "
                     f"| {_share(row['constant'])} ({row['constant_value']}) "
                     f"| {_share(control['agent']) + ' (статья сбора «бизнес», находка: судья)' if control else 'нет'} "
                     f"| {_mark(row['passed'])} |")
    pages = scores["list_pages"]
    lines += ["", f"Страницы-списки (вне метрики решения): строк {pages['rows']}, эксперт взял "
                  f"{pages['expert_accept']}, модель взяла {_share(pages['agent_accept'])} ответов."]
    lines += _stream(scores.get("stream"))
    return lines + [""]


def _stream(stream: dict[str, Any] | None) -> list[str]:
    if not stream:
        return ["", "Оценки на весь поток ленты нет: размеры страт выгружает с ядра `otbor-exam-control`."]
    strata, agent, gate = stream["strata"], stream["agent"], stream["gate"]
    return ["", f"Оценка на весь поток ленты (веса страт окна пакета: принял гейт {strata['feed_accepted']}, "
                f"отклонил {strata['feed_rejected']}): модель «брать» {_share(agent['accept'])}, «не брать» "
                f"{_share(agent['reject'])}, сбалансированная {_share(agent['balanced'])}; гейт "
                f"{_share(gate['accept'])}, {_share(gate['reject'])}, {_share(gate['balanced'])}. Эксперт взял бы "
                f"{_share(stream['expert_accept'])} потока, модель берёт {_share(stream['agent_accept'])}, гейт "
                f"{_share(stream['gate_accept'])}."]


def _mark(passed: bool | None) -> str:
    return {True: "✓", False: "✗"}.get(passed, "")


def _share(value: float | None) -> str:
    return "нет" if value is None else f"{value:.0%}"


def _usd(value: float | None) -> str:
    if value is None:
        return "нет"
    return f"${value:.2f}" if value >= 1 else f"${value:.4f}" if value < 0.01 else f"${value:.3f}"
