"""Правка русских заголовков принятых статей последних двух недель (решение владельца 03.11): enqueue-title-edit.

Новые принятые статьи правит «Суть и перевод» сам; заголовки, написанные до правки, — только этим прогоном, по слову
владельца: сначала всегда сухой прогон с N и ценой.
"""

from __future__ import annotations

import time
from datetime import datetime

from fastapi.testclient import TestClient
import pytest

from oiltech_digest import api, feed_window
from oiltech_digest.db import connection
from oiltech_digest.processing import external_ai, pipeline
from oiltech_digest.processing.openai_client import AIResponse
from oiltech_digest.processing.seed import seed_default_scoring_criteria

OFF_RULES = "Хуситы ударили по НПЗ в Эр-Рияде — Новости о нефти и газе в России и мире - Нефть и капитал"
EDITED = "Хуситы ударили по НПЗ в Эр-Рияде"
COPY = object()


def _cli(*argv: str) -> None:
    """Команда через настоящий разбор аргументов, но без cli.main: тот перенастраивает логирование."""
    from oiltech_digest import cli

    args = cli.build_parser().parse_args(list(argv))
    args.func(args)


def _freeze(monkeypatch) -> None:
    """3 ноября: открыты октябрь и ноябрь (окно ленты до 11-го), сентябрь закрыт."""
    monkeypatch.setattr(feed_window, "_now", lambda: datetime(2026, 11, 3, 12, 0, tzinfo=feed_window.MSK))


def _article(title: str, *, title_ru=COPY, relevant: bool | None = True, published: str = "2026-10-28 12:00+00",
             pending_deletion: bool = False, digest: bool = False) -> int:
    """Статья ленты с карточкой; title_ru по умолчанию — копия заголовка, как у русского до правки."""
    with connection.get_connection() as conn:
        source = conn.execute("SELECT id FROM sources WHERE name = 'Лента'").fetchone() or conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy) "
            "VALUES ('Лента', 'News', 'https://feed.example', TRUE, 'rss') RETURNING id").fetchone()
        article_id = conn.execute(
            "INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, language, "
            "pending_deletion) VALUES (%s, %s, %s, %s, %s, 'Текст публикации.', 'ru', %s) RETURNING id",
            (source[0], title, f"https://feed.example/{time.monotonic_ns()}", published, published, pending_deletion),
        ).fetchone()[0]
        conn.execute("INSERT INTO article_cards (article_id, summary, title_ru, relevant) VALUES (%s, 'Суть', %s, %s)",
                     (article_id, title[:200] if title_ru is COPY else title_ru, relevant))
        if digest:
            user_id = conn.execute("INSERT INTO users (email, password_salt, password_hash) "
                                   "VALUES (%s, 's', 'h') RETURNING id", (f"{article_id}@example.com",)).fetchone()[0]
            conn.execute("INSERT INTO article_marks (set_by_user_id, article_id, status) VALUES (%s, %s, 'digest')",
                         (user_id, article_id))
        conn.commit()
    return int(article_id)


def _translation_run(model: str, input_tokens: int, output_tokens: int) -> None:
    with connection.get_connection() as conn:
        conn.execute(
            "INSERT INTO ai_processing_runs (stage, provider, model, input_tokens, output_tokens, total_tokens, "
            "cost_usd, status) VALUES ('translation', 'openai', %s, %s, %s, %s, 0, 'ok')",
            (model, input_tokens, output_tokens, input_tokens + output_tokens),
        )
        conn.commit()


def _jobs() -> list[tuple]:
    with connection.get_connection() as conn:
        return conn.execute("SELECT kind, queue_name, payload_json FROM background_jobs ORDER BY id").fetchall()


