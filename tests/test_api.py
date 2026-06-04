from fastapi.testclient import TestClient

from oiltech_digest import api


def test_source_diagnose_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com"}
    monkeypatch.setattr(
        api.repository,
        "get_source",
        lambda source_id: {"id": source_id, "name": "Example", "parse_strategy": "request"},
    )
    monkeypatch.setattr(
        api,
        "diagnose_source",
        lambda source, limit=5: {
            "source_id": source["id"],
            "source_name": source["name"],
            "strategy": source["parse_strategy"],
            "limit": limit,
            "verdict": "ok",
        },
    )
    try:
        client = TestClient(app)
        response = client.get("/api/sources/7/diagnose?limit=3")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "source_id": 7,
        "source_name": "Example",
        "strategy": "request",
        "limit": 3,
        "verdict": "ok",
    }


def test_source_diagnose_endpoint_accepts_unsaved_overrides(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com"}
    captured = {}
    monkeypatch.setattr(
        api.repository,
        "get_source",
        lambda source_id: {
            "id": source_id,
            "name": "Example",
            "parse_strategy": "request",
            "listing_url": "https://old.example.com/news",
        },
    )

    def fake_diagnose(source, limit=5):
        captured.update(source)
        return {"source_id": source["id"], "listing_url": source["listing_url"], "limit": limit, "verdict": "ok"}

    monkeypatch.setattr(api, "diagnose_source", fake_diagnose)
    try:
        client = TestClient(app)
        response = client.post(
            "/api/sources/7/diagnose?limit=4",
            json={"listing_url": "https://new.example.com/news", "listing_selector": ".card"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "source_id": 7,
        "listing_url": "https://new.example.com/news",
        "limit": 4,
        "verdict": "ok",
    }
    assert captured["listing_selector"] == ".card"


def test_create_monthly_digest_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com"}
    monkeypatch.setattr(
        api,
        "save_digest_draft",
        lambda month, limit=20, min_score=60: {
            "id": 9,
            "month": month,
            "title": f"Digest {month}",
            "status": "draft",
            "items": limit,
            "min_score": min_score,
        },
    )
    try:
        client = TestClient(app)
        response = client.post(
            "/api/monthly-digests",
            json={"month": "2026-05", "limit": 7, "min_score": 65},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "id": 9,
        "month": "2026-05",
        "title": "Digest 2026-05",
        "status": "draft",
        "items": 7,
        "min_score": 65,
    }


def test_source_health_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com"}
    monkeypatch.setattr(
        api.repository,
        "source_health_report",
        lambda stale_days=3, limit=500, verdict=None: [
            {
                "id": 7,
                "name": "Example",
                "verdict": verdict or "no_articles",
                "articles": 0,
                "stale_days": stale_days,
                "limit": limit,
            }
        ],
    )
    try:
        client = TestClient(app)
        response = client.get("/api/source-health?stale_days=5&limit=10&verdict=stale")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == [
        {"id": 7, "name": "Example", "verdict": "stale", "articles": 0, "stale_days": 5, "limit": 10}
    ]
