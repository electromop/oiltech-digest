from fastapi.testclient import TestClient

from oiltech_digest import api


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def execute(self, sql, params=None):
        self.connection.executed.append((sql, list(params or [])))
        if "FROM article_score_items" in sql:
            self.rows = [
                {
                    "article_id": 42,
                    "name": "Технологическая значимость",
                    "weight": 40,
                    "final_score": 88,
                    "ai_score": 90,
                    "keyword_score": 80,
                    "rationale": "Strong match",
                }
            ]
        else:
            self.rows = [
                {
                    "id": 42,
                    "title": "Directional drilling automation",
                    "url": "https://example.com/drilling",
                    "language": "en",
                    "raw_text": "Directional drilling automation improves well construction.",
                    "published_at": None,
                    "collected_at": None,
                    "text_truncated": False,
                    "source_name": "World Oil",
                    "summary": "Compact AI summary",
                    "status": "digest",
                    "relevant": True,
                    "relevance_reason": "Oilfield technology",
                    "selected_for_digest": True,
                    "total_score": 88,
                    "score_label": "High",
                    "score_explanation": "Relevant",
                    "tag_name": "Бурение",
                    "parent_tag_name": "Технологии",
                    "tag_confidence": 0.91,
                    "tag_rationale": "Keyword match",
                }
            ]
        return self

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self):
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self, row_factory=None):
        return FakeCursor(self)


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


