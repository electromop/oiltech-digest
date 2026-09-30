"""Общее для тестов пути «отметить → собрать → выгрузить» (решение владельца 29.09).

Пользователь продукта с запросами экрана, статьи и карточки радара на настоящей базе,
фикстуры выпуска, Chromium и «молчащего» хоста картинок. Сами тесты —
test_digest_user_path.py (сборка) и test_digest_user_export.py (выгрузка).
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from oiltech_digest import api, background_jobs, config, feed_window
from oiltech_digest.db import connection, repository
from oiltech_digest.processing import digest as digest_module

MSK = feed_window.MSK
SEPT = "2026-09"
THEME = "Бурение и заканчивание скважин"


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def msk(*args: int) -> datetime:
    return datetime(*args, tzinfo=MSK)


def issue_key(item: dict) -> tuple[str, int]:
    """Позиция выпуска как («article» | «signal», номер)."""
    kind = item.get("item_type") or "article"
    return kind, item["article_id"] if kind == "article" else item["signal_id"]


class Person:
    """Пользователь продукта: каждый запрос идёт от его имени (как сессия в браузере)."""

    def __init__(self, user_id: int, role: str = "user") -> None:
        self.id = user_id
        self.role = role
        self._client = TestClient(api.app)

    def _auth(self) -> None:
        api.app.dependency_overrides[api.require_user] = lambda: {
            "id": self.id, "email": f"user{self.id}@example.test", "role": self.role,
        }

    def get(self, url: str, **kwargs):
        self._auth()
        return self._client.get(url, **kwargs)

    def put(self, url: str, **kwargs):
        self._auth()
        return self._client.put(url, **kwargs)

    def patch(self, url: str, **kwargs):
        self._auth()
        return self._client.patch(url, **kwargs)

    def post(self, url: str, **kwargs):
        self._auth()
        return self._client.post(url, **kwargs)

    # --- действия экрана ---------------------------------------------------

    def mark(self, article_id: int, status: str = "digest") -> None:
        """Статус статьи в ленте: «В дайджест» или любой другой (снятие)."""
        response = self.patch(f"/api/articles/{article_id}", json={"status": status})
        assert response.status_code == 200, response.text

    def mark_signal(self, signal_id: int, selected: bool = True) -> None:
        """Кнопка «В дайджест» / «Убрать» на технологическом радаре."""
        response = self.patch(f"/api/signals/{signal_id}", json={"selected_for_digest": selected})
        assert response.status_code == 200, response.text

    def issue(self, month: str = SEPT) -> list[tuple[str, int]]:
        """Превью выпуска теми же параметрами, что шлёт конструктор по умолчанию."""
        return [issue_key(item) for item in self.issue_items(month)]

    def issue_items(self, month: str = SEPT) -> list[dict]:
        response = self.get(
            "/api/digest-content",
            params={"month": month, "limit": 500, "min_score": 0, "max_score": 100},
        )
        assert response.status_code == 200, response.text
        return response.json()["news"]

    def save(self, month: str, article_ids: list[int]):
        return self.put(
            f"/api/monthly-digests/{month}",
            json={
                "title": f"Нефтесервисный дайджест · {month}",
                "status": "draft",
                "items": [{"article_id": article_id} for article_id in article_ids],
            },
        )

    def export(self, export_format: str, month: str = SEPT) -> bytes:
        """Кнопка выгрузки: задача → воркер → скачивание, как на экране."""
        return self.download(self.start_export(export_format, month))

    def start_export(self, export_format: str, month: str = SEPT) -> int:
        response = self.post(
            "/api/jobs/digest-export",
            json={"month": month, "export_format": export_format, "limit": 500, "min_score": 0,
                  "max_score": 100, "search": "", "top_tag": ""},
        )
        assert response.status_code == 200, response.text
        job_id = response.json()["job"]["id"]
        background_jobs.run(job_id)
        job = self.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "ok", job
        return job_id

    def download(self, job_id: int) -> bytes:
        download = self.get(f"/api/jobs/{job_id}/download")
        assert download.status_code == 200, download.text
        return download.content


def add_article(conn, source_id: int, slug: str, *, published: datetime | None,
                collected: datetime | None = None, score: float | None = 70,
                image_url: str = "", title: str | None = None) -> int:
    article_id = conn.execute(
        "INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language, image_url) "
        "VALUES (%s, %s, %s, %s, %s, 'Текст материала.', 'ru', %s) RETURNING id",
        (source_id, title or f"Материал {slug}", f"https://news.example.org/{slug}", published,
         collected or published or utc(2026, 9, 15, 12), image_url or None),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO article_cards (article_id, summary, relevant) VALUES (%s, %s, TRUE)",
        (article_id, f"Суть материала {slug}."),
    )
    if score is not None:
        conn.execute(
            "INSERT INTO article_scores (article_id, model, total_score, score_label, explanation) "
            "VALUES (%s, 'offline', %s, 'Средняя', 'почему')",
            (article_id, score),
        )
    return article_id


def add_signal(key: str, *, first_seen: datetime, score: float = 75, maturity: str = "shortlist",
               evidence_published: datetime | None = None, title: str | None = None) -> int:
    signal_id = repository.upsert_signal({
        "signal_key": key, "title": title or f"Сигнал {key}", "title_ru": title or f"Сигнал {key}",
        "theme": THEME, "summary": f"Суть сигнала {key}.", "maturity": maturity, "score": score,
    })
    repository.upsert_signal_evidence(signal_id, {
        "source_url": f"https://radar.example.org/{key}", "title": f"Источник {key}",
        "publisher": "radar.example.org", "published_at": evidence_published, "strength": 0.8,
    })
    with connection.get_connection() as conn:
        conn.execute(
            "UPDATE signals SET first_seen_at = %s, last_seen_at = %s, created_at = %s WHERE id = %s",
            (first_seen, first_seen, first_seen, signal_id),
        )
        # Ссылка найдена вместе с карточкой. Часы базы не заморожены: без этого у сентябрьской
        # карточки ссылка числилась бы найденной в день прогона тестов (после 05.10 — после
        # закрытия сентября), и выбор ссылки закрытого выпуска зависел бы от даты прогона.
        conn.execute("UPDATE signal_evidence SET created_at = %s WHERE signal_id = %s", (first_seen, signal_id))
        conn.commit()
    return signal_id


def pdf_content(count: int, images: dict[int, str]) -> dict:
    """Готовый выпуск для рендера без базы: count карточек, картинки — у номеров из images."""
    news = [
        {
            "category": ("Бурение", "Добыча", "Цифровизация", "Рынок")[n % 4],
            "item_type": "article", "article_id": n + 1, "signal_id": None,
            "title": f"Материал {n + 1:02d}", "source": "World Oil",
            "url": f"https://news.example.org/{n + 1}", "published_at": "2026-09-15",
            "summary": "Краткая суть материала для карточки выпуска.",
            "image_url": images.get(n, ""),
        }
        for n in range(count)
    ]
    return {
        "month": SEPT,
        "issue": {"title": "Нефтесервисный дайджест", "intro": "Вступление.", "news_title": "Новости"},
        "hero": {}, "news": news, "items": news,
        "footer": {"contact_text": "", "contact_email": "", "note": "Информационная рассылка", "socials": []},
    }


@pytest.fixture()
def issue(isolated_db, monkeypatch, tmp_path):
    """Трое: аналитик, его коллега и админ. Часы окна — 29.09 12:00 МСК: сентябрь открыт,
    август — архив."""
    monkeypatch.setattr(feed_window, "_now", lambda: msk(2026, 9, 29, 12, 0))
    # Выгрузка — фоновой задачей, но исполняем её в тесте сами, без пула потоков.
    monkeypatch.setattr(config, "BACKGROUND_JOB_INLINE", False)
    monkeypatch.setattr(digest_module, "EXPORTS_DIR", tmp_path)
    # DOCX тянет картинки статей из сети — в тесте сети нет.
    monkeypatch.setattr(digest_module, "_fetch_docx_image", lambda url, timeout=8: None)
    analyst = int(repository.create_user("analyst@example.test", "long-enough-password", "user")["id"])
    colleague = int(repository.create_user("colleague@example.test", "long-enough-password", "user")["id"])
    admin = int(repository.create_user("admin@example.test", "long-enough-password", "admin")["id"])
    with connection.get_connection() as conn:
        source_id = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('World Oil', 'Media', 'https://news.example.org', TRUE, 'request') RETURNING id"
        ).fetchone()[0]
        articles = {
            "hi": add_article(conn, source_id, "hi", published=utc(2026, 9, 10, 12), score=88),
            "mid": add_article(conn, source_id, "mid", published=utc(2026, 9, 11, 12), score=64),
            "low": add_article(conn, source_id, "low", published=utc(2026, 9, 12, 12), score=12),
            "unscored": add_article(conn, source_id, "unscored", published=utc(2026, 9, 13, 12), score=None),
            "nodate": add_article(conn, source_id, "nodate", published=None, collected=utc(2026, 9, 16, 12), score=55),
            "aug": add_article(conn, source_id, "aug", published=utc(2026, 8, 20, 12), score=90),
        }
        conn.commit()
    signals = {
        "sep": add_signal("sep", first_seen=msk(2026, 9, 12, 0, 20), evidence_published=utc(2026, 9, 11, 9)),
    }
    yield {
        "analyst": Person(analyst), "colleague": Person(colleague), "admin": Person(admin, "admin"),
        "source": source_id, "a": articles, "s": signals,
    }
    api.app.dependency_overrides.clear()


@pytest.fixture(scope="module")
def chromium():
    """Есть ли Chromium — проверка мимо кода продукта, чтобы его поломка не выглядела пропуском."""
    probe = ("from playwright.sync_api import sync_playwright\n"
             "with sync_playwright() as pw:\n"
             "    pw.chromium.launch(headless=True, args=['--no-sandbox']).close()\n")
    try:
        result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        pytest.skip("Chromium не запустился за 60 с")
    if result.returncode != 0:
        pytest.skip(f"Chromium недоступен: {result.stderr.strip()[-200:]}")


@pytest.fixture()
def silent_image_host():
    """Хост картинки, который принимает соединение и молчит — как недоступный с ядра сайт."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    held: list[socket.socket] = []

    def accept() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            held.append(conn)  # не отвечаем и не закрываем

    threading.Thread(target=accept, daemon=True).start()
    yield f"http://127.0.0.1:{server.getsockname()[1]}"
    server.close()
    for conn in held:
        conn.close()


class _Refuse(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — имя задаёт http.server
        self.send_response(403)
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


@pytest.fixture()
def refusing_image_host():
    """Хост картинки, который сразу отвечает 403 — как сайт, закрытый для РФ-адресов."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Refuse)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
