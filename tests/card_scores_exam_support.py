"""Общее для тестов экзамена «Карточки» (#133, шов 2 спеки #128): тема основного вызова и баллы второго.

Пакет «Отбора» выгружает otbor-expert-pack на изолированной базе (otbor_pack_support), разметка — как у эксперта:
решения и метки «Отбора» как в otbor_exam_support, у взятых бизнес- и технологических строк — баллы по критериям,
общая оценка и тема по кругу (CLASSES, THEMES). Выгрузка с ядра (card-scores-exam-control) — с той же базы, в ней два
«сильных сверх пакета»: статья, выбранная «в дайджест», и карточка из списка заказчика. Рядом — пакет экзамена
карточки #98 по строкам прежней разметки (сверка атрибутов основного вызова). Подставная модель отвечает
за три вызова — суть (article_summary), основной вызов «Карточки» (signal_card) и второй (signal_scores) — и
запоминает их. Сами тесты — test_card_scores_exam*.py.
"""

from __future__ import annotations

import json

import pytest

from oiltech_digest import (card_scores_exam_calls, card_scores_exam_score, cli, signal_card, signal_card_exam,
                            signal_card_pack, signal_scores)
from oiltech_digest.db import marks, repository
from oiltech_digest.otbor_pack_format import MANIFEST_NAME, load_rubrics
from oiltech_digest.processing.openai_client import AIResponse
from tests.otbor_exam_support import ROWS_NAME, expert_codes, markup_columns, special_rows, write_rows
from tests.otbor_pack_support import BUSINESS, TECH, THEMES, fill, radar
from tests.signal_card_support import row as card_row
from tests.signal_card_support import write_pack as write_card_pack

CARD = "gpt-6-sol"  # основной вызов — модель ядра
SUMMARY = "gpt-5-mini"  # «Суть и перевод» — модель ленты
LUNA, MINI, FULL = "gpt-6-luna", "gpt-5.4-mini", "gpt-5.5"
CONTROL_NAME = "card_scores_exam_control.json"
# Общая оценка эксперта и балл каждого критерия строки — по кругу; у «Свежести» технологии — «нет даты».
CLASSES = (("Сильный сигнал", 90), ("Полезный сигнал", 72), ("Наблюдать", 50), ("Фоновый материал", 45),
           ("Низкая ценность / шум", 25))
BAND_OF = {"Сильный сигнал": "strong", "Полезный сигнал": "useful", "Наблюдать": "watch", "Фоновый материал": "watch",
           "Низкая ценность / шум": "weak"}
MATURITY = "Зрелость / доказательность"


def approve_scores(out, approved=True):
    """Отметка утверждения части «Карточки» в файле строк экзамена: True — текущих версий инструкций и линеек,
    строка — как есть, None — без отметки."""
    rows = json.loads((out / ROWS_NAME).read_text(encoding="utf-8"))
    rows.pop("approved_scores", None)
    if approved is True:
        approved = (f"утверждено владельцем: карточка {signal_card.PROMPT_VERSION}, баллы "
                    f"{signal_scores.PROMPT_VERSION}, линейки {load_rubrics()['version']}, "
                    f"{card_scores_exam_score.rules_text()}")
    if approved:
        rows["approved_scores"] = approved
    (out / ROWS_NAME).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def export_control(out, capsys):
    """Выгрузка с ядра (card-scores-exam-control) — рядом с манифестом, как её кладёт оператор."""
    capsys.readouterr()
    cli.main(["card-scores-exam-control", "--manifest", str(out / MANIFEST_NAME)])
    data = json.loads(capsys.readouterr().out)
    (out / CONTROL_NAME).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def add_strong(corpus):
    """«Сильные сверх пакета» на базе: статья вне окна пакета, выбранная «в дайджест», и карточка радара из списка
    заказчика с одной ссылкой (в пакет не попала)."""
    late = next(one for one in corpus["excluded_feed"] if _article_title(one).endswith("accepted-late"))
    marks.set_article_mark(late, status="digest")
    top = radar("customer-top", links=1)
    with repository.get_connection() as conn:
        conn.execute("UPDATE signal_evidence SET raw_payload_json = raw_payload_json || %s::jsonb WHERE signal_id = %s",
                     ('{"origin": "customer_list"}', top))
        conn.commit()
    return {"digest_article": late, "top_signal": top}


