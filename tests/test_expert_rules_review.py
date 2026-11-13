"""Правила эксперта (#190, спека #187, ADR 0007), шов 2: «Разбор ОС» недельного среза предлагает правила агентам,
задача идёт через настоящий клиент воркера NL с подставной моделью.

Правило предлагается от двух отметок об одном за окно или от одной явной просьбы; похожее на действующее: уточнением
его, у агента с полным набором (20): сведением похожих. Код ядра отбрасывает правило ниже порога, без цитаты из текста
отметок, без новой отметки с прошлого среза, с агентом или шагом не из списка и правило поиска со свободным текстом.
Каждое записанное предложение сразу получает автопроверку. Имена авторов модели не уходят.
"""

from __future__ import annotations

from oiltech_digest import config, feedback_review
from oiltech_digest.processing.openai_client import AIResponse
from tests.digest_user_path_support import issue  # noqa: F401: фикстура
from tests.expert_rules_support import agent_rules, approve, approved
from tests.os_journal_support import business, entry, journal, mark
from tests.otbor_support import contour  # noqa: F401: фикстура
from tests.signal_card_support import DRILLING, run_worker, tag

HINT = "PoC это не промышленное внедрение"


class RulesReview:
    """Подставная модель разбора ОС: у сигналов улучшать нечего, о правилах: заготовка (функция от входа: номера
    отметок в нём заранее не известны). Вызовы запоминает."""

    def __init__(self, rules):
        self.rules = rules
        self.calls = []

    def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None,
                      reasoning_effort=None):
        self.calls.append({"instructions": instructions, "input": user_input, "schema": schema["name"]})
        data = ({"rules": self.rules(user_input) if callable(self.rules) else self.rules}
                if schema["name"] == "feedback_rules" else {"improvements": []})
        return AIResponse(data=data, model=f"{model}-2026-11-01", input_tokens=6000, output_tokens=700)


def rule(agent, step, text, marks, quote, *, category="", topic="", kind="new", replaces=()):
    return {"agent": agent, "step": step, "category": category, "topic": topic, "text": text, "kind": kind,
            "replaces": list(replaces), "marks": marks, "quote": quote}


def _slice(admin, monkeypatch, rules):
    # Автопроверки встают в полосу пересчётов: здесь их не прогоняют (tests/test_rule_check.py).
    monkeypatch.setattr(config, "AI_BULK_LANE_ENABLED", True)
    response = admin.post("/api/feedback-journal/slices")
    assert response.status_code == 200, response.text
    model = RulesReview(rules)
    monkeypatch.setattr(feedback_review, "make_client", lambda: model)
    run_worker("external-ai")
    return model


def _proposals(person):
    """Предложения правил, которые ждут решения: правило → строка журнала."""
    return {row["rule"]["text"]: row for row in journal(person, kind="rule_proposal", status="pending")}


def test_review_proposes_rules_from_two_similar_marks_or_one_explicit_request_and_each_gets_an_autocheck(
        issue, contour, monkeypatch):
    admin, colleague, a = issue["admin"], issue["colleague"], issue["a"]
    tag(DRILLING)
    first, second, third = business(a["hi"]), business(a["mid"]), business(a["low"])
    similar = [mark(colleague, first, verdict="watch_later", comment=f"{HINT}, рано в выпуск")["event_id"],
               mark(admin, second, verdict="approved", reason=f"{HINT}: стадия завышена")["event_id"]]
    explicit = mark(admin, third, verdict="approved",
                    comment="Всегда переводить well control как контроль скважины")["event_id"]
    single = mark(colleague, third, comment="Сделка оператора, это бизнес")["event_id"]

    model = _slice(admin, monkeypatch, [
        rule("card", "maturity", "«PoC» не промышленное внедрение.", similar, HINT),
        rule("summary", "terms", "Переводить well control как контроль скважины.", [explicit],
             "Всегда переводить well control как контроль скважины"),
        rule("otbor", "category", "Сделка оператора: бизнес.", [single], "Сделка оператора, это бизнес"),
        rule("otbor", "decision", "Брать отчёты о бурении.", similar, "цитаты такой нет"),
        rule("card", "stage", "Шаг не из списка.", similar, HINT),
        rule("search", "topics", "Ищи только на сайтах операторов", similar, HINT, topic=DRILLING),
        rule("search", "topics", "интеллектуальное заканчивание", similar, HINT, topic="Космос"),
    ])

    rules_call = next(call for call in model.calls if call["schema"] == "feedback_rules")
    assert "colleague@example.test" not in rules_call["input"] and "admin@example.test" not in rules_call["input"]
    assert f"Отметка №{similar[1]} (администратор" in rules_call["input"]
    assert f"Отметка №{similar[0]} (участник" in rules_call["input"]
    proposals = _proposals(admin)
    assert set(proposals) == {"«PoC» не промышленное внедрение.", "Переводить well control как контроль скважины."}
    stage = proposals["«PoC» не промышленное внедрение."]
    assert (stage["status"], stage["rule"]["agent"], stage["rule"]["step"], stage["event_ids"], stage["quote"]) == (
        "pending", "card", "maturity", sorted(similar), HINT)
    assert {basis["author"] for basis in stage["basis"]} == {"colleague@example.test", "admin@example.test"}
    # Каждое предложение сразу получает автопроверку; эталона ОС у этих шагов ещё нет: «мало данных: 0» сразу.
    assert {(row["check"]["status"], row["check"]["advice"], row["check"]["little_data"])
            for row in proposals.values()} == {("done", "мало данных: 0", True)}
    assert stage["confirm"] == ["мало данных: 0"]
    assert [step["label"] for step in stage["path"]][0].startswith("Предложено разбором ОС (срез №")
    slices = admin.get("/api/feedback-journal/overview").json()["slices"]
    assert slices[0]["proposals"] == 2
    assert agent_rules(admin, "card")["rules"] == []


