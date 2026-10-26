"""Экзамен агента «Отбор» (#133, спека #128): модель выходит в работу по правилу прохождения на разметке эксперта.

Набор — пакет эксперту (#129): манифест хранит входы агента (заголовок, текст, издатель, язык, даты, ссылки находки),
таблица — разметку эксперта; она проходит otbor_markup без нарушений. Агенту подаются входы манифеста в той же форме,
что у ядра (otbor._item, otbor._signal_item), — как их видел эксперт. Коды списков справочников — те, из которых
выбирал эксперт; тексты правил — новой версии: её и экзаменуют.

Рядом с манифестом — файл строк экзамена (утверждает владелец): страницы-списки радара (вне метрики решения,
решение 29) и строки, которые стоят примерами в справочниках «Отбора» (только в настройке: иначе модель видит ответ
в инструкции). Деление на настройку и экзамен — по долям «группа манифеста × решение эксперта», страницы-списки —
своей долей, четверть в настройку; оно пишется рядом с манифестом при первом прогоне и дальше не меняется. Инструкцию
правят только по строкам настройки.

Каждая строка экзамена — три прогона. Мерится то, что дошло бы до сигнала: ответы воркера (otbor.process_payload,
с повтором при конфликте шагов) после проверки кодом ядра (otbor_answer.outcome). Модели — лестницей по цене: первая,
прошедшая правило и пороги (otbor_exam_score), — решение; с --all идут все — полная таблица качества и цены. Модель,
которую поставщик пометил устаревшей, экзамен меряет, но не выбирает. Прогон — только после утверждения (approved в
файле строк экзамена): деление фиксируется первым же прогоном. Отчёт — рядом с манифестом, вне репозитория: он
публичный.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from oiltech_digest import config, otbor, otbor_exam_report, otbor_markup, otbor_pack
from oiltech_digest.db import otbor_pack_store, otbor_store
from oiltech_digest.otbor_answer import codes, outcome
from oiltech_digest.otbor_exam_score import BALANCED_MIN, FEED_GROUPS, LABELS, RULES, THRESHOLDS, evaluate
from oiltech_digest.otbor_pack_format import CHOICE_COLUMNS, MANIFEST_NAME, PackError, load_dictionaries
from oiltech_digest.signal_card_pack import REPO_ROOT, _rank, publication_day, publication_text

# Кандидаты (решение 20 журнала гриля): gpt-5.4-nano, gpt-5.4-mini, gpt-5.5 и самые дешёвые из новых — по прайс-листу
# OpenAI 26.10.2026 это gpt-6-luna и gpt-5.6-luna. Лестница по цене, следующая — только при провале предыдущей.
MODELS = ("gpt-6-luna", "gpt-5.6-luna", "gpt-5.4-nano", "gpt-5.4-mini", "gpt-5.5")
# Помеченные поставщиком устаревшими: экзамен их меряет, но решением не выбирает — их скоро снимут (как gpt-5 и
# gpt-5-mini 11.12.2026).
DEPRECATED = {"gpt-5.4-nano": "OpenAI пометила устаревшей (страница моделей, 26.10.2026)"}
RUNS = 3
TUNING_SHARE = 1 / 4
SPLIT_NAME = "otbor_exam_split.json"
# Строки экзамена: {"list_pages": [...], "in_instruction": [...], "approved": "когда и кто"} — номера строк
# манифеста; утверждает владелец, без approved прогона нет.
ROWS_NAME = "otbor_exam_rows.json"
ROW_LISTS = ("list_pages", "in_instruction")
# Контроль с ядра (otbor-exam-control): размеры страт ленты и категория судьи у радарных строк.
CONTROL_NAME = "otbor_exam_control.json"
LIST_PAGE = "list_page"


class ExamError(ValueError):
    pass


def run(markup_path: str | Path, *, manifest_path: str | Path | None = None, models: tuple[str, ...] = MODELS,
        runs: int = RUNS, part: str = "exam", every: bool = False) -> dict[str, Any]:
    """Экзамен по лестнице моделей (every — все модели, полная таблица): отчёт и решение — рядом с манифестом."""
    _check_runs(runs, part)
    exam = load(markup_path, manifest_path)
    if not exam["approved"]:
        raise ExamError(f"{ROWS_NAME}: нет отметки approved. До первого прогона владелец утверждает инструкцию, пороги "
                        "и строки экзамена: первый же прогон, даже настройки, фиксирует деление")
    ladder = _by_price(models)
    split = _split(exam, write=True)
    items = _part_items(exam, split, part)
    dictionaries = load_dictionaries()
    tried: list[dict[str, Any]] = []
    for model in ladder:
        tried.append(_model_scores(model, _answers(model, items, runs, dictionaries), items, dictionaries,
                                   exam["control"]))
        print(f"{model}: " + ("правило и пороги пройдены" if tried[-1]["passed"]
                              else "не прошла: " + ", ".join(tried[-1]["failed"])))
        if not every and _decision(tried, part):
            break
    settings = {"part": part, "runs": runs, "split": split, "reasoning": config.OTBOR_REASONING,
                "prompt_version": otbor.PROMPT_VERSION}
    return _report(exam, items, dictionaries, tried, settings)


def rescore(report_path: str | Path, markup_path: str | Path, *,
            manifest_path: str | Path | None = None) -> dict[str, Any]:
    """Пересчёт сохранённого отчёта без вызовов модели: те же ответы — по нынешним проверке кодом, строкам экзамена,
    выгрузке с ядра и порогам; новый отчёт — рядом. Ответы относятся к текстам своего манифеста и к своей версии
    справочников: с другими пересчёт отказывает."""
    source = Path(report_path).expanduser().resolve()
    try:
        saved = otbor_exam_report.read(source)
    except ValueError as exc:
        raise ExamError(str(exc)) from exc
    exam = load(markup_path, manifest_path)
    dictionaries = load_dictionaries()
    if saved["manifest"]["sha256"] != exam["manifest"]["sha256"]:
        raise ExamError(f"{source.name}: ответы получены на другом манифесте, тексты другие: пересчёт неверен")
    if saved["dictionaries_version"] != dictionaries.get("version"):
        raise ExamError(f"{source.name}: ответы на справочниках {saved['dictionaries_version']}, сейчас "
                        f"{dictionaries.get('version')}: пересчёт неверен, нужен новый прогон")
    items = _part_items(exam, saved["split"], saved["part"])
    # Цена и токены — исходного прогона: новых вызовов нет, а ставки с тех пор могли поменяться.
    tried = [{**_model_scores(one["model"], one["answers"], items, dictionaries, exam["control"]),
              **{key: one[key] for key in ("calls", "input_tokens", "output_tokens", "cost_usd")}}
             for one in saved["models"]]
    settings = {key: saved[key] for key in ("part", "runs", "split", "reasoning", "prompt_version")}
    decided = saved.get("decision")
    current = _rules(exam)
    changed = [name for name, value in current.items() if (saved.get("rules") or {}).get(name) != value]
    return _report(exam, items, dictionaries, tried, settings, rescored_from=source.name,
                   markup_changed=saved["markup"]["sha256"] != exam["markup"]["sha256"], rules_changed=changed,
                   original_decision=decided.get("model") if isinstance(decided, dict) else None)


def plan(markup_path: str | Path, *, manifest_path: str | Path | None = None, models: tuple[str, ...] = MODELS,
         runs: int = RUNS, part: str = "exam") -> dict[str, Any]:
    """Сухой прогон: деление, число вызовов и цена по моделям; ничего не пишет и не вызывает модель. Цена — по длине
    запросов (инструкция со справочниками и вход публикации), ответ — оценкой signal_card.estimate; повтор при
    конфликте шагов не учтён."""
    from oiltech_digest.signal_card import estimate

    _check_runs(runs, part)
    exam = load(markup_path, manifest_path)
    split = _split(exam, write=False)
    items = _part_items(exam, split, part)
    instructions = len(otbor._instructions(load_dictionaries()))
    chars = sum(instructions + len(otbor.model_input(model_item(item))) for item in items)
    stratum = {item["id"]: _stratum(item) for item in exam["items"]}
    return {"part": part, "publications": len(items), "runs": runs, "rows": len(exam["items"]),
            "split_new": split["new"], "approved": exam["approved"],
            "split": {key: dict(sorted(Counter(stratum[one] for one in split[key]).items()))
                      for key in ("tuning", "exam")},
            "models": [estimate(chars * runs, len(items) * runs, model) for model in _by_price(models)]}


def _check_runs(runs: int, part: str) -> None:
    """Экзамен — RUNS прогонов на строку (спека): меньше — устойчивость не измерить. Настройка — от одного."""
    if runs < 1 or (part == "exam" and runs != RUNS):
        raise ExamError(f"--runs {runs}: на экзамене {RUNS} прогона на строку, на настройке от одного")


def _by_price(models: tuple[str, ...]) -> list[str]:
    """Лестница — от дешёвой модели к дорогой, в каком порядке модели ни назвали: решение — самая дешёвая из
    прошедших. Модели без своей строки в таблице цен — отказ: цена по умолчанию поставила бы её не на своё место, а
    отчёт солгал бы о цене."""
    unknown = [model for model in models if not any(model.startswith(prefix) for prefix in config.OPENAI_MODEL_PRICES)]
    if unknown:
        raise ExamError(f"цены {', '.join(unknown)} нет в config.OPENAI_MODEL_PRICES: лестница идёт по цене, а отчёт "
                        "считает расход; добавьте ставку с прайс-листа")
    return sorted(models, key=config.price_for_model)


def _decision(tried: list[dict[str, Any]], part: str) -> str | None:
    """Решение экзамена — первая по цене модель, прошедшая правило и пороги и не устаревшая; настройка модель не
    выбирает."""
    if part != "exam":
        return None
    return next((one["model"] for one in tried if one["passed"] and one["model"] not in DEPRECATED), None)


def _part_items(exam: dict[str, Any], split: dict[str, Any], part: str) -> list[dict[str, Any]]:
    ids = set(split[part])
    return [item for item in exam["items"] if item["id"] in ids]


# --- Набор ---------------------------------------------------------------------------------------


def load(markup_path: str | Path, manifest_path: str | Path | None = None) -> dict[str, Any]:
    """Манифест и разметка эксперта (через проверку otbor_markup): строки с входами агента и метками в кодах."""
    markup = Path(markup_path).expanduser().resolve()
    manifest_file = Path(manifest_path).expanduser().resolve() if manifest_path else markup.with_name(MANIFEST_NAME)
    folder = manifest_file.parent
    if folder == REPO_ROOT or REPO_ROOT in folder.parents:
        raise ExamError(f"{folder} внутри репозитория, а он публичный: пакет эксперту и отчёт держите вне его")
    try:
        checked = otbor_markup.check(markup, manifest_file)
    except PackError as exc:
        raise ExamError(str(exc)) from exc
    if checked["violations"]:
        raise ExamError(f"разметка с нарушениями ({len(checked['violations'])}), сначала otbor-markup-check: "
                        + "; ".join(checked["violations"][:3]))
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    _check_codes(manifest["lists"], load_dictionaries())
    ids = {item["id"] for item in manifest["items"]}
    rows = _rows(folder / ROWS_NAME, ids)
    labels = {item["id"]: item for item in checked["items"]}
    items = [{**_inputs(item), "labels": {key: labels[item["id"]][key] for key in LABELS},
              "list_page": item["id"] in rows["rows"]["list_pages"]} for item in manifest["items"]]
    return {"folder": folder, "seed": manifest.get("seed"), "items": items, "rows": rows["rows"],
            "approved": rows["approved"], "control": _control(folder / CONTROL_NAME, ids),
            "pack_versions": manifest.get("recipe") or {},
            "manifest": _file_mark(manifest_file), "markup": _file_mark(markup)}


def _check_codes(lists: dict[str, Any], dictionaries: dict[str, Any]) -> None:
    """Агент выбирает из тех же кодов, из которых выбирал эксперт: тексты правил новой версии меняются по его же
    правкам, а другой набор кодов сделал бы метки несравнимыми."""
    changed = []
    for column, key in CHOICE_COLUMNS:
        if key == "theme":
            continue
        seen = [one["code"] for one in lists.get(column) or []]
        if sorted(seen) != sorted(codes(dictionaries, key)):
            changed.append(f"«{column}» (у эксперта {', '.join(seen)}; сейчас {', '.join(codes(dictionaries, key))})")
    if changed:
        raise ExamError("коды справочников не те, из которых выбирал эксперт: " + "; ".join(changed))


def _rows(path: Path, ids: set[str]) -> dict[str, list[str]]:
    """Файл строк экзамена — решение владельца, его нет — отказ: пустые списки — тоже решение."""
    if not path.exists():
        raise ExamError(f"нет {path.name} рядом с манифестом: страницы-списки (вне метрики решения) и строки-примеры "
                        "инструкции (только в настройке) утверждает владелец до первого прогона; пустые списки "
                        "тоже решение")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(isinstance(data.get(key), list) and all(
            isinstance(one, str) for one in data[key]) for key in ROW_LISTS):
        raise ExamError(f"{path.name}: нужны списки {' и '.join(ROW_LISTS)}, номера строк манифеста")
    unknown = sorted({one for key in ROW_LISTS for one in data[key]} - ids)
    if unknown:
        raise ExamError(f"{path.name}: строк {', '.join(unknown)} нет в манифесте")
    approved = data.get("approved")
    return {"rows": {key: sorted(set(data[key])) for key in ROW_LISTS},
            "approved": approved.strip() if isinstance(approved, str) and approved.strip() else None}


def _control(path: Path, ids: set[str]) -> dict[str, Any] | None:
    """Контроль с ядра (otbor-exam-control) проверяется при загрузке набора, до вызовов модели: битый файл не должен
    сорвать оплаченный прогон."""
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    strata, rows = (data.get("strata"), data.get("rows")) if isinstance(data, dict) else (None, None)
    if not (isinstance(strata, dict) and all(isinstance(strata.get(group), int) and strata[group] >= 0
                                             for group in FEED_GROUPS)
            and isinstance(rows, dict) and all(isinstance(one, dict) for one in rows.values())):
        raise ExamError(f"{path.name}: нужны strata {{{', '.join(FEED_GROUPS)}: число}} и rows {{строка: "
                        "{signal_category}}}, выгрузка otbor-exam-control")
    unknown = sorted(set(rows) - ids)
    if unknown:
        raise ExamError(f"{path.name}: строк {', '.join(unknown[:5])} нет в манифесте, выгрузка другого пакета")
    return data


def _inputs(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item.get(key) for key in ("id", "group", "kind", "origin", "title", "text", "publisher", "language",
                                           "published", "received", "links")}


def model_item(item: dict[str, Any]) -> dict[str, Any]:
    """Публикация строки манифеста так, как её получает модель на ядре (otbor._item, otbor._signal_item): те же поля и
    текст; издатель и ссылки находки — как их видел эксперт (в манифесте они уже подписаны так, как на ядре)."""
    return {
        "ref": item["id"], "title": publication_text(item.get("title"))[:500],
        "text": publication_text(item.get("text")), "publisher": item.get("publisher") or "",
        "language": item.get("language") or "", "published": publication_day(item.get("published")),
        "received": publication_day(item.get("received")),
        "links": [{"title": publication_text(link.get("title"))[:300] or None, "publisher": link.get("publisher") or "",
                   "published": publication_day(link.get("published"))} for link in item.get("links") or []],
        "feedback": {"similar_signals": [], "text": ""},
    }


def _rules(exam: dict[str, Any]) -> dict[str, Any]:
    """Правила, по которым считается решение экзамена: пороги, правило прохождения, строки экзамена. Пересчёт
    сверяет их с исходным прогоном: правило, сменённое после того, как ответы увидели, — не то, что утверждали."""
    return {"thresholds": dict(THRESHOLDS), "balanced_min": BALANCED_MIN,
            "rule": [[name, list(kinds)] for name, kinds in RULES], "rows": exam["rows"]}


def _file_mark(path: Path) -> dict[str, str]:
    return {"file": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _files(folder: Path) -> dict[str, dict[str, str] | None]:
    """Отметки файлов рядом с манифестом: строки экзамена, деление, контроль с ядра."""
    return {name: _file_mark(folder / file) if (folder / file).exists() else None
            for name, file in (("rows", ROWS_NAME), ("split", SPLIT_NAME), ("control", CONTROL_NAME))}


# --- Деление -------------------------------------------------------------------------------------


def _stratum(item: dict[str, Any]) -> str:
    return LIST_PAGE if item["list_page"] else f"{item['group']}:{item['labels']['decision']}"


def stratum_label(code: str) -> str:
    """Доля деления для людей: группа манифеста и решение эксперта; страницы-списки — своей долей."""
    if code == LIST_PAGE:
        return "страницы-списки"
    group, decision = code.split(":", 1)
    return f"{otbor_pack.GROUPS.get(group, group)}, «{'брать' if decision == 'accept' else 'не брать'}»"


def _split(exam: dict[str, Any], *, write: bool) -> dict[str, Any]:
    """Деление на настройку и экзамен: из файла рядом с манифестом, а при первом прогоне — новое. Строка-пример
    инструкции в экзаменационной части — отказ: инструкцию по строкам экзамена не дополняют."""
    path = exam["folder"] / SPLIT_NAME
    ids = sorted(item["id"] for item in exam["items"])
    forced = set(exam["rows"]["in_instruction"])
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(saved, dict) or not all(isinstance(saved.get(key), list) for key in ("tuning", "exam")):
            raise ExamError(f"{path.name} испорчен: нужны списки tuning и exam")
        if sorted(saved["tuning"] + saved["exam"]) != ids:
            raise ExamError(f"{path.name}: деление другого набора, манифест сменился после первого прогона")
        leaked = sorted(forced & set(saved["exam"]))
        if leaked:
            raise ExamError(f"строки-примеры инструкции в экзаменационной части деления: {', '.join(leaked)}; "
                            "инструкцию правят только по строкам настройки")
        return {**saved, "new": False}
    by_stratum: dict[str, list[str]] = defaultdict(list)
    for item in exam["items"]:
        by_stratum[_stratum(item)].append(item["id"])
    total = round(len(ids) * TUNING_SHARE)
    quotas = {stratum: len(group) * total / len(ids) for stratum, group in by_stratum.items()}
    counts = {stratum: int(quota) for stratum, quota in quotas.items()}
    ranked = sorted(by_stratum, key=lambda stratum: (counts[stratum] - quotas[stratum], -len(by_stratum[stratum]),
                                                     stratum))
    for stratum in ranked[:total - sum(counts.values())]:
        counts[stratum] += 1
    tuning: set[str] = set()
    for stratum, group in by_stratum.items():
        if len(group) >= 2:  # каждая доля — в обеих частях
            counts[stratum] = min(max(counts[stratum], 1), len(group) - 1)
        ordered = sorted(group, key=lambda one: (one not in forced, _rank("split", exam["seed"], one)))
        tuning.update(ordered[:max(counts[stratum], sum(one in forced for one in group))])
    split = {"created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "rule": "четверть в настройку по долям «группа × решение эксперта» (страницы-списки своей долей), "
                     "наибольшие остатки, доля от двух строк в обеих частях; примеры инструкции в настройке; "
                     "порядок в доле по хэшу зерна и номера",
             "tuning": [item["id"] for item in exam["items"] if item["id"] in tuning],
             "exam": [item["id"] for item in exam["items"] if item["id"] not in tuning]}
    if write:
        path.write_text(json.dumps(split, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**split, "new": True}


# --- Прогоны -------------------------------------------------------------------------------------


def _answers(model: str, items: list[dict[str, Any]], runs: int, dictionaries: dict[str, Any]) -> list[dict[str, Any]]:
    """Ответы воркера «Отбора»: строка на (прогон, публикацию). Технический сбой (модель не ответила) — одна повторная
    попытка, как у шага планировщика; обе попытки оплачены. Ответ не по правилам не повторяется, как на ядре."""
    payload = {"model": model, "reasoning": config.OTBOR_REASONING, "dictionaries": dictionaries,
               "items": [model_item(item) for item in items]}
    answers: list[dict[str, Any]] = []
    for number in range(1, runs + 1):
        decisions = otbor.process_payload(payload)["decisions"]
        decisions = [{**decision, "attempts": 1, "model_calls": len(decision["calls"])} for decision in decisions]
        failed = [index for index, decision in enumerate(decisions) if _technical(decision, dictionaries)]
        if failed:
            retry = otbor.process_payload({**payload, "items": [payload["items"][index] for index in failed]})
            for index, decision in zip(failed, retry["decisions"]):
                paid = decisions[index]
                decisions[index] = {**decision, "attempts": 2,
                                    "model_calls": len(paid["calls"]) + len(decision["calls"]),
                                    "input_tokens": paid["input_tokens"] + decision["input_tokens"],
                                    "output_tokens": paid["output_tokens"] + decision["output_tokens"]}
        for item, decision in zip(items, decisions):
            answers.append({"run": number, "id": item["id"], "calls": decision["calls"], "model": decision["model"],
                            "attempts": decision["attempts"], "model_calls": decision["model_calls"],
                            "input_tokens": int(decision["input_tokens"]),
                            "output_tokens": int(decision["output_tokens"])})
        failures = sum(_technical(decision, dictionaries) for decision in decisions)
        print(f"{model}: прогон {number}/{runs}: публикаций {len(decisions)}, повторов после сбоя {len(failed)}, "
              f"без ответа {failures}")
    return answers


def _technical(decision: dict[str, Any], dictionaries: dict[str, Any]) -> bool:
    return outcome(decision, dictionaries).failure == otbor_store.FAILURE_TECHNICAL


def _finals(answers: list[dict[str, Any]], items: list[dict[str, Any]],
            dictionaries: dict[str, Any]) -> dict[str, list[dict[str, Any] | None]]:
    """Значения сигнала по прогонам — как их записало бы ядро (otbor_answer.outcome, «спорное» рядом); сбой — None."""
    links = {item["id"]: len(item["links"] or []) for item in items}
    finals: dict[str, list[dict[str, Any] | None]] = {item["id"]: [] for item in items}
    for answer in sorted(answers, key=lambda one: one["run"]):
        checked = outcome(answer, dictionaries, links=links[answer["id"]])
        finals[answer["id"]].append(None if checked.values is None
                                    else {**checked.values, "disputed": checked.disputed})
    return finals


def _model_scores(model: str, answers: list[dict[str, Any]], items: list[dict[str, Any]],
                  dictionaries: dict[str, Any], control: dict[str, Any] | None) -> dict[str, Any]:
    scores = evaluate(items, _finals(answers, items, dictionaries), control)
    return {"model": model, **_spend(answers), "answers": answers, "scores": scores, "passed": scores["passed"],
            "failed": scores["failed"]}


def _spend(answers: list[dict[str, Any]]) -> dict[str, Any]:
    tokens_in = sum(answer["input_tokens"] for answer in answers)
    tokens_out = sum(answer["output_tokens"] for answer in answers)
    cost = sum((answer["input_tokens"] * price[0] + answer["output_tokens"] * price[1]) / 1_000_000
               for answer in answers for price in [config.price_for_model(answer["model"])])
    return {"calls": sum(answer["model_calls"] for answer in answers), "input_tokens": tokens_in,
            "output_tokens": tokens_out, "cost_usd": round(cost, 4)}


def _report(exam: dict[str, Any], items: list[dict[str, Any]], dictionaries: dict[str, Any],
            tried: list[dict[str, Any]], settings: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """Отчёт прогона или пересчёта — рядом с манифестом, JSON и читаемый .md (otbor_exam_report.write)."""
    part, decision = settings["part"], _decision(tried, settings["part"])
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "part": part,
        **{key: settings[key] for key in ("runs", "reasoning", "prompt_version")},
        "dictionaries_version": dictionaries.get("version"), "pack_versions": exam["pack_versions"],
        "manifest": exam["manifest"], "markup": exam["markup"], "files": _files(exam["folder"]),
        "rows_file": exam["rows"], "approved": exam["approved"], "rules": _rules(exam),
        "deprecated": {one["model"]: DEPRECATED[one["model"]] for one in tried if one["model"] in DEPRECATED},
        "split": {key: settings["split"][key] for key in ("tuning", "exam")}, "publications": len(items),
        "strata": (exam["control"] or {}).get("strata"), "models": tried,
        "decision": {"model": decision, "reason": otbor_exam_report.reason(decision, tried) if part == "exam"
                     else "настройка: модель выбирает только экзамен"},
        **extra,
    }
    path, readable = otbor_exam_report.write(exam["folder"], report)
    return {**report, "report": str(path), "readable": str(readable)}


# --- Контроль с ядра ------------------------------------------------------------------------------


def control_values(manifest: dict[str, Any]) -> dict[str, Any]:
    """Контроль с ядра (там база): размеры страт ленты в окне пакета — кандидаты выборки пакета (otbor_pack.feed_pool),
    — и категория находки, по которой витрина решала показ (судья или человек), у радарных строк манифеста.
    Решение машины экзамен берёт из группы манифеста, а не отсюда."""
    window = manifest.get("window") if isinstance(manifest, dict) else None
    items = manifest.get("items") if isinstance(manifest, dict) else None
    if not (isinstance(window, dict) and window.get("since") and window.get("until") and isinstance(items, list)):
        raise ExamError("не манифест пакета «Отбора»: нужны окно ленты window и строки items")
    card = {int(item["article_id"]) for item in items if item.get("group") == "card_markup" and item.get("article_id")}
    pool = otbor_pack.feed_pool(window["since"], window["until"], exclude=card)
    counted = Counter(item["group"] for item in pool)
    radar = {item["id"]: int(item["signal_id"]) for item in items if item.get("origin") == "radar"
             and item.get("signal_id")}
    categories = otbor_pack_store.judged_categories(sorted(set(radar.values())))
    return {"window": {"since": window["since"], "until": window["until"]},
            "strata": {group: counted[group] for group in FEED_GROUPS},
            "rows": {number: {"signal_id": signal_id, "signal_category": categories.get(signal_id)}
                     for number, signal_id in radar.items()}}
