"""Бот «Материалы коллег» (NL) с подставными Telegram и ядром: шаги, свой цикл у каждого, отправка."""

from pathlib import Path
import re

import pytest

from oiltech_digest import colleague_materials as cm
from oiltech_digest import config, signal_feedback
from oiltech_digest import tg_materials_bot as bot_module
from oiltech_digest.tg_materials_bot import CoreError, MaterialsBot, TelegramError

GROUP = -100500
ROOT = Path(__file__).resolve().parents[1]


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.next_id = 1000
        self.members = {}
        self.files = {}

    def call(self, method, http_timeout=30, **params):
        self.calls.append((method, params))
        if method == "sendMessage":
            self.next_id += 1
            return {"message_id": self.next_id, "chat": {"id": params["chat_id"]}}
        if method == "getChatMember":
            status = self.members.get(params["user_id"])
            if status is None:
                raise TelegramError("Telegram getChatMember: 400 user not found")
            return {"status": status}
        return True

    def download(self, file_id):
        return self.files[file_id]

    def sent(self):
        return [params for method, params in self.calls if method == "sendMessage"]

    def last(self):
        return self.sent()[-1]

    def acks(self):
        return [params for method, params in self.calls if method == "answerCallbackQuery"]


class FakeCore:
    def __init__(self):
        self.saved = []
        self.error = None

    def save(self, material, file):
        if self.error:
            raise self.error
        self.saved.append((material, file))
        return {"id": 12}

    def recent(self, tg_user_id, limit=5):
        return [{"id": 12, "kind": "link", "title": "Сделка", "verdict_label": "Наблюдать — рано", "created_at": "2026-10-19T10:00:00"}]


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setattr(config, "TG_MATERIALS_MAX_FILE_BYTES", 20 * 1024 * 1024)
    monkeypatch.setattr(config, "TG_MATERIALS_DIALOG_TTL_MINUTES", 30)
    tg, core, clock = FakeTelegram(), FakeCore(), Clock()
    bot = MaterialsBot(tg, core, chat_ids=[GROUP], clock=clock)
    bot.username = "oiltech_materials_bot"
    return bot, tg, core, clock


def user(uid, name="Иван"):
    return {"id": uid, "first_name": name, "is_bot": False, "username": f"u{uid}"}


_mid = iter(range(1, 10_000))


def say(bot, uid, text=None, *, chat=GROUP, reply_to=None, **extra):
    chat_obj = {"id": chat, "type": "private" if chat > 0 else "supergroup"}
    message = {"message_id": next(_mid), "chat": chat_obj, "from": user(uid), **extra}
    if text is not None:
        message["text"] = text
    if reply_to:
        message["reply_to_message"] = {"message_id": reply_to}
    bot.handle({"update_id": message["message_id"], "message": message})
    return message


def press(bot, tg, uid, data, *, chat=GROUP, message_id=None):
    bot.handle({"update_id": 1, "callback_query": {
        "id": f"q{len(tg.calls)}", "from": user(uid), "data": data,
        "message": {"message_id": message_id or tg.next_id, "chat": {"id": chat, "type": "supergroup"}},
    }})


def prompt_id(tg):
    return tg.next_id


def answer(bot, tg, uid, text=None, **extra):
    return say(bot, uid, text, reply_to=prompt_id(tg), **extra)


def walk_event(bot, tg, uid=1):
    say(bot, uid, "/add@oiltech_materials_bot")
    press(bot, tg, uid, f"m:{uid}:kind:event")
    answer(bot, tg, uid, "Татнефть выбрала подрядчика по ГРП")
    answer(bot, tg, uid, "Итог тендера: три года, объём вырос на 20 % — конкурент взял объём.")
    answer(bot, tg, uid, "Татнефть, ТаграС")
    press(bot, tg, uid, f"m:{uid}:verdict:watch_later")
    answer(bot, tg, uid, "-")


