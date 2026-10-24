"""Материалы коллег через «Отбор» (#131, спека #128), шов 1 — контур целиком: материал от бота → ядро (сигнал третьего
происхождения) → шаг «Отбора» → воркер NL на подставной модели → ядро → «Суть и перевод» → «Карточка» → витрина;
итог «Отбора» — автору через настоящий клиент бота против тестового ядра (tests/otbor_support.py,
tests/otbor_radar_support.py).
"""

import io
import json

from docx import Document
from fastapi.testclient import TestClient
import pytest

from oiltech_digest import api, cli, colleague_materials, signal_card, signal_summary, tg_materials_bot
from oiltech_digest.processing.openai_client import AIClientError
from tests.otbor_radar_support import event, page, run_radar, summarize
from tests.otbor_support import answer, decide, reject, showcase
from tests.otbor_support import contour  # noqa: F401 — фикстура
from tests.signal_card_support import FakeModel, _Core, admin, api_get, run_worker, user  # noqa: F401 — фикстуры
from tests.signal_card_support import answer as card_answer

TITLE = "Татнефть выбрала подрядчика по ГРП на 2027 год"
DESCRIPTION = "На совещании в Альметьевске объявили итог тендера: три года, объём вырос на 20 %."
FILE_TEXT = "Протокол тендера: подрядчик выполнит 400 операций ГРП на месторождениях Татнефти в 2027–2029 годах."
WORKER = {"Authorization": "Bearer secret", "X-Worker-Build": "abc1234", "X-Worker-Contract": "5"}


def _material(**changes):
    return {"kind": "event", "title": TITLE, "description": DESCRIPTION, "companies": ["Татнефть", "ТаграС"],
            "verdict": "strong_signal", "verdict_comment": "Конкурент взял объём", "tg_chat_id": -100500,
            "tg_message_id": 77, "tg_user_id": 9001, "tg_username": "ivanov", "tg_author": "Иван Иванов", **changes}


def _docx(text):
    document = Document()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _send(material, file=None):
    """Материал от бота NL — как его шлёт бот (multipart с токеном воркера); строка материала."""
    response = TestClient(api.app).post(
        "/api/external-worker/materials", headers=WORKER, files={"file": file} if file else None,
        data={"material": json.dumps(material, ensure_ascii=False), "worker_id": "nl-tg-bot-1"})
    assert response.status_code == 200, response.text
    return colleague_materials.get_material(response.json()["material"]["id"])


class _Telegram:
    """Подставной Telegram бота: отправленные сообщения; ошибка следующей отправки — fail."""

    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    def call(self, method, http_timeout=30, **params):
        if method == "sendMessage" and self.fail:
            raise self.fail
        self.sent.append(params)
        return {"message_id": 500 + len(self.sent)}


def _bot(telegram):
    """Бот с настоящим клиентом ядра против тестового приложения."""
    core = tg_materials_bot.CoreClient("http://testserver", "secret", "nl-tg-bot-1")
    session = _Core(api.app)
    session.headers.update(dict(core.session.headers))
    core.session = session
    return tg_materials_bot.MaterialsBot(telegram, core, chat_ids=[-100500])


def _card(monkeypatch, answers):
    """Шаг «Карточки» и воркер NL с подставной моделью."""
    monkeypatch.setattr(api.config, "SIGNAL_CARD_ENABLED", True)
    monkeypatch.setattr(api.config, "SIGNAL_CARD_MODEL", "gpt-5.5")
    model = FakeModel(answers)
    monkeypatch.setattr(signal_card, "make_client", lambda: model)
    cli.main(["enqueue-signal-cards"])
    run_worker("external-ai")
    return model


@pytest.fixture()
def files(monkeypatch, tmp_path):
    monkeypatch.setattr(api.config, "UPLOAD_DIR", str(tmp_path))


