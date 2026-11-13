"""Правила эксперта (#190, спека #187, ADR 0007), шов 1: HTTP API платформы на тестовой базе.

Предложение правила ждёт решения администратора в журнале ОС. «Согласовать» и «изменить» выпускают новую версию набора
правил агента сразу, откат делает действующей прежнюю; больше 20 действующих правил у агента согласовать нельзя.
Согласование без готовой автопроверки, с «мало данных» или с перевесом «от эталона» требует отдельного подтверждения.
Решения, откат и проверка заново только у администратора. Прошлые сигналы при согласовании и откате не пересчитываются.
"""

from __future__ import annotations

from oiltech_digest.db import connection
from tests.digest_user_path_support import issue  # noqa: F401: фикстура
from tests.expert_rules_support import agent_rules, approve, checked, decide, proposal, rollback
from tests.os_journal_support import business, cards, entry, journal
from tests.signal_card_support import tag

STAGE_HINTS = ("«expected to contribute» значит ожидаемый, не подтверждённый эффект; «evaluate feasibility» значит "
               "применимость ещё оценивается; «PoC» не промышленное внедрение.")


def _labels(found: dict) -> list[str]:
    return [step["label"] for step in found["path"]]


def test_approval_issues_a_new_rule_set_version_and_rollback_makes_the_previous_one_current(issue):
    admin, colleague = issue["admin"], issue["colleague"]
    first = proposal("card", "maturity", STAGE_HINTS)
    second = proposal("card", "theme", "Обзор рынка одной технологии относится к теме рынка.", category="business")
    checked(first)
    checked(second)

    pending = entry(colleague, f"action:{first}")
    assert (pending["kind"], pending["status"], pending["can_decide"]) == ("rule_proposal", "pending", False)
    assert (pending["rule"]["agent_label"], pending["rule"]["step_label"], pending["rule"]["text"]) == (
        "«Карточка»", "стадия", STAGE_HINTS)
    assert (pending["check"]["rows"], pending["check"]["toward"], pending["check"]["away"]) == (40, 6, 1)
    assert pending["confirm_reasons"] == [] and entry(admin, f"action:{first}")["can_decide"] is True

    assert approve(admin, first)["version"] == 1
    assert approve(admin, second)["version"] == 2
    card = agent_rules(colleague, "card")
    assert card["version"] == 2
    assert [(rule["step"], rule["category"], rule["text"]) for rule in card["rules"]] == [
        ("maturity", None, STAGE_HINTS), ("theme", "business", "Обзор рынка одной технологии относится к теме рынка.")]
    applied = entry(admin, f"action:{second}")
    assert (applied["status"], applied["version"], applied["can_rollback"]) == ("applied", 2, True)
    assert _labels(applied)[1:] == ["Согласовано", "Применено системой: версия 2 набора правил агента «Карточка»"]
    # Откатить можно только действующую версию.
    assert entry(admin, f"action:{first}")["can_rollback"] is False
    assert rollback(admin, first).status_code == 409

    response = rollback(admin, second)
    assert response.status_code == 200, response.text
    assert (response.json()["from_version"], response.json()["to_version"]) == (2, 1)
    card = agent_rules(colleague, "card")
    assert (card["version"], [rule["text"] for rule in card["rules"]]) == (1, [STAGE_HINTS])
    undone = entry(admin, f"action:{second}")
    assert (undone["status"], _labels(undone)[-1]) == ("undone", "Откат: действует версия 1 вместо 2")
    assert entry(admin, f"action:{first}")["can_rollback"] is True
    assert rollback(admin, first).json()["to_version"] == 0
    assert agent_rules(colleague, "card")["rules"] == []
    # Следующее согласование: новый номер версии, прежние номера не переиспользуются.
    third = proposal("card", "region", "Регион события: по месту работ, а не по штаб-квартире заказчика.")
    checked(third)
    assert approve(admin, third)["version"] == 3


def test_more_than_twenty_active_rules_of_an_agent_cannot_be_approved_but_a_merge_can(issue):
    admin = issue["admin"]
    approved = []
    for number in range(20):
        action_id = proposal("otbor", "decision", f"Правило отбора номер {number + 1}: брать отчёт о бурении.")
        checked(action_id, "otbor")
        approve(admin, action_id)
        approved.append(action_id)
    rules = agent_rules(admin, "otbor")["rules"]
    assert len(rules) == 20

    extra = proposal("otbor", "decision", "Двадцать первое правило: брать итоги тендеров на бурение.")
    checked(extra, "otbor")
    refused = decide(admin, extra)
    assert refused.status_code == 409
    assert "не больше 20" in refused.json()["detail"]
    assert entry(admin, f"action:{extra}")["status"] == "pending"

    merge = proposal("otbor", "decision", "Брать отчёты о бурении и итоги тендеров на бурение.", kind="merge",
                     replaces=(rules[0]["id"], rules[1]["id"]))
    checked(merge, "otbor")
    assert approve(admin, merge)["version"] == 21
    assert len(agent_rules(admin, "otbor")["rules"]) == 19
    assert approve(admin, extra)["version"] == 22