def test_dry_run_counts_russian_copies_off_the_rules_in_open_months_and_enqueues_nothing(
        isolated_db, monkeypatch, capsys):
    _freeze(monkeypatch)
    _article(OFF_RULES)
    # Отметка «в дайджест» — выбор человека, даже если гейт статью ещё не судил.
    _article("«Росатом» завершил оснащение АЭС «Руппур» (Бангладеш) системами управления", relevant=None, digest=True)
    _article("«Роснефть» совершенствует технологии ремонта скважин")  # по правилам
    _article("Halliburton wins a contract — Reuters", title_ru="Halliburton выиграла контракт")  # иноязычный
    _article(OFF_RULES, title_ru=EDITED)  # уже правлен
    _article(OFF_RULES, published="2026-09-20 12:00+00")  # закрытый месяц: выпуск только на просмотр
    # Октябрь открыт, но собрана раньше двух недель: пересчёты только по последним двум неделям (владелец 03.11).
    _article(OFF_RULES, published="2026-10-15 12:00+00")
    _article(OFF_RULES, relevant=False)  # отклонён
    _article(OFF_RULES, relevant=False, digest=True)  # отклонён гейтом: из ленты скрыт, отметка его не возвращает
    _article(OFF_RULES, pending_deletion=True)  # помечен на удаление
    _translation_run("gpt-6.1-terra", 1_000, 200)

    _cli("enqueue-title-edit")

    out = capsys.readouterr().out
    assert "N=2" in out and "в дайджест: 1" in out
    # (1 000·1 + 200·5) / 10⁶ = 0,002 за вызов; N = 2 → 0,004.
    assert "модель gpt-6.1-terra" in out and "$0.00200 за вызов" in out and "цена ≈ $0.00" in out
    assert "[dry-run]" in out and _jobs() == []


def test_refuses_without_the_external_contour_or_the_bulk_lane(isolated_db, monkeypatch):
    """Как у пересчёта балла (rescore.enqueue_stage): локальный конвейер пометки only не знает, а в потоке дня пачки
    встали бы перед новыми статьями — только сознательно, --allow-live-lane."""
    _freeze(monkeypatch)
    _article(OFF_RULES)
    monkeypatch.setattr("oiltech_digest.config.EXTERNAL_WORKERS_ENABLED", False)
    with pytest.raises(SystemExit, match="внешний контур"):
        _cli("enqueue-title-edit", "--no-dry-run")
    monkeypatch.setattr("oiltech_digest.config.EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr("oiltech_digest.config.AI_EXECUTION_REGION", "external")
    monkeypatch.setattr("oiltech_digest.config.AI_BULK_LANE_ENABLED", False)
    with pytest.raises(SystemExit, match="полоса пересчётов выключена"):
        _cli("enqueue-title-edit", "--no-dry-run")
    assert _jobs() == []


def test_jobs_edit_the_title_on_the_worker_and_the_core_writes_it(isolated_db, monkeypatch, capsys):
    """Задачи — process_articles с only=["translation"] в полосе пересчётов: правит тот же шаг воркера, что у новых
    статей, ядро пишет только заголовок; правленая статья в выборку больше не попадает, стоящая в задаче — тоже."""
    _freeze(monkeypatch)
    seed_default_scoring_criteria()
    first, second = _article(OFF_RULES), _article(OFF_RULES)
    with connection.get_connection() as conn:  # пакет без тематик воркер не примет
        conn.execute("INSERT INTO tags (name, enabled, sort_order) VALUES ('Бурение', TRUE, 1)")
        conn.commit()
    monkeypatch.setattr("oiltech_digest.config.EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr("oiltech_digest.config.AI_EXECUTION_REGION", "external")
    monkeypatch.setattr("oiltech_digest.config.AI_BULK_LANE_ENABLED", True)

    _cli("enqueue-title-edit", "--no-dry-run", "--batch-size", "1")

    assert _jobs() == [("process_articles", "external-ai-bulk",
                        {"article_ids": [one], "limit": 1, "offline": False, "only": ["translation"]})
                       for one in (first, second)]
    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))
    core = TestClient(api.app)
    auth = {"Authorization": "Bearer secret"}
    deadline = time.monotonic() + 2.0  # часы ВМ colima подводятся назад: задача на миг «из будущего»
    job = None
    while job is None and time.monotonic() < deadline:
        job = core.post("/api/external-worker/claim", headers=auth, json={
            "worker_id": "nl-ai-bulk-1", "queues": ["external-ai-bulk"], "capabilities": ["openai"],
        }).json()["job"]
    assert job is not None

    class _Editor:
        def complete_json(self, instructions, user_input, schema, **kwargs):
            assert schema["name"] == "article_title_edit"
            return AIResponse(data={"title_ru": EDITED}, model="gpt-6.1-terra", input_tokens=900, output_tokens=40)

    monkeypatch.setattr(external_ai, "make_client", lambda offline=False: _Editor())
    result = external_ai.process_payload(job["payload"])
    assert set(result["articles"][0]) == {"article_id", "errors", "translation"}
    done = core.post(f"/api/external-worker/jobs/{job['id']}/complete", headers=auth,
                     json={"lease_token": job["lease_token"], "result": result})

    assert done.status_code == 200
    with connection.get_connection() as conn:
        assert conn.execute("SELECT title_ru FROM article_cards WHERE article_id = %s", (first,)).fetchone()[0] == EDITED
    capsys.readouterr()
    _cli("enqueue-title-edit")
    assert "N=0" in capsys.readouterr().out


