"""Общее для тестов экзамена «Отбора» (#133, шов 2 спеки #128).

Пакет эксперту выгружается командой otbor-expert-pack на изолированной базе (otbor_pack_support), разметка —
как её вернул эксперт: метки по группам манифеста, как в настоящей разметке 23.10 (гейт отклонил больше, чем
эксперт, а принял и лишнее; «Отсеянные» радара эксперт почти все берёт). Рядом с манифестом — файл строк экзамена
(страницы-списки и строки-примеры инструкции). Подставная модель отвечает по модели и заголовку публикации и
запоминает вызовы. Сами тесты — test_otbor_exam.py.
"""

from __future__ import annotations

from collections import defaultdict
import json

import pytest

from oiltech_digest import cli, otbor
from oiltech_digest.otbor_pack_format import load_dictionaries
from oiltech_digest.processing.openai_client import AIResponse
from tests.otbor_pack_support import TAKEN, business, fill, rejected, technology
from tests.otbor_support import occasion_for

# gpt-6-luna — самая дешёвая из действующих; gpt-5.4-nano поставщик пометил устаревшей (экзамен её не выбирает).
LUNA, NANO, MINI, FULL = "gpt-6-luna", "gpt-5.4-nano", "gpt-5.4-mini", "gpt-5.5"
ROWS_NAME = "otbor_exam_rows.json"
# Сколько строк группы эксперт взял (первые по номеру строки); остальные отклонил.
TAKEN_BY_EXPERT = {"feed_rejected": 5, "feed_accepted": 20}
OLD_EVENTS = 2  # «Отсеянные» радара, которые эксперт отклонил как старое событие (первые по номеру)
LIST_PAGES = 2  # страницы-списки среди взятых «Отсеянных» (следующие по номеру)


def expert_codes(items):
    """Метки эксперта в кодах справочников по номеру строки: решение, отрасль, связь, причина, категория."""
    codes, groups = {}, defaultdict(list)
    for item in sorted(items, key=lambda one: one["id"]):
        groups[item["group"]].append(item)
    for group, members in groups.items():
        for index, item in enumerate(members):
            codes[item["id"]] = _expert(group, item["origin"], index)
    return codes


def _expert(group, origin, index):
    def label(decision, industry, relevance, reason=None, category=None):
        return {"decision": decision, "industry": industry, "oilfield_relevance": relevance, "reject_reason": reason,
                "category": category}

    if group == "feed_rejected":
        return (label("accept", "other_industry", "transferable", category="technology")
                if index < TAKEN_BY_EXPERT[group] else
                label("reject", "other_industry", "none", "other_industry_no_transfer"))
    if group == "feed_accepted":
        return (label("accept", "oil_gas", "direct", category="business") if index < TAKEN_BY_EXPERT[group]
                else label("reject", "oil_gas", "none", "oil_gas_no_link"))
    if group == "card_markup":
        return label("accept", *(("oil_gas", "direct") if origin == "feed" else ("oilfield_services", "direct")),
                     category="business" if origin == "feed" else "technology")
    if group == "radar_filtered" and index < OLD_EVENTS:
        return label("reject", "oilfield_services", "direct", "old_event")
    if group == "materials":
        return label("accept", "other_industry", "transferable", category="other")
    return label("accept", "oilfield_services", "direct", category="technology")


def markup_columns(codes):
    """Метки эксперта → колонки таблицы разметки (подписи справочников, баллы и обоснование у взятых)."""
    dictionaries = load_dictionaries()

    def label(block, code):
        return next(value["label"] for value in dictionaries[block]["values"] if value["code"] == code)

    shared = {"Отрасль": label("industry", codes["industry"]),
              "Связь с нефтесервисом": label("oilfield_relevance", codes["oilfield_relevance"])}
    if codes["decision"] == "reject":
        return rejected(**shared, **{"Причина отказа": label("reject_reason", codes["reject_reason"])})
    build = {"business": business, "technology": technology}.get(codes["category"])
    return {**(build() if build else {**TAKEN, "Категория": "другое"}), **shared}


