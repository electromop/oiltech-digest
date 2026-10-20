"""Список дочерних обществ «дочка → мать» (решение владельца 20.10).

Без базы: проверка новой пары, дочки компании по цепочке и разбор вставки списком — по правилу «одна ли это
компания», которое API передаёт в company_parents (api._same: полное правило, как у фильтра), и раскрытие компании
для фильтра «всё про компанию» (signal_card_filters.company_group). На изолированной базе: API экрана «Дочерние
общества» — вход, добавление, список, удаление, отказы с причиной, вставка с разбором и без записи. Фильтр
сигналов с дочками на изолированной базе — tests/test_signal_business_feed.py."""

import pytest
from fastapi.testclient import TestClient

from oiltech_digest import admission, api
from oiltech_digest.company_parents import MAX_LINES, check_pair, descendants, parse_lines
from oiltech_digest.db import company_parents_repo, repository
from oiltech_digest.signal_card_filters import company_group, company_names

same = api._same()
PASSWORD = "long-enough-password"
ADMIN = "parents-admin@example.test"
GAZPROM = [("Газпром нефть", "Газпром"), ("Газпромнефть-Заполярье", "Газпром нефть")]
FORMAT = "Нужен формат «дочка; мать», третьим полем можно примечание"


# --- Проверка пары, без базы ---------------------------------------------------------------------


def test_daughter_cannot_be_her_own_mother_under_any_spelling():
    assert check_pair("Газпром", "Газпром", [], same) == "Дочка и мать: одна и та же компания («Газпром» и «Газпром»)"
    # Форма собственности, кавычки и падеж: та же компания.
    assert check_pair("ПАО «Газпром»", "Газпрома", [], same) == (
        "Дочка и мать: одна и та же компания («ПАО «Газпром»» и «Газпрома»)")


def test_second_mother_refused_and_old_row_named():
    assert check_pair("ПАО «Газпром нефть»", "Роснефть", GAZPROM, same) == (
        "У «Газпром нефть» уже есть мать «Газпром»: удалите старую строку")


def test_repeat_refused_under_other_spelling():
    assert check_pair("Газпром нефть ПАО", "ПАО «Газпром»", GAZPROM, same) == (
        "Такая пара уже есть: «Газпром нефть» → «Газпром»")


def test_cycle_of_two_refused():
    assert check_pair("Газпром", "Газпром нефть", GAZPROM, same) == (
        "Получится цикл: «Газпром» → «Газпром нефть» → «Газпром»")


def test_cycle_of_three_refused():
    assert check_pair("Газпром", "Газпромнефть-Заполярье", GAZPROM, same) == (
        "Получится цикл: «Газпром» → «Газпромнефть-Заполярье» → «Газпром нефть» → «Газпром»")


def test_new_branch_accepted_and_empty_or_long_names_refused():
    # Дочка дочки и дочка другой группы: «Газпромнефть-Хантос» — не «Газпромнефть-Заполярье».
    assert check_pair("Газпромнефть-Хантос", "Газпром нефть", GAZPROM, same) is None
    assert check_pair("Башнефть", "Роснефть", GAZPROM, same) is None
    assert check_pair("  ", "Газпром", GAZPROM, same) == "Не указано имя дочки"
    assert check_pair("Газпром", "", GAZPROM, same) == "Не указано имя матери"
    assert check_pair("x" * 201, "Газпром", GAZPROM, same) == "Имя дочки длиннее 200 знаков"
    assert check_pair("Башнефть", "x" * 201, GAZPROM, same) == "Имя матери длиннее 200 знаков"


# --- Дочки по цепочке, без базы ------------------------------------------------------------------


def test_descendants_by_chain_nearest_first():
    assert descendants("ПАО «Газпром»", GAZPROM, same) == ["Газпром нефть", "Газпромнефть-Заполярье"]
    assert descendants("Газпром нефти", GAZPROM, same) == ["Газпромнефть-Заполярье"]


