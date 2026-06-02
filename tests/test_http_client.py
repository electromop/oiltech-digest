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


def test_proxy_for_returns_none_without_config(monkeypatch):
    monkeypatch.setattr(http_client, "PROXY_URL", "")
    monkeypatch.setattr(http_client, "PROXY_HOST_OVERRIDES", {})
    assert http_client._proxy_for("example.com") is None


def test_proxy_for_uses_global_url(monkeypatch):
    monkeypatch.setattr(http_client, "PROXY_URL", "http://u:p@proxy.local:8080")
    monkeypatch.setattr(http_client, "PROXY_HOST_OVERRIDES", {})
    expected = {"http": "http://u:p@proxy.local:8080", "https": "http://u:p@proxy.local:8080"}
    assert http_client._proxy_for("example.com") == expected


def test_proxy_for_host_override_wins(monkeypatch):
    monkeypatch.setattr(http_client, "PROXY_URL", "http://global:0@proxy.local:8080")
    monkeypatch.setattr(
        http_client, "PROXY_HOST_OVERRIDES", {"example.com": "http://ru:1@ru.proxy:8080"}
    )
    expected = {"http": "http://ru:1@ru.proxy:8080", "https": "http://ru:1@ru.proxy:8080"}
    assert http_client._proxy_for("www.example.com") == expected


def test_request_passes_proxies_to_session(monkeypatch):
    http_client._host_cooldown_until.clear()
    http_client._host_next_allowed.clear()
    monkeypatch.setattr(http_client, "PROXY_URL", "http://u:p@proxy.local:8080")
    monkeypatch.setattr(http_client, "PROXY_HOST_OVERRIDES", {})
    captured = {}

    class DummySession:
        def get(self, *args, **kwargs):
            captured.update(kwargs)
            return DummyResponse(status_code=200, content=b"ok")

    monkeypatch.setattr(http_client, "_get_session", lambda: DummySession())
    monkeypatch.setattr(http_client, "_wait_for_host_slot", lambda host: None)

    assert http_client.fetch("https://example.com/feed") == b"ok"
    assert captured["proxies"] == {
        "http": "http://u:p@proxy.local:8080",
        "https": "http://u:p@proxy.local:8080",
    }


def test_mask_proxy_hides_credentials():
    masked = http_client._mask_proxy("http://user:secret@proxy.local:8080")
    assert "secret" not in masked
    assert "user" not in masked
    assert "proxy.local:8080" in masked


def test_tally_counts_statuses():
    http_client._status_counts.clear()
    http_client._tally(200)
    http_client._tally(200)
    http_client._tally(403)
    assert http_client._status_counts == {"200": 2, "403": 1}
