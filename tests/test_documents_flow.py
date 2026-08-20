"""Сквозной путь приёма файла: загрузка → разбор → карточка с фактами.

Проверяется ВНЕШНЕ НАБЛЮДАЕМОЕ поведение, а не устройство: что пользователь получил
карточку, что чужой документ не отдаётся, что выдуманное число помечено неподтверждённым.
"""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

from oiltech_digest import api, config, network_policy
from oiltech_digest.db import documents_repo
from oiltech_digest.documents import external as documents_external


def _docx_bytes(paragraphs: list[str]) -> bytes:
    from docx import Document

    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "UPLOAD_DIR", str(tmp_path / "documents"))
    monkeypatch.setattr(api.config, "UPLOAD_DIR", str(tmp_path / "documents"))
    monkeypatch.setattr(api.config, "UPLOAD_DOCS_ENABLED", True)
    # Внешний контур: загрузка отклоняется на входе, если его нет, поэтому в тесте
    # маршрутизацию фиксируем явно.
    for module in (network_policy, api.network_policy):
        monkeypatch.setattr(
            module, "route_ai_processing",
            lambda: network_policy.ExecutionDecision(
                "external-ai", "external", "openai", "test"),
        )
    return TestClient(api.app)


def _as(role: str, user_id: int):
    return lambda: {"id": user_id, "email": f"{role}@example.com", "role": role}


def _make_user(isolated_db, email: str, role: str) -> int:
    from oiltech_digest.db import connection

    with connection.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (email, password_hash, password_salt, role) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (email, "x", "x", role),
        )
        user_id = int(cur.fetchone()[0])
        conn.commit()
        return user_id


