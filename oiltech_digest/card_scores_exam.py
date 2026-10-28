"""Экзамен «Карточки» (#133, спека #128): тема основного вызова и баллы второго вызова на разметке эксперта.

Набор — пакет «Отбора» (#129) с его делением и файлом строк (otbor_exam): строки, которые эксперт взял с категорией
«бизнес» или «технология» (у них баллы по критериям профиля и общая оценка), без страниц-списков. На экзамене к ним
добавляются «сильные сверх пакета» из выгрузки с ядра: выбранное «в дайджест» и эталонный ТОП заказчика (спека); они
показаны отдельно, решение — по строкам пакета (card_scores_exam_score).

Каждая строка проходит путь сигнала после «Отбора»: «Суть и перевод» (один раз на строку, с полями «Отбора»
эксперта), основной вызов «Карточки» (модель ядра, три прогона: тема, дата события и зрелость) и второй вызов —
модели лестницы, по прогону на каждый прогон основного: вход — категория, отрасль и связь эксперта, дата и зрелость
основного вызова того же прогона. Ответы проходят проверку кодом ядра (signal_card.card_values,
signal_scores.score_values): мерится то, что дошло бы до сигнала. Атрибуты основного вызова сверяются с разметкой
экзамена карточки (#98) на строках его экзамена, по его порогам. Суть и ответы основного вызова лежат рядом с
манифестом по отпечатку запроса (card_scores_exam_calls). Критерии «Скоринга» (веса, ключевые слова), теги тем и
контроль — из выгрузки с ядра (card_scores_exam_control): экзамен идёт без базы. Модель второго вызова — самая дешёвая
из прошедших; экзамен (не настройка) — только после утверждения владельцем (approved_scores называет инструкции,
линейки и пороги). Отчёт — рядом с манифестом, вне репозитория: он публичный.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from oiltech_digest import (card_scores_exam_control, card_scores_exam_report, config, otbor_exam, otbor_markup,
                            signal_card, signal_card_exam, signal_card_exam_score, signal_scores, signal_scoring,
                            signal_themes)
from oiltech_digest.card_scores_exam_calls import Calls, ExamError, Inputs
from oiltech_digest.card_scores_exam_score import (BAND_RULE, EXPERT_BANDS, EXTRA, THRESHOLDS, baseline,
                                                   control_agreement, country_region, evaluate, rules_text, theme)
from oiltech_digest.otbor_pack_format import CATEGORY_PROFILE, load_rubrics
from oiltech_digest.signal_card_countries import Countries
from oiltech_digest.signal_card_pack import load_dictionaries as card_dictionaries
from oiltech_digest.signal_card_pack import publication_text

# Кандидаты второго вызова (решение 20 журнала гриля, спека): лестница «Отбора» и gpt-6-sol — модель «Отбора» и
# основного вызова на проде с 26.10; с 27.10 линейка 6.1 (terra и sol, решение владельца; 5.6-terra и 5.6-sol не
# берём). Лестница — по цене.
MODELS = ("gpt-6-luna", "gpt-5.6-luna", "gpt-5.4-nano", "gpt-5.4-mini", "gpt-6.1-terra", "gpt-6-sol", "gpt-6.1-sol",
          "gpt-5.5")
RUNS = otbor_exam.RUNS
APPROVAL_KEY = "approved_scores"


def run(markup_path: str | Path, *, manifest_path: str | Path | None = None, models: tuple[str, ...] = MODELS,
        runs: int = RUNS, part: str = "exam", every: bool = False, workers: int = 1, card_model: str | None = None,
        summary_model: str | None = None, card_markup: str | Path | None = None,
        theme_before: str | Path | None = None) -> dict[str, Any]:
    """Экзамен второго вызова по лестнице моделей (every — все модели) и тема основного вызова; card_markup — разметка
    экзамена карточки (#98): атрибуты основного вызова того же прогона сверяются с ней; theme_before — прежний отчёт:
    тема и ошибки «Рынок против темы отрасли» до и после (#149). Отчёт рядом с манифестом."""
    _check_runs(runs, part)
    exam = load(markup_path, manifest_path)
    before = _theme_before(theme_before, exam) if theme_before else None
    if part == "exam":
        _check_approved(exam["approved_scores"])
    card_exam = _card_exam(card_markup, exam, part)
    ladder = _by_price(models)
    calls = _calls(exam, card_model, summary_model, workers)
    split = otbor_exam._split(exam, write=True)
    rows = _rows(exam, split, part)
    _check_control(rows, exam)
    summaries = calls.summaries(rows)
    cards = calls.cards(rows, summaries, runs)
    tried: list[dict[str, Any]] = []
    for model in ladder:
        tried.append(_model_scores(model, calls.scores(model, rows, summaries, cards, runs), rows, summaries, cards,
                                   calls.inputs, exam["scores_control"]["rows"]))
        print(f"{model}: " + ("ступень и порядок пройдены" if tried[-1]["passed"]
                              else "не прошла: " + ", ".join(tried[-1]["failed"])), flush=True)
        if not every and _decision(tried, part):
            break
    settings = {"part": part, "runs": runs, "split": split, "card_model": calls.card_model,
                "summary_model": calls.summary_model, "card_prompt_version": signal_card.PROMPT_VERSION,
                "scores_prompt_version": signal_scores.PROMPT_VERSION, "card_reasoning": config.SIGNAL_CARD_REASONING,
                "scores_reasoning": config.SIGNAL_SCORES_REASONING}
    return _report(exam, rows, summaries, cards, tried, settings, spend=calls.spend, card_exam=card_exam,
                   theme_before=before)


def rescore(report_path: str | Path, markup_path: str | Path, *, manifest_path: str | Path | None = None,
            card_markup: str | Path | None = None, theme_before: str | Path | None = None) -> dict[str, Any]:
    """Пересчёт сохранённого отчёта без вызовов модели: те же суть и ответы — по нынешним проверке кодом, выгрузке с
    ядра, границам ступеней и порогам; новый отчёт — рядом. Ответы относятся к текстам своего манифеста, к своим
    линейкам и справочникам карточки: с другими пересчёт отказывает."""
    source = Path(report_path).expanduser().resolve()
    try:
        saved = card_scores_exam_report.read(source)
    except ValueError as exc:
        raise ExamError(str(exc)) from exc
    exam = load(markup_path, manifest_path)
    before = _theme_before(theme_before, exam) if theme_before else None
    card_exam = _card_exam(card_markup, exam, saved["part"])
    inputs = Inputs(exam)
    if saved["manifest"]["sha256"] != exam["manifest"]["sha256"]:
        raise ExamError(f"{source.name}: ответы получены на другом манифесте, тексты другие: пересчёт неверен")
    for key, name, current in (("rubrics_version", "линейках", load_rubrics()["version"]),
                               ("dictionaries_version", "справочниках карточки", inputs.dictionaries.get("version"))):
        if saved[key] != current:
            raise ExamError(f"{source.name}: ответы на {name} {saved[key]}, сейчас {current}: пересчёт неверен, нужен "
                            "новый прогон")
    found = {row["id"]: row for row in _rows(exam, saved["split"], saved["part"])}
    missing = [one["id"] for one in saved["rows"] if one["id"] not in found]
    if missing:
        raise ExamError(f"{source.name}: строк {', '.join(missing[:5])} нет в наборе (сменились выгрузка с ядра, "
                        "разметка или деление): пересчёт неверен")
    rows = [found[one["id"]] for one in saved["rows"]]
    _check_control(rows, exam)
    summaries, cards = saved["summaries"], saved["cards"]["answers"]
    # Цена и токены — исходного прогона: новых вызовов нет, а ставки с тех пор могли поменяться.
    tried = [{**_model_scores(one["model"], one["answers"], rows, summaries, cards, inputs,
                              exam["scores_control"]["rows"]),
              **{key: one[key] for key in ("calls", "input_tokens", "output_tokens", "cost_usd")}}
             for one in saved["models"]]
    settings = {key: saved[key] for key in ("part", "runs", "split", "card_model", "summary_model",
                                            "card_prompt_version", "scores_prompt_version", "card_reasoning",
                                            "scores_reasoning")}
    changed = [name for name, value in rules(exam).items() if (saved.get("rules") or {}).get(name) != value]
    decided = saved.get("decision")
    # Выгрузка с ядра задаёт порог ступени: снятая заново после прогона меняет его так же, как разметка — счёт.
    control_before = ((saved.get("files") or {}).get("control") or {}).get("sha256")
    control_now = (card_scores_exam_report.files(exam["folder"])["control"] or {}).get("sha256")
    return _report(exam, rows, summaries, cards, tried, settings, spend=saved.get("spend") or {},
                   card_exam=card_exam, theme_before=before, rescored_from=source.name,
                   markup_changed=saved["markup"]["sha256"] != exam["markup"]["sha256"],
                   control_changed=control_before != control_now, rules_changed=changed,
                   original_decision=decided.get("model") if isinstance(decided, dict) else None)


def plan(markup_path: str | Path, *, manifest_path: str | Path | None = None, models: tuple[str, ...] = MODELS,
         runs: int = RUNS, part: str = "exam", card_model: str | None = None,
         summary_model: str | None = None) -> dict[str, Any]:
    """Сухой прогон: строки, число вызовов и цена по этапам; ничего не пишет и не вызывает модель. Суть и основной
    вызов, которые уже лежат рядом с манифестом, не считаются. Цена — по длине запросов (signal_card.estimate); вход
    второго вызова — без даты и зрелости (их даст основной вызов); повтор после сбоя не учтён."""
    _check_runs(runs, part)
    exam = load(markup_path, manifest_path)
    calls = _calls(exam, card_model, summary_model, 1)
    split = otbor_exam._split(exam, write=False)
    rows = _rows(exam, split, part)
    _check_control(rows, exam)
    summaries = calls.cached_summaries()
    summary_todo = calls.summary_todo(rows)
    card_todo, card_chars = calls.card_todo(rows, summaries, runs)
    scores_chars = calls.scores_chars(rows, summaries) * runs
    return {"part": part, "runs": runs, "split_new": split["new"], "approved": exam["approved_scores"],
            "pack": sum(not row["extra"] for row in rows), "extra": sum(row["extra"] for row in rows),
            "profiles": {profile: sum(not row["extra"] and row["profile"] == profile for row in rows)
                         for profile in CATEGORY_PROFILE.values()},
            "summary": signal_card.estimate(calls.summary_chars(summary_todo), len(summary_todo), calls.summary_model),
            "card": signal_card.estimate(card_chars, card_todo, calls.card_model),
            "models": [signal_card.estimate(scores_chars, len(rows) * runs, model) for model in _by_price(models)]}


def _check_runs(runs: int, part: str) -> None:
    if runs < 1 or (part == "exam" and runs != RUNS):
        raise ExamError(f"--runs {runs}: на экзамене {RUNS} прогона на строку, на настройке от одного")


def _check_control(rows: list[dict[str, Any]], exam: dict[str, Any]) -> None:
    """Порог ступени — нынешние баллы ядра на строках пакета: без них он не считается, лестница оплатилась бы вся, и
    каждая модель «не прошла» бы ступень."""
    if not control_agreement(rows, exam["scores_control"]["rows"])["rows"]:
        raise ExamError(f"{card_scores_exam_control.NAME}: нет нынешнего балла ядра ни у одной строки пакета, порог "
                        "ступени не посчитать: выгрузку снять заново (card-scores-exam-control)")


def _check_approved(approved: str | None) -> None:
    """Экзамен идёт только на правилах, которые утвердил владелец: отметка называет текущие инструкции обоих вызовов,
    линейки и пороги (rules_text). Настройку это не держит."""
    names = (f"карточка {signal_card.PROMPT_VERSION}", f"баллы {signal_scores.PROMPT_VERSION}",
             f"линейки {load_rubrics()['version']}", rules_text())
    missing = [name for name in names if name not in (approved or "")]
    if missing:
        raise ExamError(f"{otbor_exam.ROWS_NAME}: {APPROVAL_KEY} не называет текущие правила ({', '.join(missing)}): "
                        "до платного прогона владелец утверждает инструкции, линейки и пороги")


def _calls(exam: dict[str, Any], card_model: str | None, summary_model: str | None, workers: int) -> Calls:
    """Вызовы экзамена; модели основного вызова и сути — с их ставками в таблице цен, иначе расход в отчёте солгал
    бы."""
    card_model, summary_model = card_model or config.SIGNAL_CARD_MODEL, summary_model or config.OPENAI_MODEL
    if card_model:
        _by_price((card_model, summary_model))
    return Calls(Inputs(exam), card_model, summary_model, workers)


def _by_price(models: tuple[str, ...]) -> list[str]:
    try:
        return otbor_exam._by_price(models)
    except otbor_exam.ExamError as exc:
        raise ExamError(str(exc)) from exc


def _decision(tried: list[dict[str, Any]], part: str) -> str | None:
    """Решение — первая по цене модель, прошедшая ступень и порядок, не устаревшая; настройка модель не выбирает."""
    if part != "exam":
        return None
    return next((one["model"] for one in tried if one["passed"] and one["model"] not in otbor_exam.DEPRECATED), None)


# --- Набор ---------------------------------------------------------------------------------------


def load(markup_path: str | Path, manifest_path: str | Path | None = None) -> dict[str, Any]:
    """Набор «Отбора» (его проверки: репозиторий, разметка, коды справочников, файл строк) с метками баллов эксперта,
    выгрузкой с ядра и отметкой утверждения части «Карточки»."""
    try:
        exam = otbor_exam.load(markup_path, manifest_path)
    except otbor_exam.ExamError as exc:
        raise ExamError(str(exc)) from exc
    if not exam["approved"]:
        raise ExamError(f"{otbor_exam.ROWS_NAME}: нет отметки approved: деление и строки экзамена утверждает "
                        "владелец до первого прогона")
    manifest_file = Path(exam["manifest"]["file"])
    manifest = {item["id"]: item for item in json.loads(manifest_file.read_text(encoding="utf-8"))["items"]}
    scored = {item["id"]: item for item in otbor_markup.check(exam["markup"]["file"], manifest_file)["items"]}
    rows = json.loads((exam["folder"] / otbor_exam.ROWS_NAME).read_text(encoding="utf-8"))
    approved = rows.get(APPROVAL_KEY)
    try:
        control = card_scores_exam_control.read(exam["folder"], set(manifest))
    except card_scores_exam_control.ControlError as exc:
        raise ExamError(str(exc)) from exc
    items = [{**item, "publisher_type": manifest[item["id"]].get("publisher_type") or "",
              "url": manifest[item["id"]].get("url") or "", "scored": scored[item["id"]]} for item in exam["items"]]
    return {**exam, "items": items, "scores_control": control,
            "approved_scores": approved.strip() if isinstance(approved, str) and approved.strip() else None}


def _theme_before(path: str | Path, exam: dict[str, Any]) -> dict[str, Any]:
    """Тема основного вызова прежнего прогона (#149) — «до» в отчёте, на тех же текстах: по его сохранённым ответам и
    строкам. Схема ответа держит тему в списке тематик, поэтому сверка точная; ответ прежней схемы (до #137, без
    стран) годится: берётся только тема."""
    source = Path(path).expanduser().resolve()
    try:
        saved = card_scores_exam_report.read(source)
    except ValueError as exc:
        raise ExamError(str(exc)) from exc
    if saved["manifest"]["sha256"] != exam["manifest"]["sha256"]:
        raise ExamError(f"{source.name}: прежний прогон на другом манифесте, тексты другие: «до» не сравнить")
    themes = Inputs(exam).themes
    answers = (saved.get("cards") or {}).get("answers") or {}
    rows = [{"id": row["id"], "theme": row["theme"], "extra": row["group"] == EXTRA} for row in saved["rows"]]
    cards = {row["id"]: [{"theme": _saved_theme(card, themes)} for card in answers.get(row["id"]) or []]
             for row in rows}
    found = theme(rows, cards, signal_themes.market(themes))
    return {"report": source.name, "agent": found["agent"], "n": found["n"], "market": found["market"]}


def _saved_theme(card: dict[str, Any], themes: list[str]) -> str | None:
    value = None if card.get("error") else (card.get("answer") or {}).get("theme")
    return value if value in themes else None


def _card_exam(markup: str | Path | None, exam: dict[str, Any], part: str) -> dict[str, Any] | None:
    """Разметка экзамена карточки (#98) для сверки атрибутов основного вызова того же прогона (тикет #133): на
    экзамене обязательна. Только строки его экзаменационной части (по строкам настройки #98 правили инструкцию) и только
    те же тексты, что видел эксперт «Отбора»."""
    if not markup:
        if part == "exam":
            raise ExamError("экзамен сверяет атрибуты основного вызова с разметкой экзамена карточки #98: нужен "
                            "--card-markup (таблица #98, манифест рядом)")
        return None
    try:
        card = signal_card_exam.load(markup)
        split = signal_card_exam._split(card, write=False)
    except signal_card_exam.ExamError as exc:
        raise ExamError(f"разметка #98: {exc}") from exc
    if split["new"]:
        raise ExamError(f"нет {signal_card_exam.SPLIT_NAME} рядом с манифестом #98: строки его настройки не отделить")
    texts = {item["id"]: publication_text(item["text"]) for item in exam["items"]}
    wrong = sorted(item["id"] for item in card["items"] if item["id"] in texts and item["text"] != texts[item["id"]])
    if wrong:
        raise ExamError(f"разметка #98: текст строк {', '.join(wrong[:5])} не тот, что в пакете «Отбора»: атрибуты "
                        "сверялись бы с разметкой другого текста")
    exam_ids = set(split["exam"])
    return {"items": [item for item in card["items"] if item["id"] in exam_ids], "markup": card["markup"],
            "names": signal_card_exam_score.CompanyNames(card_dictionaries()["companies"], card["aliases"])}


def _rows(exam: dict[str, Any], split: dict[str, Any], part: str) -> list[dict[str, Any]]:
    """Строки части: взятые экспертом бизнес и технология без страниц-списков; на экзамене и «сильные сверх пакета»."""
    ids = set(split[part])
    rubrics = load_rubrics()
    rows = [_pack_row(item, rubrics) for item in exam["items"] if item["id"] in ids and not item["list_page"]
            and item["labels"]["decision"] == "accept" and item["labels"]["category"] in CATEGORY_PROFILE]
    if part == "exam":
        rows += [_extra_row(item, rubrics) for item in exam["scores_control"]["strong"]]
    return rows


def _pack_row(item: dict[str, Any], rubrics: dict[str, Any]) -> dict[str, Any]:
    scored, labels = item["scored"], item["labels"]
    profile = CATEGORY_PROFILE[labels["category"]]
    return {**_publication(item), "extra": False, "group": item["group"], "category": labels["category"],
            "industry": labels["industry"], "oilfield_relevance": labels["oilfield_relevance"],
            "oilfield_application": "", "profile": profile, "rating": scored["overall"],
            "band": EXPERT_BANDS.get(scored["overall"]), "theme": scored["theme"],
            "scores": {name: value for name, value in scored["scores"].items() if value is not None},
            "computed": _computed(rubrics, profile)}


def _extra_row(item: dict[str, Any], rubrics: dict[str, Any]) -> dict[str, Any]:
    """«Сильный сверх пакета»: категория, отрасль и связь — машинные (разметки эксперта у него нет), тема не мерится."""
    profile = CATEGORY_PROFILE[item["category"]]
    return {**_publication(item), "extra": True, "group": EXTRA, "category": item["category"],
            "industry": item.get("industry"), "oilfield_relevance": item.get("oilfield_relevance"),
            "oilfield_application": item.get("oilfield_application") or "", "profile": profile, "rating": EXTRA,
            "band": signal_scoring.STRONG, "theme": None, "scores": {}, "computed": _computed(rubrics, profile)}


def _publication(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item.get(key) for key in ("id", "origin", "title", "text", "publisher", "publisher_type", "language",
                                           "url", "published", "received")}


def _computed(rubrics: dict[str, Any], profile: str) -> list[str]:
    return [one["name"] for one in rubrics["profiles"][profile] if signal_scoring.computed(one)]


# --- Счёт и отчёт --------------------------------------------------------------------------------


def finals(answers: list[dict[str, Any]], rows: list[dict[str, Any]], summaries: dict[str, str],
           cards: dict[str, list[dict[str, Any]]], inputs: Inputs) -> dict[str, list[dict[str, Any] | None]]:
    """Баллы сигнала по прогонам — как их записало бы ядро (signal_scores.score_values); сбой — None."""
    by_id = {row["id"]: row for row in rows}
    found: dict[str, list[dict[str, Any] | None]] = {row["id"]: [] for row in rows}
    for answer in sorted(answers, key=lambda one: one["run"]):
        row = by_id[answer["id"]]
        found[row["id"]].append(inputs.scores_values(row, summaries[row["id"]], cards[row["id"]][answer["run"] - 1],
                                                     answer))
    return found


def _model_scores(model: str, answers: list[dict[str, Any]], rows: list[dict[str, Any]], summaries: dict[str, str],
                  cards: dict[str, list[dict[str, Any]]], inputs: Inputs, control: dict[str, Any]) -> dict[str, Any]:
    scores = evaluate(rows, finals(answers, rows, summaries, cards, inputs), control)
    return {"model": model, **_spend(answers), "answers": answers, "scores": scores, "passed": scores["passed"],
            "failed": scores["failed"], "variant": scores["variant"]}


def _spend(answers: list[dict[str, Any]]) -> dict[str, Any]:
    tokens_in = sum(answer["input_tokens"] for answer in answers)
    tokens_out = sum(answer["output_tokens"] for answer in answers)
    cost = sum((answer["input_tokens"] * price[0] + answer["output_tokens"] * price[1]) / 1_000_000
               for answer in answers for price in [config.price_for_model(answer["model"])])
    return {"calls": sum(answer["attempts"] for answer in answers), "input_tokens": tokens_in,
            "output_tokens": tokens_out, "cost_usd": round(cost, 4)}


def _report(exam: dict[str, Any], rows: list[dict[str, Any]], summaries: dict[str, str],
            cards: dict[str, list[dict[str, Any]]], tried: list[dict[str, Any]], settings: dict[str, Any],
            *, spend: dict[str, Any], card_exam: dict[str, Any] | None = None,
            theme_before: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    """Отчёт прогона или пересчёта — рядом с манифестом, JSON и читаемый .md (card_scores_exam_report.write).
    theme_before — тема прежнего прогона (_theme_before): «до» рядом с темой этого."""
    part, decision = settings["part"], _decision(tried, settings["part"])
    inputs = Inputs(exam)
    control = exam["scores_control"]
    themes = {row["id"]: [inputs.card_values(row, card) for card in cards[row["id"]]] for row in rows}
    found_theme = theme(rows, themes, signal_themes.market(inputs.themes))
    if theme_before is not None:
        found_theme.update(before=theme_before,
                           not_below_before=(found_theme["agent"] or 0) >= (theme_before["agent"] or 0))
    chosen = next((one for one in tried if one["model"] == decision), None)
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **{key: value for key, value in settings.items() if key != "split"},
        "rubrics_version": load_rubrics()["version"], "dictionaries_version": inputs.dictionaries.get("version"),
        "keywords_setting": bool(config.SIGNAL_SCORES_KEYWORDS), "rules": rules(exam),
        "manifest": exam["manifest"], "markup": exam["markup"], "files": card_scores_exam_report.files(exam["folder"]),
        "approved_scores": exam["approved_scores"],
        "split": {key: settings["split"][key] for key in ("tuning", "exam")},
        "rows": [{key: row[key] for key in ("id", "group", "profile", "rating", "band", "theme")} for row in rows],
        "strong_skipped": control.get("skipped"), "summaries": summaries,
        "cards": {"model": settings["card_model"], "answers": cards}, "spend": spend,
        "theme": found_theme, "attributes": _attributes(card_exam, rows, themes),
        "baseline": baseline(rows, _weights(inputs.criteria), control["rows"]),
        "deprecated": {one["model"]: otbor_exam.DEPRECATED[one["model"]] for one in tried
                       if one["model"] in otbor_exam.DEPRECATED},
        "models": tried, "month": card_scores_exam_report.month(exam),
        "decision": {"model": decision, "variant": chosen["variant"] if chosen else None,
                     "reason": card_scores_exam_report.reason(decision, tried) if part == "exam"
                     else "настройка: модель выбирает только экзамен"},
        **extra,
    }
    path, readable = card_scores_exam_report.write(exam["folder"], report)
    return {**report, "report": str(path), "readable": str(readable)}


def _attributes(card_exam: dict[str, Any] | None, rows: list[dict[str, Any]],
                values: dict[str, list[dict[str, Any] | None]]) -> dict[str, Any] | None:
    """Атрибуты основного вызова того же прогона против разметки #98 — по его метрикам и порогам (новых нет); страна
    события (#137) — против его региона по таблице «страна → регион», без порога."""
    if card_exam is None:
        return None
    ids = {row["id"] for row in rows if not row["extra"]}
    items = [item for item in card_exam["items"] if item["id"] in ids]
    finals = {item["id"]: values[item["id"]] for item in items}
    return {"rows": sorted(item["id"] for item in items), "markup": card_exam["markup"],
            **{view: signal_card_exam_score.evaluate(items, finals, card_exam["names"],
                                                     lenient=view == "lenient")["overall"]
               for view in ("strict", "lenient")},
            "country": country_region(items, finals, Countries(card_dictionaries()))}


def _weights(criteria: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, float]]:
    """Веса критериев «Скоринга» по профилю (выгрузка с ядра): итог эксперта самого с собой."""
    return {profile: {one["name"]: float(one["weight"]) for one in found} for profile, found in criteria.items()}


def rules(exam: dict[str, Any]) -> dict[str, Any]:
    """Правила, по которым считается решение: пороги, правило ступени, границы ступеней, строки экзамена. Пересчёт
    сверяет их с исходным прогоном."""
    return {"thresholds": dict(THRESHOLDS), "band_rule": BAND_RULE, "bands": list(config.SIGNAL_SCORE_BANDS),
            "rows": exam["rows"]}
