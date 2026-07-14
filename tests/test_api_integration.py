from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from oiltech_digest import api
from oiltech_digest.db import connection


def test_articles_api_filters_and_patch_status_against_real_db(isolated_db):
    app = api.app
    now = datetime.now(timezone.utc)

    with connection.get_connection() as conn:
        # Пер-юзерное состояние (#12) ссылается на users(id) по внешнему ключу — нужен
        # настоящий пользователь, а не выдуманный id из dependency_overrides.
        user_id = conn.execute(
            """
            INSERT INTO users (email, password_salt, password_hash, role)
            VALUES ('test@example.com', 'salt', 'hash', 'admin')
            RETURNING id
            """
        ).fetchone()[0]
        source_id = conn.execute(
            """
            INSERT INTO sources (name, source_type, url, enabled, parse_strategy, category)
            VALUES ('World Oil', 'News', 'https://example.com', TRUE, 'request', 'международные')
            RETURNING id
            """
        ).fetchone()[0]
        parent_tag_id = conn.execute(
            "INSERT INTO tags (name, enabled, sort_order) VALUES ('Технологии', TRUE, 1) RETURNING id"
        ).fetchone()[0]
        tag_id = conn.execute(
            "INSERT INTO tags (parent_id, name, enabled, sort_order) VALUES (%s, 'Бурение', TRUE, 1) RETURNING id",
            (parent_tag_id,),
        ).fetchone()[0]
        criterion_id = conn.execute(
            """
            INSERT INTO scoring_criteria (name, weight, enabled, sort_order)
            VALUES ('Технологическая значимость', 100, TRUE, 1)
            RETURNING id
            """
        ).fetchone()[0]
        article_id = conn.execute(
            """
            INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language)
            VALUES (%s, 'Directional drilling automation', 'https://example.com/drilling',
                    %s, %s, 'Automation improves directional drilling operations.', 'en')
            RETURNING id
            """,
            (source_id, now - timedelta(days=1), now - timedelta(days=1)),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO article_cards (article_id, summary, relevant, status, selected_for_digest)
            VALUES (%s, 'AI summary for drilling automation', TRUE, 'review', FALSE)
            """,
            (article_id,),
        )
        # Рабочий статус статьи ПЕР-ЮЗЕРНЫЙ (#12): /api/articles фильтрует по
        # COALESCE(uas.status, 'new'), а не по article_cards.status. Без строки в
        # user_article_states статус читается как 'new' и фильтр status=review даёт 0 строк.
        conn.execute(
            """
            INSERT INTO user_article_states (user_id, article_id, status)
            VALUES (%s, %s, 'review')
            """,
            (user_id, article_id),
        )
        conn.execute(
            """
            INSERT INTO article_tags (article_id, tag_id, confidence, rationale)
            VALUES (%s, %s, 0.92, 'matched drilling')
            """,
            (article_id, tag_id),
        )
        score_id = conn.execute(
            """
            INSERT INTO article_scores (article_id, model, total_score, score_label, explanation)
            VALUES (%s, 'offline', 87, 'Высокая', 'strong relevance')
            RETURNING id
            """,
            (article_id,),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO article_score_items
              (article_score_id, criterion_id, keyword_score, ai_score, final_score, rationale)
            VALUES (%s, %s, 80, 90, 86.5, 'criterion rationale')
            """,
            (score_id, criterion_id),
        )
        conn.commit()

    app.dependency_overrides[api.require_user] = lambda: {"id": user_id, "email": "test@example.com"}

    try:
        client = TestClient(app)
        response = client.get(
            "/api/articles",
            params={
                "search": "directional",
                "source": "World Oil",
                "tag": "Технологии",
                "status": "review",
                "min_score": 80,
                "limit": 10,
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert len(payload) == 1
        assert payload[0]["id"] == article_id
        assert payload[0]["source"] == "World Oil"
        assert payload[0]["tag"] == "Технологии / Бурение"
        assert payload[0]["score"] == 87
        assert payload[0]["status"] == "review"
        assert payload[0]["digest"] is False
        assert payload[0]["score_items"] == [
            {
                "name": "Технологическая значимость",
                "weight": 100.0,
                "final_score": 86.5,
                "ai_score": 90.0,
                "keyword_score": 80.0,
                "rationale": "criterion rationale",
            }
        ]

        patch_response = client.patch(
            f"/api/articles/{article_id}",
            json={"status": "digest", "analyst_comment": "Include in June digest"},
        )
        assert patch_response.status_code == 200
        assert patch_response.json() == {"ok": True}

        updated = client.get("/api/articles", params={"status": "digest", "limit": 10}).json()
        assert len(updated) == 1
        assert updated[0]["digest"] is True
        assert updated[0]["status"] == "digest"

        missing_response = client.patch("/api/articles/999999", json={"status": "digest"})
        assert missing_response.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_min_score_filters_scored_noise_but_keeps_unscored_visible(isolated_db):
    """Порог min_score применяется только к УЖЕ оценённым статьям.

    Регрессия деплоя 4ed8dd2: дефолт ленты стал min_score=50, а ещё не оценённые
    статьи (нет строки в article_scores → total_score NULL → COALESCE 0) отсекались
    порогом вместе с настоящим шумом. Свежий приток становился невидим до прохода
    ИИ, и лента выглядела замороженной («обработка сломалась»).
    Решение заказчика «скрывать <50» касается оценённого шума — оно сохраняется.
    """
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com"}
    now = datetime.now(timezone.utc)

    with connection.get_connection() as conn:
        source_id = conn.execute(
            """
            INSERT INTO sources (name, source_type, url, enabled, parse_strategy, category)
            VALUES ('World Oil', 'News', 'https://example.com', TRUE, 'request', 'международные')
            RETURNING id
            """
        ).fetchone()[0]

        def add_article(title: str, url: str, score: int | None) -> int:
            article_id = conn.execute(
                """
                INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language)
                VALUES (%s, %s, %s, %s, %s, 'raw text', 'en')
                RETURNING id
                """,
                (source_id, title, url, now - timedelta(days=1), now - timedelta(days=1)),
            ).fetchone()[0]
            if score is not None:
                conn.execute(
                    """
                    INSERT INTO article_scores (article_id, model, total_score, score_label, explanation)
                    VALUES (%s, 'offline', %s, 'Высокая', 'why')
                    """,
                    (article_id, score),
                )
            return article_id

        high_id = add_article("High signal", "https://example.com/high", 80)
        noise_id = add_article("Scored noise", "https://example.com/noise", 30)
        unscored_id = add_article("Fresh unscored", "https://example.com/fresh", None)
        conn.commit()

    try:
        client = TestClient(app)
        payload = client.get(
            "/api/articles",
            params={"min_score": 50, "max_score": 100, "sort": "score_desc", "limit": 100},
        ).json()
        ids = [row["id"] for row in payload]

        # Оценённый шум (30 < 50) отсекается — решение «скрыть <50» работает.
        assert noise_id not in ids
        # Ценный сигнал виден.
        assert high_id in ids
        # Ещё НЕ оценённая статья ОСТАЁТСЯ видимой (суть фикса).
        assert unscored_id in ids

        # При score_desc неоценённая оседает вниз и не мешает «верху» ленты.
        assert ids[0] == high_id
        assert ids[-1] == unscored_id

        unscored = next(row for row in payload if row["id"] == unscored_id)
        assert unscored["score"] == 0
        assert unscored["rating"] == "Без оценки"
    finally:
        app.dependency_overrides.clear()


def test_digest_export_endpoint_finishes_job_and_returns_file(monkeypatch, tmp_path, isolated_db):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com"}
    exported = tmp_path / "digest.html"
    exported.write_text("<html><body>Digest file</body></html>", encoding="utf-8")

    monkeypatch.setattr(
        api,
        "write_digest_export",
        lambda month, export_format, limit, min_score, **kwargs: {
            "path": str(exported),
            "filename": exported.name,
            "media_type": "text/html; charset=utf-8",
            "items": 1,
            "format": export_format,
        },
    )

    try:
        client = TestClient(app)
        response = client.get("/api/digest-export?month=2026-06&export_format=html&limit=5&min_score=10")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.text == "<html><body>Digest file</body></html>"
    assert response.headers["content-type"].startswith("text/html")

    with connection.get_connection() as conn:
        row = conn.execute("SELECT export_type, format, status, file_path, error_message FROM export_jobs").fetchone()
    assert row == ("monthly_digest", "html", "ok", str(exported), None)


def test_digest_export_endpoint_records_pdf_runtime_error(monkeypatch, isolated_db):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com"}

    # Сигнатура должна принимать ВСЕ kwargs эндпоинта (max_score/search/top_tag/user_id),
    # иначе TypeError при связывании аргументов срабатывает раньше raise RuntimeError,
    # и вместо ветки 503 тест ловит generic 500.
    def fail_export(month, export_format, limit, min_score, **kwargs):
        raise RuntimeError("PDF-экспорт требует Playwright")

    monkeypatch.setattr(api, "write_digest_export", fail_export)

    try:
        client = TestClient(app)
        response = client.get("/api/digest-export?export_format=pdf")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert "Playwright" in response.json()["detail"]

    with connection.get_connection() as conn:
        row = conn.execute("SELECT export_type, format, status, file_path, error_message FROM export_jobs").fetchone()
    assert row == ("monthly_digest", "pdf", "failed", None, "PDF-экспорт требует Playwright")


def test_background_jobs_api_lists_status_and_downloads_result(tmp_path, isolated_db):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}
    exported = tmp_path / "digest.json"
    exported.write_text('{"ok": true}', encoding="utf-8")

    with connection.get_connection() as conn:
        job_id = conn.execute(
            """
            INSERT INTO background_jobs
              (kind, status, progress, payload_json, result_json, started_at, finished_at)
            VALUES
              ('digest_export', 'ok', 100, '{"month":"2026-06"}'::jsonb,
               %s::jsonb, now(), now())
            RETURNING id
            """,
            (
                f'{{"path": "{exported}", "filename": "digest.json", '
                '"media_type": "application/json"}',
            ),
        ).fetchone()[0]
        conn.commit()

    try:
        client = TestClient(app)
        list_response = client.get("/api/jobs?kind=digest_export")
        status_response = client.get(f"/api/jobs/{job_id}")
        download_response = client.get(f"/api/jobs/{job_id}/download")
    finally:
        app.dependency_overrides.clear()

    assert list_response.status_code == 200
    assert list_response.json()[0]["id"] == job_id
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "ok"
    assert status_response.json()["result"]["filename"] == "digest.json"
    assert download_response.status_code == 200
    assert download_response.text == '{"ok": true}'


def test_background_jobs_api_hides_other_users_jobs_and_downloads(tmp_path, isolated_db):
    app = api.app
    exported = tmp_path / "own-digest.json"
    exported.write_text('{"owner": true}', encoding="utf-8")

    with connection.get_connection() as conn:
        first_user_id = conn.execute(
            """
            INSERT INTO users (email, password_salt, password_hash, role)
            VALUES ('first@example.com', 'salt', 'hash', 'user')
            RETURNING id
            """
        ).fetchone()[0]
        second_user_id = conn.execute(
            """
            INSERT INTO users (email, password_salt, password_hash, role)
            VALUES ('second@example.com', 'salt', 'hash', 'user')
            RETURNING id
            """
        ).fetchone()[0]
        own_job_id = conn.execute(
            """
            INSERT INTO background_jobs
              (user_id, kind, status, progress, payload_json, result_json, started_at, finished_at)
            VALUES
              (%s, 'digest_export', 'ok', 100, '{"month":"2026-06"}'::jsonb,
               %s::jsonb, now(), now())
            RETURNING id
            """,
            (
                first_user_id,
                f'{{"path": "{exported}", "filename": "own-digest.json", '
                '"media_type": "application/json"}',
            ),
        ).fetchone()[0]
        other_job_id = conn.execute(
            """
            INSERT INTO background_jobs
              (user_id, kind, status, progress, payload_json, result_json, started_at, finished_at)
            VALUES
              (%s, 'digest_export', 'ok', 100, '{"month":"2026-06"}'::jsonb,
               '{"path": "/tmp/other-digest.json", "filename": "other-digest.json"}'::jsonb,
               now(), now())
            RETURNING id
            """,
            (second_user_id,),
        ).fetchone()[0]
        conn.commit()

    app.dependency_overrides[api.require_user] = lambda: {
        "id": first_user_id,
        "email": "first@example.com",
        "role": "user",
    }
    try:
        client = TestClient(app)
        list_response = client.get("/api/jobs?kind=digest_export")
        own_status_response = client.get(f"/api/jobs/{own_job_id}")
        other_status_response = client.get(f"/api/jobs/{other_job_id}")
        own_download_response = client.get(f"/api/jobs/{own_job_id}/download")
        other_download_response = client.get(f"/api/jobs/{other_job_id}/download")
    finally:
        app.dependency_overrides.clear()

    assert list_response.status_code == 200
    assert [job["id"] for job in list_response.json()] == [own_job_id]
    assert own_status_response.status_code == 200
    assert own_status_response.json()["id"] == own_job_id
    assert other_status_response.status_code == 404
    assert own_download_response.status_code == 200
    assert own_download_response.text == '{"owner": true}'
    assert other_download_response.status_code == 404


def test_background_job_download_rejects_unfinished_and_missing_files(isolated_db):
    app = api.app
    app.dependency_overrides[api.require_user] = lambda: {"id": 1, "email": "test@example.com", "role": "admin"}

    with connection.get_connection() as conn:
        queued_job_id = conn.execute(
            """
            INSERT INTO background_jobs (kind, status, progress, payload_json)
            VALUES ('digest_export', 'queued', 0, '{}'::jsonb)
            RETURNING id
            """
        ).fetchone()[0]
        missing_file_job_id = conn.execute(
            """
            INSERT INTO background_jobs
              (kind, status, progress, payload_json, result_json, started_at, finished_at)
            VALUES
              ('digest_export', 'ok', 100, '{}'::jsonb,
               '{"path": "/tmp/definitely-missing-oiltech-digest.pdf", "filename": "missing.pdf"}'::jsonb,
               now(), now())
            RETURNING id
            """
        ).fetchone()[0]
        conn.commit()

    try:
        client = TestClient(app)
        queued_response = client.get(f"/api/jobs/{queued_job_id}/download")
        missing_file_response = client.get(f"/api/jobs/{missing_file_job_id}/download")
    finally:
        app.dependency_overrides.clear()

    assert queued_response.status_code == 409
    assert queued_response.json()["detail"] == "Job is not finished"
    assert missing_file_response.status_code == 404
    assert missing_file_response.json()["detail"] == "Job result file not found"
