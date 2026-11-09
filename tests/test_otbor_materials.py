"""Материалы коллег через «Отбор» (#131, спека #128), шов 1, контур целиком: материал от бота → ядро (сигнал третьего
происхождения) → шаг «Отбора» → воркер NL на подставной модели → ядро → «Суть и перевод» → «Карточка» → витрина;
итог «Отбора»: автору через настоящий клиент бота против тестового ядра (tests/otbor_support.py,
tests/otbor_radar_support.py).
"""

import io
import json

from docx import Document
from fastapi.testclient import TestClient
import pytest

from oiltech_digest import api, cli, colleague_materials, signal_card, signal_summary, tg_materials_bot
from oiltech_digest.db import repository
from oiltech_digest.processing.digest import render_digest_export_html
from oiltech_digest.processing.openai_client import AIClientError
from tests.otbor_radar_support import event, page, run_radar, summarize
from tests.otbor_support import answer, api_request, api_send, decide, reject, showcase
from tests.otbor_support import contour  # noqa: F401: фикстура
from tests.signal_card_support import FakeModel, _Core, admin, api_get, run_worker, user  # noqa: F401: фикстуры
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
    """Материал от бота NL: как его шлёт бот (multipart с токеном воркера); строка материала."""
    response = TestClient(api.app).post(
        "/api/external-worker/materials", headers=WORKER, files={"file": file} if file else None,
        data={"material": json.dumps(material, ensure_ascii=False), "worker_id": "nl-tg-bot-1"})
    assert response.status_code == 200, response.text
    return colleague_materials.get_material(response.json()["material"]["id"])


def _accumulated(monkeypatch, material):
    """Накопленный материал: бот его прислал, а сигнал при приёме не завёлся (присланные до включения «Отбора» и сбой
    записи сигнала при приёме): строка материала без сигнала, её проводит команда otbor-materials."""
    def refuse(row, **kwargs):
        raise RuntimeError("сигнал материала не записан")

    with monkeypatch.context() as patch:
        patch.setattr(api.material_signals, "to_signal", refuse)
        return _send(material)


class _Telegram:
    """Подставной Telegram бота: отправленные сообщения; ошибка следующей отправки: fail."""

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

    # Вход «Отбора» у файла: описание автора и текст файла; ссылок и издателя у материала нет.
    [call] = model.calls
    assert DESCRIPTION in call["input"] and FILE_TEXT in call["input"]
    assert "Ссылки находки: нет" in call["input"] and "Источник: не указан" in call["input"]
    # «Суть и перевод» заполняет только недостающее: суть: описание автора, заголовок русский: без вызова модели.
    summary_model = summarize(monkeypatch, {TITLE: (TITLE, "суть модели")})
    assert summary_model.calls == []
    card_model = _card(monkeypatch, {TITLE: card_answer(companies=[{"name": "Татнефть",
                                                                     "quote": "Протокол тендера"}])})
    assert len(card_model.calls) == 1

    card = showcase(user, "business")[TITLE]
    assert (card["origin"], card["otbor_decision"], card["title_ru"], card["summary"]) == (
        "material", "accept", TITLE, DESCRIPTION)
    # Поля автора не перезаписаны: участники: его компании, его оценка: блок ОС с его именем.
    assert [one["name"] for one in card["participants_json"]] == ["Татнефть", "ТаграС"]
    assert [(block["author"], block["verdict"], block["comment"]) for block in card["feedback_blocks"]] == [
        ("Иван Иванов", "strong_signal", "Конкурент взял объём")]
    # Оценка автора лежит в «Оценках людей»: счётчик «Обратная связь: N» и фильтр «Наличие ОС» её видят (03.11).
    assert card["feedback_count"] == 1
    assert TITLE in showcase(user, "business", feedback="with")
    assert TITLE not in showcase(user, "business", feedback="without")
    assert colleague_materials.get_material(material["id"])["signal_id"] == card["id"]

    # Итог, автору: бот отвечает на карточку материала в группе, с упоминанием автора; второй раз не шлёт.
    telegram = _Telegram()
    assert _bot(telegram).notify() == 1
    [message] = telegram.sent
    assert (message["chat_id"], message["reply_parameters"]["message_id"]) == (-100500, 78)
    assert message["text"].startswith('<a href="tg://user?id=9001">Иван Иванов</a>, материал №')
    assert "взят на платформу: бизнес-лента" in message["text"]
    assert _bot(telegram).notify() == 0 and len(telegram.sent) == 1