def test_daughter_does_not_pull_mother_and_company_without_daughters_has_none():
    assert descendants("Газпромнефть-Заполярье", GAZPROM, same) == []
    assert descendants("Роснефть", GAZPROM, same) == []
    assert descendants("", GAZPROM, same) == []


def test_broken_list_with_cycle_does_not_hang():
    # Правка мимо экрана: цикл A → B → C → A и вторая мать у C.
    broken = [("B", "A"), ("C", "B"), ("A", "C"), ("C", "A")]
    assert descendants("A", broken, lambda one, other: one == other) == ["B", "C"]
    assert descendants("C", broken, lambda one, other: one == other) == ["A", "B"]


# --- Раскрытие компании для фильтра «всё про компанию», без базы -------------------------------------

# Мать записана в списке с формой собственности, а спрашивают без неё; у Роснефти своя ветка.
LISTED = [("Газпром нефть", "ПАО «Газпром»"), ("Газпромнефть-Заполярье", "Газпром нефть"), ("Башнефть", "Роснефть")]


def test_company_group_is_the_company_then_its_daughters_by_the_chain():
    # Одна ступень и цепочка; «ПАО «Газпром»» в списке — тот же «Газпром», что в запросе.
    assert company_group("Газпром нефть", LISTED) == ["Газпром нефть", "Газпромнефть-Заполярье"]
    assert company_group("  Газпром ", LISTED) == ["Газпром", "Газпром нефть", "Газпромнефть-Заполярье"]
    # Справочник алиасов — тоже правило «одна компания»: «Gazprom» — это «Газпром».
    assert company_group("Gazprom", LISTED) == ["Gazprom", "Газпром нефть", "Газпромнефть-Заполярье"]


def test_daughter_does_not_pull_her_mother_and_company_without_daughters_is_alone():
    assert company_group("Газпромнефть-Заполярье", LISTED) == ["Газпромнефть-Заполярье"]
    assert company_group("Татнефть", LISTED) == ["Татнефть"]
    assert company_group("Газпром", []) == ["Газпром"]
    assert company_group("  ", LISTED) == []


def test_filter_names_are_participants_of_the_company_and_of_every_daughter():
    known = ["ПАО «Газпром нефть»", "ООО «Газпромнефть-Заполярье»", "Газпромнефть-Хантос", "Роснефть", "Башнефть"]

    assert company_names("Газпром", known, LISTED) == [
        "Газпром", "Газпром нефть", "Газпромнефть-Заполярье", "ООО «Газпромнефть-Заполярье»", "ПАО «Газпром нефть»"]
    # Дочка мать не тянет; без списка — только сама компания: «Газпром» — не «Газпром нефть».
    assert company_names("Газпромнефть-Заполярье", known, LISTED) == [
        "Газпромнефть-Заполярье", "ООО «Газпромнефть-Заполярье»"]
    assert company_names("Газпром", known) == ["Газпром"]


# --- Вставка списком, без базы -------------------------------------------------------------------


def test_lines_format_note_and_numbers_in_text():
    rows = parse_lines("Башнефть; Роснефть\n\n  \nбез точки с запятой\nРН-Юганскнефтегаз ;  Роснефть ; дочка; с 2013\n",
                       [], same)

    assert [(row["line"], row["child_name"], row["parent_name"], row["note"], row["problem"]) for row in rows] == [
        (1, "Башнефть", "Роснефть", None, None),
        (4, "без точки с запятой", "", None, FORMAT),
        (5, "РН-Юганскнефтегаз", "Роснефть", "дочка; с 2013", None),
    ]
    assert parse_lines("Башнефть; Роснефть; " + "x" * 501, [], same)[0]["problem"] == "Примечание длиннее 500 знаков"


