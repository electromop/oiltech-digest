import requests

from oiltech_digest.ingestion import http_client


class DummyResponse:
    def __init__(self, status_code=200, content=b"ok", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status={self.status_code}", response=self)


def test_fetch_respects_host_cooldown(monkeypatch):
    http_client._host_cooldown_until.clear()
    http_client._host_next_allowed.clear()
    session_calls = {"n": 0}

    class DummySession:
        def get(self, *args, **kwargs):
            session_calls["n"] += 1
            return DummyResponse(status_code=429, headers={"Retry-After": "60"})

    monkeypatch.setattr(http_client, "_get_session", lambda: DummySession())
    monkeypatch.setattr(http_client, "_wait_for_host_slot", lambda host: None)
    monkeypatch.setattr(http_client.time, "sleep", lambda seconds: None)

    first = http_client.fetch("https://example.com/news")
    second = http_client.fetch("https://example.com/news")

    assert first is None
    assert second is None
    assert session_calls["n"] == 1


def test_fetch_returns_content_on_success(monkeypatch):
    http_client._host_cooldown_until.clear()
    http_client._host_next_allowed.clear()
    class DummySession:
        def get(self, *args, **kwargs):
            return DummyResponse(status_code=200, content=b"hello")

    monkeypatch.setattr(http_client, "_get_session", lambda: DummySession())
    monkeypatch.setattr(http_client, "_wait_for_host_slot", lambda host: None)

    assert http_client.fetch("https://example.com/feed") == b"hello"
