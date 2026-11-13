"""Журнал ОС (#189, спека #187), шов 2: недельный срез и разбор ОС через настоящий клиент воркера NL с подставной
моделью (tests/os_journal_support.py).

Срез идёт раз в неделю по расписанию из настроек контура и по кнопке «собрать сейчас» и берёт отметки с прошлого среза.
Разбор ОС сводит отметки всех участников по сигналу в одно предложение улучшения карточки с основанием и цитатой; ответ
с чужими значениями или без цитаты не пишется. Согласованное применяется к карточке сразу, изменённое в новой
формулировке, убранное остаётся в журнале, без решения 30 дней: «устарело». В первый понедельник месяца: задача выборки
отклонённого «Отбором». Потолок расходов откладывает разбор, расход разбора: своей стадией.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from oiltech_digest import cli, feed_window, feedback_slice
from oiltech_digest.db import connection, repository
from oiltech_digest.feed_window import MSK
from tests.digest_user_path_support import issue  # noqa: F401: фикстура
from tests.digest_user_path_support import msk, set_card
from tests.os_journal_support import (business, cards, entry, improvement, journal, mark, review, settings,
                                      today_settings)
from tests.otbor_support import contour  # noqa: F401: фикстура
from tests.signal_card_support import tag, worker


def _reviewed(model) -> list[int]:
    return [int(call["input"].splitlines()[0].split("№")[1]) for call in model.calls]


def _collect(person) -> dict:
    response = person.post("/api/feedback-journal/slices")
    assert response.status_code == 200, response.text
    return response.json()


def _slices(person) -> list[dict]:
    return person.get("/api/feedback-journal/overview").json()["slices"]


def test_weekly_slice_runs_on_schedule_once_a_week_and_by_collect_now_and_takes_marks_since_the_last_slice(
        issue, contour, monkeypatch):
    admin, colleague, a = issue["admin"], issue["colleague"], issue["a"]
    first, second, third = business(a["hi"]), business(a["mid"]), business(a["low"])
    today_settings(admin)
    mark(colleague, first, verdict="approved", comment="Годный контракт")

    cli.main(["feedback-slice"])
    assert _reviewed(review(monkeypatch, {})) == [first]
    mark(colleague, second, comment="Стадия: пилот")
    assert _collect(admin)["status"] == "running"
    assert admin.post("/api/feedback-journal/slices").status_code == 409
    assert _reviewed(review(monkeypatch, {})) == [second]
    # Срез по расписанию этой недели уже был: второй раз за неделю не идёт.
    cli.main(["feedback-slice"])
    assert _reviewed(review(monkeypatch, {})) == []

    mark(colleague, third, verdict="reject")
    week_later = datetime.now(MSK) + timedelta(days=7)
    monkeypatch.setattr(feedback_slice, "_now", lambda: week_later)
    cli.main(["feedback-slice"])
    assert _reviewed(review(monkeypatch, {})) == [third]

    slices = _slices(admin)
    assert [(one["trigger"], one["status"], one["signals"]) for one in slices] == [
        ("schedule", "done", 1), ("manual", "done", 1), ("schedule", "done", 1)]
    assert slices[0]["window_from"] == slices[1]["window_to"]
    assert slices[1]["window_from"] == slices[2]["window_to"]


def test_review_brings_marks_of_all_participants_into_one_card_improvement_with_basis_and_quote(
        issue, contour, monkeypatch):
    admin, analyst, colleague = issue["admin"], issue["analyst"], issue["colleague"]
    signal_id = business(issue["a"]["hi"], technology_maturity="прототип")
    by_colleague = mark(colleague, signal_id, verdict="watch_later", comment="Это уже пилот у оператора, не прототип")
    by_analyst = mark(analyst, signal_id, comment="Согласен: пилот на месторождении",
                      corrected_thesis="Оператор начал пилот системы на месторождении.")
    by_admin = mark(admin, signal_id, verdict="approved", reason="Полезный сигнал")
    ids = [by_colleague["event_id"], by_analyst["event_id"]]

    _collect(admin)
    model = review(monkeypatch, {signal_id: {"improvements": [
        improvement("technology_maturity", "пилот/ОПИ", ids, "уже пилот у оператора"),
        improvement("summary", "Оператор начал пилот системы на месторождении.", [ids[1]],
                    "Оператор начал пилот системы на месторождении."),
    ]}})

    [call] = model.calls
    assert "colleague@example.test" not in call["input"] and "analyst@example.test" not in call["input"]
    assert f"Отметка №{by_admin['event_id']} (администратор" in call["input"]
    assert call["model"] == "gpt-6-sol"
    [proposal] = journal(colleague, kind="signal_proposal")
    assert (proposal["status"], proposal["signal_id"], proposal["event_ids"]) == ("pending", signal_id, sorted(ids))
    assert proposal["quote"] == "уже пилот у оператора"
    assert [(change["field"], change["shown"], change["previous"]) for change in proposal["changes"]] == [
        ("technology_maturity", "пилот/ОПИ", "прототип"),
        ("summary", "Оператор начал пилот системы на месторождении.", cards(admin)[signal_id]["summary"])]
    assert {basis["author"] for basis in proposal["basis"]} == {"colleague@example.test", "analyst@example.test"}
    assert (proposal["can_decide"], entry(admin, proposal["key"])["can_decide"]) == (False, True)
    # Сам разбор карточку не меняет; на карточке значок со ссылкой на строку журнала.
    assert cards(colleague)[signal_id]["technology_maturity"] == "прототип"
    assert cards(colleague)[signal_id]["feedback_pending"] == proposal["id"]
    colleague_mark = entry(admin, f"mark:{ids[0]}")
    assert [step["label"] for step in colleague_mark["path"]] == [
        "Записано", f"В предложении №{proposal['id']}: ждёт решения"]
    assert colleague_mark["status"] == "pending"


def test_review_answer_with_foreign_values_or_without_a_quote_is_not_written(issue, contour, monkeypatch):
    admin, colleague, a = issue["admin"], issue["colleague"], issue["a"]
    signal_id, other = business(a["hi"]), business(a["mid"])
    own = mark(colleague, signal_id, verdict="watch_later", comment="Тема: бурение, а стадия: пилот")["event_id"]
    foreign = mark(colleague, other, comment="Дата события: сентябрь 2026")["event_id"]

    _collect(admin)
    review(monkeypatch, {
        signal_id: {"improvements": [
            improvement("theme", "Космическая отрасль", [own], "Тема: бурение"),
            improvement("technology_maturity", "пилот/ОПИ", [own], "стадия: опытная эксплуатация"),
            improvement("event_type", "пилот", [foreign], "стадия: пилот"),
            improvement("signal_category", "технология", [own], "Тема: бурение"),
            # Подпись оценки («Наблюдать») не текст отметки: полю карточки цитатой не служит.
            improvement("event_type", "пилот", [own], "Наблюдать"),
        ]},
        other: {"answer": "не по схеме"},
    })

    assert journal(colleague, kind="signal_proposal") == []
    [last] = _slices(admin)
    assert (last["status"], last["proposals"]) == ("done", 0)
    assert last["note"] == "не прошло проверку кодом: 6"
    assert cards(colleague)[signal_id]["feedback_pending"] is None


def _decide(person, proposal_id: int, decision: str, **body) -> dict:
    response = person.post(f"/api/feedback-journal/proposals/{proposal_id}/decision", json={"decision": decision, **body})
    assert response.status_code == 200, response.text
    return response.json()


def _labels(found: dict) -> list[tuple[str, str | None]]:
    return [(step["label"], step["author"]) for step in found["path"]]


def test_approved_improvement_is_applied_at_once_modified_in_new_wording_dismissed_stays_and_stale_after_30_days(
        issue, contour, monkeypatch):
    admin, colleague, a = issue["admin"], issue["colleague"], issue["a"]
    stage, text, theme, kind = (business(a[key], technology_maturity="прототип") for key in ("hi", "mid", "low", "nodate"))
    marks = {signal_id: mark(colleague, signal_id, comment="пилот на месторождении, суть короче, тема бурение, "
                                                            "это контракт")["event_id"]
             for signal_id in (stage, text, theme, kind)}
    quote = "пилот на месторождении"
    _collect(admin)
    review(monkeypatch, {
        stage: {"improvements": [improvement("technology_maturity", "пилот/ОПИ", [marks[stage]], quote)]},
        text: {"improvements": [improvement("summary", "Суть по отметкам.", [marks[text]], quote)]},
        theme: {"improvements": [improvement("event_type", "пилот", [marks[theme]], quote)]},
        kind: {"improvements": [improvement("event_type", "контракт", [marks[kind]], "это контракт")]},
    })
    proposals = {row["signal_id"]: row for row in journal(admin, kind="signal_proposal")}

    assert _decide(admin, proposals[stage]["id"], "approve")["status"] == "applied"
    assert _decide(admin, proposals[text]["id"], "modify",
                   changes=[{"field": "summary", "value": "Суть в словах администратора."}])["status"] == "applied"
    assert _decide(admin, proposals[theme]["id"], "dismiss")["status"] == "dismissed"
    with connection.get_connection() as conn:
        conn.execute("UPDATE feedback_actions SET created_at = now() - interval '31 days' WHERE id = %s",
                     (proposals[kind]["id"],))
        conn.commit()
    cli.main(["feedback-slice"])

    shown = cards(colleague)
    assert (shown[stage]["technology_maturity"], shown[text]["summary"], shown[theme]["event_type"]) == (
        "пилот/ОПИ", "Суть в словах администратора.", None)
    assert [shown[one]["feedback_pending"] for one in (stage, text, theme, kind)] == [None] * 4
    statuses = {row["signal_id"]: row["status"] for row in journal(colleague, kind="signal_proposal")}
    assert statuses == {stage: "applied", text: "applied", theme: "dismissed", kind: "stale"}
    approved = entry(admin, f"action:{proposals[stage]['id']}")
    slice_id = _slices(admin)[0]["id"]
    assert _labels(approved) == [(f"Предложено разбором ОС (срез №{slice_id})", None),
                                 ("Согласовано", f"user{admin.id}@example.test"),
                                 ("Применено системой", f"user{admin.id}@example.test")]
    modified = entry(admin, f"action:{proposals[text]['id']}")
    assert [label for label, _ in _labels(modified)][1] == "Изменено"
    assert ([one["shown"] for one in modified["changes"]], [one["shown"] for one in modified["proposed_changes"]]) == (
        ["Суть в словах администратора."], ["Суть по отметкам."])
    # Что стало с отметкой участника: статус её предложения.
    assert {signal_id: entry(colleague, f"mark:{marks[signal_id]}")["status"]
            for signal_id in (stage, text, theme, kind)} == {
        stage: "applied", text: "applied", theme: "dismissed", kind: "stale"}
    assert [label for label, _ in _labels(entry(admin, f"action:{proposals[kind]['id']}"))][-1] == (
        "Устарело: без решения дольше 30 дней")
    assert admin.post(f"/api/feedback-journal/proposals/{proposals[theme]['id']}/decision",
                      json={"decision": "approve"}).status_code == 409

    undone = admin.post(f"/api/feedback-journal/actions/{proposals[stage]['id']}/undo")
    assert undone.status_code == 200, undone.text
    assert cards(colleague)[stage]["technology_maturity"] == "прототип"
    after_undo = entry(admin, f"action:{proposals[stage]['id']}")
    assert after_undo["status"] == "undone"
    assert [label for label, _ in _labels(after_undo)][1:] == ["Согласовано", "Применено системой", "Отменено"]


def test_improvement_does_not_change_a_card_of_a_closed_issue_and_can_be_decided_again(issue, contour, monkeypatch):
    admin, analyst, colleague, a = issue["admin"], issue["analyst"], issue["colleague"], issue["a"]
    signal_id = business(a["hi"], technology_maturity="прототип")
    analyst.mark(a["hi"])
    event_id = mark(colleague, signal_id, comment="Это уже пилот на месторождении")["event_id"]
    _collect(admin)
    review(monkeypatch, {signal_id: {"improvements": [
        improvement("technology_maturity", "пилот/ОПИ", [event_id], "уже пилот на месторождении")]}})
    [proposal] = journal(admin, kind="signal_proposal")
    # Сентябрь закрыт: выпуск сентября только на просмотр, его рубрику, подпись и раздел карточка не меняет.
    monkeypatch.setattr(feed_window, "_now", lambda: msk(2026, 10, 12, 12, 0))

    assert _decide(admin, proposal["id"], "approve")["status"] == "approved"

    assert cards(colleague)[signal_id]["technology_maturity"] == "прототип"
    refused = entry(admin, proposal["key"])
    assert [label for label, _ in _labels(refused)][-1] == (
        "Не применено: Стадия (зрелость): карточка в выпуске закрытого месяца: закрытый выпуск только на просмотр")
    assert refused["can_decide"] is True
    assert _decide(admin, proposal["id"], "dismiss")["status"] == "dismissed"


def _rejected(key: str, theme: str) -> int:
    signal_id = repository.upsert_signal({"signal_key": key, "title": f"Отклонено {key}", "title_ru": f"Отклонено {key}",
                                          "theme": theme, "maturity": "reject", "score": 10})
    set_card(signal_id, otbor_decision="reject", reject_reason="oil_gas_no_link",
             verdict_reason="Нефтегаз без связи с нефтесервисом", signal_category="business")
    return signal_id


def test_first_monday_of_the_month_puts_a_task_with_20_rejected_publications_in_the_journal(
        issue, contour, monkeypatch):
    admin, colleague = issue["admin"], issue["colleague"]
    themes = ["Бурение", "Геофизика", "Добыча", "Энергетика"]
    for name in themes:
        tag(name)
    for number in range(26):
        _rejected(f"rejected-{number}", themes[number % 4])
    first_monday = datetime(2026, 12, 7, 9, 30, tzinfo=MSK)
    settings(admin, slice_weekday="1", slice_time="09:00")

    monkeypatch.setattr(feedback_slice, "_now", lambda: first_monday)
    cli.main(["feedback-slice"])
    cli.main(["feedback-slice"])
    monkeypatch.setattr(feedback_slice, "_now", lambda: first_monday + timedelta(days=7))
    cli.main(["feedback-slice"])

    [task] = journal(colleague, kind="sample_task")
    assert (task["status"], task["month"], len(task["publications"])) == ("pending", "2026-12", 20)
    assert sorted({one["theme"] for one in task["publications"]}) == sorted(themes)
    assert {sum(one["theme"] == name for one in task["publications"]) for name in themes} == {5}
    assert task["agents"] == ["otbor"]
    closed = admin.post(f"/api/feedback-journal/tasks/{task['id']}/close")
    assert closed.status_code == 200, closed.text
    assert entry(admin, task["key"])["status"] == "done"


def test_cost_ceiling_postpones_the_review_the_admin_sees_a_warning_and_the_cost_is_its_own_stage(
        issue, contour, monkeypatch):
    admin, colleague, a = issue["admin"], issue["colleague"], issue["a"]
    first, second = business(a["hi"]), business(a["mid"])
    settings(admin, monthly_ceiling_usd=0.01)
    mark(colleague, first, comment="Стадия: пилот")
    _collect(admin)
    review(monkeypatch, {})

    mark(colleague, second, comment="Стадия: испытания")
    postponed = _collect(admin)

    assert postponed["status"] == "postponed"
    assert _reviewed(review(monkeypatch, {})) == []
    # Расход разбора: вызов по сигналу и вызов о правилах эксперта (#190), оба своей стадией разбора ОС.
    assert "Потолок расходов контура ОС: $0.02 из $0.01" in admin.get("/api/feedback-journal/overview").json()["warning"]
    assert colleague.get("/api/feedback-journal/overview").json()["warning"] is None
    costs = admin.get("/api/stats/monthly").json()["ai_cost"]
    assert [(row["area"], row["runs"]) for row in costs if row["area"] == "feedback_review"] == [("feedback_review", 2)]

    # Отложенный срез окна не закрыл: его отметку берёт следующий.
    settings(admin, monthly_ceiling_usd=50)
    _collect(admin)
    assert _reviewed(review(monkeypatch, {})) == [second]


def test_old_nl_build_does_not_get_the_review_job_and_the_slice_shows_it_failed(issue, contour, monkeypatch):
    admin, colleague = issue["admin"], issue["colleague"]
    signal_id = business(issue["a"]["hi"])
    mark(colleague, signal_id, comment="Стадия: пилот")
    _collect(admin)

    assert worker("external-ai", contract_number=11).claim() is None

    [failed] = _slices(admin)
    assert failed["status"] == "failed"
    # С вызовом о правилах эксперта (#190) разбору ОС нужен контракт 13: сборка 12 сделала бы срез без него, молча.
    assert "нужен контракт 13" in failed["note"]
