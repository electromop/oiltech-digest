"""Общее для тестов журнала ОС (#189, спека #187): подставная модель «Разбора ОС», срез и прогон воркера NL против
тестового ядра, отметки людей и карточки обеих витрин. Сами тесты: test_os_journal_api.py (шов 1: HTTP API на тестовой
базе) и test_os_journal_slice.py (шов 2: срез и разбор ОС через внешний воркер с подставной моделью).
"""

from __future__ import annotations

from datetime import datetime

from oiltech_digest import feedback_review
from oiltech_digest.feed_window import MSK
from oiltech_digest.processing.openai_client import AIResponse
from tests.digest_user_path_support import feed_signal, set_card
from tests.signal_card_support import run_worker

MODEL = "gpt-6-sol"


class ReviewModel:
    """Подставная модель разбора ОС: ответ по номеру карточки из первой строки входа («Карточка №N»); нет заготовки:
    улучшать нечего. Вызовы запоминает."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None,
                      reasoning_effort=None):
        self.calls.append({"instructions": instructions, "input": user_input, "schema": schema, "model": model})
        number = int(user_input.splitlines()[0].split("№", 1)[1])
        reply = self.answers.get(number, {"improvements": []})
        if isinstance(reply, Exception):
            raise reply
        return AIResponse(data=reply, model=f"{model}-2026-11-01", input_tokens=4000, output_tokens=500)


def improvement(field: str, value: str, marks: list[int], quote: str) -> dict:
    return {"field": field, "value": value, "marks": marks, "quote": quote}


def review(monkeypatch, answers) -> ReviewModel:
    """Воркер NL с подставной моделью разбора: задачи разбора ОС в очереди дня."""
    model = ReviewModel(answers)
    monkeypatch.setattr(feedback_review, "make_client", lambda: model)
    run_worker("external-ai")
    return model


def mark(person, signal_id: int, **fields) -> dict:
    """Отметка ОС с карточки, как её шлёт форма."""
    response = person.post("/api/signals/feedback", json={"signal_id": signal_id, "comment": "", **fields})
    assert response.status_code == 200, response.text
    return response.json()


def card(signal_id: int, *, category: str = "business", **fields) -> int:
    """Карточка после «Отбора»: принята, категория витрины, карточка и баллы есть."""
    set_card(signal_id, otbor_decision="accept", signal_category=category, card_recipe={"agent": "signal_card"},
             **fields)
    return signal_id


def business(article_id: int, **fields) -> int:
    """Сигнал ленты статьи в бизнес-ленте, принятый «Отбором»."""
    return card(feed_signal(article_id), **fields)


def journal(person, **params) -> list[dict]:
    response = person.get("/api/feedback-journal", params={"limit": 200, **params})
    assert response.status_code == 200, response.text
    return response.json()["entries"]


def entry(person, key: str) -> dict:
    [found] = [one for one in journal(person) if one["key"] == key]
    return found


def cards(person, showcase: str = "business") -> dict[int, dict]:
    response = person.get("/api/signals", params={"limit": 200, "showcase": showcase})
    assert response.status_code == 200, response.text
    return {row["id"]: row for row in response.json()}


def settings(person, **values) -> dict:
    response = person.put("/api/feedback-journal/settings", json={"values": values})
    assert response.status_code == 200, response.text
    return response.json()["values"]


def today_settings(person) -> dict:
    """Срез по расписанию сегодня с полуночи по Москве: шаг планировщика возьмёт его на этом же цикле."""
    return settings(person, slice_weekday=str(datetime.now(MSK).isoweekday()), slice_time="00:00")
