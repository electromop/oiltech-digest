"""Общее для тестов «Карточки» в конвейере «Отбора» (#132, спека #128): статья сбора проходит «Отбор» и обработку
ленты (суть), затем оба вызова «Карточки» на подставной модели через настоящий клиент воркера NL
(tests/signal_card_support.worker). Сами тесты — test_card_theme.py (основной вызов: тема, дата события, возврат в
«Отбор»), test_signal_scores.py (второй вызов: баллы, ступень, «Решение платформы», «не тот блок») и
test_otbor_return.py (возврат отклонённого человеком).
"""

from datetime import datetime, timezone

import pytest
from psycopg.types.json import Json

from oiltech_digest import api, cli, signal_card
from oiltech_digest.db import repository
from oiltech_digest.processing.openai_client import AIResponse
from oiltech_digest.processing.seed import seed_default_scoring_criteria
from oiltech_digest.scoring_profiles import SCORING_PRESETS
from tests.otbor_support import answer, article, decide
from tests.signal_card_support import FakeModel, run_worker, tag
from tests.signal_card_support import answer as card_answer

CARD_MODEL, SCORES_MODEL = "gpt-5.4-nano", "gpt-5.4-mini"
DRILLING = "Бурение, заканчивание и внутрискважинные работы"
MARKET = "Рынок нефтесервиса"
DIGITAL = "Автоматизация, цифровизация"
# Тематики заказчика с ключами: по ним ядро считает темы-кандидаты.
THEMES = {DRILLING: ["бурение", "скважин"], MARKET: ["контракт", "сделка"], DIGITAL: ["цифровой", "робот"]}
PUBLISHED = datetime(2026, 10, 20, 9, tzinfo=timezone.utc)


@pytest.fixture()
def pipeline(monkeypatch, contour):
    """Конвейер «Отбора» с обоими вызовами «Карточки»: модели заданы, тематики и критерии скоринга заведены."""
    monkeypatch.setattr(api.config, "SIGNAL_CARD_ENABLED", True)
    monkeypatch.setattr(api.config, "SIGNAL_CARD_MODEL", CARD_MODEL)
    monkeypatch.setattr(api.config, "SIGNAL_SCORES_MODEL", SCORES_MODEL)
    for name, keywords in THEMES.items():
        theme(name, keywords)
    # Сид заводит в оба пустых профиля набор заказчика (SCORING_PRESETS["viktor"]).
    seed_default_scoring_criteria()


def theme(name, keywords):
    """Корневая тематика заказчика с ключами; номер тега."""
    tag_id = tag(name)
    with repository.get_connection() as conn:
        conn.execute("UPDATE tags SET keywords_json = %s WHERE id = %s", (Json(keywords), tag_id))
        conn.commit()
    return tag_id


def process_feed():
    """Обработка ленты (суть, перевод, тег, балл) — шаг планировщика и воркер NL с офлайн-клиентом."""
    cli.main(["enqueue-process", "--limit", "10", "--offline"])
    return run_worker("external-ai")


def accepted(monkeypatch, answers, texts):
    """Статьи сбора (texts: заголовок → текст) прошли «Отбор» (answers: заголовок → ответ «Отбора» или список
    ответов по очереди вызовов) и обработку ленты: у принятых есть суть. Номера статей по заголовкам и подставная
    модель «Отбора»: она отвечает и на возврат из «Карточки»."""
    ids = {title: article(title, text, published=PUBLISHED) for title, text in texts.items()}
    model = decide(monkeypatch, answers)
    process_feed()
    return ids, model


def fill_cards(monkeypatch, answers):
    """Основной вызов «Карточки»: шаг планировщика и воркер NL с подставной моделью; подставная модель."""
    model = FakeModel(answers)
    monkeypatch.setattr(signal_card, "make_client", lambda: model)
    cli.main(["enqueue-signal-cards"])
    run_worker("external-ai")
    return model


class ScoresModel:
    """Подставная модель второго вызова: ответ — заготовка по заголовку публикации; вызовы запоминает."""

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
        if callable(reply):
            reply = reply(schema)
        return AIResponse(data=reply, model=f"{model}-2026-03-17", input_tokens=2500, output_tokens=500)


def scores(points, *, named_customer=None, why="Так по линейке"):
    """Ответ второго вызова: баллы по именам критериев (points: имя → балл); у профиля с «Зрелостью» — назван ли
    заказчик внедрения."""
    reply = {"scores": [{"criterion": name, "score": value, "why": why} for name, value in points.items()]}
    if named_customer is not None:
        reply["named_customer"] = named_customer
    return reply


def profile_names(profile):
    """Имена критериев набора заказчика у профиля (business или tech_radar), по порядку."""
    return [criterion["name"] for criterion in SCORING_PRESETS["viktor"][profile]]


def even(profile, value, **overrides):
    """Баллы по всем критериям профиля, кроме «Свежести» (её считает код): value у каждого, overrides — по имени."""
    return {name: overrides.get(name, value) for name in profile_names(profile) if name != "Свежесть"}


def run_scores(monkeypatch, answers):
    """Второй вызов «Карточки»: шаг планировщика и воркер NL с подставной моделью; подставная модель."""
    from oiltech_digest import signal_scores

    model = ScoresModel(answers)
    monkeypatch.setattr(signal_scores, "make_client", lambda: model)
    cli.main(["enqueue-signal-scores"])
    run_worker("external-ai")
    return model


__all__ = ["CARD_MODEL", "DIGITAL", "DRILLING", "MARKET", "PUBLISHED", "SCORES_MODEL", "THEMES", "ScoresModel",
           "accepted", "answer", "card_answer", "even", "fill_cards", "pipeline", "process_feed", "profile_names",
           "run_scores", "scores", "theme"]
