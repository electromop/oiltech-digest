from oiltech_digest.ingestion import relevance_filter
from oiltech_digest.ingestion.relevance_filter import should_keep_article


def test_prefilter_rejects_sports_noise_without_domain_signal():
    result = should_keep_article(
        'Гандболистки ЦСКА победили "Ростов-Дон" в третьем матче финала Суперлиги',
        "Игра завершилась со счетом 30:28.",
        {"name": "Интерфакс ТЭК", "category": ""},
    )

    assert result.keep is False
    assert "гандбол" in result.matched_noise


def test_prefilter_rejects_generic_airport_drone_news():
    result = should_keep_article(
        "Нижегородский аэропорт приостановил работу после атаки БПЛА",
        "Рейсы временно задержаны, пострадавших нет.",
        {"name": "Интерфакс ТЭК", "category": ""},
    )

    assert result.keep is False


def test_prefilter_does_not_match_ru_short_ai_inside_words():
    result = should_keep_article(
        "Временные ограничения сняты в аэропорту Пулково",
        "Рейсы выполняются по расписанию.",
        {"name": "Интерфакс ТЭК", "category": ""},
    )

    assert result.keep is False


def test_prefilter_keeps_drone_attack_on_refinery():
    result = should_keep_article(
        "Атака БПЛА на НПЗ привела к остановке установки переработки нефти",
        "Компания оценивает влияние на поставки топлива и ремонт промышленного оборудования.",
        {"name": "Интерфакс ТЭК", "category": ""},
    )

    assert result.keep is True
    assert any(match in result.matched_keywords for match in ("нпз", "нефт", "переработк"))


def test_prefilter_keeps_industrial_energy_adjacent_news():
    result = should_keep_article(
        "СИБУР и Росавтодор расширят применение синтетических материалов",
        "Проект связан с нефтехимией, дорожной инфраструктурой и промышленным производством.",
        {"name": "EnergyLand", "category": "энергетика"},
    )

    assert result.keep is True


def test_prefilter_does_not_match_english_noise_inside_words():
    result = should_keep_article(
        "From Paper Chaos to Control: Solving the Hidden Risks on the Factory Floor",
        "A manufacturing automation article about industrial process control.",
        {"name": "Automation World", "category": ""},
    )

    assert result.keep is True
    assert "actor" not in result.matched_noise


def test_prefilter_does_not_match_ru_noise_inside_words(monkeypatch):
    """Слово шума совпадает с начала слова: «спорт» внутри «транспорта», «актер» внутри
    «охарактеризовали» статью не отсеивают (АНГИ 04.11: так терялись «зимники Ямала»).
    Тематики заказчика выключены: их кэш от соседних тестов не должен менять вердикт."""
    monkeypatch.setattr(relevance_filter, "tag_keywords", lambda: ((), ()))
    winter_roads = should_keep_article(
        "В ЯНАО готовят к открытию три зимника общей протяженностью 614 км",
        "Сезонные дороги нужны для транспорта грузов на удалённые участки.",
        {"name": "Агентство нефтегазовой информации (сайт)", "category": ""},
    )
    material = should_keep_article("Учёные охарактеризовали новый материал для мембран", "")

    assert winter_roads.keep is True
    assert "спорт" not in winter_roads.matched_noise
    assert material.keep is True
    assert "актер" not in material.matched_noise


def test_prefilter_still_rejects_ru_noise_at_word_start(monkeypatch):
    """Основа шума с начала слова по-прежнему отсеивает: окончание любое."""
    monkeypatch.setattr(relevance_filter, "tag_keywords", lambda: ((), ()))
    result = should_keep_article("Итоги спортивного сезона и новый состав сборной", "")

    assert result.keep is False
    assert "спорт" in result.matched_noise


def test_prefilter_uses_customer_tag_keywords(monkeypatch):
    """Ключевые слова тематик заказчика участвуют в предфильтре.

    Раньше словарь был статическим: правки заказчика на отбор не влияли.
    """
    from oiltech_digest.db import repository

    relevance_filter._TAG_KEYWORDS_CACHE.update({"positive": (), "negative": (), "at": 0.0})
    monkeypatch.setattr(
        repository, "list_enabled_tags",
        lambda: [{"keywords_json": ["телеметрия"], "keywords_en_json": [],
                  "negative_keywords_json": []}],
    )
    result = relevance_filter.should_keep_article("Новая телеметрия на объекте", "")
    assert result.keep is True
    assert "телеметрия" in result.matched_keywords


def test_prefilter_ignores_too_short_tag_keywords(monkeypatch):
    """Короткие ключи («ГРП», «AI») отбрасываются: на нормализованном тексте они дают
    ложные совпадения чаще, чем пользу."""
    from oiltech_digest.db import repository

    relevance_filter._TAG_KEYWORDS_CACHE.update({"positive": (), "negative": (), "at": 0.0})
    monkeypatch.setattr(
        repository, "list_enabled_tags",
        lambda: [{"keywords_json": ["ГРП"], "keywords_en_json": ["AI"],
                  "negative_keywords_json": []}],
    )
    positive, _ = relevance_filter.tag_keywords()
    assert positive == ()


def test_prefilter_stop_words_block_article(monkeypatch):
    """Стоп-слова тематик реально отбивают статью это то новое, что теги дают
    предфильтру (он разрешительный, позитивные слова меняют исход редко)."""
    from oiltech_digest.db import repository

    relevance_filter._TAG_KEYWORDS_CACHE.update({"positive": (), "negative": (), "at": 0.0})
    monkeypatch.setattr(
        repository, "list_enabled_tags",
        lambda: [{"keywords_json": [], "keywords_en_json": [],
                  "negative_keywords_json": ["вакансия"]}],
    )
    result = relevance_filter.should_keep_article("Открыта вакансия оператора", "")
    assert result.keep is False
    assert "вакансия" in result.matched_noise