def test_upload_parses_document_and_lists_it(isolated_db, client):
    admin_id = _make_user(isolated_db, "admin@example.com", "admin")
    api.app.dependency_overrides[api.require_admin] = _as("admin", admin_id)
    api.app.dependency_overrides[api.require_user] = _as("admin", admin_id)
    try:
        data = _docx_bytes(["Отчёт о добыче", "Добыча составила 1 234 тыс. тонн за квартал."])
        response = client.post(
            "/api/documents",
            files={"file": ("отчёт.docx", data,
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            data={"attested": "true"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["duplicate"] is False
        document = body["document"]
        assert document["kind"] == "docx"
        assert document["anchor_count"] == 2
        assert document["status"] == "processing"

        listing = client.get("/api/documents").json()
        assert [d["id"] for d in listing["documents"]] == [document["id"]]
    finally:
        api.app.dependency_overrides.clear()


def test_upload_without_attestation_is_rejected(isolated_db, client):
    admin_id = _make_user(isolated_db, "a2@example.com", "admin")
    api.app.dependency_overrides[api.require_admin] = _as("admin", admin_id)
    try:
        response = client.post(
            "/api/documents",
            files={"file": ("x.docx", _docx_bytes(["Текст документа достаточной длины, чтобы не считаться сканом."]), "application/octet-stream")},
            data={"attested": "false"},
        )
        assert response.status_code == 400
        assert "подтверждение" in response.json()["detail"].lower()
    finally:
        api.app.dependency_overrides.clear()


def test_same_file_twice_returns_existing_document(isolated_db, client):
    admin_id = _make_user(isolated_db, "a3@example.com", "admin")
    api.app.dependency_overrides[api.require_admin] = _as("admin", admin_id)
    api.app.dependency_overrides[api.require_user] = _as("admin", admin_id)
    try:
        data = _docx_bytes(["Одинаковый текст документа для проверки дедупликации по хешу."])
        r1 = client.post("/api/documents", files={"file": ("a.docx", data)}, data={"attested": "true"})
        assert r1.status_code == 200, r1.text
        first = r1.json()
        r2 = client.post("/api/documents", files={"file": ("b.docx", data)}, data={"attested": "true"})
        assert r2.status_code == 200, r2.text
        second = r2.json()
        assert second["duplicate"] is True
        assert second["document"]["id"] == first["document"]["id"]
    finally:
        api.app.dependency_overrides.clear()


def test_foreign_document_is_not_returned(isolated_db, client):
    owner_id = _make_user(isolated_db, "owner@example.com", "admin")
    other_id = _make_user(isolated_db, "other@example.com", "admin")
    api.app.dependency_overrides[api.require_admin] = _as("admin", owner_id)
    api.app.dependency_overrides[api.require_user] = _as("admin", owner_id)
    try:
        response = client.post(
            "/api/documents", files={"file": ("secret.docx", _docx_bytes(["Личный материал, который не должен быть виден другому пользователю."]))},
            data={"attested": "true"})
        assert response.status_code == 200, response.text
        document_id = response.json()["document"]["id"]
    finally:
        api.app.dependency_overrides.clear()

    api.app.dependency_overrides[api.require_user] = _as("admin", other_id)
    try:
        assert client.get(f"/api/documents/{document_id}").status_code == 404
        assert client.get(f"/api/documents/{document_id}/original").status_code == 404
        assert client.get("/api/documents").json()["documents"] == []
    finally:
        api.app.dependency_overrides.clear()


def test_full_parse_pipeline_offline_produces_card_and_verifies_facts(isolated_db, client):
    admin_id = _make_user(isolated_db, "a5@example.com", "admin")
    api.app.dependency_overrides[api.require_admin] = _as("admin", admin_id)
    api.app.dependency_overrides[api.require_user] = _as("admin", admin_id)
    try:
        document_id = client.post(
            "/api/documents",
            files={"file": ("отчёт.docx", _docx_bytes([
                "Обзор рынка нефтесервиса",
                "Добыча составила 1 234 тыс. тонн за квартал.",
                "Средняя стоимость перфорации 87 руб за метр.",
            ]))},
            data={"attested": "true"},
        ).json()["document"]["id"]

        # build → process (офлайн, без обращения к платной модели) → apply
        payload = documents_external.build_document_payload({"document_id": document_id, "offline": True})
        assert payload["kind"] == "process_document"
        assert payload["chunks"], "нарезчик не отдал ни одного фрагмента"

        result = documents_external.process_document_payload({**payload, "offline": True})
        assert result["process_document"] is True
        applied = documents_external.apply_document_result(result, job_id=None)

        card = client.get(f"/api/documents/{document_id}").json()
        assert card["card"] is not None
        assert card["card"]["essence"]
        # Главное: числа, которые модель «взяла со страницы», сверены с текстом якоря.
        assert applied["facts"] >= 1, "ни один факт не дошёл до применения"
        # Офлайн-заглушка берёт первое число фрагмента и приписывает его первому якорю —
        # привязать его правильно она не может. Поэтому здесь проверяется, что факты
        # ПРОШЛИ сверку как процесс; корректность самой сверки доказывает
        # test_invented_number_is_marked_unverified на подставных данных.
        assert applied["verified"] + applied["unverified"] == applied["facts"]
    finally:
        api.app.dependency_overrides.clear()


def test_invented_number_is_marked_unverified(isolated_db, client):
    """Число, которого нет на указанном якоре, обязано попасть в неподтверждённые."""
    admin_id = _make_user(isolated_db, "a6@example.com", "admin")
    api.app.dependency_overrides[api.require_admin] = _as("admin", admin_id)
    api.app.dependency_overrides[api.require_user] = _as("admin", admin_id)
    try:
        document_id = client.post(
            "/api/documents",
            files={"file": ("d.docx", _docx_bytes(["Годовой отчёт. Добыча составила 1 234 тыс. тонн за отчётный период."]))},
            data={"attested": "true"},
        ).json()["document"]["id"]

        fabricated = {
            "process_document": True, "kind": "process_document", "document_id": document_id,
            "card": {"essence": "тест", "summary": [], "claims": []},
            "facts": [
                {"value": "1 234", "unit": "тыс. тонн", "context": "добыча", "anchor": 1},
                {"value": "9 999", "unit": "тыс. тонн", "context": "выдумка", "anchor": 1},
            ],
            "usage": [], "stats": {}, "errors": [],
        }
        applied = documents_external.apply_document_result(fabricated, job_id=None)
        assert applied["verified"] == 1
        assert applied["unverified"] == 1

        facts = {f["value"]: f["verified"] for f in client.get(f"/api/documents/{document_id}").json()["facts"]}
        assert facts["1 234"] is True
        assert facts["9 999"] is False
    finally:
        api.app.dependency_overrides.clear()


def test_card_reports_anchors_without_text(isolated_db, client):
    """Якоря без текста считаются и отдаются наружу.

    На реальном заключении экспертизы текст был только на 40 страницах из 107.
    Без этого числа карточка выглядит полнее, чем есть, и человек не понимает,
    почему в ней мало фактов.
    """
    admin_id = _make_user(isolated_db, "a7@example.com", "admin")
    api.app.dependency_overrides[api.require_admin] = _as("admin", admin_id)
    api.app.dependency_overrides[api.require_user] = _as("admin", admin_id)
    try:
        document_id = client.post(
            "/api/documents",
            files={"file": ("отчёт.docx", _docx_bytes([
                "Первый блок отчёта достаточной длины, чтобы не считаться сканом.",
                "Второй содержательный блок отчёта о результатах работ на объекте.",
            ]))},
            data={"attested": "true"},
        ).json()["document"]["id"]

        # Дописываем пустые якоря — так выглядит наполовину распознанный документ.
        from oiltech_digest.db import connection

        with connection.get_connection() as conn:
            conn.execute(
                "INSERT INTO document_anchors (document_id, number, text) VALUES (%s, 3, ''), (%s, 4, '   ')",
                (document_id, document_id),
            )
            conn.commit()

        body = client.get(f"/api/documents/{document_id}").json()
        assert body["document"]["empty_anchors"] == 2

        listing = client.get("/api/documents").json()["documents"]
        assert listing[0]["empty_anchors"] == 2
    finally:
        api.app.dependency_overrides.clear()


def test_document_without_empty_anchors_reports_zero(isolated_db, client):
    admin_id = _make_user(isolated_db, "a8@example.com", "admin")
    api.app.dependency_overrides[api.require_admin] = _as("admin", admin_id)
    api.app.dependency_overrides[api.require_user] = _as("admin", admin_id)
    try:
        document_id = client.post(
            "/api/documents",
            files={"file": ("чистый.docx", _docx_bytes([
                "Единственный блок документа, целиком читаемый и достаточно длинный.",
            ]))},
            data={"attested": "true"},
        ).json()["document"]["id"]
        assert client.get(f"/api/documents/{document_id}").json()["document"]["empty_anchors"] == 0
    finally:
        api.app.dependency_overrides.clear()
