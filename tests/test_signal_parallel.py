"""Параллельный судья пар «Дедупа» радара: быстрее, а итог тот же, что по одному."""

from __future__ import annotations

import threading
import time

import pytest

from oiltech_digest import config, signal_dedup
from oiltech_digest.processing.openai_client import AIResponse


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
