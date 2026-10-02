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


# --- Второе мнение о тематике при расхождении судьи с ключами (02.10) ---------------------------

from oiltech_digest.processing.openai_client import AIResponse  # noqa: E402

CHECK_TAGS = [
    {"id": 1, "name": DRILLING, "parent_id": None, "description": "Бурение скважин, MPD, буровые установки",
     "keywords_json": ["бурение", "буровая"], "keywords_en_json": ["drilling", "mpd", "rig"]},
    {"id": 2, "name": DIGITAL, "parent_id": None, "description": "Общие цифровые платформы и промышленный edge",
     "keywords_json": ["цифровизация"], "keywords_en_json": ["edge"]},
]


class _Checker:
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer, error, []

    def complete_json(self, instructions, user_input, schema, **kwargs):
        self.calls.append({"input": user_input, "enum": schema["schema"]["properties"]["theme"]["enum"], **kwargs})
        if self.error:
            raise self.error
        return AIResponse(data=self.answer, model="gpt-5-mini-check", input_tokens=300, output_tokens=40)


def _check(monkeypatch, signal, judged, checker):
    monkeypatch.setattr(signal_discovery, "make_client", lambda offline: checker)
    snapshot = {"tags": CHECK_TAGS, "radar_themes": [DRILLING, DIGITAL], "radar_criteria": []}
    with signal_discovery.use_discovery_snapshot(snapshot):
        return signal_discovery._choose_theme(signal, {"theme": judged}, [], DRILLING, [DRILLING, DIGITAL])


NABORS = {"title_ru": "Nabors встроила автоматизированное MPD в систему буровой", "summary": "MPD на rig, drilling"}


def test_judge_theme_without_any_keyword_gets_a_second_opinion(monkeypatch):
    checker = _Checker({"theme": DRILLING, "reason": "MPD — операция бурения"})

    theme, choice = _check(monkeypatch, NABORS, DIGITAL, checker)

    assert theme == DRILLING
    assert choice["reason"] == "theme_check"
    assert choice["theme_check"]["judge_theme"] == DIGITAL and choice["theme_check"]["reason"] == "MPD — операция бурения"
    # Выбор — только из двух тематик, с их описаниями.
    assert checker.calls[0]["enum"] == [DIGITAL, DRILLING]
    assert "Бурение скважин, MPD" in checker.calls[0]["input"]


def test_second_opinion_can_confirm_the_judge(monkeypatch):
    theme, choice = _check(monkeypatch, NABORS, DIGITAL, _Checker({"theme": DIGITAL, "reason": "общая платформа"}))

    assert theme == DIGITAL
    assert choice["reason"] == "judge_confirmed"


def test_no_check_when_keywords_support_the_judge(monkeypatch):
    checker = _Checker({"theme": DIGITAL, "reason": "x"})

    theme, choice = _check(monkeypatch, NABORS, DRILLING, checker)

    assert theme == DRILLING and choice["reason"] == "judge"
    assert checker.calls == []


def test_no_check_on_a_single_passing_keyword(monkeypatch):
    checker = _Checker({"theme": DRILLING, "reason": "x"})
    signal = {"title_ru": "Платформа данных для объектов", "summary": "упоминается бурение"}

    theme, _ = _check(monkeypatch, signal, DIGITAL, checker)

    assert theme == DIGITAL
    assert checker.calls == []


def test_failed_check_keeps_the_judge_theme(monkeypatch):
    theme, choice = _check(monkeypatch, NABORS, DIGITAL, _Checker(error=RuntimeError("503")))

    assert theme == DIGITAL
    assert choice["theme_check"]["result"] == "error"


def test_check_answer_outside_the_two_themes_is_ignored(monkeypatch):
    theme, choice = _check(monkeypatch, NABORS, DIGITAL, _Checker({"theme": "Чужая тема", "reason": "x"}))

    assert theme == DIGITAL
    assert choice["theme_check"]["result"] == "error"


def test_theme_check_usage_is_counted(monkeypatch):
    usage_token = signal_discovery._USAGE.set({})
    try:
        _check(monkeypatch, NABORS, DIGITAL, _Checker({"theme": DRILLING, "reason": "MPD"}))
        rows = list(signal_discovery._USAGE.get().values())
    finally:
        signal_discovery._USAGE.reset(usage_token)

    assert rows == [{"stage": "radar_theme", "model": "gpt-5-mini-check", "calls": 1, "input_tokens": 300,
                     "output_tokens": 40, "web_search_calls": 0}]


def test_judge_instruction_assigns_theme_by_operation():
    text = signal_discovery.SIGNAL_JUDGE_INSTRUCTIONS
    assert "по ОПЕРАЦИИ нефтесервиса" in text
    assert "автоматизированное MPD и автономное направленное бурение — бурение" in text