def test_lines_limit():
    text = "\n".join(f"Дочка {number}; Мать" for number in range(MAX_LINES))
    exact = lambda one, other: one == other  # noqa: E731

    assert len(parse_lines(text + "\n\n\n", [], exact)) == MAX_LINES
    with pytest.raises(ValueError, match="Не больше 500 строк за раз, во вставке 501"):
        parse_lines(text + "\nЕщё одна; Мать", [], exact)


def test_lines_conflict_inside_paste():
    rows = parse_lines("Башнефть; Роснефть\nБашнефть; Татнефть\nРоснефть; Башнефть\n"
                       "ПАО «Башнефть»; Роснефти\nСамаранефтегаз; Роснефть", [], same)

    assert [row["problem"] for row in rows] == [
        None,
        "У «Башнефть» уже есть мать «Роснефть»: удалите старую строку",
        "Получится цикл: «Роснефть» → «Башнефть» → «Роснефть»",
        "Такая пара уже есть: «Башнефть» → «Роснефть»",
        None,
    ]


def test_lines_checked_against_list_and_bad_line_not_counted():
    rows = parse_lines("Газпромнефть-Хантос; Газпром нефть\nГазпром; Газпромнефть-Хантос\n"
                       "Газпром нефть; Роснефть\nРоснефть; Газпром нефть", GAZPROM, same)

    assert [row["problem"] for row in rows] == [
        None,
        "Получится цикл: «Газпром» → «Газпромнефть-Хантос» → «Газпром нефть» → «Газпром»",
        "У «Газпром нефть» уже есть мать «Газпром»: удалите старую строку",
        # Строка 3 не принята: «Роснефть → Газпром нефть» цикла с ней не даёт.
        None,
    ]


# --- API экрана, изолированная база --------------------------------------------------------------


def _client(email, role):
    user = repository.create_user(email, PASSWORD, role)
    admission.admit(int(user["id"]))
    client = TestClient(api.app)
    assert client.post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200
    return client


@pytest.fixture()
def admin(isolated_db):
    return _client(ADMIN, "admin")


def _add(client, child, parent, note=None):
    return client.post("/api/company-parents", json={"child_name": child, "parent_name": parent, "note": note})


def test_without_login_401_and_without_admin_role_403(isolated_db):
    anonymous = TestClient(api.app)
    calls = (("GET", "/api/company-parents", None),
             ("POST", "/api/company-parents", {"child_name": "Башнефть", "parent_name": "Роснефть"}),
             ("DELETE", "/api/company-parents/1", None),
             ("POST", "/api/company-parents/import", {"text": "Башнефть; Роснефть", "apply": True}))
    for method, path, body in calls:
        assert anonymous.request(method, path, json=body).status_code == 401, (method, path)

    user = _client("parents-user@example.test", "user")
    for method, path, body in calls:
        assert user.request(method, path, json=body).status_code == 403, (method, path)
    assert company_parents_repo.pairs() == []


def test_add_list_delete(admin):
    added = _add(admin, "  Газпром  нефть ", "ПАО «Газпром»", "контроль 95%")

    assert added.status_code == 200, added.text
    pair = added.json()["pair"]
    assert (pair["child_name"], pair["parent_name"], pair["note"], pair["created_by_email"]) == (
        "Газпром нефть", "ПАО «Газпром»", "контроль 95%", ADMIN)
    listed = admin.get("/api/company-parents").json()["pairs"]
    assert [(row["id"], row["child_name"], row["parent_name"], row["note"], row["created_by_email"])
            for row in listed] == [(pair["id"], "Газпром нефть", "ПАО «Газпром»", "контроль 95%", ADMIN)]
    assert listed[0]["created_at"]

    assert admin.delete(f"/api/company-parents/{pair['id']}").json() == {"ok": True}
    assert admin.get("/api/company-parents").json()["pairs"] == []
    missing = admin.delete(f"/api/company-parents/{pair['id']}")
    assert (missing.status_code, missing.json()["detail"]) == (404, "Связь не найдена")


