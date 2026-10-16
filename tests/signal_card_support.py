"""Общее для тестов агента «Карточка сигнала» (#96): подставная модель, сигналы ленты и находки
радара на изолированной базе, настоящий клиент воркера NL против тестового ядра. Сами тесты —
test_signal_card.py (шов 1: применение и API) и test_signal_card_worker.py (шов 2: воркер, контракт,
полосы).
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from oiltech_digest import api, cli, contract, external_worker, signal_card
from oiltech_digest.db import repository
from oiltech_digest.processing import external_ai
from oiltech_digest.processing.openai_client import AIResponse

DRILLING = "Бурение, заканчивание и внутрискважинные работы"
MODEL = "gpt-5.4-nano"
CARD_KEYS = ("event_date", "participants", "event_type", "region", "technology_class", "technology_maturity",
             "oilfield_relevance", "oilfield_application", "why_interesting")


class FakeModel:
    """Подставная модель: ответ — заготовка по заголовку публикации; вызовы запоминает."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None,
                      reasoning_effort=None):
        self.calls.append({"instructions": instructions, "input": user_input, "schema": schema, "model": model})
        title = next(line.split(":", 1)[1].strip() for line in user_input.splitlines()
                     if line.startswith("Заголовок:"))
        reply = self.answers[title]
        if isinstance(reply, Exception):
            raise reply
        return AIResponse(data=reply, model=f"{model}-2026-03-17", input_tokens=3000, output_tokens=600)


def answer(**overrides):
    return {
        "event_date": "2026-09-24", "event_date_source": "из текста",
        "companies": [{"name": "SLB", "quote": "SLB подписала контракт"}],
        "event_type": "контракт", "region": "Ближний Восток",
        "technology_class": "неприменимо", "maturity": "неприменимо",
        "oilfield_relevance": "прямая", "oilfield_application": "Бурение скважин на шельфе",
        "why_important": "Крупный контракт на буровые услуги",
        **overrides,
    }


@pytest.fixture()
def user(isolated_db):
    return repository.create_user("signal-card@example.test", "long-enough-password", "user")


@pytest.fixture()
def admin(isolated_db):
    return repository.create_user("signal-card-admin@example.test", "long-enough-password", "admin")


@pytest.fixture()
def contour(monkeypatch):
    """Прод-маршрут: ИИ — на внешнем воркере, агент включён флагом, модель задана."""
    monkeypatch.setattr(api.config, "EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr(api.config, "AI_EXECUTION_REGION", "external")
    monkeypatch.setattr(api.config, "SIGNAL_CARD_ENABLED", True)
    monkeypatch.setattr(api.config, "SIGNAL_CARD_MODEL", MODEL)
    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))


def tag(name):
    with repository.get_connection() as conn:
        row = conn.execute("SELECT id FROM tags WHERE name = %s AND parent_id IS NULL", (name,)).fetchone()
        if row is None:
            row = conn.execute("INSERT INTO tags (name, enabled) VALUES (%s, TRUE) RETURNING id", (name,)).fetchone()
        conn.commit()
    return int(row[0])


def feed_signal(title, text, *, published=datetime(2026, 9, 25, 10, tzinfo=timezone.utc), source="Нефтегаз"):
    """Статья, принятая отбором, — сигнал ленты (мост #95)."""
    collected = datetime.now(timezone.utc)
    with repository.get_connection() as conn:
        source_row = conn.execute("SELECT id FROM sources WHERE name = %s", (source,)).fetchone()
        if source_row is None:
            source_row = conn.execute(
                "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
                "VALUES (%s, 'News', %s, TRUE, 'rss') RETURNING id",
                (source, f"https://{abs(hash(source))}.example"),
            ).fetchone()
        article_id = int(conn.execute(
            "INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'ru') RETURNING id",
            (source_row[0], title, f"https://feed.example/{abs(hash(title))}", published, collected, text),
        ).fetchone()[0])
        conn.commit()
    external_ai.apply_process_result({"articles": [{
        "article_id": article_id,
        "relevance": {"relevant": True, "reason": "r", "model": "m"},
        "summary": {"summary": f"Суть: {title}", "model": "m"},
        "translation": {"title_ru": title, "model": "m"},
        "tagging": {"tag_id": tag(DRILLING), "confidence": 0.9, "model": "m"},
    }]})
    return article_id


class _Core(TestClient):
    """Ядро для настоящего клиента воркера: срок запроса тестовому приложению не нужен."""

    def request(self, method, url, **kwargs):
        kwargs.pop("timeout", None)
        return super().request(method, url, **kwargs)


def radar_finding(key, title, text, **judge):
    """Находка поиска радара — как её пишет ядро после судьи (upsert_signal), со страницей издателя."""
    signal_id = repository.upsert_signal({"signal_key": key, "title": title, "title_ru": title, "theme": DRILLING,
                                          "maturity": "shortlist", "score": 70, "signal_category": "technology",
                                          **judge})
    repository.upsert_signal_evidence(signal_id, {
        "source_url": f"https://radar.example/{key}", "title": title, "publisher": "Offshore Weekly",
        "published_at": "2026-09-20T08:00:00+00:00", "extracted_fact": text,
        "raw_payload": {"evidence_source": "brave", "full_text_fetched": "true"},
    })
    repository.refresh_signal_evidence_count(signal_id)
    return signal_id


def worker(queue, *, contract_number=None):
    """Воркер NL: настоящий клиент, ядро — тестовое приложение. contract_number — номер контракта
    другой сборки; по умолчанию — текущей."""
    client = external_worker.ExternalWorkerClient(core_api_url="http://testserver", token="secret",
                                                  worker_id="nl-ai-1", queues=[queue], capabilities=["openai"])
    session = _Core(api.app)
    session.headers.update(dict(client.session.headers))
    if contract_number is not None:
        session.headers[contract.HEADER_CONTRACT] = str(contract_number)
    client.session = session
    return client


def run_worker(queue):
    client = worker(queue)
    jobs = []
    while (job := client.claim()) is not None:
        external_worker._run_job(client, job, lambda done=None: None)
        jobs.append(job)
    return jobs


def fill(monkeypatch, answers):
    """Шаг планировщика и воркер NL с подставной моделью: карточки сигналов по заготовкам ответов."""
    model = FakeModel(answers)
    monkeypatch.setattr(signal_card, "make_client", lambda: model)
    cli.main(["enqueue-signal-cards"])
    run_worker("external-ai")
    return model


def api_get(path, user, **params):
    api.app.dependency_overrides[api.require_user] = lambda: user
    try:
        response = TestClient(api.app).get(path, params=params)
    finally:
        api.app.dependency_overrides.pop(api.require_user, None)
    assert response.status_code == 200, response.text
    return response.json()


def business(user):
    return {card["title"]: card for card in api_get("/api/signals", user, showcase="business")}
