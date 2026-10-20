"""Бизнес-лента карточками (#97, спека #93), шов 1 — фильтры API списка сигналов на изолированной базе:
тип события, регион, класс технологии, зрелость и компания (по правилу «одна компания», word_forms, с дочками по
списку «дочка → мать» — решение владельца 20.10) — в витрине «бизнес» и на радаре.
Карточки — функциями хранилища, как их пишет агент «Карточка сигнала» (#96); список дочек — через API экрана
«Дочерние общества».
"""

from fastapi.testclient import TestClient

from oiltech_digest import api
from oiltech_digest.db import company_parents_repo, repository, signal_card_store
from tests.signal_card_support import api_get, business, feed_signal, radar_finding
from tests.signal_card_support import admin, user  # noqa: F401 — фикстуры


def _feed(user, title):
    """Сигнал ленты по статье, принятой отбором; номер сигнала."""
    feed_signal(title, f"Текст публикации: {title}.")
    return business(user)[title]["id"]


def _card(signal_id, **values):
    """Карточка так, как её пишет агент: значения — из справочников, рецепт — агента."""
    assert signal_card_store.write_card(signal_id, values, recipe={"agent": "signal_card", "prompt_version": "test"})


def _titles(user, showcase="business", **params):
    return sorted(card["title"] for card in api_get("/api/signals", user, showcase=showcase, **params))


def test_business_feed_narrows_by_event_type_region_technology_class_and_maturity(user):
    contract, pilot, review = _feed(user, "Контракт SLB"), _feed(user, "Пилот робота"), _feed(user, "Обзор рынка")
    _card(contract, event_type="контракт", region="Ближний Восток", technology_class="неприменимо",
          technology_maturity="неприменимо")
    _card(pilot, event_type="пилот", region="Европа", technology_class="робототехника", technology_maturity="пилот/ОПИ")
    _card(review, event_type="не определено", region="не определено", technology_class="не определено",
          technology_maturity="не определено")

    assert _titles(user, event_type="контракт") == ["Контракт SLB"]
    assert _titles(user, region="Европа") == ["Пилот робота"]
    assert _titles(user, technology_class="робототехника") == ["Пилот робота"]
    assert _titles(user, technology_maturity="пилот/ОПИ") == ["Пилот робота"]
    # «Не определено» — тоже значение фильтра: так находятся пробелы карточек.
    assert _titles(user, region="не определено") == ["Обзор рынка"]
    # Фильтры складываются.
    assert _titles(user, event_type="пилот", region="Ближний Восток") == []
    summary = api_get("/api/signals/summary", user, showcase="business", event_type="контракт")
    assert (summary["total"], summary["matching"]) == (3, 1)


def _participants(*names):
    return [{"name": name, "quote": f"{name} — участник", "verified": True} for name in names]


def test_company_filter_finds_every_signal_with_the_company_under_any_of_its_names(user):
    _card(_feed(user, "Контракт SLB и Aramco"), participants_json=_participants("SLB", "Saudi Aramco"))
    _card(_feed(user, "Сделка Aramco"), participants_json=_participants("Saudi Aramco"))
    # Человек (сервис ОС) мог записать участника алиасом — «всё про компанию» его не теряет.
    _card(_feed(user, "Ремонт скважин Шлюмберже"), participants_json=_participants("Шлюмберже"))
    _card(_feed(user, "Газпром нефть бурит"), participants_json=_participants("Газпром нефть"))
    _card(_feed(user, "Покупка Velocity Geomatics"), participants_json=_participants("ZenaTech", "Velocity Geomatics"))
    # Компания упомянута, но участником события не названа.
    _card(_feed(user, "Обзор рынка: SLB и другие"), participants_json=[])

    assert _titles(user, company="SLB") == ["Контракт SLB и Aramco", "Ремонт скважин Шлюмберже"]
    assert _titles(user, company="schlumberger") == ["Контракт SLB и Aramco", "Ремонт скважин Шлюмберже"]
    assert _titles(user, company="Aramco") == ["Контракт SLB и Aramco", "Сделка Aramco"]
    assert _titles(user, company="ГПН") == ["Газпром нефть бурит"]
    # Компания — целиком, а не подстрокой: «Газпром» — другая компания, чем «Газпром нефть».
    assert _titles(user, company="Газпром") == []
    # Компании нет в справочнике алиасов — ищется по своему имени, без различий регистра и пробелов.
    assert _titles(user, company=" velocity  geomatics ") == ["Покупка Velocity Geomatics"]
    summary = api_get("/api/signals/summary", user, showcase="business", company="Шлюмберже")
    assert (summary["total"], summary["matching"]) == (6, 2)