def test_event_walkthrough_submits_and_leaves_only_final_card(env):
    bot, tg, core, _ = env
    walk_event(bot, tg)
    card = tg.last()
    assert "Проверьте карточку" in card["text"] and "Наблюдать — рано, нужен следующий этап" in card["text"]

    press(bot, tg, 1, "m:1:send:", message_id=tg.next_id)

    material, file = core.saved[0]
    assert file is None
    assert material["kind"] == "event" and material["companies"] == ["Татнефть", "ТаграС"]
    assert material["verdict"] == "watch_later" and material["verdict_comment"] is None
    assert (material["tg_chat_id"], material["tg_message_id"], material["tg_user_id"]) == (GROUP, tg.next_id, 1)
    edits = [p for m, p in tg.calls if m == "editMessageText"]
    assert "Материал №12 принят на платформу" in edits[0]["text"]
    deleted = {p["message_id"] for m, p in tg.calls if m == "deleteMessage"}
    assert tg.next_id not in deleted and len(deleted) == 6  # шесть вопросов стёрты, итоговая карточка — нет
    assert bot.dialogs == {}


def test_two_people_in_group_do_not_mix(env):
    bot, tg, core, _ = env
    say(bot, 1, "/add")
    press(bot, tg, 1, "m:1:kind:event")
    first_prompt = prompt_id(tg)
    say(bot, 2, "/add")
    press(bot, tg, 2, "m:2:kind:link")
    second_prompt = prompt_id(tg)

    say(bot, 1, "Заголовок Ивана про сделку", reply_to=first_prompt)
    say(bot, 2, "https://example.com/news?id=1.", reply_to=second_prompt)
    say(bot, 2, "чужой ответ на вопрос Ивана", reply_to=first_prompt)  # не его вопрос — молчим

    assert bot.dialogs[(GROUP, 1)].data["title"] == "Заголовок Ивана про сделку"
    assert bot.dialogs[(GROUP, 1)].step == "description"
    assert bot.dialogs[(GROUP, 2)].data["url"] == "https://example.com/news?id=1"
    assert bot.dialogs[(GROUP, 2)].step == "title"


def test_someone_elses_button_is_refused(env):
    bot, tg, _, _ = env
    say(bot, 1, "/add")
    press(bot, tg, 2, "m:1:kind:event")

    assert tg.acks()[-1]["show_alert"] is True
    assert "другого участника" in tg.acks()[-1]["text"]
    assert bot.dialogs[(GROUP, 1)].step == "kind"


def test_group_messages_without_reply_do_not_move_dialog(env):
    bot, tg, _, _ = env
    say(bot, 1, "/add")
    press(bot, tg, 1, "m:1:kind:event")
    say(bot, 1, "просто реплика в чате")

    assert bot.dialogs[(GROUP, 1)].step == "title" and "title" not in bot.dialogs[(GROUP, 1)].data


def test_prompt_is_force_reply_addressed_to_author(env):
    bot, tg, _, _ = env
    added = say(bot, 1, "/add")
    press(bot, tg, 1, "m:1:kind:event")

    prompt = tg.last()
    assert prompt["reply_markup"]["force_reply"] is True and prompt["reply_markup"]["selective"] is True
    assert prompt["reply_parameters"]["message_id"] == added["message_id"]
    assert 'href="tg://user?id=1"' in prompt["text"]


def test_invalid_answer_reasks_with_reason(env):
    bot, tg, _, _ = env
    say(bot, 1, "/add")
    press(bot, tg, 1, "m:1:kind:link")
    answer(bot, tg, 1, "не ссылка")

    assert bot.dialogs[(GROUP, 1)].step == "url"
    assert "⚠️ Нужна ссылка" in tg.last()["text"]


def test_file_with_caption_takes_title_and_is_uploaded(env):
    bot, tg, core, _ = env
    tg.files["F1"] = b"%PDF"
    say(bot, 1, "/add")
    press(bot, tg, 1, "m:1:kind:file")
    answer(bot, tg, 1, document={"file_id": "F1", "file_name": "Обзор рынка ГРП.pdf", "file_size": 4},
           caption="Обзор рынка ГРП за 9 месяцев")
    assert bot.dialogs[(GROUP, 1)].step == "description"
    answer(bot, tg, 1, "Рынок ГРП вырос, у независимых подрядчиков доля 40 %, ценовое давление.")
    answer(bot, tg, 1, "-")
    press(bot, tg, 1, "m:1:verdict:background_material")
    answer(bot, tg, 1, "Пригодится для дайджеста")
    press(bot, tg, 1, "m:1:send:", message_id=tg.next_id)

    material, file = core.saved[0]
    assert file == ("Обзор рынка ГРП.pdf", b"%PDF")
    assert material["title"] == "Обзор рынка ГРП за 9 месяцев" and material["companies"] == []
    assert material["verdict_comment"] == "Пригодится для дайджеста"