def test_author_title_is_not_edited_even_off_the_rules(user, contour, monkeypatch):
    """Заголовок материала, значение человека: «Суть и перевод» не правит его и тогда, когда он не по правилам
    эксперта (решение владельца 03.11: правка: у статей и находок)."""
    title = "Татнефть выбрала подрядчика по ГРП на 2027 год — итоги тендера"
    _send(_material(title=title))
    decide(monkeypatch, {title: answer()})

    model = summarize(monkeypatch, {})

    assert model.calls == []
    assert showcase(user, "business")[title]["title_ru"] == title


def test_core_keeps_the_author_fields_whatever_the_worker_returns(user, contour, monkeypatch):
    # Ответ воркера, недоверенный ввод: суть, которую он прислал материалу с описанием автора, ядро не пишет, а русский
    # заголовок автора пишет как есть (решение владельца 03.11: заголовок материала не правится).
    _send(_material())
    decide(monkeypatch, {TITLE: answer()})
    [card] = showcase(user, "business").values()
    monkeypatch.setattr(signal_summary, "process_payload", lambda payload, heartbeat=None: {
        "signal_summary": True, "summaries": [{"signal_id": card["id"], "summary": {"summary": "Чужая суть"},
                                               "translation": {"title_ru": "Чужой заголовок"}}]})

    cli.main(["enqueue-signal-summary", "--ids", str(card["id"])])
    run_worker("external-ai")

    card = showcase(user, "business")[TITLE]
    assert (card["summary"], card["title_ru"]) == (DESCRIPTION, TITLE)


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
    # Материал из лички: ответ в личку, без упоминания и с заглавной буквы.
    [message] = telegram.sent
    assert message["chat_id"] == 9001 and message["text"].startswith("По материалу №")
    assert "решения нет: ответ не по правилам" in message["text"]


def test_technical_failure_reaches_the_author_only_once_the_tries_are_over(user, contour, monkeypatch):
    monkeypatch.setattr(api.config, "OTBOR_ATTEMPTS", 2)
    _send(_material())

    # Первый сбой, «Отбор» повторит: это ещё не итог, автору писать рано.
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

    # Прикрепления к существующему сигналу нет: ссылка осталась у карточки радара, материал: свой сигнал в витрине.
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


def test_card_history_run_takes_only_what_otbor_accepted(user, contour, monkeypatch, capsys):
    rejected, failed = "https://slb.example/news/rejected", "https://slb.example/news/silent"
    _send(_material())
    run_radar(monkeypatch, [event("SLB quarterly results beat expectations", rejected),
                            event("SLB opens a digital drilling centre in Oman", failed)],
              {rejected: page("SLB quarterly results beat expectations"),
               failed: page("SLB opens a digital drilling centre in Oman")})
    decide(monkeypatch, {TITLE: answer(), "SLB quarterly results beat expectations": reject("oil_gas_no_link"),
                         "SLB opens a digital drilling centre in Oman": AIClientError("timeout")})
    monkeypatch.setattr(api.config, "SIGNAL_CARD_MODEL", "gpt-5.5")
    capsys.readouterr()

    cli.main(["enqueue-signal-card-history"])

    # Дальше «Отбора» идут только принятые: прогон по истории берёт материал и не платит за отклонённую находку и
    # находку без решения.
    assert "к заполнению 1:" in capsys.readouterr().out


def test_accumulated_materials_go_through_once_with_a_dry_run_by_default(user, admin, contour, monkeypatch, capsys):
    material = _accumulated(monkeypatch, _material())
    assert material["signal_id"] is None

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


LATE_TITLE = "Газпром нефть выбрала подрядчика по бурению на 2027 год"