def test_material_becomes_a_signal_passes_the_pipeline_and_keeps_the_author_fields(
        user, admin, contour, files, monkeypatch):
    material = _send(_material(kind="file", tg_message_id=78),
                     file=("протокол.docx", _docx(FILE_TEXT), "application/octet-stream"))

    model = decide(monkeypatch, {TITLE: answer(category="business")})

    # Вход «Отбора» у файла — описание автора и текст файла; ссылок и издателя у материала нет.
    [call] = model.calls
    assert DESCRIPTION in call["input"] and FILE_TEXT in call["input"]
    assert "Ссылки находки: нет" in call["input"] and "Источник: не указан" in call["input"]
    # «Суть и перевод» заполняет только недостающее: суть — описание автора, заголовок русский — без вызова модели.
    summary_model = summarize(monkeypatch, {TITLE: (TITLE, "суть модели")})
    assert summary_model.calls == []
    card_model = _card(monkeypatch, {TITLE: card_answer(companies=[{"name": "Татнефть",
                                                                     "quote": "Протокол тендера"}])})
    assert len(card_model.calls) == 1

    card = showcase(user, "business")[TITLE]
    assert (card["origin"], card["otbor_decision"], card["title_ru"], card["summary"]) == (
        "material", "accept", TITLE, DESCRIPTION)
    # Поля автора не перезаписаны: участники — его компании, его оценка — блок ОС с его именем.
    assert [one["name"] for one in card["participants_json"]] == ["Татнефть", "ТаграС"]
    assert [(block["author"], block["verdict"], block["comment"]) for block in card["feedback_blocks"]] == [
        ("Иван Иванов", "strong_signal", "Конкурент взял объём")]
    assert colleague_materials.get_material(material["id"])["signal_id"] == card["id"]

    # Итог — автору: бот отвечает на карточку материала в группе, с упоминанием автора; второй раз не шлёт.
    telegram = _Telegram()
    assert _bot(telegram).notify() == 1
    [message] = telegram.sent
    assert (message["chat_id"], message["reply_parameters"]["message_id"]) == (-100500, 78)
    assert message["text"].startswith('<a href="tg://user?id=9001">Иван Иванов</a>, материал №')
    assert "взят на платформу: бизнес-лента" in message["text"]
    assert _bot(telegram).notify() == 0 and len(telegram.sent) == 1


def test_core_keeps_the_author_fields_whatever_the_worker_returns(user, contour, monkeypatch):
    # Ответ воркера — недоверенный ввод: суть, которую он прислал материалу с описанием автора, ядро не пишет.
    _send(_material())
    decide(monkeypatch, {TITLE: answer()})
    [card] = showcase(user, "business").values()
    monkeypatch.setattr(signal_summary, "process_payload", lambda payload, heartbeat=None: {
        "signal_summary": True, "summaries": [{"signal_id": card["id"], "summary": {"summary": "Чужая суть"},
                                               "translation": {"title_ru": "Чужой заголовок"}}]})

    cli.main(["enqueue-signal-summary", "--ids", str(card["id"])])
    run_worker("external-ai")

    card = showcase(user, "business")[TITLE]
    assert (card["summary"], card["title_ru"]) == (DESCRIPTION, "Чужой заголовок")


def test_rejected_material_is_its_signal_in_the_rejected_list_and_the_author_hears_the_reason(
        user, admin, contour, monkeypatch):
    _send(_material())

    decide(monkeypatch, {TITLE: reject("oil_gas_no_link")})

    # Положительная оценка автора решение «Отбора» не перекрывает: материал отклонён и лежит в разделе причины.
    assert showcase(user, "business") == {} and showcase(user, "technology") == {}
    [rejected] = api_get("/api/signals", admin, section="otbor:oil_gas_no_link")
    assert (rejected["title"], rejected["origin"], rejected["maturity"]) == (TITLE, "material", "reject")
    telegram = _Telegram()
    _bot(telegram).notify()
    [message] = telegram.sent
    assert "не взят: Нефтегаз без связи с нефтесервисом. Котировки нефти без связи с нефтесервисом" in message["text"]


