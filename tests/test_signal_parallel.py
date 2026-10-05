"""Параллельный судья и дедуп радара: быстрее, а итог — тот же, что по одному."""

from __future__ import annotations

import threading
import time

import pytest

from oiltech_digest import config, signal_dedup, signal_discovery
from oiltech_digest.processing.openai_client import AIClientError, AIResponse


class _Concurrency:
    def __init__(self):
        self.lock = threading.Lock()
        self.now = 0
        self.peak = 0

    def __enter__(self):
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)

    def __exit__(self, *exc):
        with self.lock:
            self.now -= 1


def _clusters(n):
    return [[{"source_url": f"https://example.com/{i}", "title": f"Событие {i}"}] for i in range(n)]


def _judging(monkeypatch, *, fail=(), delay=0.05):
    gauge = _Concurrency()

    def judge(cluster, topic, offline=True):
        with gauge:
            time.sleep(delay)
            index = int(cluster[0]["source_url"].rsplit("/", 1)[1])
            if index in fail:
                raise AIClientError(f"OpenAI API error 503: cluster {index}")
            signal_discovery._record_usage("radar_judge", "gpt-5", input_tokens=100, output_tokens=10)
            return {"title": f"Событие {index}"}, {}

    monkeypatch.setattr(signal_discovery, "judge_signal_snapshot", judge)
    monkeypatch.setattr(signal_discovery, "JUDGE_RETRY_PAUSE_SECONDS", 0)
    return gauge


def test_judge_runs_clusters_in_parallel_and_keeps_their_order(monkeypatch):
    monkeypatch.setattr(config, "SIGNAL_JUDGE_CONCURRENCY", 4)
    gauge = _judging(monkeypatch)
    beats = []

    results = signal_discovery._judge_clusters(_clusters(8), "Бурение", offline=False,
                                               beat=lambda: beats.append(1), errors=[])

    assert gauge.peak >= 2
    assert [signal["title"] for _, (signal, _raw) in results] == [f"Событие {i}" for i in range(8)]
    assert beats  # аренду продлевает основной поток, пока ждёт


def test_failed_clusters_and_errors_keep_cluster_order(monkeypatch):
    monkeypatch.setattr(config, "SIGNAL_JUDGE_CONCURRENCY", 4)
    _judging(monkeypatch, fail={1, 5}, delay=0.01)
    errors: list[str] = []

    results = signal_discovery._judge_clusters(_clusters(6), "Бурение", offline=False, beat=lambda: None, errors=errors)

    assert [index for index, (_, judged) in enumerate(results) if judged is None] == [1, 5]
    assert [error.split("cluster ")[1] for error in errors] == ["1", "5"]


def test_stop_while_waiting_does_not_pay_for_judges_not_yet_started(monkeypatch):
    # Остановка или потеря аренды посреди темы (beat бросает): очередь судей снимается, а не
    # дорабатывает для уже отпущенного прогона. Начатые — не больше размера пула.
    monkeypatch.setattr(config, "SIGNAL_JUDGE_CONCURRENCY", 2)
    _judging(monkeypatch, delay=0.3)
    judge = signal_discovery.judge_signal_snapshot
    calls = []

    def counting(cluster, topic, offline=True):
        calls.append(cluster[0]["source_url"])
        return judge(cluster, topic, offline=offline)

    monkeypatch.setattr(signal_discovery, "judge_signal_snapshot", counting)
    beats = []

    def beat():
        beats.append(1)
        if len(beats) > 1:
            raise RuntimeError("остановка")

    with pytest.raises(RuntimeError):
        signal_discovery._judge_clusters(_clusters(10), "Бурение", offline=False, beat=beat, errors=[])

    assert len(calls) <= 4


def test_usage_from_parallel_judges_is_not_lost(monkeypatch):
    monkeypatch.setattr(config, "SIGNAL_JUDGE_CONCURRENCY", 6)
    _judging(monkeypatch, delay=0.005)
    token = signal_discovery._USAGE.set({})
    try:
        signal_discovery._judge_clusters(_clusters(30), "Бурение", offline=False, beat=lambda: None, errors=[])
        usage = list(signal_discovery._USAGE.get().values())
    finally:
        signal_discovery._USAGE.reset(token)

    assert usage == [{"stage": "radar_judge", "model": "gpt-5", "calls": 30, "input_tokens": 3000,
                      "output_tokens": 300, "web_search_calls": 0}]


def test_concurrency_one_is_the_old_sequential_judge(monkeypatch):
    monkeypatch.setattr(config, "SIGNAL_JUDGE_CONCURRENCY", 1)
    gauge = _judging(monkeypatch, delay=0.01)

    signal_discovery._judge_clusters(_clusters(4), "Бурение", offline=False, beat=lambda: None, errors=[])

    assert gauge.peak == 1


def test_parallel_judges_see_the_run_context(monkeypatch):
    # Снимок тегов и контекст судьи — в ContextVar: рабочие потоки получают их копию.
    monkeypatch.setattr(config, "SIGNAL_JUDGE_CONCURRENCY", 3)
    seen = []

    def judge(cluster, topic, offline=True):
        seen.append(tuple((signal_discovery._JUDGE_CONTEXT.get() or {}).get("themes") or []))
        return {"title": "t"}, {}

    monkeypatch.setattr(signal_discovery, "judge_signal_snapshot", judge)
    with signal_discovery.use_discovery_snapshot({"radar_themes": ["Бурение", "ГРП"], "radar_criteria": []}):
        signal_discovery._judge_clusters(_clusters(3), "Бурение", offline=False, beat=lambda: None, errors=[])

    assert seen == [("Бурение", "ГРП")] * 3


class _PairJudge:
    def __init__(self):
        self.gauge = _Concurrency()

    def complete_json(self, instructions, prompt, schema, **kwargs):
        with self.gauge:
            time.sleep(0.03)
        return AIResponse(data={"same_event": "SLB" in prompt and prompt.count("SLB") >= 2, "reason": "одно событие"},
                          model="m", input_tokens=10, output_tokens=2)


def _nodes():
    titles = ["SLB запустила MPD на Ямале", "SLB MPD на Ямале запущена", "Halliburton ГРП в Техасе",
              "Halliburton провела ГРП в Техасе", "Baker Hughes ESP в Омане"]
    return [{"kind": "new", "signal": {"title_ru": title, "companies": [title.split()[0]]}, "urls": [],
             "fresh": True, "reviewed": False} for title in titles]


@pytest.mark.parametrize("workers", [1, 4])
def test_dedup_gives_the_same_groups_sequential_or_parallel(monkeypatch, workers):
    monkeypatch.setattr(config, "SIGNAL_DEDUP_CONCURRENCY", workers)
    judge = _PairJudge()

    result = signal_dedup.dedupe(_nodes(), client_factory=lambda: judge)

    assert result["assigned"] == {1: (0, "одно событие")}
    assert result["stats"]["judged"] == result["stats"]["pairs"]
    if workers > 1:
        assert judge.gauge.peak >= 2