def card_exam_pack(items, folder, *, tuning=1, text_of=None):
    """Пакет экзамена #98 по строкам прежней разметки пакета «Отбора»: те же номера и тексты; эксперт #98 поставил
    всем «контракт» и «Ближний Восток». Деление #98 — первые tuning строк в настройке. Таблица и номера экзамена."""
    text_of = text_of or {}
    rows = []
    for item in items:
        if item["group"] != "card_markup":
            continue
        built = card_row(int(item["id"].split(":")[1]), "technology" if item["origin"] == "radar" else "useful",
                         title=item["title"], text=text_of.get(item["id"], item["text"]),
                         published=item["published"] or "2026-09-30")
        rows.append({**built, "id": item["id"]})
    folder.mkdir()
    markup = write_card_pack(folder, rows)
    ids = [one["id"] for one in rows]
    (folder / signal_card_exam.SPLIT_NAME).write_text(json.dumps({"tuning": ids[:tuning], "exam": ids[tuning:]}),
                                                      encoding="utf-8")
    return markup, ids[tuning:]


def _article_title(article_id):
    with repository.get_connection() as conn:
        return conn.execute("SELECT title FROM articles WHERE id = %s", (article_id,)).fetchone()[0]


@pytest.fixture()
def scores_pack(pack, capsys):
    """Пакет, размеченный экспертом, с баллами, файлом строк (утверждены обе части) и выгрузкой с ядра."""
    out = pack["out"]
    codes = expert_codes(pack["items"])
    rows = special_rows(pack["items"])
    answers = {number: markup_columns(labels) for number, labels in codes.items()}
    expert = {}
    scored = [item for item in sorted(pack["items"], key=lambda one: one["id"])
              if codes[item["id"]]["decision"] == "accept"
              and codes[item["id"]]["category"] in ("business", "technology") and item["id"] not in rows["list_pages"]]
    for index, item in enumerate(scored):
        overall, score = CLASSES[index % len(CLASSES)]
        theme = list(THEMES)[index % len(THEMES)]
        technology = codes[item["id"]]["category"] == "technology"
        columns = TECH if technology else BUSINESS
        answers[item["id"]].update({"Общая оценка": overall, "Тема": theme, **{column: score for column in columns}})
        if technology:
            answers[item["id"]][TECH[-1]] = "нет даты"
        expert[item["id"]] = {"overall": overall, "band": BAND_OF[overall], "theme": theme, "score": score,
                              "profile": "tech_radar" if technology else "business", "title": item["title"]}
    markup = fill(out, answers)
    write_rows(out, rows)
    approve_scores(out)
    strong = add_strong(pack["corpus"])
    control = export_control(out, capsys)
    card_markup, card_exam = card_exam_pack(pack["items"], out.parent / "card98")
    titles = {**{one["title"]: number for number, one in expert.items()},
              **{one["title"]: one["id"] for one in control["strong"]}}
    return {**pack, "markup": markup, "codes": codes, "rows": rows, "expert": expert, "strong": strong,
            "control": control, "titles": titles, "card_markup": card_markup, "card_exam": card_exam}


# --- Подставная модель ---------------------------------------------------------------------------


def _title(user_input):
    return next(line.split(":", 1)[1].strip() for line in user_input.splitlines()
                if line.startswith(("Заголовок:", "title:")))


