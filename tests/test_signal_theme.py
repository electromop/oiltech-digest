"""Тема карточки радара — по содержанию, а не по запросу (замечание заказчика 22.09).

«Бурение» у карточки про ИИ и дроны: тема была темой поиска, которым нашли статью.
"""

from oiltech_digest import signal_discovery

DRILLING = "Бурение и заканчивание скважин"
DIGITAL = "Цифровизация и ИИ"
ENERGY = "Энергетика"

TAGS = [
    {"id": 1, "name": DRILLING, "parent_id": None,
     "keywords_json": ["бурение", "буровая"], "keywords_en_json": ["drilling", "rig"]},
    {"id": 11, "name": "Роботизация бурения", "parent_id": 1,
     "keywords_json": ["роботизированная буровая"], "keywords_en_json": ["robotic drilling"]},
    {"id": 2, "name": DIGITAL, "parent_id": None,
     "keywords_json": ["искусственный интеллект", "дрон"], "keywords_en_json": ["machine learning", "drone", "ai"]},
    {"id": 3, "name": ENERGY, "parent_id": None,
     "keywords_json": ["электроэнергия"], "keywords_en_json": ["power grid"]},
]
TOPICS = [DRILLING, DIGITAL, ENERGY]


def _choose(signal, cluster=(), search_topic=DRILLING):
    token = signal_discovery._TAGS_SNAPSHOT.set(TAGS)
    try:
        return signal_discovery._content_theme(signal, list(cluster), search_topic, TOPICS)
    finally:
        signal_discovery._TAGS_SNAPSHOT.reset(token)


def test_card_about_ai_and_drones_moves_to_its_own_topic():
    theme, choice = _choose({
        "title": "Drone inspections with machine learning cut downtime",
        "summary": "Оператор применил дрон и искусственный интеллект для осмотра объектов.",
    })

    assert theme == DIGITAL
    assert choice["reason"] == "content"
    assert choice["search_topic"] == DRILLING
    assert choice["hits"][DIGITAL] >= 2


def test_single_passing_mention_does_not_move_the_card():
    # Одно совпадение чужой тематики — не повод: тема поиска остаётся.
    theme, choice = _choose({"title": "Drone survey of the site", "summary": "Кратко о проекте."})

    assert theme == DRILLING
    assert choice["reason"] == "no_keywords"


def test_search_topic_stays_when_content_confirms_it():
    theme, choice = _choose({
        "title": "Robotic drilling rig deployed",
        "summary": "Роботизированная буровая работает с дроном для осмотра.",
    })

    assert theme == DRILLING
    assert choice["reason"] == "search_topic_confirmed"


def test_tie_keeps_search_topic():
    theme, _ = _choose({"title": "Drilling rig uses drone and machine learning"})

    assert theme == DRILLING


def test_evidence_titles_count_too():
    theme, _ = _choose(
        {"title": "Новый подход к обслуживанию"},
        cluster=[{"title": "Machine learning model for power grid"}, {"title": "AI drone fleet expands"}],
        search_topic=ENERGY,
    )

    assert theme == DIGITAL


def _run(monkeypatch, topics, page):
    """Прогон радара без базы: поиск темы приносит одну страницу, дальше — путь находки до «Отбора»."""
    monkeypatch.setattr(signal_discovery, "_topic_web_evidence", lambda topic, topic_name, config, **kwargs: {
        "status": "ok", "provider": "openai_web_search", "research": {}, "evidence": [page]})
    config = signal_discovery.SignalDiscoveryConfig(offline=True, dry_run=True)
    return signal_discovery.run_discovery(config, {"topics": topics, "tags": TAGS})


def test_finding_of_a_search_topic_gets_the_theme_of_its_content(monkeypatch):
    """Находку про дроны и ИИ принёс поиск темы бурения: тема находки — по ключам тематик, а не тема поиска (тему из
    13 потом ставит «Карточка», #132)."""
    page = {"source_url": "https://example.com/drones", "title": "AI drone fleet expands with machine learning",
            "extracted_fact": "Operator expands drone fleet with machine learning analytics.", "strength": 0.9}

    run = _run(monkeypatch, [{"name": DRILLING, "tag_id": 1}, {"name": DIGITAL, "tag_id": 2}], page)

    drilling_topic = run["topics"][0]
    [candidate] = drilling_topic["candidates"]
    assert drilling_topic["topic"] == DRILLING
    assert candidate["signal"]["theme"] == DIGITAL
    assert candidate["signal"]["title"] == page["title"]


def test_finding_of_a_free_text_topic_keeps_the_search_topic(monkeypatch):
    # Режим таблицы тем (без tag_id): тему ключами тематик не подменяем.
    page = {"source_url": "https://example.com/x", "title": "AI drone with machine learning"}

    run = _run(monkeypatch, [{"name": "Своя тема таблицы"}], page)

    [candidate] = run["topics"][0]["candidates"]
    assert candidate["signal"]["theme"] == "Своя тема таблицы"