def test_material_answered_off_rules_tells_the_author_there_is_no_decision(user, admin, contour, monkeypatch):
    _send(_material(tg_chat_id=9001, tg_message_id=80))

    decide(monkeypatch, {TITLE: {"decision": "accept"}})

    telegram = _Telegram()
    _bot(telegram).notify()
    # Материал из лички — ответ в личку, без упоминания и с заглавной буквы.
    [message] = telegram.sent
    assert message["chat_id"] == 9001 and message["text"].startswith("По материалу №")
    assert "решения нет: ответ не по правилам" in message["text"]


def test_technical_failure_reaches_the_author_only_once_the_tries_are_over(user, contour, monkeypatch):
    monkeypatch.setattr(api.config, "OTBOR_ATTEMPTS", 2)
    _send(_material())

    # Первый сбой — «Отбор» повторит: это ещё не итог, автору писать рано.
    decide(monkeypatch, {TITLE: AIClientError("timeout")})
    telegram = _Telegram()
    assert _bot(telegram).notify() == 0
    decide(monkeypatch, {TITLE: AIClientError("timeout")})

    assert _bot(telegram).notify() == 1
    assert "решения нет: модель: timeout" in telegram.sent[0]["text"]


def test_undeliverable_notice_is_not_retried_forever_but_a_failed_send_is(user, contour, monkeypatch):
    _send(_material())
    decide(monkeypatch, {TITLE: answer()})

    assert _bot(_Telegram(fail=tg_materials_bot.TelegramError("Telegram sendMessage: 500 Internal"))).notify() == 0
    assert _bot(_Telegram(fail=tg_materials_bot.TelegramError("Telegram sendMessage: 403 bot was kicked"))).notify() == 1
    telegram = _Telegram()
    assert _bot(telegram).notify() == 0 and telegram.sent == []


def test_link_material_whose_address_a_radar_card_holds_is_its_own_signal(user, admin, contour, monkeypatch):
    from tests.signal_card_support import radar_finding

    radar_finding("held", "Радар уже нашёл это событие", "SLB подписала контракт.", signal_category="business")
    material = _send(_material(kind="link", url="https://radar.example/held"))

    decide(monkeypatch, {TITLE: answer()})

    # Прикрепления к существующему сигналу нет: ссылка осталась у карточки радара, материал — свой сигнал в витрине.
    cards = showcase(user, "business")
    assert {TITLE, "Радар уже нашёл это событие"} <= set(cards)
    assert cards[TITLE]["id"] == colleague_materials.get_material(material["id"])["signal_id"]
    assert [link["source_url"] for link in cards["Радар уже нашёл это событие"]["evidence"]] == [
        "https://radar.example/held"]


def test_radar_does_not_bring_back_the_link_of_a_material(user, contour, monkeypatch):
    url = "https://slb.example/news/hpht-completion"
    _send(_material(kind="link", url=url))
    decide(monkeypatch, {TITLE: reject("oil_gas_no_link")})

    run_radar(monkeypatch, [event("SLB launches an HPHT completion system in Oman", url)],
              {url: page("SLB launches an HPHT completion system in Oman")})
    again = decide(monkeypatch, {})

    # Ссылку прислал и оценил человек: второй сигнал той же публикации «Отбор» не решает.
    assert again.calls == []


def test_accumulated_materials_go_through_once_with_a_dry_run_by_default(user, admin, contour, monkeypatch, capsys):
    monkeypatch.setattr(api.config, "OTBOR_ENABLED", False)
    material = _send(_material())
    assert material["signal_id"] is None
    monkeypatch.setattr(api.config, "OTBOR_ENABLED", True)

    cli.main(["otbor-materials"])
    assert "накопленных материалов 1" in capsys.readouterr().out
    assert colleague_materials.get_material(material["id"])["signal_id"] is None
    cli.main(["otbor-materials", "--apply"])
    assert "сигналов материалов 1, задач «Отбора» 1" in capsys.readouterr().out
    model = decide(monkeypatch, {TITLE: answer()})
    run_worker("external-ai-bulk")

    assert TITLE in showcase(user, "business")
    assert len(model.calls) == 1
    cli.main(["otbor-materials", "--apply"])
    assert "сигналов материалов 0, задач «Отбора» 0" in capsys.readouterr().out