def oracle_card(scores_pack, by_id=None):
    """Основной вызов, который ставит тему, как эксперт; даты события в тексте нет, зрелость «не определено», стран
    нет. by_id — {номер строки: поправки ответа}."""
    by_id = by_id or {}

    def reply(title):
        number = scores_pack["titles"][title]
        expert = scores_pack["expert"].get(number)
        answer = {"event_date": "", "event_date_source": "по дате публикации", "companies": [], "event_countries": [],
                  "event_type": signal_card_pack.UNDEFINED, "region": signal_card_pack.UNDEFINED,
                  "technology_class": signal_card_pack.UNDEFINED, "maturity": signal_card_pack.UNDEFINED,
                  "theme": expert["theme"] if expert else signal_card_pack.UNDEFINED,
                  "why_important": "Важно нефтесервису"}
        return {**answer, **by_id.get(number, {})}
    return reply


def oracle_scores(scores_pack, by_id=None):
    """Второй вызов, который ставит баллы эксперта (у «сильных сверх пакета» — 90); by_id — {номер строки: балл
    каждого критерия или функция от номера прогона}."""
    by_id = by_id or {}

    def reply(title, criteria, run):
        number = scores_pack["titles"][title]
        expert = scores_pack["expert"].get(number)
        score = by_id.get(number, expert["score"] if expert else 90)
        score = score(run) if callable(score) else score
        if isinstance(score, Exception):
            return score
        return scored(criteria, score)
    return reply


def flat(score):
    """Второй вызов, который всем критериям всех сигналов ставит один балл."""
    return lambda title, criteria, run: scored(criteria, score)


def scored(criteria, score):
    answer = {"scores": [{"criterion": name, "score": score, "why": "Как у эксперта"} for name in criteria]}
    return {**answer, "named_customer": True} if MATURITY in criteria else answer


class ScoresModel:
    """Подставная модель экзамена: вызов — по схеме ответа (суть, основной вызов, второй вызов), ответ — по модели и
    заголовку публикации; номер прогона второго вызова считает сама (по повторам заголовка у модели)."""

    def __init__(self, card, scores):
        self.card = card
        self.scores = scores
        self.calls = []
        self._seen = {}

    def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None,
                      reasoning_effort=None):
        kind, title = schema["name"], _title(user_input)
        self.calls.append({"kind": kind, "model": model, "title": title, "input": user_input,
                           "instructions": instructions})
        if kind == "article_summary":
            data = {"summary": f"Суть публикации: {title}"}
        elif kind == "signal_card":
            data = self.card(title)
        else:
            criteria = schema["schema"]["properties"]["scores"]["items"]["properties"]["criterion"]["enum"]
            run = self._seen[(model, title)] = self._seen.get((model, title), 0) + 1
            data = self.scores[model](title, criteria, run)
        if isinstance(data, Exception):
            raise data
        return AIResponse(data=data, model=f"{model}-2026-03-17", input_tokens=3000, output_tokens=400)

    def of(self, kind, model=None):
        return [call for call in self.calls if call["kind"] == kind and (model is None or call["model"] == model)]


def run_exam(monkeypatch, scores_pack, scores, *args, card=None):
    """Команда экзамена с подставной моделью; последний отчёт рядом с манифестом. Без --models лестница — модели, у
    которых есть ответы; разметка #98 — пакета фикстуры, если тест не назвал свою."""
    model = ScoresModel(card or oracle_card(scores_pack), scores)
    for module in (card_scores_exam_calls, signal_card, signal_scores):
        monkeypatch.setattr(module, "make_client", lambda: model)
    ladder = [] if "--models" in args or "--rescore" in args else ["--models", ",".join(scores)]
    options = [] if "--rescore" in args else ["--card-model", CARD, "--summary-model", SUMMARY]
    if "--card-markup" not in args:
        options += ["--card-markup", str(scores_pack["card_markup"])]
    cli.main(["card-scores-exam", "--markup", str(scores_pack["markup"]), *ladder, *options, *args])
    [report] = sorted(scores_pack["out"].glob("card_scores_exam_report_*.json"))[-1:]
    return model, json.loads(report.read_text(encoding="utf-8"))


def part_rows(report, scores_pack, name="exam"):
    """Строки пакета с баллами эксперта в части отчёта."""
    return [number for number in report["split"][name] if number in scores_pack["expert"]]
