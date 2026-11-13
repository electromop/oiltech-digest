"""Общее для тестов правил эксперта и автопроверки (#190, спека #187): предложение правила в журнале ОС, готовый итог
его автопроверки, решения администратора и набор правил агентов через HTTP API. Сами тесты: test_expert_rules_api.py
(шов 1: HTTP API на тестовой базе), test_expert_rules_agents.py и test_rule_check.py (шов 2: задачи агентов и
автопроверки через внешний воркер с подставной моделью).
"""

from __future__ import annotations

from oiltech_digest import expert_rules, feedback_rules
from oiltech_digest.db import connection, rule_check_store


def proposal(agent: str, step: str, text: str, *, category: str | None = None, topic: str | None = None,
             kind: str = "new", replaces: tuple[int, ...] = (), event_ids: tuple[int, ...] = (),
             quote: str | None = None) -> int:
    """Предложение правила в журнале ОС, как его пишет разбор ОС; номер."""
    rule = expert_rules.checked_rule(agent, step, category, topic, text, topics=[topic] if topic else [])
    with connection.get_connection() as conn:
        action_id = feedback_rules.create_proposal(rule, rule_kind=kind, replaces=list(replaces),
                                                   event_ids=list(event_ids), quote=quote, slice_id=None,
                                                   author="Разбор ОС", conn=conn)
        conn.commit()
    return action_id


def checked(action_id: int, agent: str = "card", *, rows: int = 40, toward: int = 6, away: int = 1,
            little: bool | None = None) -> int:
    """Готовая автопроверка предложения: счёт к эталону и от эталона, «мало данных» при меньше 30 строках."""
    little = rows < 30 if little is None else little
    check_id = rule_check_store.create(action_id=action_id, mode="rule", agent=agent, status="queued")
    rule_check_store.finish(check_id, status="done", row_count=rows, toward=toward, away=away,
                            unchanged=rows - toward - away, advice="мало данных" if little else "согласовать",
                            details={"little_data": little})
    return check_id


def decide(person, action_id: int, decision: str = "approve", **body):
    return person.post(f"/api/feedback-journal/proposals/{action_id}/decision", json={"decision": decision, **body})


def approve(person, action_id: int, **body) -> dict:
    response = decide(person, action_id, "approve", **body)
    assert response.status_code == 200, response.text
    return response.json()


def rollback(person, action_id: int):
    return person.post(f"/api/feedback-journal/proposals/{action_id}/rollback")


def agent_rules(person, agent: str) -> dict:
    response = person.get("/api/feedback-journal/rules")
    assert response.status_code == 200, response.text
    [found] = [one for one in response.json()["agents"] if one["agent"] == agent]
    return found


def approved(agent: str, step: str, text: str, *, category: str | None = None, topic: str | None = None) -> int:
    """Действующее правило: предложение с готовой проверкой, согласованное администратором; номер версии набора."""
    action_id = proposal(agent, step, text, category=category, topic=topic)
    checked(action_id, agent)
    return int(feedback_rules.decide(action_id, "approve", author="admin@example.test", user_id=None,
                                     confirm=True)["version"])


class RecordingModel:
    """Подставная модель одного вызова: ответ по имени схемы (или функцией от входа), вызовы запоминает."""

    def __init__(self, replies, *, model="gpt-6-test"):
        self.replies = replies
        self.model = model
        self.calls = []

    def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None,
                      reasoning_effort=None):
        from oiltech_digest.processing.openai_client import AIResponse

        self.calls.append({"instructions": instructions, "input": user_input, "schema": schema["name"],
                           "model": model})
        reply = self.replies[schema["name"]]
        if callable(reply):
            reply = reply(user_input)
        if isinstance(reply, Exception):
            raise reply
        return AIResponse(data=reply, model=model or self.model, input_tokens=1000, output_tokens=100)

    def instructions(self, schema: str) -> list[str]:
        return [call["instructions"] for call in self.calls if call["schema"] == schema]
