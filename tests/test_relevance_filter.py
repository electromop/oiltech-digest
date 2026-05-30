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