def test_company_written_past_the_agent_is_found_whatever_the_spaces_and_case(user):
    # Сервис ОС запишет участника рукой человека: неразрывный пробел, «ё», регистр — то же имя.
    _card(_feed(user, "Газпром нефть бурит"), participants_json=_participants("ГАЗПРОМ НЕФТЬ"))
    _card(_feed(user, "Самотлорнефтегаз пробурил"), participants_json=_participants("Самотлорнефтегаз  «Ёлка»"))

    assert _titles(user, company="Газпром нефть") == ["Газпром нефть бурит"]
    assert _titles(user, company="ГПН") == ["Газпром нефть бурит"]
    assert _titles(user, company="самотлорнефтегаз «елка»") == ["Самотлорнефтегаз пробурил"]


def test_company_filter_compares_participants_by_the_one_company_rule(user):
    # Имена участников сверяются по правилу «одна компания» (word_forms) при запросе: форма собственности и
    # «Имя (АББР)» — та же компания, а в базе имя хранится как дала модель.
    _card(_feed(user, "Контракт Atlas"), participants_json=_participants("Atlas Energy Solutions Inc."))
    _card(_feed(user, "Пилот Atlas"), participants_json=_participants("Atlas Energy Solutions"))
    _card(_feed(user, "Тендер PDO"), participants_json=_participants("Petroleum Development Oman (PDO)"))

    assert _titles(user, company="Atlas Energy Solutions") == ["Контракт Atlas", "Пилот Atlas"]
    assert _titles(user, company="Atlas Energy Solutions Inc.") == ["Контракт Atlas", "Пилот Atlas"]
    assert _titles(user, company="PDO") == ["Тендер PDO"]
    # Компании нет ни в одной карточке — сигналов нет, а не все сигналы.
    assert _titles(user, company="Kolibri Global Energy") == []
    summary = api_get("/api/signals/summary", user, showcase="business", company="Kolibri Global Energy")
    assert (summary["total"], summary["matching"]) == (3, 0)
    # Пустой список имён у выборки — фильтр, который ничего не нашёл, а не «без фильтра».
    assert repository.list_signals(showcase="business", company_names=[]) == []


def _edit(method, path, user, **body):
    """Правка списка дочек через API экрана «Дочерние общества» (только админ)."""
    api.app.dependency_overrides[api.require_user] = lambda: user
    try:
        response = TestClient(api.app).request(method, path, json=body or None)
    finally:
        api.app.dependency_overrides.pop(api.require_user, None)
    assert response.status_code == 200, response.text
    return response.json()


def _daughter(admin, child, parent):
    """Пара «дочка → мать» в список; номер строки списка."""
    return _edit("POST", "/api/company-parents", admin, child_name=child, parent_name=parent)["pair"]["id"]


def _group(user, company):
    return api_get("/api/signals/summary", user, showcase="business", company=company)["company_group"]


def _gazprom_and_rosneft(user):
    _card(_feed(user, "Заполярье бурит"), participants_json=_participants("Газпромнефть-Заполярье"))
    _card(_feed(user, "Газпром нефть бурит"), participants_json=_participants("Газпром нефть"))
    _card(_feed(user, "Роснефть бурит"), participants_json=_participants("Роснефть"))


def test_company_filter_shows_the_daughters_by_the_chain_but_a_daughter_never_shows_her_mother(admin, monkeypatch):
    _gazprom_and_rosneft(admin)
    # Цепочка Газпром → Газпром нефть → Газпромнефть-Заполярье; мать записана с формой собственности.
    _daughter(admin, "Газпром нефть", "ПАО «Газпром»")
    _daughter(admin, "Газпромнефть-Заполярье", "Газпром нефть")

    assert _titles(admin, company="Газпром нефть") == ["Газпром нефть бурит", "Заполярье бурит"]
    assert _titles(admin, company="Газпром") == ["Газпром нефть бурит", "Заполярье бурит"]
    assert _titles(admin, company="Роснефть") == ["Роснефть бурит"]
    assert _titles(admin, company="Газпромнефть-Заполярье") == ["Заполярье бурит"]
    # Сводка говорит экрану, кого фильтр показывает; список дочек читается один раз на запрос.
    reads = []
    listed = company_parents_repo.pairs
    monkeypatch.setattr(company_parents_repo, "pairs", lambda: reads.append(1) or listed())
    summary = api_get("/api/signals/summary", admin, showcase="business", company="Газпром")
    assert (summary["matching"], summary["company_group"], len(reads)) == (
        2, ["Газпром", "Газпром нефть", "Газпромнефть-Заполярье"], 1)
    assert _group(admin, "Роснефть") == ["Роснефть"]
    assert "company_group" not in api_get("/api/signals/summary", admin, showcase="business")