def test_silent_accumulated_materials_tell_no_author_but_a_new_material_does(user, admin, contour, monkeypatch):
    """С --silent накопленные проходят конвейер молча: итог «Отбора» их авторам не уходит, ни первый, ни следующий
    (решение владельца 04.11 для присланных до включения: ответа никто не ждёт). Материал, присланный после, итог
    получает, как прежде."""
    material = _accumulated(monkeypatch, _material())
    cli.main(["otbor-materials", "--apply", "--silent"])
    _send(_material(title=LATE_TITLE, tg_message_id=79))

    decide(monkeypatch, {TITLE: reject("oil_gas_no_link"), LATE_TITLE: answer()})
    run_worker("external-ai-bulk")

    telegram = _Telegram()
    assert _bot(telegram).notify() == 1
    [message] = telegram.sent
    assert message["reply_parameters"]["message_id"] == 79
    # Следующий итог (человек вернул отклонённое) тоже уходит молча.
    signal_id = colleague_materials.get_material(material["id"])["signal_id"]
    response = api_request("POST", f"/api/signals/{signal_id}/return", admin, {
        "industry": "oil_gas", "oilfield_relevance": "direct", "oilfield_application": "",
        "signal_category": "business"})
    assert response.status_code == 200, response.text
    assert {TITLE, LATE_TITLE} <= set(showcase(user, "business"))
    assert _bot(telegram).notify() == 0 and len(telegram.sent) == 1


NEW_TITLE = "Татнефть продлила контракт с подрядчиком по ГРП до 2029 года"
NEW_DESCRIPTION = "Итог тендера уточнили: договор на три года, объём операций ГРП вырос на 25 %."


def test_edit_of_a_material_on_the_platform_reaches_its_signal(user, admin, contour, monkeypatch):
    material = _send(_material(kind="link", url="https://news.example/tender"))
    decide(monkeypatch, {TITLE: answer(category="business")})
    summarize(monkeypatch, {})

    api_send("PATCH", f"/api/materials/{material['id']}", admin, {
        "title": NEW_TITLE, "description": NEW_DESCRIPTION, "companies": ["Татнефть", "Газпром нефть"],
        "verdict": "watch_later", "verdict_comment": "Ждём объёмы", "url": "https://news.example/tender-2"})

    # Карточка в витрине, с правкой: заголовок (и русский), суть, участники, оценка автора и ссылка.
    card = showcase(user, "business")[NEW_TITLE]
    assert (card["title"], card["title_ru"], card["summary"]) == (NEW_TITLE, NEW_TITLE, NEW_DESCRIPTION)
    assert [one["name"] for one in card["participants_json"]] == ["Татнефть", "Газпром нефть"]
    assert [(block["author"], block["verdict"], block["comment"]) for block in card["feedback_blocks"]] == [
        ("Иван Иванов", "watch_later", "Ждём объёмы")]
    assert [link["source_url"] for link in card["evidence"]] == ["https://news.example/tender-2"]
    assert card["decision_log"][-1]["stage"] == "Человек"
    # «Суть и перевод» правку человека не перезаписывает.
    assert summarize(monkeypatch, {}).calls == []
    assert showcase(user, "business")[NEW_TITLE]["title_ru"] == NEW_TITLE


def test_edit_form_sends_every_field_and_only_what_changed_reaches_the_signal(user, admin, contour, monkeypatch):
    material = _send(_material(kind="link", url="https://news.example/tender"))
    decide(monkeypatch, {TITLE: answer(category="business")})
    card = showcase(user, "business")[TITLE]
    # Русский заголовок дала «Суть и перевод».
    with repository.get_connection() as conn:
        conn.execute("UPDATE signals SET title_ru = 'Заголовок «Сути и перевода»' WHERE id = %s", (card["id"],))
        conn.commit()

    # Форма правки шлёт все поля разом; поменялся только комментарий оценки.
    api_send("PATCH", f"/api/materials/{material['id']}", admin, {
        "title": TITLE, "description": DESCRIPTION, "companies": ["Татнефть", "ТаграС"], "verdict": "strong_signal",
        "verdict_comment": "Конкурент взял объём на три года", "url": "https://news.example/tender"})

    card = showcase(user, "business")[TITLE]
    assert card["title_ru"] == "Заголовок «Сути и перевода»" and "title_ru" not in card["card_set_by"]
    assert [link["source_url"] for link in card["evidence"]] == ["https://news.example/tender"]
    assert card["decision_log"][-1]["text"] == "Материал исправлен на платформе: оценка"
    assert [block["comment"] for block in card["feedback_blocks"]] == ["Конкурент взял объём на три года"]


