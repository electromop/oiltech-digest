"""Общее для тестов агента «Отбор» (#130, спека #128): подставная модель «Отбора», статьи сбора, вставленные
так, как их вставляет сбор (repository.insert_article: при включённом флаге статья идёт через «Отбор»), и
настоящий клиент воркера NL против тестового ядра (signal_card_support.worker). Сами тесты —
test_otbor_feed.py (шов 1: контур целиком, витрины и разделы), test_otbor_feed_machinery.py (шов 1: прежняя
механика ленты при «Отборе») и test_otbor_worker.py (шов 1: воркер, контракт, сбои).
"""

from datetime import datetime, timezone
import itertools

from fastapi.testclient import TestClient
import pytest

from oiltech_digest import api, cli, otbor
from oiltech_digest.db import repository
from oiltech_digest.processing.openai_client import AIResponse
from tests.signal_card_support import api_get, run_worker

MODEL = "gpt-5.4-mini"
JUSTIFICATION = "Программа бурения оператора меняет спрос на нефтесервис"
_NUMBERS = itertools.count(1)


class OtborModel:
    """Подставная модель «Отбора»: ответ — заготовка по заголовку публикации; список заготовок — ответы по
    очереди вызовов (повтор после конфликта шагов), последняя повторяется. Вызовы запоминает."""

    def __init__(self, answers):
        self.answers = {title: list(reply) if isinstance(reply, list) else [reply] for title, reply in answers.items()}
        self.calls = []

    def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None,
                      reasoning_effort=None):
        self.calls.append({"instructions": instructions, "input": user_input, "schema": schema, "model": model})
        title = next(line.split(":", 1)[1].strip() for line in user_input.splitlines()
                     if line.startswith("Заголовок:"))
        replies = self.answers[title]
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, Exception):
            raise reply
        return AIResponse(data=reply, model=f"{model}-2026-03-17", input_tokens=2000, output_tokens=300)


def answer(**overrides):
    """Ответ «Отбора» по схеме: нефтегаз, прямая связь, брать, бизнес."""
    return {
        "industry": "oil_gas", "oilfield_relevance": "direct", "oilfield_application": "",
        "decision": "accept", "reject_reason": "none", "justification": JUSTIFICATION, "category": "business",
        **overrides,
    }


def reject(reason, **overrides):
    """Отказ «Отбора» с причиной из справочника: по умолчанию нефтегаз без связи с нефтесервисом."""
    return answer(**{"oilfield_relevance": "none", "decision": "reject", "reject_reason": reason,
                     "justification": "Котировки нефти без связи с нефтесервисом", **overrides})


@pytest.fixture()
def contour(monkeypatch):
    """Прод-маршрут: ИИ — на внешнем воркере, «Отбор» включён флагом, модель задана."""
    monkeypatch.setattr(api.config, "EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr(api.config, "AI_EXECUTION_REGION", "external")
    monkeypatch.setattr(api.config, "OTBOR_ENABLED", True)
    monkeypatch.setattr(api.config, "OTBOR_MODEL", MODEL)
    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))


def article(title, text, *, source="Нефтегаз", language="ru",
            published=datetime(2026, 10, 20, 9, tzinfo=timezone.utc)):
    """Статья сбора — как её вставляет сбор (insert_article); номер статьи."""
    number = next(_NUMBERS)
    with repository.get_connection() as conn:
        row = conn.execute("SELECT id FROM sources WHERE name = %s", (source,)).fetchone()
        if row is None:
            row = conn.execute(
                "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
                "VALUES (%s, 'Media', %s, TRUE, 'rss') RETURNING id",
                (source, f"https://source-{number}.example"),
            ).fetchone()
        conn.commit()
    url = f"https://feed.example/otbor/{number}"
    assert repository.insert_article({"source_id": int(row[0]), "title": title, "url": url, "published_at": published,
                                      "raw_text": text, "text_truncated": False, "language": language,
                                      "content_hash": f"otbor-{number}"})
    with repository.get_connection() as conn:
        return int(conn.execute("SELECT id FROM articles WHERE url = %s", (url,)).fetchone()[0])


def decide(monkeypatch, answers, *args):
    """Шаг планировщика «Отбор» и воркер NL с подставной моделью; подставная модель."""
    model = OtborModel(answers)
    monkeypatch.setattr(otbor, "make_client", lambda: model)
    cli.main(["enqueue-otbor", *args])
    run_worker("external-ai")
    return model


def showcase(user, name="business", **params):
    return {card["title"]: card for card in api_get("/api/signals", user, showcase=name, **params)}


def api_request(method, path, user, body):
    """Запрос к API от имени пользователя; ответ как есть."""
    api.app.dependency_overrides[api.require_user] = lambda: user
    try:
        return TestClient(api.app).request(method, path, json=body)
    finally:
        api.app.dependency_overrides.pop(api.require_user, None)


def api_send(method, path, user, body):
    """Запрос к API от имени пользователя, который обязан пройти; тело ответа."""
    response = api_request(method, path, user, body)
    assert response.status_code == 200, response.text
    return response.json()
