"""Прогон по истории агента «Карточка сигнала» (#98, спека #93), шов 4 — команда enqueue-signal-card-history на
подставной модели: лента с августа 2026 (по дате сбора статьи) и весь радар — по полосе пересчётов, сначала текущее
окно ленты и радар, затем остальное; сухой прогон по умолчанию печатает число сигналов и цену и ничего не ставит;
повторный запуск не платит за карточки текущего рецепта (tests/signal_card_support.py).
"""

from datetime import datetime, timezone

import pytest

from oiltech_digest import api, cli, feed_window, signal_card
from oiltech_digest.db import repository
from tests.signal_card_support import MODEL, FakeModel, answer, api_get, business, feed_signal, radar_finding, run_worker
from tests.signal_card_support import admin, contour, user  # noqa: F401 — фикстуры

MINI = "gpt-5.4-mini"
TEXT = "SLB подписала контракт с Saudi Aramco."


@pytest.fixture()
def october(monkeypatch):
    """19 октября по Москве: окно ленты — октябрь (день смены окна прошёл); полоса пересчётов включена."""
    monkeypatch.setattr(feed_window, "_now", lambda: datetime(2026, 10, 19, 12, tzinfo=feed_window.MSK))
    monkeypatch.setattr(api.config, "AI_BULK_LANE_ENABLED", True)


def published(month):
    return datetime(2026, month, 10, 10, tzinfo=timezone.utc)


def signal_of(article_id):
    with repository.get_connection() as conn:
        return int(conn.execute("SELECT id FROM signals WHERE origin_article_id = %s", (article_id,)).fetchone()[0])


def history(*args):
    cli.main(["enqueue-signal-card-history", *args])


def card_jobs(admin):
    return sorted(api_get("/api/jobs", admin, kind="signal_card"), key=lambda job: job["id"])


def test_history_goes_to_the_bulk_lane_window_and_radar_first_and_a_rerun_pays_for_nothing(
        user, admin, contour, october, monkeypatch, capsys):
    monkeypatch.setattr(api.config, "SIGNAL_CARD_BATCH", 2)
    august = signal_of(feed_signal("Август", TEXT, published=published(8)))
    current = signal_of(feed_signal("Октябрь", TEXT, published=published(10)))
    robot = radar_finding("robot", "Робот", "Equinor испытала робота.")
    september = signal_of(feed_signal("Сентябрь", TEXT, published=published(9)))
    model = FakeModel({title: answer() for title in ("Август", "Октябрь", "Робот", "Сентябрь")})
    monkeypatch.setattr(signal_card, "make_client", lambda: model)

    history("--apply")

    # Задачи выдаются по порядку создания: сначала текущее окно ленты и радар, затем остальное — и не в поток дня.
    assert [(job["queue"], job["payload"]["signal_ids"], job["payload"]["trigger"]) for job in card_jobs(admin)] == [
        ("external-ai-bulk", [current, robot], "history"), ("external-ai-bulk", [august, september], "history")]
    run_worker("external-ai-bulk")
    assert len(model.calls) == 4
    capsys.readouterr()

    history("--apply")  # повторный запуск: всё заполнено текущим рецептом

    assert "к заполнению 0" in capsys.readouterr().out
    assert len(card_jobs(admin)) == 2 and len(model.calls) == 4