def special_rows(items):
    """Страницы-списки — взятые «Отсеянные» радара после старых событий; строки-примеры инструкции — две строки
    прежней разметки карточки (как в справочниках «Отбора» стоят строки старых 50)."""
    filtered = sorted(item["id"] for item in items if item["group"] == "radar_filtered")
    card = sorted(item["id"] for item in items if item["group"] == "card_markup")
    return {"list_pages": filtered[OLD_EVENTS:OLD_EVENTS + LIST_PAGES], "in_instruction": card[:2]}


def write_rows(out, rows, *, approved=True):
    """Файл строк экзамена рядом с манифестом; approved — отметка утверждения: True — текущих инструкции и
    справочников, строка — как есть, пусто — без отметки (прогона нет)."""
    if approved is True:
        approved = (f"утверждено владельцем: инструкция {otbor.PROMPT_VERSION}, справочники "
                    f"{load_dictionaries()['version']}")
    (out / ROWS_NAME).write_text(json.dumps({**rows, **({"approved": approved} if approved else {})},
                                            ensure_ascii=False), encoding="utf-8")


@pytest.fixture()
def exam_pack(pack):
    """Пакет, размеченный экспертом, с файлом строк экзамена: таблица, манифест, метки и строки."""
    codes = expert_codes(pack["items"])
    markup = fill(pack["out"], {number: markup_columns(labels) for number, labels in codes.items()})
    rows = special_rows(pack["items"])
    write_rows(pack["out"], rows)
    titles = {item["title"]: item["id"] for item in pack["items"]}
    return {**pack, "markup": markup, "codes": codes, "rows": rows, "titles": titles}


def oracle(exam_pack, by_id=None):
    """Модель, которая отвечает, как эксперт; by_id — {номер строки: поправки ответа или функция от прогона}."""
    by_id = by_id or {}

    def reply(title):
        number = exam_pack["titles"][title]
        codes = exam_pack["codes"][number]
        relevance = codes["oilfield_relevance"]
        answer = {
            "industry": codes["industry"], "oilfield_relevance": relevance,
            "oilfield_application": "Энергоснабжение удалённых промыслов" if relevance == "transferable" else "",
            "decision": codes["decision"], "reject_reason": codes["reject_reason"] or "none",
            "justification": "Как у эксперта", "category": codes["category"] or "business",
        }
        change = by_id.get(number, {})
        changed = change(answer) if callable(change) else {**answer, **change}
        # Повод — по ответу после поправки, если поправка его не задала: иначе лишний конфликт шагов.
        return {"occasion": occasion_for(changed), **changed}
    return reply


def always_take(title):
    """Модель-константа «всегда брать»: ответ по правилам и без конфликта шагов, но не о публикации."""
    return {"industry": "oilfield_services", "occasion": "oilfield_work", "oilfield_relevance": "direct",
            "oilfield_application": "", "decision": "accept", "reject_reason": "none", "justification": "Берём всё",
            "category": "business"}


class ExamModel:
    """Подставная модель экзамена: ответ — по модели и заголовку публикации; вызовы запоминает."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None,
                      reasoning_effort=None):
        title = next(line.split(":", 1)[1].strip() for line in user_input.splitlines()
                     if line.startswith("Заголовок:"))
        self.calls.append({"model": model, "title": title, "input": user_input, "instructions": instructions})
        reply = self.answers[model](title)
        if isinstance(reply, Exception):
            raise reply
        return AIResponse(data=reply, model=f"{model}-2026-03-17", input_tokens=2000, output_tokens=300)


def run_exam(monkeypatch, markup, answers, *args):
    """Команда экзамена с подставной моделью; последний отчёт рядом с манифестом. Без --models лестница — модели, у
    которых есть ответы (лестница по умолчанию — в тесте сухого прогона)."""
    model = ExamModel(answers)
    monkeypatch.setattr(otbor, "make_client", lambda: model)
    ladder = [] if "--models" in args or "--rescore" in args else ["--models", ",".join(answers)]
    cli.main(["otbor-exam", "--markup", str(markup), *ladder, *args])
    [report] = sorted(markup.parent.glob("otbor_exam_report_*.json"))[-1:]
    return model, json.loads(report.read_text(encoding="utf-8"))


def part(report, exam_pack, name="exam"):
    """Строки части отчёта с метками эксперта и группой."""
    by_id = {item["id"]: item for item in exam_pack["items"]}
    return [{**by_id[number], "codes": exam_pack["codes"][number]} for number in report["split"][name]]