def test_edit_does_not_rewrite_a_signal_in_the_issue_of_a_closed_month(user, admin, contour, monkeypatch):
    material = _send(_material())
    decide(monkeypatch, {TITLE: answer(category="business")})
    card = showcase(user, "business")[TITLE]
    api_send("PATCH", f"/api/signals/{card['id']}", user, {"status": "digest"})
    # Карточка поступила в августе: выпуск августа закрыт и только на просмотр, его текст тоже (решение 30.09).
    with repository.get_connection() as conn:
        conn.execute("UPDATE signals SET first_seen_at = '2026-08-15 09:00+00' WHERE id = %s", (card["id"],))
        conn.commit()

    api_send("PATCH", f"/api/materials/{material['id']}", admin, {"title": NEW_TITLE})

    [item] = [item for item in api_get("/api/digest-content", user, month="2026-08")["news"]]
    assert item["title"] == TITLE
    assert colleague_materials.get_material(material["id"])["title"] == NEW_TITLE


def test_deleting_a_material_archives_its_signal_unless_it_is_chosen_for_the_digest(
        user, admin, contour, monkeypatch):
    kept = _send(_material(tg_message_id=90))
    gone = _send(_material(title="Ошибочный материал о бурении", tg_message_id=91))
    decide(monkeypatch, {TITLE: answer(category="business"), "Ошибочный материал о бурении": answer()})
    cards = showcase(user, "business")
    api_send("PATCH", f"/api/signals/{cards[TITLE]['id']}", user, {"status": "digest"})

    # Выбранный «в дайджест»: удаление отказывает с объяснением, материал и карточка на месте.
    response = api_request("DELETE", f"/api/materials/{kept['id']}", admin, None)
    assert response.status_code == 409
    assert "в дайджест" in response.json()["detail"]
    assert colleague_materials.get_material(kept["id"]) is not None
    # Остальное: материал удалён, его сигнал ушёл в архив, «Отбор» его больше не берёт.
    api_send("DELETE", f"/api/materials/{gone['id']}", admin, None)
    assert colleague_materials.get_material(gone["id"]) is None
    assert set(showcase(user, "business")) == {TITLE}
    assert api_get("/api/signals", admin, section="all") == []


def test_material_chosen_for_the_digest_is_in_the_issue_without_a_dead_link(user, contour, monkeypatch):
    _send(_material())
    decide(monkeypatch, {TITLE: answer(category="business")})
    card = showcase(user, "business")[TITLE]
    api_send("PATCH", f"/api/signals/{card['id']}", user, {"status": "digest"})

    content = api_get("/api/digest-content", user)
    [item] = [item for item in content["news"] if item["title"] == TITLE]
    assert (item["item_type"], item["url"], item["source"]) == ("signal", "", "Материал коллеги")
    # У материала без ссылки «Читать далее» в выпуске не рисуется: пустая ссылка вела бы никуда.
    html = render_digest_export_html(content)
    assert TITLE in html and 'href=""' not in html


def test_edits_on_the_materials_screen_and_on_the_digest_screen_do_not_hide_each_other(
        user, admin, contour, monkeypatch):
    """Правка материала на экране «Материалы» и правка его сигнала на экране «Дайджест» (#162) не прячут друг друга: у
    сигнала один текст, последний по времени, с какого бы экрана он ни пришёл."""
    material = _send(_material(kind="link", url="https://news.example/tender"))
    decide(monkeypatch, {TITLE: answer(category="business")})
    summarize(monkeypatch, {})
    signal_id = colleague_materials.get_material(material["id"])["signal_id"]
    api_send("PATCH", f"/api/signals/{signal_id}", user, {"selected_for_digest": True})

    def shown():
        [card] = [card for card in showcase(user, "business").values() if card["id"] == signal_id]
        return card["title_ru"], card["summary"]

    api_send("PATCH", f"/api/signals/{signal_id}/issue-material", user,
             {"title": "Заголовок с экрана «Дайджест»", "summary": "Суть с экрана «Дайджест»."})
    assert shown() == ("Заголовок с экрана «Дайджест»", "Суть с экрана «Дайджест».")
    api_send("PATCH", f"/api/materials/{material['id']}", admin, {"title": NEW_TITLE, "description": NEW_DESCRIPTION})
    assert shown() == (NEW_TITLE, NEW_DESCRIPTION)
    api_send("PATCH", f"/api/signals/{signal_id}/issue-material", user, {"title": "Снова с экрана «Дайджест»"})
    assert shown() == ("Снова с экрана «Дайджест»", NEW_DESCRIPTION)
