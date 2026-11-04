"""Описание Компании для агента «Гипотеза для Компании» (#162): экран в «Настройках», устроенный как «Дочерние
общества». Текст, кто и когда правил; читает любая допущенная учётка, правят права как у тегов (ADR 0004). Текста
описания нет в репозитории: тест берёт заглушку.
"""

from __future__ import annotations

from oiltech_digest.db import repository
from tests.digest_user_path_support import Person

STUB = "Заглушка описания для тестов: бурение, ТКРС, геологоразведка, промышленная безопасность."


def test_anyone_reads_the_description_admins_write_it_and_the_screen_shows_who_and_when(isolated_db):
    reader = Person(int(repository.create_user("reader@example.test", "long-enough-password", "user")["id"]))
    admin = Person(int(repository.create_user("editor@example.test", "long-enough-password", "admin")["id"]), "admin")
    try:
        empty = reader.get("/api/company-description").json()
        assert (empty["text"], empty["updated_by"], empty["history"]) == ("", None, [])

        assert reader.put("/api/company-description", json={"text": STUB}).status_code == 403
        saved = admin.put("/api/company-description", json={"text": f"  {STUB}  "})
        assert saved.status_code == 200, saved.text

        shown = reader.get("/api/company-description").json()
        assert (shown["text"], shown["updated_by"]) == (STUB, "editor@example.test")
        assert shown["updated_at"].startswith("20")

        admin.put("/api/company-description", json={"text": STUB + " Без внутренних проектов."})
        history = reader.get("/api/company-description").json()["history"]
        assert [(row["updated_by"], row["chars"]) for row in history] == [
            ("editor@example.test", len(STUB) + 25), ("editor@example.test", len(STUB))]
    finally:
        from oiltech_digest import api

        api.app.dependency_overrides.clear()
