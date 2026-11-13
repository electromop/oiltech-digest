"""Журнал ОС (#189, спека #187), шов 1: HTTP API платформы на тестовой базе.

Вкладка «Обратная связь»: один журнал отметок и действий по обеим витринам, со статусами, путём действия и фильтрами;
участник видит журнал без решений, его запрос решения, отмены и настройки получает отказ. Любое явное действие
администратора из формы ОС (#188) отменяется из журнала и возвращает прежнее состояние карточки; отмена склейки пишет
пару «не одно событие». Настройки контура правит администратор, они действуют без выката.

Часы окна ленты заморожены на 29.09 12:00 МСК (фикстура issue): сентябрь открыт.
"""

from __future__ import annotations

from oiltech_digest.db import feedback_store
from tests.digest_user_path_support import issue  # noqa: F401: фикстура
from tests.digest_user_path_support import set_card
from tests.os_journal_support import business, cards, entry, journal, mark, settings


def _labels(found: dict) -> list[str]:
    return [step["label"] for step in found["path"]]


def test_journal_shows_marks_of_both_showcases_with_status_path_and_filters(issue):
    admin, colleague = issue["admin"], issue["colleague"]
    feed = business(issue["a"]["hi"])
    finding = issue["s"]["sep"]
    set_card(finding, signal_category="technology")

    edit = mark(admin, feed, verdict="approved", corrected_title="Контракт на сорок скважин")
    rating = mark(colleague, finding, verdict="watch_later", comment="Стадия: пилот, рано в выпуск")

    rows = journal(colleague)
    by_key = {row["key"]: row for row in rows}
    edited, rated = by_key[f"mark:{edit['event_id']}"], by_key[f"mark:{rating['event_id']}"]
    assert (edited["showcase"], edited["status"], edited["author"]) == ("business", "done", "admin@example.test")
    assert _labels(edited) == ["Записано: отметка администратора", "Сразу: Правка: заголовок"]
    assert (rated["showcase"], rated["status"], rated["author"]) == ("technology", "done", "colleague@example.test")
    assert _labels(rated) == ["Записано", "Ждёт недельного среза"]
    # Участник видит журнал без кнопок решений.
    assert not any(row.get("can_undo") or row.get("can_decide") for row in rows)
    assert entry(admin, edited["key"])["can_undo"] is True

    assert [row["key"] for row in journal(colleague, showcase="business")] == [edited["key"]]
    assert [row["key"] for row in journal(colleague, author="colleague@example.test")] == [rated["key"]]
    assert [row["key"] for row in journal(colleague, agent="summary")] == [edited["key"]]
    assert {row["key"] for row in journal(colleague, agent="scores")} == {edited["key"], rated["key"]}
    assert journal(colleague, status="undone") == []
    assert journal(colleague, status="pending") == []
    assert colleague.get("/api/feedback-journal", params={"status": "maybe"}).status_code == 422


def test_participant_gets_a_refusal_for_decisions_undo_settings_and_collect_now(issue):
    admin, colleague = issue["admin"], issue["colleague"]
    feed = business(issue["a"]["hi"])
    action_id = mark(admin, feed, corrected_title="Правка администратора")["action_id"]

    refused = [
        colleague.post(f"/api/feedback-journal/actions/{action_id}/undo"),
        colleague.post(f"/api/feedback-journal/proposals/{action_id}/decision", json={"decision": "approve"}),
        colleague.post(f"/api/feedback-journal/tasks/{action_id}/close"),
        colleague.post("/api/feedback-journal/slices"),
        colleague.put("/api/feedback-journal/settings", json={"values": {"card_badge": False}}),
    ]

    assert [response.status_code for response in refused] == [403] * 5
    assert cards(colleague)[feed]["title_ru"] == "Правка администратора"
    overview = colleague.get("/api/feedback-journal/overview").json()
    assert (overview["can_collect"], overview["can_configure"]) == (False, False)
    assert colleague.get("/api/feedback-journal/settings").json()["can_edit"] is False


def _undo(person, action_id: int) -> dict:
    response = person.post(f"/api/feedback-journal/actions/{action_id}/undo")
    assert response.status_code == 200, response.text
    return response.json()


def test_undo_of_an_admin_edit_brings_back_the_earlier_edit_and_then_the_model_text(issue):
    admin, analyst = issue["admin"], issue["analyst"]
    feed = business(issue["a"]["hi"])
    model_title = cards(analyst)[feed]["title_ru"]
    first = mark(admin, feed, corrected_title="Первая правка", corrected_thesis="Суть эксперта.")
    second = mark(admin, feed, corrected_title="Вторая правка")
    assert cards(analyst)[feed]["title_ru"] == "Вторая правка"

    _undo(admin, second["action_id"])
    assert (cards(analyst)[feed]["title_ru"], cards(analyst)[feed]["summary"]) == ("Первая правка", "Суть эксперта.")
    _undo(admin, first["action_id"])
    assert cards(analyst)[feed]["title_ru"] == model_title
    assert entry(admin, f"mark:{first['event_id']}")["status"] == "undone"
    assert _labels(entry(admin, f"mark:{first['event_id']}"))[-1] == "Отменено"
    again = admin.post(f"/api/feedback-journal/actions/{first['action_id']}/undo")
    assert again.status_code == 409


