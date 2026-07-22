from oiltech_digest import backlog


SAMPLE_BACKLOG = """# Бэклог

## 🔜 В работе и план (по приоритету)

| # | Приоритет | Задача | Статус | Обновлено |
|---|:---:|---|:---:|---|
| 1 | **P1** | Первая задача | 🆕 | 2026-06-20 |

## 🛠 Технический долг и баги (аудит 2026-06-29)

| # | Приоритет | Проблема | Где | Что сделать | Статус |
|---|:---:|---|---|---|:---:|
| T1 | **P2 🟡** | Проблема | api.py | Исправить | 🆕 |

## 📥 Входящие — пишите сюда

| Дата | Автор | Комментарий / пожелание | Статус |
|---|---|---|:---:|
| 2026-06-21 | user@example.com | Идея | 🆕 |
"""


def test_backlog_create_and_update_sync_markdown(tmp_path, monkeypatch):
    path = tmp_path / "BACKLOG.md"
    path.write_text(SAMPLE_BACKLOG, encoding="utf-8")
    monkeypatch.setattr(backlog, "BACKLOG_PATH", path)

    created = backlog.create_plan_task("Новая задача | с пайпом", priority="P2")
    updated = backlog.update_task_status(created["id"], "in_progress")
    payload = backlog.read_backlog()

    assert created["id"] == "2"
    assert updated["status"] == "in_progress"
    assert any(task["title"] == "Новая задача / с пайпом" for task in payload["tasks"])
    assert "| 2 | **P2** | Новая задача / с пайпом | 🔵 |" in path.read_text(encoding="utf-8")
