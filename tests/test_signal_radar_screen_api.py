"""Список технологического радара для экрана — выборка на сервере, по всей базе.

Замечание заказчика 19.09: поиск радара «не очень удобен». Экран грузил 150 карточек по
баллу и искал только в них: карточка ниже 150-й по баллу поиском не находилась, «Тема» и
«Зрелость» без «Обновить» не применялись. Теперь поиск, период поступления, балл,
сортировка и страница — параметры GET /api/signals, а числа над списком —
GET /api/signals/summary с той же видимостью карточки, что и у списка.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from oiltech_digest import api
from oiltech_digest.db import repository

DRILLING = "Бурение, заканчивание и внутрискважинные работы"
ECOLOGY = "Экология, промышленная безопасность и охрана труда"


def _topics(*names):
    with repository.get_connection() as conn:
        for order, name in enumerate(names):
            conn.execute("INSERT INTO tags (name, enabled, sort_order) VALUES (%s, TRUE, %s)", (name, order))
        conn.commit()


def _card(key, title, *, theme=DRILLING, score=60, seen=None, link_title=None, publisher=None):
    signal_id = repository.upsert_signal(
        {"signal_key": key, "title": title, "title_ru": title, "theme": theme, "maturity": "watch", "score": score}
    )
    repository.upsert_signal_evidence(
        signal_id,
        {"source_url": f"https://example.com/{key}", "title": link_title or title, "publisher": publisher},
    )
    if seen:
        with repository.get_connection() as conn:
            conn.execute("UPDATE signals SET first_seen_at = %s WHERE id = %s", (seen, signal_id))
            conn.commit()
    return signal_id


@pytest.fixture()
def user(isolated_db):
    return repository.create_user("radar-screen@example.test", "long-enough-password", "user")


def _get(path, user, **params):
    api.app.dependency_overrides[api.require_user] = lambda: user
    try:
        return TestClient(api.app).get(path, params=params)
    finally:
        api.app.dependency_overrides.pop(api.require_user, None)


def _ids(response):
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()]


def test_search_finds_the_card_below_the_loaded_page_by_its_link(user):
    _card("top1", "Роботизированная буровая установка", score=90)
    _card("top2", "Цифровой двойник месторождения", score=80)
    target = _card("low", "Сейсморазведка с дронов", score=20,
                   link_title="Seismic survey by a drone swarm", publisher="Rigzone")

    assert target not in _ids(_get("/api/signals", user, limit=2))
    # Заголовок ссылки-доказательства, издатель, заголовок карточки — без учёта регистра.
    assert _ids(_get("/api/signals", user, q="Drone Swarm", limit=2)) == [target]
    assert _ids(_get("/api/signals", user, q="rigzone", limit=2)) == [target]
    assert _ids(_get("/api/signals", user, q="СЕЙСМОРАЗВЕДКА", limit=2)) == [target]
    # Номер карточки — тот, что просят в поле «ID дубля».
    assert _ids(_get("/api/signals", user, q=f"#{target}", limit=2)) == [target]
    # Символы шаблона LIKE — обычные символы: «%» не находит всё подряд.
    assert _ids(_get("/api/signals", user, q="%")) == []


def test_search_finds_the_title_people_corrected(user):
    _card("rig", "Роботизированная буровая установка", score=90)
    signal_id = _card("sat", "MethaneSAT lost contact", theme=ECOLOGY)
    repository.record_signal_feedback_event(
        None, "comment_added", signal_id=signal_id, user_id=int(user["id"]),
        corrected_title="Потеря связи с метановым спутником",
    )

    assert _ids(_get("/api/signals", user, q="метановым спутником")) == [signal_id]


def test_newest_first_sorts_by_arrival_not_by_score(user):
    old = _card("old", "Старая сильная карточка", score=95, seen=datetime(2026, 9, 10, 9, tzinfo=timezone.utc))
    new = _card("new", "Свежая карточка", score=60, seen=datetime(2026, 9, 27, 9, tzinfo=timezone.utc))
    mid = _card("mid", "Слабая карточка", score=30, seen=datetime(2026, 9, 20, 9, tzinfo=timezone.utc))

    assert _ids(_get("/api/signals", user)) == [old, new, mid]
    assert _ids(_get("/api/signals", user, sort="score_desc")) == [old, new, mid]
    assert _ids(_get("/api/signals", user, sort="date_desc")) == [new, mid, old]
    assert _ids(_get("/api/signals", user, sort="score_asc")) == [mid, new, old]


def test_pages_by_offset_cover_the_list_without_repeats(user):
    created = [_card(f"k{n}", f"Карточка {n}", score=score) for n, score in enumerate([70, 70, 70, 50, 90])]

    whole = _ids(_get("/api/signals", user, limit=5))
    pages = [_ids(_get("/api/signals", user, limit=2, offset=offset)) for offset in (0, 2, 4)]

    assert sorted(whole) == sorted(created)
    assert [row for page in pages for row in page] == whole
    assert _ids(_get("/api/signals", user, limit=2, offset=5)) == []


def test_arrival_period_is_the_moscow_date_of_the_card(user):
    # 20.09 22:30 UTC — уже 21.09 по Москве: «Поступил» на экране — по Москве.
    late = _card("late", "Поздний вечер", seen=datetime(2026, 9, 20, 22, 30, tzinfo=timezone.utc))
    early = _card("early", "Утро", seen=datetime(2026, 9, 20, 6, 0, tzinfo=timezone.utc))

    assert _ids(_get("/api/signals", user, since="2026-09-21")) == [late]
    assert _ids(_get("/api/signals", user, until="2026-09-20")) == [early]
    assert _ids(_get("/api/signals", user, since="2026-09-20", until="2026-09-20")) == [early]


def test_score_range(user):
    _card("low", "Низкий балл", score=30)
    mid = _card("mid", "Средний балл", score=55)
    _card("high", "Высокий балл", score=85)

    assert _ids(_get("/api/signals", user, min_score=40, max_score=80)) == [mid]


def test_summary_counts_the_whole_radar_and_the_selection_with_one_visibility(user):
    _topics(DRILLING, ECOLOGY)
    rig = _card("rig", "Роботизированная буровая установка", score=80)
    _card("bit", "Долото с алмазным покрытием", score=70)
    _card("sat", "Спутник MethaneSAT", theme=ECOLOGY, score=60)
    _card("early", "Ранняя карточка", theme="HSE/бурение", score=50)
    dup = _card("dup", "Роботизированная буровая — дубль", score=75)
    with repository.get_connection() as conn:
        conn.execute("UPDATE signals SET merged_into_signal_id = %s WHERE id = %s", (rig, dup))
        conn.commit()
    # Карточка без единой ссылки не видна ни в списке, ни в счётчиках.
    repository.upsert_signal({"signal_key": "empty", "title": "Без ссылок", "theme": DRILLING, "score": 99})

    summary = _get("/api/signals/summary", user)
    assert summary.status_code == 200, summary.text
    payload = summary.json()
    assert (payload["total"], payload["matching"]) == (4, 4)
    # В фильтре тем — только тематики заказчика; ранняя карточка со свободной темой — нет.
    assert payload["themes"] == [{"theme": DRILLING, "count": 2}, {"theme": ECOLOGY, "count": 1}]

    filtered = _get("/api/signals/summary", user, q="буровая").json()
    assert (filtered["total"], filtered["matching"]) == (4, 1)
    # Тот же поиск в списке — та же выборка: число над списком не расходится с ним.
    assert _ids(_get("/api/signals", user, q="буровая")) == [rig]
    assert _get("/api/signals/summary", user, theme=ECOLOGY).json()["matching"] == 1


@pytest.mark.parametrize(
    "params",
    [{"sort": "random"}, {"since": "2026-13-45"}, {"offset": -1}, {"min_score": 101}, {"q": "x" * 201}],
)
def test_bad_screen_params_are_rejected(user, params):
    assert _get("/api/signals", user, **params).status_code == 422
