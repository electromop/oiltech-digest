"""Ворота тела запроса (16.11.2026, защита входа «по полному»).

FastAPI читает тело целиком раньше проверки сессии и токена, поэтому без входа любой POST принял бы тело любого
размера. Пределы по пути стоят в Caddy (Caddyfile) и вторым слоем в приложении (request_guard): Caddyfile смонтирован
одним файлом и после выката может остаться старым до caddy reload. У больших путей (загрузка документа, воркеры NL)
токен воркера или сессия проверяются до чтения тела. Здесь же сторожа конфигурации: пределы Caddyfile равны пределам
приложения, а compose доверяет X-Forwarded-For только от внутренних адресов (так приложение видит адрес клиента)."""

import hashlib
import re

import pytest
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from oiltech_digest import api, config, request_guard
from oiltech_digest.config import REPO_ROOT

TOKEN = "worker-secret"


async def _echo(scope, receive, send):
    """Внутреннее приложение: читает тело целиком и отвечает, сколько байт получило."""
    size = 0
    while True:
        message = await receive()
        size += len(message.get("body", b""))
        if not message.get("more_body"):
            break
    await JSONResponse({"bytes": size})(scope, receive, send)


def _client():
    return TestClient(request_guard.RequestGuard(_echo))


class _Body:
    """Тело кусками без Content-Length; помнит, сколько кусков у него забрали."""

    def __init__(self, chunks, size):
        self.chunks, self.size, self.taken = chunks, size, 0

    def __iter__(self):
        for _ in range(self.chunks):
            self.taken += 1
            yield b"x" * self.size


@pytest.fixture()
def worker_token(monkeypatch):
    monkeypatch.setattr(config, "EXTERNAL_WORKER_TOKEN_HASH", hashlib.sha256(TOKEN.encode()).hexdigest())


@pytest.mark.parametrize("path, limit", [
    ("/api/auth/login", 64_000),
    ("/api/auth/register", 64_000),
    ("/api/auth/admit", 64_000),
    ("/api/documents", 32_000_000),
    ("/api/external-worker/jobs/5/complete", 32_000_000),
    ("/api/external-worker/materials", 32_000_000),
    ("/api/documents/5", 2_000_000),
    ("/api/signals/feedback", 2_000_000),
    ("/api/company-parents/import", 2_000_000),
    ("/", 2_000_000),
])
def test_body_limit_by_path(path, limit):
    assert request_guard.body_limit(path) == limit


def test_declared_size_over_the_limit_is_refused_before_reading():
    client = _client()

    assert client.post("/api/auth/login", content=b"x" * 64_000).json() == {"bytes": 64_000}
    refused = client.post("/api/auth/login", content=b"x" * 64_001)

    assert refused.status_code == 413
    assert refused.json() == {"detail": "Слишком большой запрос: не больше 64 КБ"}


def test_streamed_body_over_the_limit_is_cut():
    """Без Content-Length (chunked) предел держит счёт полученных байт."""
    refused = _client().post("/api/auth/login", content=_Body(chunks=10, size=10_000))

    assert refused.status_code == 413
    assert _client().post("/api/auth/login", content=_Body(chunks=6, size=10_000)).json() == {"bytes": 60_000}


def test_default_limit_applies_to_other_paths():
    refused = _client().post("/api/signals/feedback", content=b"x" * 2_000_001)

    assert refused.status_code == 413
    assert refused.json()["detail"] == "Слишком большой запрос: не больше 2 МБ"


def test_worker_path_without_token_is_refused_before_reading_the_body(worker_token):
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}):
        body = _Body(chunks=3, size=1_000)
        refused = _client().post("/api/external-worker/jobs/5/complete", content=body, headers=headers)
        assert refused.status_code == 401, headers
        assert body.taken == 0, headers

    accepted = _client().post("/api/external-worker/jobs/5/complete", content=b"x" * 3_000_000,
                              headers={"Authorization": f"Bearer {TOKEN}"})
    assert accepted.json() == {"bytes": 3_000_000}


def test_worker_path_without_configured_token_answers_503(monkeypatch):
    monkeypatch.setattr(config, "EXTERNAL_WORKER_TOKEN_HASH", "")

    assert _client().post("/api/external-worker/claim", content=b"{}").status_code == 503


def test_upload_needs_a_live_session_before_reading_the_body(monkeypatch):
    monkeypatch.setattr(request_guard.repository, "session_is_active", lambda token: token == "good")
    for cookies in ({}, {config.AUTH_COOKIE_NAME: "bad"}):
        body = _Body(chunks=3, size=1_000)
        client = _client()
        client.cookies.update(cookies)
        refused = client.post("/api/documents", content=body)
        assert refused.status_code == 401, cookies
        assert body.taken == 0, cookies

    client = _client()
    client.cookies.set(config.AUTH_COOKIE_NAME, "good")
    assert client.post("/api/documents", content=b"x" * 3_000_000).json() == {"bytes": 3_000_000}
    assert _client().get("/api/documents").json() == {"bytes": 0}


def test_app_runs_behind_the_guard(monkeypatch):
    called = []
    monkeypatch.setattr(api.repository, "authenticate_user", lambda *args: called.append(args))

    refused = TestClient(api.app).post("/api/auth/login", content=b'{"email": "a@example.test", "password": "' +
                                       b"x" * 64_100 + b'"}', headers={"Content-Type": "application/json"})

    assert refused.status_code == 413
    assert called == []


# --- сторожа конфигурации --------------------------------------------------------------------------------------------


def _caddy_bytes(value):
    number, unit = re.fullmatch(r"(\d+)(KB|MB)", value).groups()
    return int(number) * {"KB": 1000, "MB": 1000 * 1000}[unit]


def test_caddyfile_limits_equal_the_app_limits():
    text = (REPO_ROOT / "Caddyfile").read_text(encoding="utf-8")
    matchers = {name: (negated, paths.split()) for name, negated, paths in
                re.findall(r"^\s*@(\w+)\s+(not\s+)?path\s+([^\n]+)$", text, flags=re.M)}
    limits = {name: _caddy_bytes(size) for name, size in
              re.findall(r"request_body\s+@(\w+)\s*\{\s*max_size\s+(\S+)\s*\}", text)}

    assert limits == {"auth_body": request_guard.AUTH_BODY_BYTES, "large_body": request_guard.LARGE_BODY_BYTES,
                      "other_body": request_guard.DEFAULT_BODY_BYTES}
    assert matchers["auth_body"] == ("", ["/api/auth/*"])
    assert matchers["large_body"] == ("", ["/api/documents", "/api/external-worker/*"])
    negated, other = matchers["other_body"]
    assert negated.strip() == "not"
    assert sorted(other) == sorted(["/api/auth/*", "/api/documents", "/api/external-worker/*"])


def test_compose_trusts_forwarded_for_only_from_internal_addresses():
    text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    app_block = re.search(r"^  app:\n(.*?)(?=^  \S)", text, flags=re.M | re.S).group(1)
    value = re.search(r'FORWARDED_ALLOW_IPS:\s*"([^"]+)"', app_block).group(1)

    assert value.split(",") == ["127.0.0.1", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
