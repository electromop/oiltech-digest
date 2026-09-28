"""Один сбой судьи не роняет прогон радара (28.09: ReadTimeout на судье — 0 сигналов)."""

import pytest
import requests

from oiltech_digest import signal_discovery
from oiltech_digest.processing.openai_client import AIClientError


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    monkeypatch.setattr(signal_discovery, "JUDGE_RETRY_PAUSE_SECONDS", 0)


def _web(monkeypatch, urls):
    evidence = [{"source_url": url, "title": f"Drilling contract {i}", "extracted_fact": f"Fact {i}"}
                for i, url in enumerate(urls)]
    monkeypatch.setattr(
        signal_discovery,
        "_search_web_evidence",
        lambda topic, config, **kwargs: {"status": "ok", "queries": ["q"], "results": len(evidence), "evidence": evidence},
    )
    # Каждая статья — свой кластер: так видно, что пропадает один, а не все.
    monkeypatch.setattr(signal_discovery, "_cluster_evidence", lambda evidence, topic: [[item] for item in evidence])


def _run():
    config = signal_discovery.SignalDiscoveryConfig(offline=True, dry_run=True, web_only=True)
    return signal_discovery.run_discovery(config, {"topics": [{"name": "Бурение"}], "tags": []})


def _signal(cluster):
    return {"title": cluster[0]["title"], "theme": "Бурение", "score": 60, "maturity": "watch"}


def test_judge_timeout_is_retried(monkeypatch):
    _web(monkeypatch, ["https://example.com/a"])
    calls = []

    def flaky(cluster, topic, offline=True):
        calls.append(1)
        if len(calls) == 1:
            raise requests.exceptions.ReadTimeout("Read timed out. (read timeout=60)")
        return _signal(cluster), {}

    monkeypatch.setattr(signal_discovery, "judge_signal_snapshot", flaky)

    topic = _run()["topics"][0]

    assert len(calls) == 2
    assert len(topic["candidates"]) == 1
    assert topic["judge_errors"] == []


def test_cluster_is_skipped_after_retries_and_the_run_goes_on(monkeypatch):
    _web(monkeypatch, ["https://example.com/a", "https://example.com/b"])

    def judge(cluster, topic, offline=True):
        if cluster[0]["source_url"].endswith("/a"):
            raise requests.exceptions.ConnectionError("Failed to resolve 'api.openai.com'")
        return _signal(cluster), {}

    monkeypatch.setattr(signal_discovery, "judge_signal_snapshot", judge)

    topic = _run()["topics"][0]

    assert [c["signal"]["evidence"][0]["source_url"] for c in topic["candidates"]] == ["https://example.com/b"]
    assert len(topic["judge_errors"]) == 1
    assert topic["judge_errors"][0].startswith("ConnectionError:")


def test_rate_limit_and_server_errors_are_transient(monkeypatch):
    _web(monkeypatch, ["https://example.com/a"])
    errors = iter([AIClientError("OpenAI API error 429: rate limit"), AIClientError("OpenAI API error 503: busy")])

    def judge(cluster, topic, offline=True):
        error = next(errors, None)
        if error:
            raise error
        return _signal(cluster), {}

    monkeypatch.setattr(signal_discovery, "judge_signal_snapshot", judge)

    assert len(_run()["topics"][0]["candidates"]) == 1


def test_permanent_api_error_is_not_swallowed(monkeypatch):
    # 403 с РФ-адреса или неверный ключ — не повод тихо пропустить все кластеры.
    _web(monkeypatch, ["https://example.com/a"])
    calls = []

    def judge(cluster, topic, offline=True):
        calls.append(1)
        raise AIClientError("OpenAI API error 403: unsupported_country_region_territory")

    monkeypatch.setattr(signal_discovery, "judge_signal_snapshot", judge)

    with pytest.raises(AIClientError):
        _run()
    assert len(calls) == 1
