"""Сентябрьский выпуск собирают сами пользователи (решение владельца 29.09).

Путь «отметить → собрать → выгрузить» обычным пользователем, на настоящей базе и через
те же запросы, что шлёт экран: отметка в ленте и на радаре, конструктор выпуска
(месяц, лимит 500, «Оценка от 0 до 100»), черновик, выгрузка фоновой задачей и скачивание.
Каждый тест — один риск пути; тексты тестов называют риск, а не механику.

Часы окна ленты заморожены на 29.09 12:00 МСК: сентябрь открыт, август — архив.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from oiltech_digest import api, background_jobs, config, feed_window
from oiltech_digest.db import connection, repository
from oiltech_digest.processing import digest as digest_module

MSK = feed_window.MSK
SEPT = "2026-09"
THEME = "Бурение и заканчивание скважин"


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _msk(*args: int) -> datetime:
    return datetime(*args, tzinfo=MSK)


class _Person:
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
        return [_key(item) for item in self.issue_items(month)]

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
        download = self.get(f"/api/jobs/{job_id}/download")
        assert download.status_code == 200, download.text
        return download.content


def _key(item: dict) -> tuple[str, int]:
    kind = item.get("item_type") or "article"
    return kind, item["article_id"] if kind == "article" else item["signal_id"]


def _article(conn, source_id: int, slug: str, *, published: datetime | None,
             collected: datetime | None = None, score: float | None = 70,
             image_url: str = "", title: str | None = None) -> int:
    article_id = conn.execute(
        "INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language, image_url) "
        "VALUES (%s, %s, %s, %s, %s, 'Текст материала.', 'ru', %s) RETURNING id",
        (source_id, title or f"Материал {slug}", f"https://news.example.org/{slug}", published,
         collected or published or _utc(2026, 9, 15, 12), image_url or None),
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


def _signal(key: str, *, first_seen: datetime, score: float = 75, maturity: str = "shortlist",
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
        conn.commit()
    return signal_id


@pytest.fixture()
def issue(isolated_db, monkeypatch, tmp_path):
    """Трое: аналитик, его коллега и админ. Сентябрь открыт (29.09), август — архив."""
    monkeypatch.setattr(feed_window, "_now", lambda: _msk(2026, 9, 29, 12, 0))
    # Выгрузка — фоновой задачей, но исполняем её в тесте сами, без пула потоков.
    monkeypatch.setattr(config, "BACKGROUND_JOB_INLINE", False)
    monkeypatch.setattr(digest_module, "EXPORTS_DIR", tmp_path)
    # DOCX тянет картинки статей из сети — в тесте сети нет.
    monkeypatch.setattr(digest_module, "_fetch_docx_image", lambda url: None)
    analyst = int(repository.create_user("analyst@example.test", "long-enough-password", "user")["id"])
    colleague = int(repository.create_user("colleague@example.test", "long-enough-password", "user")["id"])
    admin = int(repository.create_user("admin@example.test", "long-enough-password", "admin")["id"])
    with connection.get_connection() as conn:
        source_id = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('World Oil', 'Media', 'https://news.example.org', TRUE, 'request') RETURNING id"
        ).fetchone()[0]
        articles = {
            "hi": _article(conn, source_id, "hi", published=_utc(2026, 9, 10, 12), score=88),
            "mid": _article(conn, source_id, "mid", published=_utc(2026, 9, 11, 12), score=64),
            "low": _article(conn, source_id, "low", published=_utc(2026, 9, 12, 12), score=12),
            "unscored": _article(conn, source_id, "unscored", published=_utc(2026, 9, 13, 12), score=None),
            "nodate": _article(conn, source_id, "nodate", published=None, collected=_utc(2026, 9, 16, 12), score=55),
            "aug": _article(conn, source_id, "aug", published=_utc(2026, 8, 20, 12), score=90),
        }
        conn.commit()
    signals = {
        "sep": _signal("sep", first_seen=_msk(2026, 9, 12, 0, 20), evidence_published=_utc(2026, 9, 11, 9)),
    }
    yield {
        "analyst": _Person(analyst), "colleague": _Person(colleague), "admin": _Person(admin, "admin"),
        "source": source_id, "a": articles, "s": signals,
    }
    api.app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
#  1. Отметка в ленте и на радаре обычным пользователем
# ---------------------------------------------------------------------------

def test_marks_from_feed_and_radar_reach_the_issue_and_unmarking_takes_them_out(issue):
    analyst, a, s = issue["analyst"], issue["a"], issue["s"]
    assert analyst.issue() == []

    analyst.mark(a["hi"])
    analyst.mark_signal(s["sep"])
    # Смешанный выпуск: статья (88) выше сигнала (75) — порядок по баллу, пока нет черновика.
    assert analyst.issue() == [("article", a["hi"]), ("signal", s["sep"])]

    # Снятие: «Убрать» на радаре и другой статус в ленте.
    analyst.mark_signal(s["sep"], selected=False)
    analyst.mark(a["hi"], status="new")
    assert analyst.issue() == []


# ---------------------------------------------------------------------------
#  2. Кандидаты — только своё; черновик — свой (п. 5: изоляция, админ — как в коде)
# ---------------------------------------------------------------------------

def test_each_person_sees_and_saves_only_own_issue_admin_included(issue):
    analyst, colleague, admin, a = issue["analyst"], issue["colleague"], issue["admin"], issue["a"]
    analyst.mark(a["hi"])
    colleague.mark(a["mid"])
    admin.mark(a["low"])

    assert analyst.issue() == [("article", a["hi"])]
    assert colleague.issue() == [("article", a["mid"])]
    # Админ собирает свой выпуск по тем же правилам: чужие отметки ему не видны.
    assert admin.issue() == [("article", a["low"])]

    assert analyst.save(SEPT, [a["hi"]]).status_code == 200
    # Чужой черновик не открывается ни коллеге, ни админу: у каждого свой выпуск за месяц.
    assert colleague.get(f"/api/monthly-digests/{SEPT}").status_code == 404
    assert admin.get(f"/api/monthly-digests/{SEPT}").status_code == 404

    # Сохранение коллеги пишет его черновик и не трогает черновик аналитика.
    assert colleague.save(SEPT, [a["mid"]]).status_code == 200
    mine = analyst.get(f"/api/monthly-digests/{SEPT}").json()
    assert [item["article_id"] for item in mine["items"]] == [a["hi"]]
    assert mine["user_id"] == analyst.id
    assert analyst.issue() == [("article", a["hi"])]
    assert colleague.issue() == [("article", a["mid"])]


# ---------------------------------------------------------------------------
#  3. Черновик: порядок, «Из выпуска», повторное открытие
# ---------------------------------------------------------------------------

def test_saved_issue_keeps_manual_order_and_removal_after_reopening(issue):
    analyst, a = issue["analyst"], issue["a"]
    for key in ("hi", "mid", "low"):
        analyst.mark(a[key])
    assert analyst.issue() == [("article", a["hi"]), ("article", a["mid"]), ("article", a["low"])]

    # Ручной порядок и «Из выпуска» для mid: отметка «в дайджест» у mid остаётся.
    assert analyst.save(SEPT, [a["low"], a["hi"]]).status_code == 200

    reopened = analyst.get(f"/api/monthly-digests/{SEPT}").json()
    assert [item["article_id"] for item in reopened["items"]] == [a["low"], a["hi"]]
    assert analyst.issue() == [("article", a["low"]), ("article", a["hi"])]
    still_marked = {row["id"] for row in analyst.get("/api/articles", params={"status": "digest", "limit": 5000}).json()}
    assert a["mid"] in still_marked


# ---------------------------------------------------------------------------
#  4. Смешанный выпуск, только сигналы, только статьи
# ---------------------------------------------------------------------------

def test_issue_of_only_signals_then_mixed_with_and_without_a_draft(issue):
    analyst, a, s = issue["analyst"], issue["a"], issue["s"]
    analyst.mark_signal(s["sep"])
    assert analyst.issue() == [("signal", s["sep"])]

    # «Сохранить draft», когда в очереди статей нет: сигналы из выпуска не пропадают.
    assert analyst.save(SEPT, []).status_code == 200
    assert analyst.issue() == [("signal", s["sep"])]

    analyst.mark(a["low"])
    assert analyst.issue() == [("signal", s["sep"]), ("article", a["low"])]

    # Черновик хранит статьи; выбранные сигналы идут за ними.
    assert analyst.save(SEPT, [a["low"]]).status_code == 200
    assert analyst.issue() == [("article", a["low"]), ("signal", s["sep"])]


def test_issue_of_only_articles_without_date_and_image(issue):
    analyst, a = issue["analyst"], issue["a"]
    analyst.mark(a["nodate"])

    [item] = analyst.issue_items()
    # Без даты публикации статья относится к месяцу сбора и выходит без даты в карточке.
    assert item["article_id"] == a["nodate"] and item["published_at"] is None
    html = analyst.get("/api/digest-email", params={"month": SEPT, "limit": 500, "min_score": 0}).text
    assert "Материал nodate" in html
    assert 'class="news-card-meta">World Oil</div>' in html
    # Без картинки — плашка рубрики, а не пустое место.
    assert 'class="news-card-image" src="data:image/svg+xml;base64,' in html


# ---------------------------------------------------------------------------
#  5. Правки заголовка и сути из ОС коллег — в выпуске (накладываются при чтении, #77)
# ---------------------------------------------------------------------------

def test_colleague_corrections_of_a_radar_card_reach_the_issue(issue):
    analyst, colleague, s = issue["analyst"], issue["colleague"], issue["s"]
    analyst.mark_signal(s["sep"])

    for person, title, thesis in (
        (colleague, "Правленый заголовок коллеги", "Правленая суть коллеги."),
        (analyst, "Последняя правка заголовка", "Последняя правка сути."),
    ):
        response = person.post("/api/signals/feedback", json={
            "signal_id": s["sep"], "corrected_title": title, "corrected_thesis": thesis,
        })
        assert response.status_code == 200, response.text

    [item] = analyst.issue_items()
    # Последнее слово — у последней правки, чья бы она ни была.
    assert item["title"] == "Последняя правка заголовка"
    assert item["summary"] == "Последняя правка сути."


def test_human_duplicate_verdict_does_not_take_a_chosen_card_out_of_the_issue(issue):
    analyst, colleague, s = issue["analyst"], issue["colleague"], issue["s"]
    main = _signal("main", first_seen=_msk(2026, 9, 14, 0, 20), evidence_published=_utc(2026, 9, 13, 9))
    analyst.mark_signal(s["sep"])

    response = colleague.post("/api/signals/feedback", json={
        "signal_id": s["sep"], "verdict": "merge_duplicate", "duplicate_of_signal_id": main,
    })
    assert response.status_code == 200, response.text
    assert response.json()["merged"] is False

    assert analyst.issue() == [("signal", s["sep"])]


# ---------------------------------------------------------------------------
#  6. Выгрузка: 45 позиций — HTML и DOCX; превью = выгрузка
# ---------------------------------------------------------------------------

def test_export_of_a_45_item_issue_in_html_and_docx_matches_the_preview(issue):
    analyst = issue["analyst"]
    with connection.get_connection() as conn:
        big = [
            # Номер с нулём: «big-1» не должен находиться внутри «big-10».
            _article(
                conn, issue["source"], f"big-{n:02d}",
                published=None if n % 10 == 0 else _utc(2026, 9, 1 + n % 28, 12),
                collected=_utc(2026, 9, 1 + n % 28, 13),
                score=None if n % 7 == 0 else 20 + n,
                image_url="" if n % 3 else f"https://img.example.org/{n}.jpg",
            )
            for n in range(40)
        ]
        conn.commit()
    signals = [
        _signal(f"big-{n}", first_seen=_msk(2026, 9, 2 + n, 0, 20), score=30 + n,
                evidence_published=None if n % 2 else _utc(2026, 9, 1 + n, 9))
        for n in range(5)
    ]
    for article_id in big:
        analyst.mark(article_id)
    for signal_id in signals:
        analyst.mark_signal(signal_id)

    preview = analyst.issue_items()
    assert len(preview) == 45
    assert {_key(item) for item in preview} == {("article", i) for i in big} | {("signal", i) for i in signals}

    html = analyst.export("html").decode("utf-8")
    assert html.count('class="news-card"') == 45
    for item in preview:
        assert item["title"] in html
    # Chromium выбрасывает из PDF относительные ссылки (урок выпуска за август): у каждой
    # карточки ссылка «Читать далее» — абсолютная.
    links = re.findall(r'<a href="([^"]*)" style="color:#e83d08', html)
    assert len(links) == 45 and all(link.startswith("https://") for link in links)
    # Порядок выгрузки — порядок превью (тот же сборщик).
    positions = [html.index(item["title"]) for item in preview]
    assert positions == sorted(positions)

    docx = analyst.export("docx")
    with ZipFile(BytesIO(docx)) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")
    for item in preview:
        assert item["title"] in document_xml


# ---------------------------------------------------------------------------
#  Дефект: снятая отметка оставалась в сохранённом выпуске
# ---------------------------------------------------------------------------

def test_unmarked_article_leaves_the_saved_issue_without_resaving(issue):
    """«Из дайджеста» на экране выпуска (статус archive) или другой статус в ленте: статья
    пропадала из очереди, экран не показывал несохранённых правок — а превью и выгрузка
    брали её из сохранённого черновика, пока человек не пересохранит его сам."""
    analyst, a = issue["analyst"], issue["a"]
    for key in ("hi", "mid", "low"):
        analyst.mark(a[key])
    assert analyst.save(SEPT, [a["low"], a["mid"], a["hi"]]).status_code == 200

    analyst.mark(a["mid"], status="archive")  # «Из дайджеста» на экране выпуска
    analyst.mark(a["hi"], status="noise")     # передумал в ленте

    assert analyst.issue() == [("article", a["low"])]
    html = analyst.export("html").decode("utf-8")
    assert "Материал low" in html
    assert "Материал mid" not in html and "Материал hi" not in html

    # Снова отмечена — возвращается на своё место в черновике.
    analyst.mark(a["mid"])
    assert analyst.issue() == [("article", a["low"]), ("article", a["mid"])]


# ---------------------------------------------------------------------------
#  Дефект: оценка модели («Отклонено») уносила выбранную человеком карточку радара
# ---------------------------------------------------------------------------

def test_radar_card_rated_reject_by_the_model_stays_in_the_issue_it_was_chosen_for(issue):
    """Экран радара показывает и карточки со зрелостью «Отклонено» — с кнопкой «В дайджест»;
    повторная находка перезаписывает зрелость и балл карточки. Сборщик выпуска такие
    карточки отбрасывал: радар отвечал «Сигнал добавлен в дайджест», а в выпуске его не было.
    Отметка человека — членство в выпуске, оценка модели — только порядок."""
    analyst, s = issue["analyst"], issue["s"]
    weak = _signal("weak", first_seen=_msk(2026, 9, 15, 0, 20), maturity="reject", score=18,
                   evidence_published=_utc(2026, 9, 14, 9))
    analyst.mark_signal(weak)
    analyst.mark_signal(s["sep"])

    # Повторная находка выбранной карточки: модель теперь считает её слабой.
    repository.upsert_signal({
        "signal_key": "sep", "title": "Сигнал sep", "title_ru": "Сигнал sep", "theme": THEME,
        "summary": "Суть сигнала sep.", "maturity": "reject", "score": 4,
    })

    assert analyst.issue() == [("signal", weak), ("signal", s["sep"])]


# ---------------------------------------------------------------------------
#  Дефект: месяц карточки радара в выпуске «плыл» с каждой повторной находкой
# ---------------------------------------------------------------------------

def test_radar_card_belongs_to_the_issue_of_the_month_it_arrived_in(issue):
    """Месяц сигнала в выпуске считался по дате лучшей ссылки, а без даты — по last_seen_at,
    который сдвигает каждая повторная находка (touch_signal, upsert_signal). Ежедневный
    радар 1–4 октября уносил выбранную в сентябре карточку в октябрьский выпуск, а карточку
    со старой ссылкой (найдена в сентябре, статья августовская) сентябрьский выпуск не
    видел вовсе. Месяц карточки — месяц поступления на радар по Москве: эту дату и
    показывает экран радара («дата поступления»)."""
    analyst = issue["analyst"]
    undated = _signal("undated", first_seen=_msk(2026, 9, 20, 0, 15))
    old_link = _signal("old-link", first_seen=_msk(2026, 9, 3, 0, 15), evidence_published=_utc(2026, 8, 28, 9))
    # Прогон радара 01.10 в 00:15 МСК — по UTC это ещё 30.09, а экран показывает 01.10.2026.
    october = _signal("october", first_seen=_msk(2026, 10, 1, 0, 15))
    for signal_id in (undated, old_link, october):
        analyst.mark_signal(signal_id)
    # Радар 02.10 снова нашёл сентябрьскую карточку.
    with connection.get_connection() as conn:
        conn.execute("UPDATE signals SET last_seen_at = %s WHERE id = %s", (_msk(2026, 10, 2, 0, 15), undated))
        conn.commit()

    september = set(analyst.issue(SEPT))
    assert {("signal", undated), ("signal", old_link)} <= september
    assert ("signal", october) not in september
    assert analyst.issue("2026-10") == [("signal", october)]
    assert analyst.issue("2026-08") == []


# ---------------------------------------------------------------------------
#  Балл (п. 3): владелец 29.09 меняет критерии и пересчитывает сентябрь
# ---------------------------------------------------------------------------

def test_new_scores_do_not_take_marked_items_out_of_the_issue(issue):
    """Отметка человека не должна пропадать из выпуска из-за нового балла: ни в превью и
    выгрузке, ни в черновике, сохранённом любым путём API. До 29.09 POST /api/monthly-digests
    по умолчанию сохранял черновик с полом 60 — после пересчёта из него выпадало бы всё,
    что опустилось ниже."""
    analyst, a, s = issue["analyst"], issue["a"], issue["s"]
    marked = [a["hi"], a["mid"], a["low"], a["unscored"]]
    for article_id in marked:
        analyst.mark(article_id)
    analyst.mark_signal(s["sep"])
    expected = {("article", article_id) for article_id in marked} | {("signal", s["sep"])}
    assert set(analyst.issue()) == expected

    # Пересчёт: баллы сентября упали до нуля, у одной статьи строки балла пока нет,
    # сигнал при повторной находке получил 3.
    with connection.get_connection() as conn:
        conn.execute("UPDATE article_scores SET total_score = 0, score_label = 'Низкая'")
        conn.execute("DELETE FROM article_scores WHERE article_id = %s", (a["hi"],))
        conn.commit()
    repository.upsert_signal({
        "signal_key": "sep", "title": "Сигнал sep", "title_ru": "Сигнал sep", "theme": THEME,
        "summary": "Суть сигнала sep.", "maturity": "watch", "score": 3,
    })

    assert set(analyst.issue()) == expected
    html = analyst.export("html").decode("utf-8")
    for title in ("Материал hi", "Материал mid", "Материал low", "Материал unscored", "Сигнал sep"):
        assert title in html

    # Черновик через POST с умолчаниями API — без «Оценки от».
    response = analyst.post("/api/monthly-digests", json={"month": SEPT})
    assert response.status_code == 200, response.text
    saved = analyst.get(f"/api/monthly-digests/{SEPT}").json()
    assert {item["article_id"] for item in saved["items"]} == set(marked)
    assert set(analyst.issue()) == expected