def test_list_edit_on_the_screen_changes_the_company_filter_at_once(admin):
    _gazprom_and_rosneft(admin)
    assert _titles(admin, company="Газпром нефть") == ["Газпром нефть бурит"]

    pair_id = _daughter(admin, "Газпромнефть-Заполярье", "Газпром нефть")
    assert _titles(admin, company="Газпром нефть") == ["Газпром нефть бурит", "Заполярье бурит"]
    assert _group(admin, "Газпром нефть") == ["Газпром нефть", "Газпромнефть-Заполярье"]

    _edit("DELETE", f"/api/company-parents/{pair_id}", admin)
    assert _titles(admin, company="Газпром нефть") == ["Газпром нефть бурит"]
    assert _group(admin, "Газпром нефть") == ["Газпром нефть"]


def test_radar_takes_the_same_card_filters(user):
    robot = radar_finding("robot", "Робот-бурильщик", "Equinor и SLB испытали робота-бурильщика.")
    sensor = radar_finding("sensor", "Датчик утечек", "Датчик утечек на трубопроводе.")
    _card(robot, event_type="пилот", region="Европа", technology_class="робототехника", technology_maturity="пилот/ОПИ",
          participants_json=_participants("Equinor", "SLB"))
    _card(sensor, event_type="запуск продукта", region="Северная Америка", technology_class="датчики и IoT",
          technology_maturity="коммерческое внедрение", participants_json=_participants("Baker Hughes"))
    # Сигнал ленты с тем же атрибутом в витрину технологий не попадает.
    _card(_feed(user, "Пилот в ленте"), event_type="пилот", participants_json=_participants("SLB"))

    radar = "technology"
    assert _titles(user, radar, event_type="пилот") == ["Робот-бурильщик"]
    assert _titles(user, radar, region="Северная Америка") == ["Датчик утечек"]
    assert _titles(user, radar, technology_class="робототехника", technology_maturity="пилот/ОПИ") == ["Робот-бурильщик"]
    assert _titles(user, radar, company="Schlumberger") == ["Робот-бурильщик"]
    assert _titles(user, radar, company="BKR") == ["Датчик утечек"]
    assert api_get("/api/signals/summary", user, company="SLB")["matching"] == 1


def test_screen_gets_the_filter_values_from_the_dictionaries(user):
    payload = api_get("/api/signals/dictionaries", user)

    # Значения — справочники карточки (спека #93), по параметрам фильтров API; «не определено» и подписи
    # полей экран добавляет сам.
    assert payload["technology_maturity"] == [
        "исследование", "прототип", "пилот/ОПИ", "коммерческое внедрение", "масштабное внедрение", "неприменимо",
    ]
    assert "контракт" in payload["event_type"] and "не определено" not in payload["event_type"]
    assert "Ближний Восток" in payload["region"] and "робототехника" in payload["technology_class"]
    # Подсказки к полю «Компания» — канонические имена справочника алиасов, без самих алиасов.
    assert "SLB" in payload["companies"] and "Schlumberger" not in payload["companies"]


def test_value_outside_the_dictionaries_is_refused_not_silently_empty(user):
    api.app.dependency_overrides[api.require_user] = lambda: user
    try:
        client = TestClient(api.app)
        for path in ("/api/signals", "/api/signals/summary"):
            response = client.get(path, params={"showcase": "business", "event_type": "форум"})
            assert response.status_code == 422, response.text
            assert "Тип события" in response.json()["detail"]
        assert client.get("/api/signals", params={"technology_maturity": "TRL 7"}).status_code == 422
        # Значения справочников — как в справочнике, «не определено» — тоже значение.
        assert client.get("/api/signals", params={"region": "Ближний Восток"}).status_code == 200
        assert client.get("/api/signals", params={"technology_class": "не определено"}).status_code == 200
    finally:
        api.app.dependency_overrides.pop(api.require_user, None)
