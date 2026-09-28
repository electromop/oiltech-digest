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


def test_run_discovery_writes_content_theme_and_explains_it(monkeypatch):
    page = {"source_url": "https://example.com/drones", "title": "AI drone fleet expands",
            "extracted_fact": "Operator expands drone fleet with machine learning analytics."}
    monkeypatch.setattr(
        signal_discovery,
        "_search_web_evidence",
        lambda topic, config, **kwargs: {"status": "ok", "queries": ["q"], "results": 1, "evidence": [page]},
    )
    monkeypatch.setattr(signal_discovery, "_has_industry_context", lambda row: True)
    monkeypatch.setattr(
        signal_discovery,
        "judge_signal_snapshot",
        lambda cluster, topic, offline=True: (
            {"title": "Drone fleet with machine learning", "title_ru": "Флот дронов с ИИ", "theme": topic,
             "summary": "Дрон и искусственный интеллект для осмотра.", "maturity": "watch", "score": 60},
            {"theme": topic},
        ),
    )
    config = signal_discovery.SignalDiscoveryConfig(offline=True, dry_run=True, web_only=True)
    snapshot = {"topics": [{"name": DRILLING, "tag_id": 1}, {"name": DIGITAL, "tag_id": 2}], "tags": TAGS}

    run = signal_discovery.run_discovery(config, snapshot)

    drilling_topic = run["topics"][0]
    candidate = drilling_topic["candidates"][0]
    assert drilling_topic["topic"] == DRILLING
    assert candidate["signal"]["theme"] == DIGITAL
    assert candidate["raw_output"]["theme_choice"]["reason"] == "content"
    assert candidate["raw_output"]["theme_choice"]["search_topic"] == DRILLING


def test_free_text_topics_keep_model_theme(monkeypatch):
    # Режим таблицы тем (без tag_id): тема не подменяется ни запросом, ни содержанием.
    monkeypatch.setattr(
        signal_discovery,
        "_search_web_evidence",
        lambda topic, config, **kwargs: {"status": "ok", "queries": ["q"], "results": 1,
                                         "evidence": [{"source_url": "https://example.com/x", "title": "AI drone"}]},
    )
    monkeypatch.setattr(
        signal_discovery,
        "judge_signal_snapshot",
        lambda cluster, topic, offline=True: ({"title": "AI drone", "theme": "Своя тема модели", "score": 60}, {}),
    )
    config = signal_discovery.SignalDiscoveryConfig(offline=True, dry_run=True, web_only=True)

    run = signal_discovery.run_discovery(config, {"topics": [{"name": DRILLING}], "tags": TAGS})

    candidate = run["topics"][0]["candidates"][0]
    assert candidate["signal"]["theme"] == "Своя тема модели"
    assert "theme_choice" not in candidate["raw_output"]