def test_history_refills_only_cards_of_another_recipe_and_skips_duplicates_and_running_jobs(
        user, admin, contour, october, monkeypatch, capsys):
    titles = ("Заполнен", "Старые справочники", "Старая инструкция", "Дубль")
    for title in titles:
        feed_signal(title, TEXT, published=published(10))
    monkeypatch.setattr(signal_card, "make_client", lambda: FakeModel({title: answer() for title in titles}))
    cli.main(["enqueue-signal-cards"])  # шаг планировщика: модель отвечает снимком с датой — это та же модель
    run_worker("external-ai")
    ids = {title: card["id"] for title, card in business(user).items()}
    with repository.get_connection() as conn:
        for title, key in (("Старые справочники", "dictionaries_version"), ("Старая инструкция", "prompt_version")):
            conn.execute("UPDATE signals SET card_recipe = card_recipe || jsonb_build_object(%s::text, '2026-10-01.1') "
                         "WHERE id = %s", (key, ids[title]))
        conn.commit()
    assert repository.mark_signal_merged(ids["Дубль"], ids["Заполнен"], "то же событие")
    fresh = signal_of(feed_signal("Новый", TEXT, published=published(10)))
    queued = signal_of(feed_signal("В задаче", TEXT, published=published(10)))
    cli.main(["enqueue-signal-cards", "--ids", str(queued)])  # пересчёт названного сигнала ещё идёт
    capsys.readouterr()

    history()

    out = capsys.readouterr().out
    assert "сигналов 5; заполнены этим рецептом — 1, уже в задачах агента — 1, без главной публикации — 0" in out
    assert "к заполнению 3" in out
    history("--apply")
    assert card_jobs(admin)[-1]["payload"]["signal_ids"] == sorted(
        [ids["Старые справочники"], ids["Старая инструкция"], fresh])

    # Другая модель — другой рецепт: заполненную карточку прогон по истории перезаполнит.
    monkeypatch.setattr(api.config, "SIGNAL_CARD_MODEL", MINI)
    capsys.readouterr()
    history()
    out = capsys.readouterr().out
    assert "заполнены этим рецептом — 0, уже в задачах агента — 4" in out and "к заполнению 1" in out


def test_dry_run_prints_signals_and_price_and_enqueues_nothing(user, admin, contour, october, monkeypatch, capsys):
    # Цена входа — ноль, выхода — круглая: в цене остаётся только оценка ответа (600 токенов на вызов).
    monkeypatch.setitem(signal_card.config.OPENAI_MODEL_PRICES, MODEL, (0.0, 1.0))
    for month in (8, 9, 10):
        feed_signal(f"Статья {month}", TEXT, published=published(month))
    july = feed_signal("Июль", TEXT, published=published(7))
    with repository.get_connection() as conn:  # собрана до августа — история её не берёт (#93)
        conn.execute("UPDATE articles SET collected_at = %s WHERE id = %s", (published(7), july))
        conn.commit()
    archived = radar_finding("archived", "В архиве", "Датчик утечек на трубопроводе.")
    assert repository.archive_signals([archived], reason="старое") == 1
    radar_finding("robot", "Робот", "Equinor испытала робота.")
    repository.upsert_signal({"signal_key": "bare", "title": "Без ссылок", "theme": "Бурение", "maturity": "watch",
                              "score": 50, "signal_category": "technology"})  # агенту нечего читать
    model = FakeModel({})
    monkeypatch.setattr(signal_card, "make_client", lambda: model)
    monkeypatch.setattr(api.config, "SIGNAL_CARD_ENABLED", False)  # прикинуть цену можно и до включения флага

    history()

    out = capsys.readouterr().out
    assert model.calls == [] and card_jobs(admin) == []
    # Весь радар, в том числе архивный; лента — с августа по дате сбора.
    assert "сигналов 6; заполнены этим рецептом — 0, уже в задачах агента — 0, без главной публикации — 1" in out
    assert "к заполнению 5: сначала текущее окно ленты (2026-10) и радар — 3, затем остальное — 2" in out
    assert "вызовов 5" in out and "≈ $0.003" in out  # 5 × 600 токенов × $1 за 1 млн
    assert "сухой прогон" in out


def test_history_is_put_only_into_the_bulk_lane_behind_the_flag_and_with_the_model(
        user, admin, contour, october, monkeypatch, capsys):
    feed_signal("Контракт SLB", TEXT, published=published(10))
    monkeypatch.setattr(api.config, "AI_BULK_LANE_ENABLED", False)

    history()
    assert "полоса пересчётов выключена" in capsys.readouterr().out
    # Без полосы пересчётов история встала бы в поток дня перед новыми статьями.
    with pytest.raises(SystemExit, match="AI_BULK_LANE_ENABLED"):
        history("--apply")

    monkeypatch.setattr(api.config, "AI_BULK_LANE_ENABLED", True)
    monkeypatch.setattr(api.config, "SIGNAL_CARD_ENABLED", False)
    with pytest.raises(SystemExit, match="SIGNAL_CARD_ENABLED"):
        history("--apply")
    monkeypatch.setattr(api.config, "SIGNAL_CARD_MODEL", "")
    with pytest.raises(SystemExit, match="SIGNAL_CARD_MODEL"):
        history()
    assert card_jobs(admin) == []