def test_refusals_carry_reason_and_write_nothing(admin):
    for child, parent in GAZPROM:
        assert _add(admin, child, parent).status_code == 200

    refused = {
        ("Газпром", "ПАО «Газпром»"): "Дочка и мать: одна и та же компания («Газпром» и «ПАО «Газпром»»)",
        ("Газпром нефть", "Роснефть"): "У «Газпром нефть» уже есть мать «Газпром»: удалите старую строку",
        ("Газпром нефть", "Газпром"): "Такая пара уже есть: «Газпром нефть» → «Газпром»",
        ("Газпром", "Газпромнефть-Заполярье"):
            "Получится цикл: «Газпром» → «Газпромнефть-Заполярье» → «Газпром нефть» → «Газпром»",
        ("", "Газпром"): "Не указано имя дочки",
        ("Башнефть", "Роснефть", "x" * 501): "Примечание длиннее 500 знаков",
    }
    for args, reason in refused.items():
        response = _add(admin, *args)
        assert (response.status_code, response.json()["detail"]) == (400, reason)
    assert company_parents_repo.pairs() == GAZPROM


def test_import_without_apply_writes_nothing(admin):
    response = admin.post("/api/company-parents/import",
                          json={"text": "Газпром нефть; Газпром\nбез формата\n\nГазпром; Газпром нефть"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["ok"], body["applied"], body["problems"]) == (False, False, 2)
    assert [(line["line"], line["problem"]) for line in body["lines"]] == [
        (1, None), (2, FORMAT), (4, "Получится цикл: «Газпром» → «Газпром нефть» → «Газпром»")]
    assert company_parents_repo.pairs() == []


def test_import_with_apply_writes_all_lines(admin):
    text = "Газпром нефть; Газпром\nГазпромнефть-Заполярье; Газпром нефть; с 2017 года"

    response = admin.post("/api/company-parents/import", json={"text": text, "apply": True})

    assert response.status_code == 200, response.text
    assert (response.json()["ok"], response.json()["applied"]) == (True, True)
    rows = admin.get("/api/company-parents").json()["pairs"]
    assert sorted((row["child_name"], row["parent_name"], row["note"], row["created_by_email"]) for row in rows) == [
        ("Газпром нефть", "Газпром", None, ADMIN),
        ("Газпромнефть-Заполярье", "Газпром нефть", "с 2017 года", ADMIN),
    ]
    # Та же вставка ещё раз: обе строки уже есть.
    again = admin.post("/api/company-parents/import", json={"text": text}).json()
    assert again["problems"] == 2


def test_import_with_apply_and_one_problem_writes_nothing(admin):
    response = admin.post("/api/company-parents/import",
                          json={"text": "Газпром нефть; Газпром\nГазпром; Газпром нефть", "apply": True})

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert (detail["message"], detail["problems"]) == ("Ничего не записано: строк с ошибками 1 из 2", 1)
    assert [line["problem"] for line in detail["lines"]] == [
        None, "Получится цикл: «Газпром» → «Газпром нефть» → «Газпром»"]
    assert company_parents_repo.pairs() == []


def test_import_refuses_empty_and_too_long_paste(admin):
    too_many = "\n".join(f"Дочка {number}; Мать" for number in range(MAX_LINES + 1))

    for text, reason in ((" \n\n", "Нет ни одной строки «дочка; мать»"),
                         (too_many, "Не больше 500 строк за раз, во вставке 501")):
        for apply in (False, True):
            response = admin.post("/api/company-parents/import", json={"text": text, "apply": apply})
            assert (response.status_code, response.json()["detail"]) == (400, reason)
    assert company_parents_repo.pairs() == []


def test_editing_block_with_error_writes_nothing(isolated_db):
    with pytest.raises(RuntimeError):
        with company_parents_repo.editing() as edit:
            edit.add([{"child_name": "Башнефть", "parent_name": "Роснефть"}], user_id=None)
            raise RuntimeError("сбой после записи")

    assert company_parents_repo.pairs() == []