def test_too_big_file_is_refused_before_download(env):
    bot, tg, _, _ = env
    say(bot, 1, "/add")
    press(bot, tg, 1, "m:1:kind:file")
    answer(bot, tg, 1, document={"file_id": "BIG", "file_name": "x.zip", "file_size": 50 * 1024 * 1024})

    assert bot.dialogs[(GROUP, 1)].step == "file"
    assert "до 20 МБ" in tg.last()["text"]


def test_core_failure_keeps_dialog_for_retry(env):
    bot, tg, core, _ = env
    walk_event(bot, tg)
    core.error = CoreError("платформа недоступна (ConnectionError)", retryable=True)

    press(bot, tg, 1, "m:1:send:", message_id=tg.next_id)

    assert "ещё раз" in tg.acks()[-1]["text"]
    assert (GROUP, 1) in bot.dialogs
    core.error = None
    press(bot, tg, 1, "m:1:send:", message_id=bot.dialogs[(GROUP, 1)].card_id)
    assert len(core.saved) == 1 and bot.dialogs == {}


def test_double_press_after_success_does_not_resend(env):
    bot, tg, core, _ = env
    walk_event(bot, tg)
    card = tg.next_id
    press(bot, tg, 1, "m:1:send:", message_id=card)
    press(bot, tg, 1, "m:1:send:", message_id=card)

    assert len(core.saved) == 1
    assert "уже отправлен" in tg.acks()[-1]["text"]


def test_abandoned_dialog_expires(env):
    bot, tg, _, clock = env
    say(bot, 1, "/add")
    press(bot, tg, 1, "m:1:kind:event")
    clock.now += 31 * 60

    answer(bot, tg, 1, "Поздний заголовок материала")

    assert bot.dialogs == {}


def test_other_group_and_strangers_in_private_are_ignored(env):
    bot, tg, _, _ = env
    say(bot, 1, "/add", chat=-100999)
    assert tg.sent() == []

    say(bot, 5, "/add", chat=5)
    assert "участников рабочей группы" in tg.last()["text"]
    assert (5, 5) not in bot.dialogs

    tg.members[6] = "member"
    say(bot, 6, "/add", chat=6)
    assert (6, 6) in bot.dialogs


def test_private_chat_needs_no_reply(env):
    bot, tg, _, _ = env
    tg.members[6] = "member"
    say(bot, 6, "/add", chat=6)
    press(bot, tg, 6, "m:6:kind:event", chat=6)
    say(bot, 6, "Заголовок в личке без ответа", chat=6)

    assert bot.dialogs[(6, 6)].step == "description"
    assert "force_reply" in tg.last()["reply_markup"]


def test_command_for_another_bot_is_ignored(env):
    bot, tg, _, _ = env
    say(bot, 1, "/add@other_bot")
    assert tg.sent() == []


def test_help_lists_whole_scale(env):
    bot, tg, _, _ = env
    say(bot, 1, "/help")
    text = tg.last()["text"]
    for _value, _short, full in cm.VERDICTS:
        assert full.replace("/", "/") in text.replace("&quot;", '"')


def test_scale_is_the_radar_screen_scale():
    """«Идентично оценке платформы»: те же девять пунктов и подписи, что на экране радара."""
    source = (ROOT / "frontend/src/features/signals/SignalRadarPage.tsx").read_text(encoding="utf-8")
    block = source.split("const VERDICT_LABELS", 1)[1].split("\n];", 1)[0]
    screen = [(v, label) for v, label in re.findall(r'value: "([a-z_]*)", label: "([^"]+)"', block) if v]

    assert screen == [(value, full) for value, _short, full in cm.VERDICTS]
    for value, _short, _full in cm.VERDICTS:
        assert signal_feedback._normalize_verdict(value) == value


def test_card_is_html_escaped():
    text = bot_module.card_text({"kind": "link", "title": "<b>x</b>", "description": "a & b", "url": "https://e.x/?a=1&b=2",
                                 "verdict": "approved"}, author="<script>", header="Проверьте карточку")
    assert "<b>x</b>" not in text and "&lt;b&gt;x&lt;/b&gt;" in text and "&lt;script&gt;" in text
