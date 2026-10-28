"""Страна сигнала у «Карточки» (#137, спека #128), шов 1: статья или находка → основной вызов «Карточки» на подставной
модели через настоящий клиент воркера NL → ядро → API витрин (tests/signal_card_support.py). Страна в двух ролях:
страны события (до трёх) и страна каждого участника; написания сводятся к имени справочника, чужое отбрасывается.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from oiltech_digest import api, signal_card_countries, signal_card_pack
from oiltech_digest.db import connection
from tests.signal_card_support import answer, api_get, business, feed_signal, fill, radar_finding
from tests.signal_card_support import contour, user  # noqa: F401 — фикстуры

CONTRACT = "Контракт SLB в ОАЭ"
CONTRACT_TEXT = "Американская SLB подписала контракт с ADNOC на бурение 40 скважин в ОАЭ."
ROBOT = "Робот Equinor в Норвегии"
ROBOT_TEXT = "Equinor испытала робота-бурильщика на платформе в Норвегии."


def _titles(user, showcase="business", **params):
    return sorted(card["title"] for card in api_get("/api/signals", user, showcase=showcase, **params))


def _status(user, path, **params):
    api.app.dependency_overrides[api.require_user] = lambda: user
    try:
        return TestClient(api.app).get(path, params=params).status_code
    finally:
        api.app.dependency_overrides.pop(api.require_user, None)


def test_countries_are_written_in_both_roles_spellings_collapse_and_the_unknown_is_dropped(user, contour,
                                                                                         monkeypatch):
    feed_signal(CONTRACT, CONTRACT_TEXT)

    model = fill(monkeypatch, {CONTRACT: answer(
        event_countries=["Объединённые Арабские Эмираты", "Марс", "UAE", "саудовская аравия", "Оман", "Катар"],
        companies=[{"name": "SLB", "quote": "Американская SLB подписала контракт", "country": "Соединённые Штаты"},
                   {"name": "ADNOC", "quote": "контракт с ADNOC на бурение", "country": "Атлантида"}])})

    card = business(user)[CONTRACT]
    # Написания — к имени справочника; повтор и страна не из справочника отброшены; стран события не больше трёх.
    assert card["event_countries"] == ["ОАЭ", "Саудовская Аравия", "Оман"]
    # Страна участника хранится при нём: роль видна у каждого значения; не из справочника — пусто.
    assert [(one["name"], one["country"]) for one in card["participants_json"]] == [("SLB", "США"), ("ADNOC", None)]
    assert card["card_set_by"]["event_countries"] == "agent"
    # Полного списка стран модель не видит: ни в инструкции, ни перечнем в схеме ответа.
    [call] = model.calls
    assert "Буркина-Фасо" not in call["instructions"] and "Эсватини" not in call["instructions"]
    properties = call["schema"]["schema"]["properties"]
    assert properties["event_countries"] == {"type": "array", "items": {"type": "string"}}
    assert properties["companies"]["items"]["properties"]["country"] == {"type": "string"}


def test_country_filter_finds_a_signal_by_the_event_country_and_by_a_participant_country_in_both_showcases(
        user, contour, monkeypatch):
    feed_signal(CONTRACT, CONTRACT_TEXT)
    feed_signal("Сделка в России", "Татнефть купила сервисную компанию в России.")
    radar_finding("robot", ROBOT, ROBOT_TEXT)
    fill(monkeypatch, {
        CONTRACT: answer(event_countries=["ОАЭ"], companies=[
            {"name": "SLB", "quote": "Американская SLB подписала контракт", "country": "США"}]),
        "Сделка в России": answer(event_countries=["Россия"], companies=[]),
        ROBOT: answer(event_countries=["Норвегия"], companies=[
            {"name": "Equinor", "quote": "Equinor испытала робота-бурильщика", "country": "Норвегия"}]),
    })

    # Контракт американской сервисной компании в ОАЭ виден и в «США» (участник), и в «ОАЭ» (место события).
    assert _titles(user, country="ОАЭ") == [CONTRACT]
    assert _titles(user, country="США") == [CONTRACT]
    assert _titles(user, country="Норвегия") == []
    # Технологический радар — тот же фильтр.
    assert _titles(user, "technology", country="Норвегия") == [ROBOT]
    assert _titles(user, "technology", country="США") == []
    summary = api_get("/api/signals/summary", user, showcase="business", country="США")
    assert (summary["total"], summary["matching"]) == (2, 1)
    # Значение фильтра — имя справочника: чужое и написание вместо имени — 422, а не «сигналов нет».
    assert [_status(user, "/api/signals", country=wrong) for wrong in ("Марс", "USA")] == [422, 422]
    # Значения фильтра на экране — имена справочника стран, без написаний.
    countries = api_get("/api/signals/dictionaries", user)["country"]
    assert {"ОАЭ", "США", "Россия", "Норвегия"} <= set(countries) and "USA" not in countries


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "signals" / "card-country-before-deploy.sql"


def test_before_deploy_sql_adds_the_event_countries_to_the_old_schema_and_repeats_safely(isolated_db):
    with connection.get_connection() as conn:
        conn.execute("ALTER TABLE signals DROP COLUMN event_countries")
        conn.commit()

    for _ in range(2):  # повтор безопасен
        with connection.get_connection() as conn:
            conn.commit()  # SET search_path открыл транзакцию — у скрипта своя
            conn.autocommit = True
            conn.execute(SCRIPT.read_text(encoding="utf-8"))

    with connection.get_connection() as conn:
        found = conn.execute(
            "SELECT data_type FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'signals' AND column_name = 'event_countries'").fetchone()
    assert found == ("ARRAY",)


def test_participant_country_goes_with_the_participants_and_a_human_list_stays_without_it(user, contour,
                                                                                        monkeypatch):
    """Защита от регресса: страна участника — в его записи и пишется вместе с участниками. Участников человека
    (автор материала, правка) агент не трогает, и страны у них нет; страны события агент пишет."""
    feed_signal(CONTRACT, CONTRACT_TEXT)
    with connection.get_connection() as conn:
        conn.execute("UPDATE signals SET participants_json = '[{\"name\": \"ADNOC\", \"quote\": \"\"}]', "
                     "card_set_by = '{\"participants\": \"human\"}' WHERE title = %s", (CONTRACT,))
        conn.commit()

    fill(monkeypatch, {CONTRACT: answer(event_countries=["ОАЭ"], companies=[
        {"name": "SLB", "quote": "Американская SLB подписала контракт", "country": "США"}])})

    card = business(user)[CONTRACT]
    assert card["participants_json"] == [{"name": "ADNOC", "quote": ""}]
    assert card["event_countries"] == ["ОАЭ"]
    assert _titles(user, country="США") == [] and _titles(user, country="ОАЭ") == [CONTRACT]


def test_country_dictionary_names_and_spellings_are_unique_and_regions_come_from_the_region_dictionary():
    dictionaries = signal_card_pack.load_dictionaries()
    entries = dictionaries["countries"]
    spellings = [signal_card_countries.spelling_key(one)
                 for entry in entries for one in (entry["name"], *entry["aliases"])]
    # Одно написание — одна страна: иначе код сводил бы его к той, что в справочнике раньше.
    assert len(spellings) == len(set(spellings))
    regions = set(dictionaries["region"]["values"]) - {"глобально"}
    assert all(entry["regions"] and set(entry["regions"]) <= regions for entry in entries)
    countries = signal_card_countries.Countries(dictionaries)
    assert [countries.canonical(one) for one in ("РФ", "Российская Федерация", "U.S.A.", "Соединенные Штаты",
                                                 "кот-д’ивуар", "Шотландия", "Atlantis", 7)] == [
        "Россия", "Россия", "США", "США", "Кот-д'Ивуар", "Великобритания", None, None]
    assert countries.regions("Турция") == ["Европа", "Ближний Восток"] and countries.regions("Марс") == []