def test_approval_without_a_check_with_little_data_or_away_from_reference_needs_a_separate_confirmation(issue):
    admin = issue["admin"]
    unchecked = proposal("scores", "criteria", "Рекорд компании не новизна для отрасли.")
    little = proposal("scores", "criteria", "Сумма договора не эффект для заказчика.")
    away = proposal("scores", "criteria", "Пилот без заказчика: зрелость ниже внедрения.")
    checked(little, "scores", rows=12, toward=3, away=0)
    checked(away, "scores", rows=60, toward=2, away=5)

    assert entry(admin, f"action:{unchecked}")["confirm_reasons"] == ["автопроверки ещё не было"]
    assert entry(admin, f"action:{little}")["confirm_reasons"] == ["мало данных: 12"]
    assert entry(admin, f"action:{away}")["confirm_reasons"] == ["от эталона 5, к эталону 2"]
    for action_id, reason in ((unchecked, "автопроверки ещё не было"), (little, "мало данных: 12"),
                              (away, "от эталона 5, к эталону 2")):
        refused = decide(admin, action_id)
        assert refused.status_code == 409
        assert refused.json()["detail"] == f"нужно отдельное подтверждение: {reason}"
        assert entry(admin, f"action:{action_id}")["status"] == "pending"
    assert agent_rules(admin, "scores")["version"] == 0

    assert approve(admin, little, confirm=True)["version"] == 1
    assert _labels(entry(admin, f"action:{little}"))[1] == "Согласовано с отдельным подтверждением (мало данных: 12)"


def test_modify_applies_the_admin_wording_and_a_search_rule_with_free_text_is_refused(issue):
    admin = issue["admin"]
    tag("Цифровизация")
    card_rule = proposal("card", "maturity", "PoC не промышленное внедрение.")
    search_rule = proposal("search", "topics", "цифровой двойник; предиктивное обслуживание", topic="Цифровизация")
    checked(card_rule)
    checked(search_rule, "search")

    # Проверка мерила исходную формулировку: новую согласовать можно только с отдельным подтверждением.
    refused = decide(admin, card_rule, "modify", text="PoC и пилот: не промышленное внедрение.")
    assert (refused.status_code, refused.json()["detail"]) == (
        409, "нужно отдельное подтверждение: новая формулировка не проходила автопроверку")
    response = decide(admin, card_rule, "modify", text="  PoC и пилот:   не промышленное внедрение. ", confirm=True)
    assert response.status_code == 200, response.text
    assert agent_rules(admin, "card")["rules"][0]["text"] == "PoC и пилот: не промышленное внедрение."
    modified = entry(admin, f"action:{card_rule}")
    assert (modified["rule"]["text"], modified["proposed_rule"]["text"]) == (
        "PoC и пилот: не промышленное внедрение.", "PoC не промышленное внедрение.")
    assert _labels(modified)[1] == ("Изменено с отдельным подтверждением (новая формулировка не проходила "
                                    "автопроверку)")

    for text in ("Ищи только на сайте example.com", "не искать слухи; цифровой двойник",
                 "Всегда начинай поиск с отчётов операторов, а потом переходи к новостям сервисных компаний"):
        refused = decide(admin, search_rule, "modify", text=text, confirm=True)
        assert refused.status_code == 409, text
        assert "свободный текст" in refused.json()["detail"]
    assert agent_rules(admin, "search")["rules"] == []

    assert decide(admin, search_rule, "dismiss").json() == {"status": "dismissed"}
    dismissed = entry(admin, f"action:{search_rule}")
    assert (dismissed["status"], _labels(dismissed)[-1], dismissed["can_decide"]) == (
        "dismissed", "Убрано, остаётся в журнале", False)


def test_rule_decisions_rollback_and_recheck_are_admin_only_and_the_rules_are_visible_to_all(issue):
    admin, colleague = issue["admin"], issue["colleague"]
    action_id = proposal("dedup", "same_event", "Пилот и промышленный запуск: разные события.")
    checked(action_id, "dedup", rows=4, toward=1, away=0)

    assert decide(colleague, action_id).status_code == 403
    assert rollback(colleague, action_id).status_code == 403
    assert colleague.post(f"/api/feedback-journal/proposals/{action_id}/check").status_code == 403
    approve(admin, action_id, confirm=True)
    assert rollback(colleague, action_id).status_code == 403

    seen = agent_rules(colleague, "dedup")
    assert (seen["label"], seen["version"], [rule["text"] for rule in seen["rules"]]) == (
        "«Дедуп»", 1, ["Пилот и промышленный запуск: разные события."])
    assert colleague.get("/api/feedback-journal/rules").json()["can_rollback"] is False
    assert [row["key"] for row in journal(colleague, agent="dedup", kind="rule_proposal")] == [f"action:{action_id}"]


def test_approval_and_rollback_do_not_recompute_past_signals(issue):
    admin = issue["admin"]
    signal_id = business(issue["a"]["hi"], technology_maturity="прототип", score_band="useful", score=70)
    before = cards(admin)[signal_id]
    jobs_before = _jobs()
    action_id = proposal("card", "maturity", "PoC не промышленное внедрение.")
    checked(action_id)

    approve(admin, action_id)
    assert rollback(admin, action_id).status_code == 200
    after = cards(admin)[signal_id]
    assert {key: after[key] for key in ("technology_maturity", "score", "maturity")} == {
        key: before[key] for key in ("technology_maturity", "score", "maturity")}
    # Ни согласование, ни откат задач агентам не ставят: правило действует для новых вызовов.
    assert _jobs() == jobs_before


def _jobs() -> list[tuple[str, int]]:
    with connection.get_connection() as conn:
        return conn.execute("SELECT kind, count(*) FROM background_jobs GROUP BY kind ORDER BY kind").fetchall()