def test_review_refines_a_similar_rule_instead_of_a_duplicate_and_merges_when_the_set_is_full(
        issue, contour, monkeypatch):
    admin, a = issue["admin"], issue["a"]
    approved("card", "maturity", "PoC не внедрение.")
    for number in range(20):
        approved("otbor", "decision", f"Правило отбора номер {number + 1}.")
    card_rule = agent_rules(admin, "card")["rules"][0]["id"]
    full = [one["id"] for one in agent_rules(admin, "otbor")["rules"]]
    first, second = business(a["hi"]), business(a["mid"])
    marks = [mark(admin, first, verdict="approved", reason=f"{HINT}, и пилот тоже")["event_id"],
             mark(admin, second, verdict="watch_later", reason=f"{HINT}: это проверка")["event_id"]]

    _slice(admin, monkeypatch, [
        rule("card", "maturity", "PoC и пилот не промышленное внедрение.", marks, HINT, kind="refine",
             replaces=[card_rule]),
        rule("card", "maturity", "PoC не внедрение.", marks, HINT),
        rule("otbor", "decision", "Двадцать первое правило отбора.", marks, HINT),
        rule("otbor", "decision", "Правила отбора 1 и 2 вместе.", marks, HINT, kind="merge", replaces=full[:2]),
        rule("otbor", "decision", "Уточнение чужого правила.", marks, HINT, kind="refine", replaces=[card_rule]),
    ])

    proposals = _proposals(admin)
    assert set(proposals) == {"PoC и пилот не промышленное внедрение.", "Правила отбора 1 и 2 вместе."}
    refine = proposals["PoC и пилот не промышленное внедрение."]
    assert (refine["rule_kind"], [one["text"] for one in refine["replaced"]]) == ("refine", ["PoC не внедрение."])
    merge = proposals["Правила отбора 1 и 2 вместе."]
    assert (merge["rule_kind"], len(merge["replaced"])) == ("merge", 2)

    approve(admin, refine["id"], confirm=True)
    assert [one["text"] for one in agent_rules(admin, "card")["rules"]] == ["PoC и пилот не промышленное внедрение."]
    approve(admin, merge["id"], confirm=True)
    assert len(agent_rules(admin, "otbor")["rules"]) == 19


def test_rule_without_a_new_mark_since_the_last_slice_is_not_proposed(issue, contour, monkeypatch):
    admin, a = issue["admin"], issue["a"]
    first, second = business(a["hi"]), business(a["mid"])
    old = [mark(admin, first, verdict="approved", reason=f"{HINT}, рано")["event_id"],
           mark(admin, second, verdict="approved", reason=f"{HINT}, тоже")["event_id"]]
    _slice(admin, monkeypatch, [])
    new = mark(admin, business(a["low"]), verdict="approved", reason=f"{HINT} и здесь")["event_id"]

    _slice(admin, monkeypatch, [rule("card", "maturity", "«PoC» не промышленное внедрение.", old, HINT),
                                rule("card", "maturity", "PoC и пилот: не внедрение.", [*old, new], HINT)])
    assert set(_proposals(admin)) == {"PoC и пилот: не внедрение."}
    assert entry(admin, f"action:{_proposals(admin)['PoC и пилот: не внедрение.']['id']}")["status"] == "pending"
