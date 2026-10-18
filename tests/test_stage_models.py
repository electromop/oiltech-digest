"""Свои модели стадий, «Дедуп» без отката на модель ленты и совместимость с gpt-6 (переезд с gpt-5 до 11.12.2026).

Уровни рассуждения — страницы моделей OpenAI, сверено 18.10.2026 (gpt-6-sol и gpt-6.1-terra — 03.11.2026): gpt-6.1-sol и
gpt-6-astra принимают low…max (ни none, ни minimal); gpt-6-luna, gpt-6-sol, gpt-6.1-terra и gpt-5.6-sol/-terra/-luna —
none…max (без minimal). Наш код слал gpt-6 «minimal» как есть — каждый вызов без строки уровня получил бы 400.
Суть и перевод (#150) проверяет test_processing.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from oiltech_digest import config, signal_dedup, signal_discovery
from oiltech_digest.db import connection, repository
from oiltech_digest.processing import external_ai, openai_client, pipeline, reprints
from oiltech_digest.processing.openai_client import (
    AIResponse,
    OpenAIResponsesClient,
    _reasoning_effort,
    output_budget,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("model, configured, expected", [
    # GPT-6.1 Sol и Astra: ни none, ни minimal — самый лёгкий уровень у них low.
    ("gpt-6.1-sol", "minimal", "low"),
    ("gpt-6.1-sol", "none", "low"),
    ("gpt-6.1-sol", "", "low"),
    ("gpt-6.1-sol", "medium", "medium"),
    ("gpt-6.1-sol", "max", "max"),
    ("gpt-6-astra", "minimal", "low"),
    # GPT-6 Luna, Sol, 6.1 Terra и GPT-5.6: none есть, minimal нет.
    ("gpt-6-luna", "minimal", "none"),
    ("gpt-6-luna", "", "none"),
    ("gpt-6-luna", "high", "high"),
    ("gpt-6-sol", "minimal", "none"),
    ("gpt-6-sol", "low", "low"),
    ("gpt-6.1-terra", "minimal", "none"),
    ("gpt-6.1-terra", "low", "low"),
    ("gpt-5.6-sol", "minimal", "none"),
    ("gpt-5.6-terra", "", "none"),
    ("gpt-5.6-luna", "xhigh", "xhigh"),
    # Имя с датой — по началу имени.
    ("gpt-6-luna-2026-12-01", "minimal", "none"),
    # Незнакомая модель семейства gpt-6 — уровень, который принимают все известные gpt-6: low.
    ("gpt-6.2-nova", "minimal", "low"),
    # Прежние поколения — как до правки.
    ("gpt-5-mini", "none", "minimal"),
    ("gpt-5-2025-08-07", "", "minimal"),
    ("gpt-5.4-mini", "minimal", "none"),
])
def test_reasoning_effort_for_new_models(model, configured, expected):
    assert _reasoning_effort(model, configured) == expected


def test_output_budget_knows_max_level():
    assert output_budget(100, "max") > output_budget(100, "xhigh")


class _Response:
    status_code = 200

    def __init__(self, text: str) -> None:
        self._text = text

    def json(self) -> dict:
        return {
            "model": "m",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": self._text}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }


class _Post:
    def __init__(self, text: str = '{"ok": true}') -> None:
        self.payloads: list[dict] = []
        self.text = text

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.payloads.append(json)
        return _Response(self.text)


SCHEMA = {"name": "s", "schema": {"type": "object"}}


def test_client_raises_budget_when_model_cannot_do_requested_level(monkeypatch):
    """Стадия просила minimal и лимит без запаса, а gpt-6.1-sol minimal не умеет — уходит low, и лимит растёт на запас low.
    Иначе рассуждение съело бы весь ответ («ответ без текста», как 28.09)."""
    post = _Post()
    monkeypatch.setattr(openai_client.requests, "post", post)
    client = OpenAIResponsesClient(api_key="k", model="gpt-6.1-sol")

    client.complete_json("i", "u", SCHEMA, max_output_tokens=700, model="gpt-6.1-sol", reasoning_effort="minimal")

    payload = post.payloads[-1]
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["max_output_tokens"] == output_budget(700, "low")


def test_client_keeps_budget_when_level_is_supported(monkeypatch):
    post = _Post()
    monkeypatch.setattr(openai_client.requests, "post", post)
    client = OpenAIResponsesClient(api_key="k", model="gpt-6-luna")

    client.complete_json("i", "u", SCHEMA, max_output_tokens=700, model="gpt-6-luna", reasoning_effort="minimal")
    client.complete_json("i", "u", SCHEMA, max_output_tokens=2700, model="gpt-6-luna", reasoning_effort="low")

    assert [p["reasoning"] for p in post.payloads] == [{"effort": "none"}, {"effort": "low"}]
    assert [p["max_output_tokens"] for p in post.payloads] == [700, 2700]


def test_research_call_raises_budget_too(monkeypatch):
    post = _Post('{"events": []}')
    monkeypatch.setattr(openai_client.requests, "post", post)
    client = OpenAIResponsesClient(api_key="k", model="gpt-6.1-sol")

    client.research_json("i", "u", SCHEMA, model="gpt-6.1-sol", reasoning_effort="none", max_output_tokens=4500,
                         timeout=10)

    payload = post.payloads[-1]
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["max_output_tokens"] == output_budget(4500, "low")


@pytest.mark.parametrize("model, price", [
    ("gpt-6.1-sol", (2.0, 10.0)),
    ("gpt-6.1-terra", (1.0, 5.0)),
    ("gpt-6-sol", (2.0, 10.0)),
    ("gpt-6-luna", (0.10, 0.50)),
    ("gpt-6-astra", (10.0, 50.0)),
    # Без своей строки gpt-5.6-* попали бы под префикс gpt-5 — по цене 1,25 / 10.
    ("gpt-5.6-sol", (4.0, 20.0)),
    ("gpt-5.6-terra", (2.0, 12.0)),
    ("gpt-5.6-luna", (0.20, 1.20)),
    ("gpt-5-2025-08-07", (1.25, 10.0)),
    ("gpt-5-mini-2025-08-07", (0.25, 2.0)),
])
def test_prices_of_new_models(model, price):
    assert config.price_for_model(model) == price


class _RecordingClient:
    model = "fake"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None, reasoning_effort=None):
        self.calls.append({"name": schema["name"], "model": model, "reasoning": reasoning_effort,
                           "max_output_tokens": max_output_tokens})
        data = {
            "article_tag": {"tag_id": 1, "confidence": 0.9, "rationale": "бурение"},
            "article_reprint_verdict": {"same_event": False, "primary_id": 1, "reason": "разные события"},
        }[schema["name"]]
        return AIResponse(data=data, model=model or self.model, input_tokens=1500, output_tokens=60)


ARTICLE = {"id": 1, "source_name": "A", "published_at": "2026-10-01", "title": "New rig", "raw_text": "x"}


def test_tags_use_their_own_model_reasoning_and_budget(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_TAG_MODEL", "tag-model")
    monkeypatch.setattr(config, "OPENAI_TAG_REASONING", "medium")
    client = _RecordingClient()

    pipeline.tag_article({"title": "Drilling", "raw_text": "Новый буровой станок."}, [{"id": 1, "name": "Бурение"}], client)

    (call,) = client.calls
    assert (call["model"], call["reasoning"]) == ("tag-model", "medium")
    assert call["max_output_tokens"] == output_budget(1000, "medium")


def test_reprint_judge_uses_its_own_model_reasoning_and_budget(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_REPRINT_MODEL", "reprint-model")
    monkeypatch.setattr(config, "OPENAI_REPRINT_REASONING", "low")
    client = _RecordingClient()

    reprints.judge_pair(ARTICLE, {**ARTICLE, "id": 2, "source_name": "B"}, client)

    (call,) = client.calls
    assert (call["model"], call["reasoning"]) == ("reprint-model", "low")
    assert call["max_output_tokens"] == output_budget(700, "low")


def test_minimal_keeps_todays_limits(monkeypatch):
    """На нынешнем уровне (minimal) лимиты ответа прежние: правка не меняет поведение прода до смены моделей."""
    for name in ("OPENAI_TAG_REASONING", "OPENAI_REPRINT_REASONING"):
        monkeypatch.setattr(config, name, "minimal")
    client = _RecordingClient()

    pipeline.tag_article(ARTICLE, [{"id": 1, "name": "Бурение"}], client)
    reprints.judge_pair(ARTICLE, {**ARTICLE, "id": 2, "source_name": "B"}, client)

    assert [c["max_output_tokens"] for c in client.calls] == [1000, 700]


def test_tags_fall_back_but_dedup_does_not():
    """Теги без строки в .env — на OPENAI_MODEL, как раньше. У «Дедупа» отката нет (спека #128, история 48): пусто — пусто,
    и стадия падает с причиной. Второе мнение о теме (прежняя цепочка радара) без своей строки и без модели дедупа —
    на модели ленты, как до правки."""
    # Пустые, а не удалённые: config при импорте читает .env, и строка оттуда подменила бы отсутствующую переменную.
    env = {**os.environ, "OPENAI_MODEL": "main-model", "OPENAI_REASONING_EFFORT": "low", "OPENAI_TAG_MODEL": "",
           "OPENAI_TAG_REASONING": "", "OPENAI_REPRINT_MODEL": "", "OPENAI_REPRINT_REASONING": "",
           "SIGNAL_DEDUP_MODEL": "", "SIGNAL_DEDUP_REASONING": "", "SIGNAL_THEME_CHECK_MODEL": ""}
    probe = ("from oiltech_digest import config as c; "
             "print(repr((c.OPENAI_TAG_MODEL, c.OPENAI_TAG_REASONING, c.OPENAI_REPRINT_MODEL, c.OPENAI_REPRINT_REASONING, "
             "c.SIGNAL_DEDUP_MODEL, c.SIGNAL_DEDUP_REASONING, c.SIGNAL_THEME_CHECK_MODEL)))")

    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60,
                            cwd=REPO_ROOT, env=env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == repr(("main-model", "low", "", "low", "", "low", "main-model"))


# --- «Дедуп» без модели: сбой с причиной до вызовов, а не «разные события» у каждой пары --------------------------


def test_reprint_review_without_model_fails_before_any_call(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_REPRINT_MODEL", "")
    client = _RecordingClient()
    monkeypatch.setattr(external_ai, "make_client", lambda *args, **kwargs: client)
    payload = {"pairs": [{"overlap": 0.5, "a": ARTICLE, "b": {**ARTICLE, "id": 2}}]}

    with pytest.raises(reprints.ReprintSetupError, match="OPENAI_REPRINT_MODEL"):
        external_ai.process_reprint_review_payload(payload)
    with pytest.raises(reprints.ReprintSetupError):
        reprints.review_candidates([{"a_id": 1, "b_id": 2, "overlap": 0.5}], client)

    assert client.calls == []


def _pair_nodes() -> list[dict]:
    return [
        {"kind": "new", "signal": {"title_ru": "SLB купила стартап бурения", "companies": ["SLB"]},
         "urls": ["https://a/1"], "fresh": True, "reviewed": False},
        {"kind": "new", "signal": {"title_ru": "SLB стартап бурения купила", "companies": ["SLB"]},
         "urls": ["https://b/2"], "fresh": True, "reviewed": False},
    ]


def test_dedupe_without_model_raises_instead_of_marking_pairs_distinct(monkeypatch):
    monkeypatch.setattr(config, "SIGNAL_DEDUP_MODEL", "")
    factory_calls: list[int] = []

    with pytest.raises(signal_dedup.DedupSetupError, match="SIGNAL_DEDUP_MODEL"):
        signal_dedup.dedupe(_pair_nodes(), client_factory=lambda: factory_calls.append(1))

    assert factory_calls == []


def test_dedupe_without_pairs_needs_no_model(monkeypatch):
    monkeypatch.setattr(config, "SIGNAL_DEDUP_MODEL", "")

    result = signal_dedup.dedupe(_pair_nodes()[:1], client_factory=lambda: pytest.fail("клиент не нужен"))

    assert result["stats"]["judged"] == 0


def test_radar_run_without_dedup_model_fails_before_search(monkeypatch):
    monkeypatch.setattr(config, "SIGNAL_DEDUP_MODEL", "")
    searched: list[str] = []
    monkeypatch.setattr(signal_discovery, "_topic_web_evidence", lambda *args, **kwargs: searched.append("x"))
    run_config = signal_discovery.SignalDiscoveryConfig(offline=False, dry_run=True, web_only=True)

    with pytest.raises(signal_dedup.DedupSetupError):
        signal_discovery.run_discovery(run_config, {"topics": [{"name": "Бурение"}], "tags": []})

    assert searched == []


# --- Расход судьи перепечаток — в ai_processing_runs -------------------------------------------------------------


def test_worker_verdict_carries_tokens(monkeypatch):
    client = _RecordingClient()
    monkeypatch.setattr(external_ai, "make_client", lambda *args, **kwargs: client)

    result = external_ai.process_reprint_review_payload(
        {"pairs": [{"overlap": 0.5, "a": ARTICLE, "b": {**ARTICLE, "id": 2, "source_name": "B"}}], "dry_run": True})

    (verdict,) = result["verdicts"]
    assert (verdict["model"], verdict["input_tokens"], verdict["output_tokens"]) == ("test-reprint-model", 1500, 60)


def _verdict(a_id: int, b_id: int, *, model: str = "gpt-6-luna", tokens: tuple[int, int] = (1_000_000, 100_000),
             same: bool = False) -> dict:
    return {"a_id": a_id, "b_id": b_id, "same_event": same, "model": model, "input_tokens": tokens[0],
            "output_tokens": tokens[1], "reason": "разные", "a_len": 10, "b_len": 20, "overlap": 0.4}


def _reprint_runs(job_id: int) -> list[tuple]:
    with connection.get_connection() as conn:
        return conn.execute(
            "SELECT stage, model, input_tokens, output_tokens, cost_usd, error_message, article_id "
            "FROM ai_processing_runs WHERE job_id = %s ORDER BY id", (job_id,),
        ).fetchall()


def test_reprint_usage_is_recorded_for_partial_and_rest_and_dry_run(isolated_db):
    # Частичный итог (воркер остановлен) и итог остатка той же задачи судят разные пары — строки складываются.
    external_ai.apply_reprint_review_result({"reprint_review": True, "verdicts": [_verdict(1, 2)]}, job_id=41)
    external_ai.apply_reprint_review_result({"reprint_review": True, "verdicts": [_verdict(3, 4), _verdict(5, 6)]},
                                            job_id=41)
    # Сухой прогон пометок не пишет, а вызовы оплачены и в нём.
    applied = external_ai.apply_reprint_review_result(
        {"reprint_review": True, "dry_run": True, "verdicts": [_verdict(7, 8, model="gpt-5-mini-2025-08-07")]},
        job_id=42)

    rows = _reprint_runs(41)
    # 1 млн входных × $0,10 + 100 тыс. выходных × $0,50 = $0,15 за пару у gpt-6-luna.
    assert [(r[0], r[1], r[2], float(r[4]), r[5], r[6]) for r in rows] == [
        ("reprint_review", "gpt-6-luna", 1_000_000, 0.15, "pairs=1", None),
        ("reprint_review", "gpt-6-luna", 2_000_000, 0.3, "pairs=2", None),
    ]
    assert applied == {"applied": 0, "dry_run": True}
    assert [(r[1], float(r[4])) for r in _reprint_runs(42)] == [("gpt-5-mini-2025-08-07", 0.45)]


def test_reprint_usage_skips_errors_and_old_build_verdicts(isolated_db):
    old_build = {key: value for key, value in _verdict(1, 2).items() if key not in {"input_tokens", "output_tokens"}}
    broken = {**_verdict(5, 6), "input_tokens": "много"}

    assert repository.record_reprint_usage(43, [old_build, {"a_id": 3, "b_id": 4, "error": "timeout"}, broken]) == 0
    assert _reprint_runs(43) == []