def test_undo_of_wrong_block_brings_back_the_category_and_the_scores_of_this_card(issue):
    admin, analyst = issue["admin"], issue["analyst"]
    feed = business(issue["a"]["hi"], score=71, score_band="useful", score_profile="business",
                    scores_recipe={"agent": "signal_scores"})

    saved = mark(admin, feed, verdict="wrong_block", reason="Это технология")
    assert saved["recategorized"] == "technology"
    assert cards(analyst, "technology")[feed]["scores_pending"] is True

    _undo(admin, saved["action_id"])

    back = cards(analyst)[feed]
    assert (back["signal_category"], back["score"], back["score_band"], back["scores_pending"]) == (
        "business", 71, "useful", False)
    assert feed not in cards(analyst, "technology")


def test_undo_of_a_merge_splits_the_cards_and_writes_a_pair_not_the_same_event(issue):
    admin, analyst, a = issue["admin"], issue["analyst"], issue["a"]
    root, duplicate, child = business(a["hi"]), business(a["mid"]), business(a["low"])
    assert mark(admin, child, verdict="merge_duplicate", duplicate_of_signal_id=duplicate)["merged"] is True
    saved = mark(admin, duplicate, verdict="merge_duplicate", duplicate_of_signal_id=root)
    assert saved["merged"] is True
    assert cards(analyst)[root]["merged_count"] == 2

    _undo(admin, saved["action_id"])

    shown = cards(analyst)
    assert (duplicate in shown, shown[root]["merged_count"], shown[duplicate]["merged_count"]) == (True, 0, 1)
    assert [(pair["signal_id"], pair["other_signal_id"], pair["same_event"])
            for pair in feedback_store.pairs(duplicate)] == [(duplicate, root, False)]


def test_undo_of_not_relevant_brings_back_the_previous_otbor_decision_and_return_closes_it_too(issue):
    admin, analyst, a = issue["admin"], issue["analyst"], issue["a"]
    first, second = business(a["hi"]), business(a["mid"])
    removed = [mark(admin, signal_id, verdict="wrong_domain", reason="Вне интересов") for signal_id in (first, second)]
    assert all(one["removed"] for one in removed)
    assert first not in cards(analyst) and second not in cards(analyst)

    _undo(admin, removed[0]["action_id"])
    # «Вернуть» в разделе «Убрано людьми» мимо журнала: в журнале действие тоже отменено.
    assert admin.post(f"/api/signals/{second}/restore").status_code == 200

    shown = cards(analyst)
    assert (first in shown, second in shown) == (True, True)
    assert shown[first]["otbor_decision"] == "accept"
    assert [entry(admin, f"mark:{one['event_id']}")["status"] for one in removed] == ["undone", "undone"]
    assert _labels(entry(admin, f"mark:{removed[1]['event_id']}"))[-1] == "Отменено кнопкой «Вернуть»"


def test_contour_settings_are_edited_by_the_admin_on_the_tab_and_act_without_a_deploy(issue):
    admin, colleague = issue["admin"], issue["colleague"]
    feed = business(issue["a"]["hi"])
    mark(colleague, feed, verdict="approved")

    values = settings(admin, tab_visible_to="admin", proposal_lifetime_days=14, review_model="gpt-6-luna")

    assert (values["tab_visible_to"], values["proposal_lifetime_days"], values["review_model"]) == (
        "admin", 14, "gpt-6-luna")
    assert colleague.get("/api/feedback-journal").status_code == 403
    assert admin.get("/api/feedback-journal").status_code == 200
    for wrong in ({"review_model": "gpt-5.4-mini"}, {"slice_time": "25:00"}, {"monthly_ceiling_usd": -1},
                  {"no_such_setting": 1}):
        response = admin.put("/api/feedback-journal/settings", json={"values": wrong})
        assert response.status_code == 422, wrong
    assert admin.get("/api/feedback-journal/settings").json()["values"]["review_model"] == "gpt-6-luna"
    settings(admin, tab_visible_to="all")
    assert colleague.get("/api/feedback-journal").status_code == 200


def test_removed_by_people_section_opens_to_participants_only_by_the_setting(issue):
    admin, colleague = issue["admin"], issue["colleague"]
    feed = business(issue["a"]["hi"])
    mark(admin, feed, verdict="wrong_domain", reason="Вне интересов")

    assert colleague.get("/api/signals", params={"section": "people"}).status_code == 403
    settings(admin, removed_visible_to="all")
    response = colleague.get("/api/signals", params={"section": "people"})
    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [feed]
    assert colleague.get("/api/signals", params={"section": "all"}).status_code == 403