class _FixedEdit:
    """Модель правки, которая отвечает заданным заголовком."""

    def __init__(self, edited: str):
        self.edited = edited

    def complete_json(self, instructions, user_input, schema, **kwargs):
        assert schema["name"] == "article_title_edit"
        return AIResponse(data={"title_ru": self.edited}, model="fake", input_tokens=1, output_tokens=1)


@pytest.mark.parametrize(("title", "text", "edited", "taken"), [
    # 06.11, ФИПС: правка просклоняла несклоняемую фамилию («мастер-класс Олега Ена» стало «Олег Ен провёл»).
    pytest.param(
        "Мастер-класс Олега Ена «Детальная технологическая экспертиза ИТ-патентов в области медицины» на Ярмарке "
        "разработок", "Руководитель проектного офиса ФИПС Олег Ена провёл мастер-класс для разработчиков.",
        "Олег Ен провёл мастер-класс по экспертизе ИТ-патентов в медицине на Ярмарке разработок", False,
        id="surname-cut"),
    # Сокращение, которого нет ни в заголовке, ни в тексте (правило инструкции: не заменять аббревиатурой).
    pytest.param(
        "На российских АЭС стартовала опытная эксплуатация отечественной системы прогнозирования состояния "
        "оборудования", "Система прогнозирования введена в опытную эксплуатацию на станциях концерна.",
        "На АЭС РФ начали опытную эксплуатацию отечественной системы прогноза состояния техники", False,
        id="new-abbreviation"),
    # Падеж названий менять можно: основа слова есть во входе (правки с прода, верные).
    pytest.param(
        "Учёные нашли способ очистить территорию Ставропольского края от накопленных нефтезагрязнений",
        "Метод очистки почвы испытан на полигоне.",
        "Учёные нашли способ очистить Ставропольский край от накопленных нефтезагрязнений", True,
        id="region-case"),
    pytest.param(
        "«Газпром» прокладывает газопровод «Белогорск — Хабаровск»", "Сварено 120 км газопровода.",
        "«Газпром» прокладывает газопровод от Белогорска до Хабаровска", True, id="city-case"),
    # Короткое название тоже склоняется: у слова до четырёх букв основа без последней буквы.
    pytest.param(
        "Нефтеперерабатывающий завод в Ухте начнёт выпуск нового топлива для северных районов страны — Нефтегаз",
        "Завод модернизировал установку.", "Завод в Ухте начнёт выпуск нового топлива для северных районов",
        True, id="short-name-case"),
    pytest.param(
        "Завод в Ухте начнёт выпуск нового топлива для северных районов страны и арктических проектов — Нефтегаз",
        "Завод модернизировал установку.", "Завод Ухты начнёт выпуск нового топлива для северных районов",
        True, id="short-name-new-case"),
    # Термин словаря, который инструкция велит ставить буквально, модель видела во входе (блок domain_glossary).
    pytest.param(
        "«Новатэк» отгрузил первую партию с проекта Arctic LNG 2 потребителям в странах Азии — Нефть и капитал",
        "Компания отгрузила груз с проекта Arctic LNG 2.",
        "«Новатэк» отгрузил первую партию СПГ с проекта Arctic LNG 2", True, id="glossary-term"),
    # Первое слово не проверяется: там с заглавной и обычное слово в новой форме (правка с прода, верная).
    pytest.param(
        "Учёные создали систему онлайн-контроля датчиков, сигнализирующих о цунами, — РИА Новости",
        "Система следит за состоянием датчиков.",
        "Создана система онлайн-контроля датчиков, сигнализирующих о цунами", True, id="first-word"),
])
def test_edit_with_a_name_the_model_did_not_see_is_dropped(title, text, edited, taken):
    """Правка с именем или названием, которого нет во входе модели, не берётся: остаётся заголовок издателя, как у
    правки с чужим числом. Короткое слово (до трёх букв) должно стоять во входе как есть, длинному хватает основы."""
    article = {"title": title, "raw_text": text, "source_name": "Источник"}
    assert not pipeline.title_follows_rules(title), "заголовок должен идти на правку"

    result, response = pipeline.title_ru_for_article(article, _FixedEdit(edited), edit_russian=True)

    assert result == (edited if taken else title)
    assert response is not None, "расход правки учитывается и при отказе"