def test_source_diagnose_endpoint_can_enqueue_background_job(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com"}
    monkeypatch.setattr(
        api.repository,
        "get_source",
        lambda source_id: {
            "id": source_id,
            "name": "Example",
            "parse_strategy": "playwright",
            "listing_url": "https://old.example.com/news",
        },
    )
    monkeypatch.setattr(
        api.background_jobs,
        "enqueue",
        lambda kind, payload, **kwargs: {
            "id": 120,
            "kind": kind,
            "queue_name": kwargs.get("queue_name", "default"),
            "status": "queued",
            "progress": 0,
            "attempts": 0,
            "max_attempts": kwargs.get("max_attempts", 3),
            "run_after": None,
            "payload_json": payload,
            "result_json": None,
            "error_message": None,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
        },
    )
    try:
        client = TestClient(app)
        response = client.post(
            "/api/sources/7/diagnose?limit=4&background=true",
            json={"listing_url": "https://new.example.com/news", "listing_selector": ".card"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["job"] == {
        "id": 120,
        "kind": "diagnose_source",
        "queue": "playwright",
        "status": "queued",
        "progress": 0.0,
        "attempts": 0,
        "max_attempts": 3,
        "payload": {
            "source_id": 7,
            "overrides": {"listing_url": "https://new.example.com/news", "listing_selector": ".card"},
            "limit": 4,
        },
        "result": {},
        "error": None,
        "run_after": None,
        "created_at": None,
        "started_at": None,
        "finished_at": None,
    }


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


def test_digest_branding_endpoints(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com"}
    branding = {
        "header": {
            "brand_text": "Тест бренд",
            "brand_suffix": "Тест слоган",
            "department_text": "Тест департамент",
        },
        "hero": {
            "badge": "ТЕСТ",
            "headline": "Тестовый дайджест",
            "subtitle": "Подзаголовок",
            "image_url": "https://example.com/hero.jpg",
        },
        "issue": {
            "title_template": "Дайджест",
            "title_template_with_month": "Дайджест · {month}",
            "period_label_all": "всё время",
            "preheader": "Прехедер",
            "intro_template": "Интро",
            "intro_template_with_month": "Интро {month}",
            "highlights_title": "Итоги",
            "news_title": "Сигналы",
            "read_more_label": "Открыть",
            "empty_summary_text": "Нет сути",
            "preview_empty_text": "Пусто",
        },
        "footer": {
            "contact_text": "Пишите нам",
            "contact_email": "digest@example.com",
            "note": "Тест",
            "socials": [{"label": "Portal", "accent": "#111111", "text": "P"}],
        },
        "highlights": {
            "analytics_source_keywords": ["rystad"],
            "analytics_category_keywords": ["аналит"],
            "business_category_keywords": ["контракт"],
            "cards": [
                {"metric": "total", "icon": "doc", "prefix": "", "suffix": "", "noun_one": "новость", "noun_few": "новости", "noun_many": "новостей"},
                {"metric": "analytics", "icon": "chart", "prefix": "аналитических", "suffix": "", "noun_one": "материал", "noun_few": "материала", "noun_many": "материалов"},
                {"metric": "business", "icon": "people", "prefix": "", "suffix": "для бизнеса", "noun_one": "возможность", "noun_few": "возможности", "noun_many": "возможностей"},
            ],
        },
    }
    monkeypatch.setattr(api, "get_digest_branding", lambda: branding)
    monkeypatch.setattr(api, "save_digest_branding", lambda payload: payload)
    try:
        client = TestClient(app)
        get_response = client.get("/api/digest-branding")
        put_response = client.put("/api/digest-branding", json=branding)
    finally:
        app.dependency_overrides.clear()

    assert get_response.status_code == 200
    assert get_response.json() == branding
    assert put_response.status_code == 200
    assert put_response.json() == {"ok": True, "branding": branding}


def test_readiness_endpoint_reports_ok(monkeypatch):
    app = api.app
    monkeypatch.setattr(
        api,
        "readiness_check",
        lambda: {
            "ok": True,
            "database": {"ok": True},
            "schema": {"ok": True, "missing_tables": []},
            "jobs": {"queued_or_running": 0, "stale_running": 0, "stale_minutes": 60},
            "articles": 10,
        },
    )
    client = TestClient(app)
    response = client.get("/api/readiness")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["jobs"]["stale_running"] == 0


def test_readiness_endpoint_returns_503_for_not_ready(monkeypatch):
    app = api.app
    monkeypatch.setattr(
        api,
        "readiness_check",
        lambda: {
            "ok": False,
            "database": {"ok": True},
            "schema": {"ok": False, "missing_tables": ["background_jobs"]},
            "jobs": {"queued_or_running": 3, "stale_running": 1, "stale_minutes": 60},
            "articles": 10,
        },
    )
    client = TestClient(app)
    response = client.get("/api/readiness")

    assert response.status_code == 503
    payload = response.json()
    assert payload["ok"] is False
    assert payload["schema"]["missing_tables"] == ["background_jobs"]


def test_readiness_endpoint_returns_503_for_db_error(monkeypatch):
    app = api.app
    monkeypatch.setattr(api, "readiness_check", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    client = TestClient(app)
    response = client.get("/api/readiness")

    assert response.status_code == 503
    assert response.json() == {"ok": False, "database": {"ok": False}, "error": "db down"}


def test_list_articles_applies_filters_and_score_items(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com"}
    fake_conn = FakeConnection()
    monkeypatch.setattr(api, "get_connection", lambda: fake_conn)
    try:
        client = TestClient(app)
        response = client.get(
            "/api/articles",
            params={
                "search": "drilling",
                "source": "World Oil",
                "tag": "Бурение",
                "status": "digest",
                "min_score": 80,
                "limit": 25,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["id"] == 42
    assert payload[0]["tag"] == "Технологии / Бурение"
    assert payload[0]["score"] == 88
    assert payload[0]["digest"] is True
    assert payload[0]["score_items"][0]["name"] == "Технологическая значимость"

    articles_sql, articles_params = fake_conn.executed[0]
    assert "LOWER(a.title" in articles_sql
    assert "s.name = %s" in articles_sql
    assert "(t.name = %s OR parent.name = %s)" in articles_sql
    assert "COALESCE(c.status, 'new') = %s" in articles_sql
    assert "COALESCE(sc.total_score, 0) >= %s" in articles_sql
    assert articles_params == ["%drilling%", "World Oil", "Бурение", "Бурение", "digest", 80.0, 25]


def test_update_source_persists_non_rss_scraper_fields(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com"}
    captured = {}

    class UpdateConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params):
            captured["sql"] = sql
            captured["params"] = params

            class Result:
                def fetchone(self):
                    return {"id": 9}

            return Result()

        def commit(self):
            captured["committed"] = True

    monkeypatch.setattr(api, "get_connection", lambda: UpdateConnection())
    try:
        client = TestClient(app)
        response = client.patch(
            "/api/sources/9",
            json={
                "parse_strategy": "playwright",
                "listing_url": "https://example.com/news",
                "listing_selector": ".card",
                "article_link_selector": ".card a",
                "article_date_selector": "time",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert "parse_strategy = %s" in captured["sql"]
    assert "listing_selector = %s" in captured["sql"]
    assert captured["params"] == [
        "playwright",
        "https://example.com/news",
        ".card",
        ".card a",
        "time",
        9,
    ]
    assert captured["committed"] is True


def test_scrape_source_endpoint_routes_request_strategy(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com"}
    monkeypatch.setattr(
        api.repository,
        "get_source",
        lambda source_id: {"id": source_id, "name": "Request Source", "parse_strategy": "request"},
    )
    monkeypatch.setattr(api.request_parser, "parse_source", lambda source: {"added": 2, "attempted": 3})
    try:
        client = TestClient(app)
        response = client.post("/api/sources/12/scrape")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"ok": True, "stats": {"added": 2, "attempted": 3}}


def test_scrape_source_endpoint_can_enqueue_background_job(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com"}
    monkeypatch.setattr(
        api.repository,
        "get_source",
        lambda source_id: {"id": source_id, "name": "Request Source", "parse_strategy": "request"},
    )
    monkeypatch.setattr(
        api.background_jobs,
        "enqueue",
        lambda kind, payload, **kwargs: {
            "id": 99,
            "kind": kind,
            "queue_name": kwargs.get("queue_name", "default"),
            "status": "queued",
            "progress": 0,
            "attempts": 0,
            "max_attempts": kwargs.get("max_attempts", 3),
            "run_after": None,
            "payload_json": payload,
            "result_json": None,
            "error_message": None,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
        },
    )
    try:
        client = TestClient(app)
        response = client.post("/api/sources/12/scrape?background=true")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["job"] == {
        "id": 99,
        "kind": "scrape_source",
        "queue": "default",
        "status": "queued",
        "progress": 0.0,
        "attempts": 0,
        "max_attempts": 3,
        "payload": {"source_id": 12},
        "result": {},
        "error": None,
        "run_after": None,
        "created_at": None,
        "started_at": None,
        "finished_at": None,
    }


def test_scrape_source_endpoint_routes_playwright_strategy(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com"}
    monkeypatch.setattr(
        api.repository,
        "get_source",
        lambda source_id: {"id": source_id, "name": "Rendered Source", "parse_strategy": "playwright"},
    )
    monkeypatch.setattr(api.playwright_parser, "parse_source", lambda source: {"added": 1, "attempted": 1})
    try:
        client = TestClient(app)
        response = client.post("/api/sources/13/scrape")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"ok": True, "stats": {"added": 1, "attempted": 1}}


def test_scrape_source_endpoint_rejects_non_scraper_strategy(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com"}
    monkeypatch.setattr(
        api.repository,
        "get_source",
        lambda source_id: {"id": source_id, "name": "RSS Source", "parse_strategy": "rss"},
    )
    try:
        client = TestClient(app)
        response = client.post("/api/sources/14/scrape")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert "request/playwright" in response.json()["detail"]


def test_auth_register_login_me_and_logout(monkeypatch):
    app = api.app
    sessions = {}
    users = {"user@example.com": {"id": 1, "email": "user@example.com"}}

    monkeypatch.setattr(api.repository, "create_user", lambda email, password: users[email])
    monkeypatch.setattr(api.repository, "authenticate_user", lambda email, password: users.get(email))
    monkeypatch.setattr(api.repository, "create_user_session", lambda user_id: f"session-{user_id}")
    monkeypatch.setattr(api.repository, "get_user_by_session", lambda token: sessions.get(token))
    monkeypatch.setattr(api.repository, "delete_user_session", lambda token: sessions.pop(token, None))

    client = TestClient(app)
    register_response = client.post("/api/auth/register", json={"email": " USER@example.com ", "password": "12345678"})
    assert register_response.status_code == 200
    assert register_response.json()["user"]["email"] == "user@example.com"

    sessions["session-1"] = users["user@example.com"]
    me_response = client.get("/api/auth/me")
    assert me_response.status_code == 200
    assert me_response.json()["user"]["email"] == "user@example.com"

    login_response = client.post("/api/auth/login", json={"email": "user@example.com", "password": "12345678"})
    assert login_response.status_code == 200

    logout_response = client.post("/api/auth/logout")
    assert logout_response.status_code == 200
    assert logout_response.json() == {"ok": True}


def test_auth_rejects_invalid_payloads_and_missing_session(monkeypatch):
    client = TestClient(api.app)

    assert client.get("/api/auth/me").status_code == 401
    assert client.post("/api/auth/register", json={"email": "bad", "password": "12345678"}).status_code == 400
    assert client.post("/api/auth/register", json={"email": "user@example.com", "password": "1234567"}).status_code == 400

    monkeypatch.setattr(api.repository, "authenticate_user", lambda email, password: None)
    assert client.post("/api/auth/login", json={"email": "user@example.com", "password": "12345678"}).status_code == 401


def test_enqueue_digest_export_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com"}
    captured = {}

    def fake_enqueue(kind, payload, **kwargs):
        captured["kind"] = kind
        captured["payload"] = payload
        captured["kwargs"] = kwargs
        return {
            "id": 7,
            "kind": kind,
            "queue_name": kwargs.get("queue_name", "default"),
            "status": "queued",
            "progress": 0,
            "attempts": 0,
            "max_attempts": kwargs.get("max_attempts", 3),
            "run_after": None,
            "payload_json": payload,
            "result_json": None,
            "error_message": None,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
        }

    monkeypatch.setattr(api.background_jobs, "enqueue", fake_enqueue)
    try:
        client = TestClient(app)
        response = client.post(
            "/api/jobs/digest-export",
            json={"month": "2026-06", "export_format": "pdf", "limit": 25, "min_score": 60},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert captured == {
        "kind": "digest_export",
        "payload": {"month": "2026-06", "export_format": "pdf", "limit": 25, "min_score": 60.0},
        "kwargs": {"queue_name": "playwright"},
    }
    assert response.json()["job"]["status"] == "queued"
    assert response.json()["job"]["queue"] == "playwright"


def test_enqueue_process_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com"}
    monkeypatch.setattr(
        api.background_jobs,
        "enqueue",
        lambda kind, payload, **kwargs: {
            "id": 8,
            "kind": kind,
            "queue_name": kwargs.get("queue_name", "default"),
            "status": "queued",
            "progress": 0,
            "attempts": 0,
            "max_attempts": kwargs.get("max_attempts", 3),
            "run_after": None,
            "payload_json": payload,
            "result_json": None,
            "error_message": None,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
        },
    )
    try:
        client = TestClient(app)
        response = client.post("/api/jobs/process", json={"article_ids": [1, 2], "offline": True})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["job"]["kind"] == "process_articles"
    assert response.json()["job"]["queue"] == "ai"
    assert response.json()["job"]["payload"]["article_ids"] == [1, 2]


def test_maintenance_status_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com"}
    monkeypatch.setattr(
        api,
        "maintenance_status",
        lambda: {
            "retention": {"stale_minutes": 60, "background_job_days": 30, "export_job_days": 14},
            "expired_sessions": 2,
            "stale_running_jobs": 1,
            "cleanup_candidates": {"background_jobs": 5, "export_jobs": 3},
        },
    )
    try:
        client = TestClient(app)
        response = client.get("/api/maintenance/status")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["expired_sessions"] == 2
    assert response.json()["cleanup_candidates"]["background_jobs"] == 5


def test_maintenance_cleanup_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com"}
    captured = {}

    def fake_cleanup(**kwargs):
        captured.update(kwargs)
        return {"expired_sessions": 1, "background_jobs": 4, "background_job_days": 10, "export_jobs": 2, "export_job_days": 5}

    monkeypatch.setattr(api, "maintenance_cleanup", fake_cleanup)
    try:
        client = TestClient(app)
        response = client.post("/api/maintenance/cleanup", json={"background_job_days": 10, "export_job_days": 5})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert captured == {"background_job_days": 10, "export_job_days": 5}
    assert response.json()["ok"] is True
    assert response.json()["result"]["background_jobs"] == 4


def test_maintenance_cleanup_endpoint_rejects_invalid_retention():
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com"}
    try:
        client = TestClient(app)
        response = client.post("/api/maintenance/cleanup", json={"background_job_days": 0})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert "background_job_days" in response.json()["detail"]


def test_maintenance_benchmark_endpoint(monkeypatch):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com"}
    captured = {}

    def fake_benchmark(**kwargs):
        captured.update(kwargs)
        return {
            "iterations": kwargs["iterations"],
            "warn_ms": kwargs["warn_ms"],
            "params": {"articles_limit": kwargs["articles_limit"]},
            "benchmarks": [{"name": "articles_list", "status": "ok", "rows": 10, "p50_ms": 12.0, "p95_ms": 20.0, "max_ms": 25.0}],
            "counts": {"articles": 42},
            "warnings": [],
        }

    monkeypatch.setattr(api, "run_readiness_benchmark", fake_benchmark)
    try:
        client = TestClient(app)
        response = client.get("/api/maintenance/benchmark?iterations=2&articles_limit=150")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert captured["iterations"] == 2
    assert captured["articles_limit"] == 150
    assert response.json()["counts"]["articles"] == 42
    assert response.json()["benchmarks"][0]["name"] == "articles_list"
