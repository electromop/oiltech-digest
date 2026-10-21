"""Материалы коллег на ядре: приём от бота NL по токену воркера, повтор без дубля, экран."""

import json

import pytest
from fastapi.testclient import TestClient

from oiltech_digest import api, colleague_materials, config
from oiltech_digest.db import repository

WORKER = {"Authorization": "Bearer worker-token", "X-Worker-Build": "abc1234", "X-Worker-Contract": "3"}


def _material(**changes):
    material = {
        "kind": "event",
        "title": "Татнефть выбрала подрядчика по ГРП на 2027 год",
        "description": "На совещании в Альметьевске объявили итог тендера: три года, объём вырос на 20 %.",
        "companies": ["Татнефть", "ТаграС"],
        "verdict": "strong_signal",
        "verdict_comment": "Конкурент взял объём",
        "tg_chat_id": -100500,
        "tg_message_id": 77,
        "tg_user_id": 9001,
        "tg_username": "@ivanov",
        "tg_author": "Иван Иванов",
    }
    material.update(changes)
    return material


@pytest.fixture()
def core(isolated_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("worker-token"))
    monkeypatch.setattr(config, "UPLOAD_DIR", str(tmp_path))
    yield TestClient(api.app)
    api.app.dependency_overrides.pop(api.require_user, None)


def _post(client, material, file=None, headers=WORKER):
    files = {"file": file} if file else None
    return client.post("/api/external-worker/materials", headers=headers, files=files,
                       data={"material": json.dumps(material, ensure_ascii=False), "worker_id": "nl-tg-bot-1"})


def test_event_saved_with_scale_label_and_bot_checked_in(core):
    response = _post(core, _material())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] is True
    saved = body["material"]
    assert saved["verdict_label"] == "Сильный сигнал — вынести в дайджест / обсуждать"
    assert saved["companies"] == ["Татнефть", "ТаграС"]
    assert saved["author_username"] == "ivanov"
    bot = next(row for row in repository.list_external_consumers() if row["consumer"] == "nl-tg-bot-1")
    assert (bot["queues"], bot["build"], bot["contract"]) == ([], "abc1234", 3)


def test_repeat_of_same_bot_message_is_not_a_duplicate(core):
    first = _post(core, _material()).json()
    again = _post(core, _material(title="Тот же материал повторной отправкой")).json()

    assert again["created"] is False
    assert again["material"]["id"] == first["material"]["id"]
    assert len(colleague_materials.list_materials()) == 1


def test_file_stored_under_generated_name_and_downloadable(core, tmp_path):
    response = _post(core, _material(kind="file", tg_message_id=78),
                     file=("../../etc/Отчёт Q3.PDF", b"%PDF-1.4 test", "application/pdf"))

    assert response.status_code == 200, response.text
    saved = response.json()["material"]
    assert saved["has_file"] and saved["file_size"] == 13
    row = colleague_materials.get_material(saved["id"])
    assert row["file_path"].startswith(str(tmp_path / "colleague_materials"))
    assert row["file_path"].endswith(".pdf") and ".." not in row["file_path"]

    api.app.dependency_overrides[api.require_user] = lambda: {"id": 1, "role": "user"}
    download = core.get(f"/api/materials/{saved['id']}/file")
    assert download.status_code == 200 and download.content == b"%PDF-1.4 test"


@pytest.mark.parametrize("changes, file", [
    ({"kind": "link", "url": "ftp://example.com/x"}, None),
    ({"verdict": "maybe"}, None),
    ({"description": "коротко"}, None),
    ({"kind": "file"}, None),
    ({}, ("a.txt", b"x", "text/plain")),
])
def test_invalid_material_refused_with_reason(core, changes, file):
    response = _post(core, _material(**changes), file=file)

    assert response.status_code == 422
    assert response.json()["detail"]
    assert colleague_materials.list_materials() == []


def test_worker_token_required(core):
    assert _post(core, _material(), headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert core.get("/api/materials").status_code == 401


def test_screen_lists_materials_with_full_scale(core):
    _post(core, _material())
    _post(core, _material(tg_message_id=79, kind="link", url="https://example.com/news", verdict="overrated"))
    api.app.dependency_overrides[api.require_user] = lambda: {"id": 1, "role": "user"}

    body = core.get("/api/materials").json()

    assert [row["kind"] for row in body["materials"]] == ["link", "event"]
    assert [item["value"] for item in body["verdicts"]] == [value for value, _s, _f in colleague_materials.VERDICTS]
    assert len(body["verdicts"]) == 9


def test_bot_recent_list_is_per_author(core):
    _post(core, _material())
    _post(core, _material(tg_message_id=80, tg_user_id=42, tg_author="Пётр"))

    mine = core.get("/api/external-worker/materials", headers=WORKER, params={"tg_user_id": 9001}).json()

    assert [row["author"] for row in mine["materials"]] == ["Иван Иванов"]


def test_bot_history_pages_with_total_and_status(core):
    for number in range(12):
        _post(core, _material(tg_message_id=100 + number, title=f"Материал номер {number} про сделку"))
    path, user = "/api/external-worker/materials", {"tg_user_id": 9001, "limit": 10}

    first = core.get(path, headers=WORKER, params=user).json()
    rest = core.get(path, headers=WORKER, params={**user, "offset": 10}).json()

    assert (first["total"], len(first["materials"]), len(rest["materials"])) == (12, 10, 2)
    assert (first["materials"][0]["status"], first["materials"][0]["status_label"]) == ("accepted", "Принят на платформу")
    assert {row["id"] for row in first["materials"]}.isdisjoint(row["id"] for row in rest["materials"])
