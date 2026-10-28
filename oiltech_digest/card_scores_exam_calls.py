"""Вызовы экзамена «Карточки» (#133): «Суть и перевод», основной вызов и второй вызов — входом и проверкой ядра.

Вход каждого вызова строится кодом ядра (signal_card.payload_signal, signal_scores.context и payload_item), ответ
проверяется им же (signal_card.card_values, signal_scores.score_values): мерится то, что дошло бы до сигнала. Суть и
ответы основного вызова лежат рядом с манифестом по отпечатку входа (модель, инструкция и вход): все модели второго
вызова получают одни и те же,
повторный запуск их не оплачивает; ответ без карточки в файл не идёт. Технический сбой — одна повторная попытка, обе
оплачены; расход считается по этапам.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from oiltech_digest import config, signal_card, signal_scores, signal_themes
from oiltech_digest.otbor_pack_format import load_rubrics
from oiltech_digest.processing.pipeline import _article_prompt, make_client, summarize_article
from oiltech_digest.processing.prompts import SUMMARY_INSTRUCTIONS
from oiltech_digest.signal_card_pack import load_dictionaries as card_dictionaries

SUMMARIES_NAME = "card_scores_exam_summaries.json"
CARDS_NAME = "card_scores_exam_cards.json"
NO_CARD = "основной вызов не дал карточки"


class ExamError(ValueError):
    pass


class Inputs:
    """Вход вызовов и проверка ответов кодом ядра — без вызовов модели: теги, критерии, справочники карточки."""

    def __init__(self, exam: dict[str, Any]):
        control = exam["scores_control"]
        self.folder, self.tags, self.criteria = exam["folder"], control["tags"], control["criteria"]
        self.themes = signal_themes.names(self.tags)
        self.dictionaries = card_dictionaries()

    def card_row(self, row: dict[str, Any], summary: str | None) -> dict[str, Any]:
        """Строка экзамена как строка ядра для основного вызова (signal_card_store.card_inputs)."""
        return {"signal_id": 0, "publication": row["id"], "title": row["title"], "text": row["text"],
                "published": row["published"], "received": row["received"], "publisher": row["publisher"],
                "publisher_type": row["publisher_type"], "summary": summary, "category": row["category"]}

    def card_values(self, row: dict[str, Any], card: dict[str, Any]) -> dict[str, Any] | None:
        """Значения карточки, как их записало бы ядро (signal_card.card_values); None — карточки нет."""
        publication = signal_card._publication(self.card_row(row, None))
        return signal_card.card_values(card, publication, self.dictionaries, self.themes)[0]

    def context(self, row: dict[str, Any], summary: str, card: dict[str, Any] | None) -> dict[str, Any] | None:
        """Сигнал во входе второго вызова: поля «Отбора» эксперта, дата события и зрелость основного вызова того же
        прогона. Карточки нет — баллов нет, как на ядре."""
        values = self.card_values(row, card) if card else None
        return None if values is None else self.context_from(row, summary, values)

    def context_from(self, row: dict[str, Any], summary: str, values: dict[str, Any]) -> dict[str, Any]:
        """Вход второго вызова кодом ядра (signal_scores.context) по значениям карточки."""
        return signal_scores.context({
            "signal_id": 0, "publication": row["id"], "signal_category": row["category"], "title": row["title"],
            "text": row["text"], "publisher": row["publisher"], "summary": summary, "published": row["published"],
            "received": row["received"], "industry": row["industry"], "oilfield_relevance": row["oilfield_relevance"],
            "oilfield_application": row["oilfield_application"], "event_date": values["event_date"],
            "event_date_source": values["event_date_source"], "event_date_precision": values["event_date_precision"],
            "technology_maturity": values["technology_maturity"],
        })

    def scores_values(self, row: dict[str, Any], summary: str, card: dict[str, Any],
                      answer: dict[str, Any]) -> dict[str, Any] | None:
        """Баллы сигнала, как их записало бы ядро (signal_scores.score_values); None — карточки или баллов нет."""
        item = self.context(row, summary, card)
        return signal_scores.score_values(answer, item, self.criteria[row["profile"]])[0] if item else None


def summary_input(row: dict[str, Any]) -> dict[str, Any]:
    """Публикация во входе «Сути и перевода» — как у сигнала после «Отбора» (signal_summary._item): поля «Отбора»
    кодами."""
    return {"title": (row["title"] or "")[:500], "source_name": row["publisher"] or "", "url": row["url"] or "",
            "language": row["language"] or "", "published_at": row["published"] or "", "summary": None,
            "raw_text": row["text"] or "", "signal_category": row["category"],
            "oilfield_relevance": row["oilfield_relevance"], "oilfield_application": row["oilfield_application"]}


class Calls:
    """Вызовы трёх этапов с учётом расхода (spend: этап → вызовы, токены, цена)."""

    def __init__(self, inputs: Inputs, card_model: str, summary_model: str, workers: int):
        if not card_model:
            raise ExamError("нет модели основного вызова: --card-model (на проде SIGNAL_CARD_MODEL)")
        self.inputs, self.card_model, self.summary_model, self.workers = inputs, card_model, summary_model, workers
        self.spend: dict[str, dict[str, Any]] = {}

    # --- «Суть и перевод» ---

    def summaries(self, rows: list[dict[str, Any]]) -> dict[str, str]:
        """Суть — один раз на строку; сбой — одна повторная попытка, без сути экзамен не идёт (вход был бы не тот)."""
        path = self.inputs.folder / SUMMARIES_NAME
        stored = _read(path)
        fingerprints = self._summary_fingerprints(rows)
        for _ in range(2):
            todo = _missing(rows, stored, fingerprints)
            for row, answer in zip(todo, self._each(todo, self._summary)):
                self._count("summary", answer.get("model"), answer.get("input_tokens"), answer.get("output_tokens"))
                if answer.get("summary"):
                    stored[row["id"]] = {**answer, "input": fingerprints[row["id"]]}
        _write(path, stored)
        lost = _missing(rows, stored, fingerprints)
        if lost:
            raise ExamError(f"суть не получена у {len(lost)} строк: готовые сохранены в {SUMMARIES_NAME}, повторите "
                            "запуск")
        return {row["id"]: stored[row["id"]]["summary"] for row in rows}

    def summary_todo(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Строки без сути на этот вход в файле рядом с манифестом."""
        return _missing(rows, _read(self.inputs.folder / SUMMARIES_NAME), self._summary_fingerprints(rows))

    def cached_summaries(self) -> dict[str, str]:
        """Суть, которая уже лежит рядом с манифестом (для оценки цены до вызовов)."""
        return {number: one.get("summary") or "" for number, one in _read(self.inputs.folder / SUMMARIES_NAME).items()}

    def summary_chars(self, rows: list[dict[str, Any]]) -> int:
        """Длина запросов сути в знаках (оценка: инструкция, заголовок и текст)."""
        return sum(len(SUMMARY_INSTRUCTIONS) + len(row["title"] or "") + len(row["text"] or "") for row in rows)

    def _summary_fingerprints(self, rows: list[dict[str, Any]]) -> dict[str, str]:
        """Отпечаток запроса сути: модель, инструкция и вход (pipeline._article_prompt, как у ядра)."""
        return {row["id"]: _fingerprint(self.summary_model, SUMMARY_INSTRUCTIONS,
                                        _article_prompt(summary_input(row), otbor=True)) for row in rows}

    def _summary(self, row: dict[str, Any]) -> dict[str, Any]:
        try:
            response = summarize_article(summary_input(row), _WithModel(make_client(), self.summary_model))
        except Exception as exc:  # noqa: BLE001 - сбой одной строки не роняет остальные
            return {"error": str(exc)[:300]}
        return {"summary": str(response.data.get("summary") or "").strip(), "model": response.model,
                "input_tokens": response.input_tokens, "output_tokens": response.output_tokens}

    # --- Основной вызов ---

    def cards(self, rows: list[dict[str, Any]], summaries: dict[str, str], runs: int) -> dict[str, list[dict]]:
        """Ответы основного вызова по прогонам; сбой — одна повторная попытка. В файл идёт только ответ с карточкой."""
        path = self.inputs.folder / CARDS_NAME
        stored = _read(path)
        by_run = stored.setdefault(self.card_model, {})
        signals, fingerprints, _ = self._card_requests(rows, summaries)
        payload = {"model": self.card_model, "reasoning": config.SIGNAL_CARD_REASONING,
                   "dictionaries": self.inputs.dictionaries, "themes": self.inputs.themes}
        found: dict[str, list[dict[str, Any]]] = {row["id"]: [] for row in rows}
        for run in range(1, runs + 1):
            answers = by_run.setdefault(str(run), {})
            for attempt in (1, 2):
                todo = _missing(rows, answers, fingerprints)
                done = self._chunks(todo, lambda chunk: signal_card.process_payload(
                    {**payload, "signals": [signals[row["id"]] for row in chunk]})["cards"])
                for row, card in zip(todo, done):
                    self._count("card", card.get("model"), card.get("input_tokens"), card.get("output_tokens"))
                    if self.inputs.card_values(row, card) is not None:
                        answers[row["id"]] = {**card, "input": fingerprints[row["id"]], "attempts": attempt}
            lost = {row["id"] for row in _missing(rows, answers, fingerprints)}
            for row in rows:
                found[row["id"]].append({"error": NO_CARD} if row["id"] in lost else answers[row["id"]])
            print(f"{self.card_model}: основной вызов, прогон {run}/{runs}: строк {len(rows)}, без карточки "
                  f"{len(lost)}", flush=True)
        _write(path, stored)
        return found

    def card_todo(self, rows: list[dict[str, Any]], summaries: dict[str, str], runs: int) -> tuple[int, int]:
        """Сколько вызовов основного вызова нужно (без ответов в файле рядом с манифестом) и их длина в знаках."""
        by_run = _read(self.inputs.folder / CARDS_NAME).get(self.card_model) or {}
        _, fingerprints, chars = self._card_requests(rows, summaries)
        todo = [row for run in range(1, runs + 1) for row in _missing(rows, by_run.get(str(run)) or {}, fingerprints)]
        return len(todo), sum(chars[row["id"]] for row in todo)

    def _card_requests(self, rows: list[dict[str, Any]],
                       summaries: dict[str, str]) -> tuple[dict[str, dict[str, Any]], dict[str, str], dict[str, int]]:
        """Вход основного вызова по строкам — кодом ядра (signal_card.payload_signal), его отпечаток и длина."""
        signals = {row["id"]: signal_card.payload_signal(self.inputs.card_row(row, summaries.get(row["id"])),
                                                         self.inputs.tags) for row in rows}
        requests = {number: signal_card.request(item, self.inputs.dictionaries, self.inputs.themes)
                    for number, item in signals.items()}
        fingerprints = {number: _fingerprint(self.card_model, config.SIGNAL_CARD_REASONING, request)
                        for number, request in requests.items()}
        return signals, fingerprints, {number: sum(map(len, request)) for number, request in requests.items()}

    # --- Второй вызов ---

    def scores(self, model: str, rows: list[dict[str, Any]], summaries: dict[str, str],
               cards: dict[str, list[dict[str, Any]]], runs: int) -> list[dict[str, Any]]:
        """Ответы второго вызова: строка на (прогон, строку). Без карточки основного вызова того же прогона баллов нет;
        технический сбой — одна повторная попытка, обе оплачены."""
        payload = {"model": model, "reasoning": config.SIGNAL_SCORES_REASONING, **self._profiles()}
        answers: list[dict[str, Any]] = []
        for run in range(1, runs + 1):
            contexts = {row["id"]: self.inputs.context(row, summaries[row["id"]], cards[row["id"]][run - 1])
                        for row in rows}
            items = {number: item for number, item in contexts.items() if item is not None}
            found: dict[str, dict[str, Any]] = {}
            for attempt in (1, 2):
                todo = [row for row in rows if row["id"] in items
                        and (row["id"] not in found or found[row["id"]].get("error"))]
                done = self._chunks(todo, lambda chunk: signal_scores.process_payload(
                    {**payload, "items": [signal_scores.payload_item(items[row["id"]]) for row in chunk]})["scores"])
                for row, answer in zip(todo, done):
                    paid = found.get(row["id"]) or {}
                    found[row["id"]] = {**answer, "attempts": attempt,
                                        **{key: int(paid.get(key) or 0) + int(answer.get(key) or 0)
                                           for key in ("input_tokens", "output_tokens")}}
            for row in rows:
                answer = found.get(row["id"]) or {"error": NO_CARD, "attempts": 0}
                answers.append({"run": run, "id": row["id"], "answer": answer.get("answer"),
                                "error": answer.get("error"), "model": answer.get("model"),
                                "attempts": answer["attempts"], "input_tokens": int(answer.get("input_tokens") or 0),
                                "output_tokens": int(answer.get("output_tokens") or 0)})
            print(f"{model}: прогон {run}/{runs}: строк {len(rows)}, без баллов "
                  f"{sum(bool(one['error']) for one in answers if one['run'] == run)}", flush=True)
        return answers

    def scores_chars(self, rows: list[dict[str, Any]], summaries: dict[str, str]) -> int:
        """Длина запросов второго вызова на прогон в знаках; дату и зрелость даст основной вызов — без них."""
        profiles = self._profiles()["profiles"]
        blank = {"event_date": None, "event_date_source": None, "event_date_precision": None,
                 "technology_maturity": None}
        return sum(len(signal_scores.instructions(profiles[row["profile"]])) + len(signal_scores.model_input(
            signal_scores.payload_item(self.inputs.context_from(row, summaries.get(row["id"]) or "", blank))))
            for row in rows)

    def _profiles(self) -> dict[str, Any]:
        rubrics = load_rubrics()
        return {"rubrics_version": rubrics["version"],
                "profiles": {profile: signal_scores.model_criteria(criteria, rubrics, profile)
                             for profile, criteria in self.inputs.criteria.items()}}

    # --- Общее ---

    def _chunks(self, rows: list[dict[str, Any]], call: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]):
        """Ответы воркера по порядку строк: с workers > 1 строки делятся на куски, куски идут параллельно — у каждого
        свой вызов воркера и свой клиент модели."""
        if not rows:
            return []
        if self.workers <= 1 or len(rows) <= 1:
            return call(rows)
        size = -(-len(rows) // self.workers)
        chunks = [rows[start:start + size] for start in range(0, len(rows), size)]
        with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
            return [answer for part in pool.map(call, chunks) for answer in part]

    def _each(self, rows: list[dict[str, Any]], call: Callable[[dict[str, Any]], dict[str, Any]]):
        if self.workers <= 1 or len(rows) <= 1:
            return [call(row) for row in rows]
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return list(pool.map(call, rows))

    def _count(self, stage: str, model: str | None, tokens_in: Any, tokens_out: Any) -> None:
        """Расход этапа: только в основном потоке (после ответов кусков)."""
        tokens_in, tokens_out = int(tokens_in or 0), int(tokens_out or 0)
        if not tokens_in + tokens_out:
            return
        price_in, price_out = config.price_for_model(model)
        spent = self.spend.setdefault(stage, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
        spent["calls"] += 1
        spent["input_tokens"] += tokens_in
        spent["output_tokens"] += tokens_out
        spent["cost_usd"] = round(spent["cost_usd"] + (tokens_in * price_in + tokens_out * price_out) / 1_000_000, 6)


class _WithModel:
    """Клиент с заданной моделью экзамена: она подменяет ту, что передаёт «Суть и перевод» (OPENAI_SUMMARY_MODEL)."""

    def __init__(self, client: Any, model: str):
        self.client, self.model = client, model

    def complete_json(self, *args: Any, **kwargs: Any) -> Any:
        return self.client.complete_json(*args, **{**kwargs, "model": self.model})


def _missing(rows: list[dict[str, Any]], stored: dict[str, Any],
             fingerprints: dict[str, str]) -> list[dict[str, Any]]:
    """Строки, у которых в файле нет ответа на этот вход."""
    return [row for row in rows if (stored.get(row["id"]) or {}).get("input") != fingerprints[row["id"]]]


def _fingerprint(*parts: Any) -> str:
    """Отпечаток входа вызова: другой вход — другой ответ, файл рядом с манифестом его не отдаёт."""
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ExamError(f"{path.name} испорчен: нужен объект")
    return data


def _write(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
