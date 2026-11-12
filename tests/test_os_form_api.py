"""Контур ОС (#188, спека #187), шов 1: HTTP API платформы на тестовой базе.

Одна форма ОС у любой карточки обеих витрин и в «Отклонённых» и один путь записи отметки, без отказа 422. Явные действия
администратора применяются сразу: правка заголовка и сути, «Не тот блок», «Дубль», «Не релевантно». Отметка остальных
участников только записывается: с автором и блоком на карточке, карточку она не меняет. Оценка карточку не двигает, память
агента отметка не пишет.

Часы окна ленты заморожены на 29.09 12:00 МСК (фикстура issue): сентябрь открыт; в тестах окно закрывает прошлый месяц
5-го (tests/conftest.py).
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from oiltech_digest import api, feed_window
from tests.card_pipeline_support import accepted
from tests.digest_user_path_support import MARKET, add_signal, feed_signal, issue_key, msk, set_card
from tests.digest_user_path_support import issue  # noqa: F401: фикстура
from tests.otbor_support import answer, api_request, api_send, decide, showcase
from tests.otbor_support import contour  # noqa: F401: фикстура
from tests.signal_card_support import admin, api_get, user  # noqa: F401: фикстуры


def _mark(person, signal_id: int, **fields) -> dict:
    """Отметка ОС с карточки: форма шлёт всё, что в ней вписано."""
    response = person.post("/api/signals/feedback", json={"signal_id": signal_id, "comment": "", **fields})
    assert response.status_code == 200, response.text
    return response.json()


def _cards(person, showcase: str = "technology", **params) -> dict[int, dict]:
    response = person.get("/api/signals", params={"limit": 200, "showcase": showcase, **params})
    assert response.status_code == 200, response.text
    return {row["id"]: row for row in response.json()}


def _business(article_id: int) -> int:
    """Сигнал ленты статьи в бизнес-ленте."""
    signal_id = feed_signal(article_id)
    set_card(signal_id, signal_category="business", theme=MARKET)
    return signal_id


def _issue_news(person, article_id: int) -> dict:
    [news] = [item for item in person.issue_items() if issue_key(item) == ("article", article_id)]
    return news


def test_admin_edit_of_a_feed_signal_shows_at_once_on_the_card_in_the_search_and_in_the_open_issue(
        issue, monkeypatch):
    admin, analyst, a = issue["admin"], issue["analyst"], issue["a"]
    signal_id = _business(a["hi"])
    analyst.mark(a["hi"])

    saved = _mark(admin, signal_id, verdict="approved", corrected_title="Контракт на бурение сорока скважин",
                  corrected_thesis="Суть эксперта о контракте.")

    assert saved["applied"] is True
    card = _cards(analyst, "business")[signal_id]
    assert (card["title_ru"], card["summary"]) == ("Контракт на бурение сорока скважин", "Суть эксперта о контракте.")
    assert signal_id in _cards(analyst, "business", q="сорока скважин")
    news = _issue_news(analyst, a["hi"])
    assert (news["title"], news["summary"]) == ("Контракт на бурение сорока скважин", "Суть эксперта о контракте.")
    # Закрытый выпуск берёт только правки до закрытия месяца, а эта записана позже.
    monkeypatch.setattr(feed_window, "_now", lambda: msk(2026, 10, 12, 12, 0))
    assert _issue_news(analyst, a["hi"])["title"] == "Материал hi"


def _shape(card: dict) -> tuple:
    """Что меняют явные действия: текст, категория, склейка и место в витрине."""
    return card["title_ru"], card["summary"], card["signal_category"], card["merged_count"]


def test_participant_mark_is_kept_with_its_author_and_shown_as_a_block_but_changes_nothing_on_the_card(issue):
    admin, colleague, a = issue["admin"], issue["colleague"], issue["a"]
    main, signal_id = _business(a["mid"]), _business(a["hi"])
    before = _cards(admin, "business")

    saved = [
        _mark(colleague, signal_id, verdict="wrong_block", reason="Это технология",
              corrected_title="Заголовок коллеги", corrected_thesis="Суть коллеги."),
        _mark(colleague, signal_id, verdict="merge_duplicate", duplicate_of_signal_id=main),
        _mark(colleague, signal_id, verdict="wrong_domain", comment="Вне интересов"),
        _mark(colleague, signal_id, corrected_title="Только заголовок коллеги"),
    ]

    assert [(one["applied"], set(one) & {"recategorized", "merged", "removed"}) for one in saved] == [(False, set())] * 4
    after = _cards(admin, "business")
    assert {key: _shape(card) for key, card in after.items()} == {key: _shape(card) for key, card in before.items()}
    assert _cards(admin, "business", q="Заголовок коллеги") == {}
    blocks = after[signal_id]["feedback_blocks"]
    assert [(block["author"], block["verdict"], block["applied"]) for block in blocks] == [
        ("colleague@example.test", "wrong_block", False), ("colleague@example.test", "merge_duplicate", False),
        ("colleague@example.test", "wrong_domain", False), ("colleague@example.test", None, False)]
    assert (blocks[0]["proposed_title"], blocks[0]["proposed_thesis"], blocks[0]["reason"]) == (
        "Заголовок коллеги", "Суть коллеги.", "Это технология")
    assert blocks[1]["duplicate_of_signal_id"] == main
    assert blocks[3]["proposed_title"] == "Только заголовок коллеги"
    assert after[signal_id]["feedback_count"] == 4
    # Экран кладёт блок из ответа в список, не перечитывая витрину: блок ответа тот же, что в списке. Автор в ответе:
    # почта сессии (у подставной сессии теста своя), время: часы приложения, а не базы.
    assert [_content(block) for block in blocks] == [_content(one["block"]) for one in saved]


def _content(block: dict) -> dict:
    return {key: value for key, value in block.items() if key not in ("author", "created_at")}


def test_admin_duplicate_merges_into_the_root_of_the_group_but_never_the_card_chosen_for_the_issue(issue):
    admin, analyst, a = issue["admin"], issue["analyst"], issue["a"]
    root, middle, late, chosen = (_business(a[key]) for key in ("hi", "mid", "low", "nodate"))

    assert _mark(admin, middle, verdict="merge_duplicate", duplicate_of_signal_id=root)["merged"] is True
    # Главную назвали номером карточки, которая сама уже дубль: склейка идёт к корню группы.
    assert _mark(admin, late, verdict="merge_duplicate", duplicate_of_signal_id=middle)["merged"] is True
    shown = _cards(analyst, "business")
    assert (middle in shown, late in shown, shown[root]["merged_count"]) == (False, False, 2)

    # У сигнала ленты «в дайджест» стоит на его статье: склейка такую карточку не прячет и говорит почему.
    analyst.mark(a["nodate"])
    refused = _mark(admin, chosen, verdict="merge_duplicate", duplicate_of_signal_id=root)

    assert refused["merged"] is False
    assert "выбрана «в дайджест»" in refused["merge_refused"]
    assert chosen in _cards(analyst, "business")
    unknown = _mark(admin, chosen, verdict="merge_duplicate", duplicate_of_signal_id=999999)
    assert (unknown["merged"], unknown["merge_refused"]) == (False, "главной карточки №999999 нет")



def _ordered(person, showcase: str = "business") -> list[tuple[int, str]]:
    """Витрина по баллу, как на экране: номер и «Решение платформы» по порядку."""
    response = person.get("/api/signals", params={"limit": 200, "showcase": showcase, "sort": "score_desc"})
    assert response.status_code == 200, response.text
    return [(row["id"], row["maturity"]) for row in response.json()]


def test_rating_moves_no_card_and_leaves_the_platform_decision(issue):
    admin, colleague, a = issue["admin"], issue["colleague"], issue["a"]
    ids = {key: _business(a[key]) for key in ("hi", "mid", "low")}
    for key, (score, maturity) in {"hi": (88, "shortlist"), "mid": (64, "watch"), "low": (12, "watch")}.items():
        set_card(ids[key], score=score, maturity=maturity)

    _mark(admin, ids["low"], verdict="strong_signal", reason="Взять в выпуск")
    _mark(admin, ids["mid"], verdict="overrated")
    _mark(colleague, ids["hi"], verdict="reject")
    _mark(colleague, ids["low"], verdict="watch_later")

    assert _ordered(admin) == [(ids["hi"], "shortlist"), (ids["mid"], "watch"), (ids["low"], "watch")]


def test_participant_rating_neither_hides_nor_brings_back_a_card_decided_before_otbor(issue):
    """До «Отбора» решение машины не прячет карточку, которую одобрил администратор (решение 05.10, «Отсеянные»);
    отметка участника без прав администратора её видимость не меняет ни в какую сторону."""
    admin, colleague = issue["admin"], issue["colleague"]
    returned, sifted = (add_signal(key, first_seen=msk(2026, 9, 12, 0, 20)) for key in ("returned", "sifted"))
    for signal_id in (returned, sifted):
        set_card(signal_id, filter_stage="judge", filter_reason="брак судьи")

    _mark(admin, returned, verdict="approved")
    _mark(colleague, returned, verdict="reject")
    _mark(colleague, sifted, verdict="strong_signal")

    radar = _cards(admin)
    assert (returned in radar, sifted in radar) == (True, False)


def test_marks_and_the_digest_choice_write_no_agent_memory(issue):
    admin, analyst, colleague, a = issue["admin"], issue["analyst"], issue["colleague"], issue["a"]
    finding, feed = issue["s"]["sep"], _business(a["hi"])

    _mark(admin, finding, verdict="approved", comment="closed-loop control → управление с замкнутым контуром",
          corrected_title="Правка эксперта", corrected_thesis="Суть эксперта.")
    _mark(colleague, feed, verdict="wrong_block", comment="патент → пилот → опытно-промышленная установка")
    analyst.mark_signal(finding)
    analyst.mark(a["hi"])

    assert admin.get("/api/signals/memory", params={"status": ""}).json() == []

DEAL = "Сервисная компания взяла контракт на ремонт скважин"
DEAL_TEXT = "Сервисная компания подписала контракт на капитальный ремонт 120 скважин в Западной Сибири."


def _rejected_cards(person) -> dict[int, dict]:
    return {card["id"]: card for card in api_get("/api/signals", person, section="all")}


def test_admin_not_relevant_moves_the_card_to_removed_by_people_and_return_brings_back_the_old_decision(
        user, admin, contour, monkeypatch):
    ids, _ = accepted(monkeypatch, {DEAL: answer()}, {DEAL: DEAL_TEXT})
    signal_id = showcase(user)[DEAL]["id"]

    saved = api_send("POST", "/api/signals/feedback", admin, {
        "signal_id": signal_id, "verdict": "wrong_domain", "reason": "Ремонт скважин вне интересов Компании",
        "comment": ""})

    assert saved["removed"] is True
    assert DEAL not in showcase(user)
    removed = _rejected_cards(admin)[signal_id]
    assert removed["section_reason"] == (
        "Люди → Убрано людьми: signal-card-admin@example.test: Ремонт скважин вне интересов Компании")
    assert (removed["otbor_decision"], removed["card_set_by"]["otbor_decision"]) == ("reject", "human")
    # Повтор «Отбора» решение человека не меняет: ни названной командой, ни запоздалым итогом задачи.
    decide(monkeypatch, {DEAL: answer()}, "--ids", str(ids[DEAL]))
    assert DEAL not in showcase(user)

    # «Вернуть» только у администратора: прежнее решение «Отбора», и карточка снова в витрине.
    assert api_request("POST", f"/api/signals/{signal_id}/restore", user, {}).status_code == 403
    api_send("POST", f"/api/signals/{signal_id}/restore", admin, {})

    back = showcase(user)[DEAL]
    assert (back["otbor_decision"], back["human_removal"], "otbor_decision" in back["card_set_by"]) == (
        "accept", None, False)
    assert signal_id not in _rejected_cards(admin)
    assert api_request("POST", f"/api/signals/{signal_id}/restore", admin, {}).status_code == 409



MATERIAL = "Нефтяная компания выбрала подрядчика по ГРП на 2027 год"
BOT = {"Authorization": "Bearer secret", "X-Worker-Build": "abc1234", "X-Worker-Contract": "11"}


def _colleague_material(user, monkeypatch) -> int:
    """Материал коллеги от бота (#131) прошёл «Отбор»: сигнал третьего происхождения в бизнес-ленте; его номер."""
    material = {"kind": "event", "title": MATERIAL, "description": "Итог тендера: три года, объём вырос на 20 %.",
                "companies": ["Нефтяная компания"], "verdict": "strong_signal", "verdict_comment": "",
                "tg_chat_id": -100500, "tg_message_id": 77, "tg_user_id": 9001, "tg_username": "author",
                "tg_author": "Автор материала"}
    response = TestClient(api.app).post(
        "/api/external-worker/materials", headers=BOT,
        data={"material": json.dumps(material, ensure_ascii=False), "worker_id": "nl-tg-bot-1"})
    assert response.status_code == 200, response.text
    decide(monkeypatch, {MATERIAL: answer()})
    return showcase(user)[MATERIAL]["id"]


def test_admin_edits_and_merges_a_colleague_material_like_any_other_card(user, admin, contour, monkeypatch):
    material = _colleague_material(user, monkeypatch)
    accepted(monkeypatch, {DEAL: answer()}, {DEAL: DEAL_TEXT})
    main = showcase(user)[DEAL]["id"]

    api_send("POST", "/api/signals/feedback", admin, {
        "signal_id": material, "corrected_title": "Подрядчик по ГРП выбран на три года",
        "corrected_thesis": "Суть эксперта о тендере.", "comment": ""})

    shown = {card["id"]: card for card in api_get("/api/signals", user, showcase="business")}
    assert (shown[material]["title_ru"], shown[material]["summary"]) == (
        "Подрядчик по ГРП выбран на три года", "Суть эксперта о тендере.")
    merged = api_send("POST", "/api/signals/feedback", admin, {
        "signal_id": material, "verdict": "merge_duplicate", "duplicate_of_signal_id": main, "comment": ""})
    assert merged["merged"] is True
    shown = {card["id"]: card for card in api_get("/api/signals", user, showcase="business")}
    assert (material in shown, shown[main]["merged_count"]) == (False, 1)


def _material_notices() -> list[dict]:
    """Итоги «Отбора», которые бот материалов ещё не доставил авторам (#131)."""
    response = TestClient(api.app).get("/api/external-worker/materials/notices", headers=BOT)
    assert response.status_code == 200, response.text
    return response.json()["notices"]


def test_removal_by_people_and_its_return_tell_the_material_author_nothing(user, admin, contour, monkeypatch):
    """Итог «Отбора» автору материала уходит в Telegram (#131). Уборка людьми не итог «Отбора»: автор о ней не узнаёт,
    и «Вернуть» второго сообщения не шлёт."""
    material = _colleague_material(user, monkeypatch)
    [notice] = _material_notices()
    delivered = TestClient(api.app).post("/api/external-worker/materials/notices", headers=BOT,
                                         json={"material_id": notice["material_id"], "outcome": notice["outcome"]})
    assert delivered.status_code == 200, delivered.text

    api_send("POST", "/api/signals/feedback", admin, {"signal_id": material, "verdict": "wrong_domain",
                                                      "reason": "Вне интересов Компании", "comment": ""})
    assert _material_notices() == []
    api_send("POST", f"/api/signals/{material}/restore", admin, {})
    assert _material_notices() == []


def test_return_brings_back_a_card_decided_before_otbor_as_it_was(issue):
    """Карточка до «Отбора» (статья решена гейтом ленты): «Вернуть» возвращает её в витрину с прежним «Решением
    платформы», а не с «отклонено», которое поставила уборка."""
    admin, analyst, a = issue["admin"], issue["analyst"], issue["a"]
    signal_id = _business(a["hi"])
    set_card(signal_id, maturity="shortlist")

    assert _mark(admin, signal_id, verdict="wrong_domain", reason="Вне интересов")["removed"] is True
    assert signal_id not in _cards(analyst, "business")
    assert admin.post(f"/api/signals/{signal_id}/restore").status_code == 200

    back = _cards(analyst, "business")[signal_id]
    assert (back["maturity"], back["otbor_decision"], back["human_removal"]) == ("shortlist", None, None)


def test_admin_wrong_block_says_why_a_card_not_accepted_by_otbor_keeps_its_category(issue):
    admin, a = issue["admin"], issue["a"]
    signal_id = _business(a["hi"])

    saved = _mark(admin, signal_id, verdict="wrong_block")

    assert "recategorized" not in saved
    assert saved["recategorize_refused"] == (
        "«Не тот блок» сразу меняет категорию только у карточки, принятой «Отбором» в бизнес-ленту или технологический "
        "радар")
    assert _cards(admin, "business")[signal_id]["signal_category"] == "business"


def test_issue_material_is_edited_only_by_the_admin(issue):
    """Материал выпуска правит только администратор (решение владельца 12.11, #188): у участника правки ни с экрана
    «Дайджест», ни с карточки нет, сервер отвечает 403; правку текста участник предлагает отметкой ОС на карточке."""
    admin, analyst, a = issue["admin"], issue["analyst"], issue["a"]
    signal_id = _business(a["hi"])
    analyst.mark(a["hi"])

    for fields in ({"title": "Заголовок участника"}, {"summary": "Суть участника."}, {"label": "Подпись участника"},
                   {"hypothesis": "Оценить контракт для Компании."}, {"picture": "placeholder"}):
        response = analyst.patch(f"/api/signals/{signal_id}/issue-material", json=fields)
        assert response.status_code == 403, (fields, response.text)
    assert _issue_news(analyst, a["hi"])["title"] == "Материал hi"

    response = admin.patch(f"/api/signals/{signal_id}/issue-material", json={"title": "Правка администратора"})
    assert response.status_code == 200, response.text
    assert _issue_news(analyst, a["hi"])["title"] == "Правка администратора"
