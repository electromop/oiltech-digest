"""Бот «Материалы коллег»: приём материалов для нефтесервиса из рабочей группы Telegram (NL).

Сотрудник пишет /add — бот ведёт его по шагам: тип (ссылка, файл, событие) → ссылка или
файл → заголовок → описание → компании → оценка по шкале Виктора (те же девять пунктов, что
у карточки радара) → комментарий к оценке → карточка на проверку → «Отправить». Материал
уходит в ядро (`/api/external-worker/materials`, токен воркера) и виден на экране
«Материалы коллег»; в группе остаётся итоговая карточка.

Каждый работает в своём цикле. Диалог — по паре (чат, человек): ответы других участников его
не двигают, а нажатие чужой кнопки бот отклоняет. В группе бот просит ответить на свой вопрос
(ForceReply, selective): такие ответы бот получает и в режиме приватности, ему не нужно читать
всю переписку группы. Промежуточные вопросы бот после отправки стирает — в ленте группы
остаются ответы автора и итоговая карточка.

Почему NL, а не ядро: с РФ-адреса Telegram недоступен (тот же довод, что у кода допуска,
admission.py). Почему свой цикл getUpdates, а не очередь ядра: бот должен отвечать за секунды,
а задачи очереди живут минутами. Состояние диалогов — в памяти: перезапуск контейнера сбросит
недописанные материалы (автору — «начните заново»), отправленные уже в ядре.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import html
import json
import logging
import re
import signal
import time
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

from oiltech_digest import colleague_materials as cm
from oiltech_digest import config, contract

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org"
_MSK = ZoneInfo("Europe/Moscow")
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
_POLL_SECONDS = 50
_HELLO_EVERY_SECONDS = 300
_MEMBER_CACHE_SECONDS = 600
_HISTORY_PAGE = 10
# В группе /history и ответ на неё стираются через столько секунд: лента группы — для материалов.
_GROUP_REPLY_SECONDS = 120
_CARD_DESCRIPTION_MAX = 2500
# Сообщение Telegram — до 4096 знаков после разбора HTML, в единицах UTF-16. В карточке на показ ссылка,
# компании и комментарий урезаются, описание получает остаток; полный текст — на платформе.
_TELEGRAM_TEXT_MAX = 4096
_CARD_URL_SHOWN, _CARD_COMPANIES_SHOWN, _CARD_COMMENT_SHOWN = 300, 600, 600

KIND_ICONS = {"link": "🔗", "file": "📎", "event": "📰"}

COMMANDS = [
    ("add", "Внести материал"),
    ("history", "История и статус моих материалов"),
    ("cancel", "Отменить ввод"),
    ("help", "Формат и шкала оценки"),
]

SHORT_DESCRIPTION = "Материалы коллег для нефтесервиса: ссылка, файл или событие с вашей оценкой — на платформу OilTech."

DESCRIPTION = (
    "Бот принимает материалы, полезные нефтесервисным обществам: ссылку, файл или описание "
    "события — с акцентом на бизнес: сделки, контракты, рынок, заказчики, конкуренты, регулирование.\n\n"
    "Команда /add проведёт по шагам: тип → ссылка или файл → заголовок → описание → компании → "
    "ваша оценка по шкале платформы → карточка на проверку.\n\n"
    "Материалы попадают на платформу OilTech: по ним ориентируются аналитики и учатся агенты."
)

STEP_PROMPTS = {
    "url": ("Пришлите <b>ссылку</b> на материал.", "https://…"),
    "file": ("Пришлите <b>файл</b> ответом на это сообщение (PDF, DOCX, XLSX, PPTX, изображение — до {max_mb} МБ).", "Файл"),
    "title": ("<b>Заголовок</b> — одной строкой, о чём материал.", "Например: «Татнефть заключила контракт на ГРП…»"),
    "description": ("<b>Описание</b>: что произошло, кто участники, когда и чем это важно для нефтесервиса.", "Описание"),
    "description_event": ("<b>Опишите событие</b>: что произошло, кто участники, когда, откуда известно и чем это важно для нефтесервиса.", "Описание события"),
    "companies": ("<b>Компании</b> — через запятую. Нет компаний — отправьте «-».", "Газпром нефть, Halliburton"),
    "comment": ("<b>Комментарий к оценке</b> — почему так. Без комментария — отправьте «-».", "Комментарий"),
}

HELP = (
    "<b>Материалы коллег</b> — полезное для нефтесервисных обществ, с упором на бизнес: сделки и "
    "контракты, рынок и цены, заказчики и конкуренты, регулирование, кадры, финансы.\n\n"
    "<b>Как внести</b>\n"
    "/add — бот проведёт по шагам. В группе отвечайте на вопросы бота <i>ответом</i> (reply) — "
    "так бот не спутает ваши ответы с чужими. Удобнее в личке — напишите боту туда.\n\n"
    "<b>Формат</b>\n"
    "1. Тип: 🔗 ссылка + описание · 📎 файл + описание · 📰 событие своими словами\n"
    "2. Ссылка или файл (до {max_mb} МБ)\n"
    "3. Заголовок — одной строкой\n"
    "4. Описание — что произошло и чем важно (от {min_desc} символов)\n"
    "5. Компании — через запятую, можно пропустить\n"
    "6. Оценка по шкале платформы\n"
    "7. Комментарий к оценке — можно пропустить\n"
    "Перед отправкой бот покажет карточку целиком.\n\n"
    "<b>Шкала оценки</b> (та же, что на платформе)\n{verdicts}\n\n"
    "/history: история и статус ваших материалов · /cancel: отменить ввод"
)


def _help_text() -> str:
    verdicts = "\n".join(f"• {html.escape(label)}" for _value, _short, label in cm.VERDICTS)
    return HELP.format(max_mb=config.TG_MATERIALS_MAX_FILE_BYTES // 1024 // 1024,
                       min_desc=cm.DESCRIPTION_MIN, verdicts=verdicts)


class TelegramError(RuntimeError):
    """Bot API не принял запрос. Текст без токена бота."""


class CoreError(RuntimeError):
    """Ядро не приняло материал. retryable — сбой связи или ядра, а не отказ по существу."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class TelegramApi:
    def __init__(self, token: str) -> None:
        self.token = token
        self.session = requests.Session()

    def _redact(self, text: str) -> str:
        return text.replace(self.token, "***") if self.token else text

    def call(self, method: str, http_timeout: float = 30, **params: Any) -> Any:
        payload = {key: value for key, value in params.items() if value is not None}
        try:
            response = self.session.post(f"{_TELEGRAM_API}/bot{self.token}/{method}", json=payload,
                                         timeout=http_timeout)
        except requests.RequestException as exc:
            # В тексте исключения requests — адрес запроса, а в нём токен.
            raise TelegramError(f"Telegram недоступен: {type(exc).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or not body.get("ok"):
            description = self._redact(str(body.get("description") or ""))[:200]
            raise TelegramError(f"Telegram {method}: {response.status_code} {description}".strip())
        return body.get("result")

    def download(self, file_id: str) -> bytes:
        info = self.call("getFile", file_id=file_id)
        path = (info or {}).get("file_path")
        if not path:
            raise TelegramError("Telegram getFile: нет пути к файлу")
        try:
            response = self.session.get(f"{_TELEGRAM_API}/file/bot{self.token}/{path}", timeout=120)
        except requests.RequestException as exc:
            raise TelegramError(f"Telegram недоступен: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise TelegramError(f"Telegram file: {response.status_code}")
        return response.content


class CoreClient:
    def __init__(self, core_api_url: str, token: str, worker_id: str) -> None:
        if not core_api_url:
            raise ValueError("CORE_API_URL is required for tg-materials-bot")
        if not token:
            raise ValueError("EXTERNAL_WORKER_TOKEN is required for tg-materials-bot")
        self.url = core_api_url.rstrip("/")
        self.worker_id = worker_id
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            contract.HEADER_BUILD: config.OILTECH_BUILD,
            contract.HEADER_CONTRACT: str(contract.CONTRACT),
        })

    def hello(self) -> None:
        response = self.session.post(f"{self.url}/api/external-worker/materials/hello",
                                     json={"worker_id": self.worker_id}, timeout=20)
        response.raise_for_status()

    def save(self, material: dict[str, Any], file: tuple[str, bytes] | None) -> dict[str, Any]:
        files = {"file": (file[0], file[1], "application/octet-stream")} if file else None
        try:
            response = self.session.post(
                f"{self.url}/api/external-worker/materials",
                data={"material": json.dumps(material, ensure_ascii=False), "worker_id": self.worker_id},
                files=files, timeout=120,
            )
        except requests.RequestException as exc:
            raise CoreError(f"платформа недоступна ({type(exc).__name__})", retryable=True) from None
        if response.status_code == 200:
            return response.json()["material"]
        try:
            detail = str(response.json().get("detail") or "")
        except ValueError:
            detail = ""
        retryable = response.status_code >= 500 or response.status_code in {401, 408, 429}
        raise CoreError(detail or f"платформа ответила {response.status_code}", retryable=retryable)

    def history(self, tg_user_id: int, limit: int = 10, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        """Материалы автора, новые первыми, и сколько их всего; ядро старее бота total не отдаёт — тогда счёт по странице."""
        response = self.session.get(f"{self.url}/api/external-worker/materials",
                                    params={"tg_user_id": tg_user_id, "limit": limit, "offset": offset}, timeout=20)
        response.raise_for_status()
        body = response.json()
        return body["materials"], int(body.get("total", len(body["materials"])))


@dataclass
class Dialog:
    chat_id: int
    user_id: int
    author: str
    username: str | None
    private: bool
    step: str = "kind"
    data: dict[str, Any] = field(default_factory=dict)
    prompt_id: int | None = None      # вопрос, на который ждём ответ
    bot_messages: list[int] = field(default_factory=list)  # что стереть после отправки
    user_messages: list[int] = field(default_factory=list)  # /add и ответы автора: тоже стираются
    card_id: int | None = None
    touched: float = field(default_factory=time.monotonic)


def _author(user: dict[str, Any]) -> str:
    name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part).strip()
    return name or (f"@{user['username']}" if user.get("username") else f"id{user.get('id')}")


def _size(value: int | None) -> str:
    if not value:
        return ""
    if value < 1024 * 1024:
        return f"{max(1, round(value / 1024))} КБ"
    return f"{value / 1024 / 1024:.1f} МБ".replace(".", ",")


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit].rstrip() + "…"


def _visible_len(text: str) -> int:
    """Длина так, как её считает Telegram: текст после разбора HTML, в единицах UTF-16."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)).encode("utf-16-le")) // 2


def card_text(data: dict[str, Any], *, author: str, header: str, when: datetime | None = None) -> str:
    """Карточка материала: и на проверку автору, и итоговая в группе."""
    esc = html.escape
    kind = data.get("kind") or "event"
    head = [f"{KIND_ICONS.get(kind, '•')} <b>{esc(header)}</b> · {esc(cm.KINDS.get(kind, kind))}", "",
            f"<b>{esc(str(data.get('title') or '—'))}</b>", ""]
    tail = [""]
    if data.get("url"):
        tail.append(f"🔗 {esc(_clip(str(data['url']), _CARD_URL_SHOWN))}")
    if data.get("file_name"):
        size = _size(data.get("file_size"))
        tail.append(f"📎 {esc(data['file_name'])}" + (f" · {size}" if size else ""))
    companies = _clip(", ".join(data.get("companies") or []), _CARD_COMPANIES_SHOWN)
    tail.append(f"🏢 Компании: {esc(companies) if companies else '—'}")
    verdict = cm.VERDICT_LABELS.get(str(data.get("verdict") or ""), "—")
    tail.append(f"⭐ Оценка: <b>{esc(verdict)}</b>")
    if data.get("verdict_comment"):
        tail.append(f"💬 {esc(_clip(str(data['verdict_comment']), _CARD_COMMENT_SHOWN))}")
    stamp = (when or datetime.now(_MSK)).astimezone(_MSK).strftime("%d.%m.%Y %H:%M")
    tail.append(f"👤 {esc(author)} · {stamp} МСК")
    # Описание получает, что осталось от предела сообщения, с запасом на знак обрезки и перевод строки.
    room = _TELEGRAM_TEXT_MAX - 8 - _visible_len("\n".join(head + tail))
    description = _clip(str(data.get("description") or ""), max(0, min(_CARD_DESCRIPTION_MAX, room)))
    return "\n".join([*head, esc(description) or "—", *tail])


def history_text(rows: list[dict[str, Any]], total: int, offset: int = 0) -> str:
    """Страница истории: номер, дата, заголовок, оценка автора (коротко) и статус материала."""
    if not total:
        return "Ваших материалов пока нет. Внести первый: /add."
    shorts = {value: short for value, short, _full in cm.VERDICTS}
    lines = [f"📋 <b>История загрузок</b> · всего {total}", ""]
    for row in rows:
        when = str(row.get("created_at") or "")[:10]
        day = ".".join(reversed(when.split("-"))) if when else ""
        verdict = shorts.get(str(row.get("verdict") or ""), str(row.get("verdict_label") or ""))
        status = str(row.get("status_label") or "Принят на платформу")
        lines.append(f"{KIND_ICONS.get(row.get('kind'), '•')} №{row['id']} · {html.escape(day)} · "
                     f"<b>{html.escape(str(row.get('title') or ''))}</b>")
        lines.append(f"     ⭐ {html.escape(verdict)} · {html.escape(status)}")
    if total > len(rows):
        lines += ["", f"Показаны с {offset + 1} по {offset + len(rows)}."]
    return "\n".join(lines)


def _button(text: str, data: str) -> dict[str, str]:
    return {"text": text, "callback_data": data}


class MaterialsBot:
    """Логика бота без сети: Telegram и ядро приходят снаружи (в тестах — подставные)."""

    def __init__(self, tg: Any, core: Any, *, chat_ids: list[int], clock: Callable[[], float] = time.monotonic) -> None:
        self.tg = tg
        self.core = core
        self.chat_ids = set(chat_ids)
        self.clock = clock
        self.dialogs: dict[tuple[int, int], Dialog] = {}
        self.username = ""
        self._members: dict[int, tuple[bool, float]] = {}
        self._later: list[tuple[float, int, int]] = []  # (когда, чат, сообщение): стереть позже, tick()

    # --- вход ---------------------------------------------------------------------------------

    def handle(self, update: dict[str, Any]) -> None:
        try:
            if "callback_query" in update:
                self._on_callback(update["callback_query"])
            elif "message" in update:
                self._on_message(update["message"])
        except TelegramError as exc:
            logger.warning("tg_materials_update_failed update=%s error=%s", update.get("update_id"), exc)
        except Exception:  # noqa: BLE001 - упавший процесс получил бы это обновление снова: петля перезапусков
            logger.exception("tg_materials_update_crashed update=%s", update.get("update_id"))
            self._tell_crash(update)

    def _tell_crash(self, update: dict[str, Any]) -> None:
        """Автор не остаётся в тишине: кнопке — всплывающий ответ, сообщению — короткий ответ в тот же чат."""
        text = "Что-то пошло не так. Попробуйте ещё раз или начните заново: /add."
        try:
            if "callback_query" in update:
                self._ack(update["callback_query"], text, alert=True)
            else:
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                if chat.get("id") is not None:
                    self._send(chat["id"], text, reply_to=message.get("message_id"))
        except Exception:  # noqa: BLE001 - сообщить об ошибке — по возможности, не ценой второго падения
            logger.warning("tg_materials_crash_notice_failed update=%s", update.get("update_id"))

    def _allowed(self, chat: dict[str, Any], user: dict[str, Any]) -> bool:
        if chat.get("type") == "private":
            return self._is_member(int(user["id"]))
        return int(chat["id"]) in self.chat_ids

    def _is_member(self, user_id: int) -> bool:
        cached = self._members.get(user_id)
        if cached and self.clock() - cached[1] < _MEMBER_CACHE_SECONDS:
            return cached[0]
        member = False
        for chat_id in self.chat_ids:
            try:
                info = self.tg.call("getChatMember", chat_id=chat_id, user_id=user_id)
            except TelegramError:
                continue
            status = (info or {}).get("status")
            if status in {"creator", "administrator", "member"} or (status == "restricted" and info.get("is_member")):
                member = True
                break
        self._members[user_id] = (member, self.clock())
        return member

    def _command(self, text: str) -> str | None:
        if not text.startswith("/"):
            return None
        head = text.split()[0][1:]
        name, _, target = head.partition("@")
        if target and self.username and target.lower() != self.username.lower():
            return None  # команда другому боту группы
        return name.lower()

    def _on_message(self, message: dict[str, Any]) -> None:
        chat, user = message.get("chat") or {}, message.get("from") or {}
        if not chat or not user or user.get("is_bot"):
            return
        text = str(message.get("text") or message.get("caption") or "").strip()
        command = self._command(text)
        if not self._allowed(chat, user):
            if chat.get("type") == "private" and command:
                self._send(chat["id"], "Бот работает для участников рабочей группы материалов. "
                                       "Попросите администратора добавить вас в группу.")
            return
        key = (int(chat["id"]), int(user["id"]))
        if command in {"start", "help"}:
            self._send(chat["id"], _help_text(), reply_to=message.get("message_id"))
            return
        if command in {"add", "new"}:
            self._start(chat, user, message)
            return
        if command == "cancel":
            self._cancel(key, message)
            return
        if command in {"history", "my"}:
            self._history(chat, user, message)
            return
        if command:
            return
        dialog = self._dialog(key)
        if dialog is None:
            if chat.get("type") == "private":
                self._send(chat["id"], "Чтобы внести материал, отправьте /add. Формат и шкала — /help.")
            return
        replied = (message.get("reply_to_message") or {}).get("message_id")
        if not dialog.private and replied != dialog.prompt_id:
            return  # в группе ход диалога — только ответом на свой вопрос
        self._answer(dialog, message, text)

    def _dialog(self, key: tuple[int, int]) -> Dialog | None:
        dialog = self.dialogs.get(key)
        if dialog is None:
            return None
        if self.clock() - dialog.touched > config.TG_MATERIALS_DIALOG_TTL_MINUTES * 60:
            self.dialogs.pop(key, None)
            self._cleanup(dialog)
            return None
        return dialog

    # --- шаги ---------------------------------------------------------------------------------

    def _start(self, chat: dict[str, Any], user: dict[str, Any], message: dict[str, Any], *,
               own: bool = True) -> None:
        """own — /add самого автора (стирается вместе с вводом); «Заново» начинает без нового сообщения."""
        key = (int(chat["id"]), int(user["id"]))
        previous = self.dialogs.pop(key, None)
        if previous:
            self._cleanup(previous)
        dialog = Dialog(chat_id=key[0], user_id=key[1], author=_author(user),
                        username=user.get("username"), private=chat.get("type") == "private",
                        touched=self.clock())
        # На это сообщение автора бот отвечает вопросами: ForceReply с selective показывается
        # тому, на чьё сообщение ответ, и тем, кто упомянут, — остальным в группе нет.
        dialog.data["_last_user_message"] = message.get("message_id")
        if own and message.get("message_id"):
            dialog.user_messages.append(int(message["message_id"]))
        self.dialogs[key] = dialog
        buttons = [[_button(f"{KIND_ICONS[kind]} {label}", f"m:{dialog.user_id}:kind:{kind}")]
                   for kind, label in cm.KINDS.items()]
        buttons.append([_button("✖ Отмена", f"m:{dialog.user_id}:cancel:")])
        sent = self._send(dialog.chat_id, f"{self._intro(dialog)}что вносим?\n\nСсылку или файл — с описанием; "
                                          f"событие — описанием своими словами.",
                          reply_to=message.get("message_id"), markup={"inline_keyboard": buttons})
        self._remember(dialog, sent)

    @staticmethod
    def _intro(dialog: Dialog) -> str:
        # В группе — упоминание автора: видно, чей это вопрос, и ForceReply адресован ему.
        if dialog.private:
            return ""
        return f'<a href="tg://user?id={dialog.user_id}">{html.escape(dialog.author)}</a>, '

    def _ask(self, dialog: Dialog, step: str, *, note: str = "") -> None:
        # Шаг сдвигается только после того, как Telegram принял вопрос: при сбое (лимит сообщений в
        # группе) диалог остаётся на прежнем вопросе, и тот же ответ, отправленный ещё раз, ведёт дальше.
        prompt_key = "description_event" if step == "description" and dialog.data.get("kind") == "event" else step
        text, placeholder = STEP_PROMPTS[prompt_key]
        text = text.format(max_mb=config.TG_MATERIALS_MAX_FILE_BYTES // 1024 // 1024)
        body = (f"{html.escape(note)}\n\n" if note else "") + self._intro(dialog) + text
        if not dialog.private:
            body += "\n<i>Ответьте на это сообщение.</i>"
        sent = self._send(dialog.chat_id, body, markup={
            "force_reply": True, "selective": True, "input_field_placeholder": placeholder[:64],
        }, reply_to=dialog.data.get("_last_user_message"))
        self._remember(dialog, sent)
        dialog.step = step
        dialog.prompt_id = sent.get("message_id") if sent else None

    def _ask_verdict(self, dialog: Dialog) -> None:
        rows = [[_button(short, f"m:{dialog.user_id}:verdict:{value}")] for value, short, _full in cm.VERDICTS]
        scale = "\n".join(f"• {html.escape(full)}" for _value, _short, full in cm.VERDICTS)
        sent = self._send(dialog.chat_id, f"{self._intro(dialog)}<b>ваша оценка</b> — по шкале платформы:\n\n{scale}",
                          markup={"inline_keyboard": rows})
        self._remember(dialog, sent)
        dialog.step = "verdict"
        dialog.prompt_id = None

    def _answer(self, dialog: Dialog, message: dict[str, Any], text: str) -> None:
        dialog.touched = self.clock()
        if message.get("message_id"):
            dialog.user_messages.append(int(message["message_id"]))
        dialog.data["_last_user_message"] = message.get("message_id")
        step = dialog.step
        try:
            if step == "url":
                found = _URL_RE.search(text)
                dialog.data["url"] = cm.check_url(found.group(0).rstrip(".,;)") if found else text)
                self._ask(dialog, "title")
            elif step == "file":
                self._take_file(dialog, message)
                if dialog.step == "file":
                    return
                caption = str(message.get("caption") or "").strip()
                try:
                    dialog.data["title"] = cm.check_title(caption.splitlines()[0]) if caption else None
                except cm.InvalidMaterial:
                    dialog.data["title"] = None
                if dialog.data["title"]:
                    self._ask(dialog, "description", note=f"Заголовок взят из подписи к файлу: «{dialog.data['title']}».")
                else:
                    self._ask(dialog, "title", note="Файл принят.")
            elif step == "title":
                dialog.data["title"] = cm.check_title(text)
                self._ask(dialog, "description")
            elif step == "description":
                dialog.data["description"] = cm.check_description(text)
                self._ask(dialog, "companies")
            elif step == "companies":
                dialog.data["companies"] = cm.parse_companies(text)
                self._ask_verdict(dialog)
            elif step == "comment":
                dialog.data["verdict_comment"] = None if text in {"-", "—"} else cm.check_comment(text)
                self._show_card(dialog)
            else:
                return  # ждём нажатия кнопки, а не текста
        except cm.InvalidMaterial as exc:
            self._ask(dialog, step, note=f"⚠️ {exc}")

    def _take_file(self, dialog: Dialog, message: dict[str, Any]) -> None:
        document = message.get("document")
        if document:
            file_id, name, size = document.get("file_id"), document.get("file_name") or "файл", document.get("file_size")
        elif message.get("photo"):
            photo = max(message["photo"], key=lambda item: item.get("file_size") or 0)
            file_id, name, size = photo.get("file_id"), "фото.jpg", photo.get("file_size")
        else:
            self._ask(dialog, "file", note="⚠️ Здесь нужен файл — приложите его ответом на сообщение.")
            return
        if size and size > config.TG_MATERIALS_MAX_FILE_BYTES:
            self._ask(dialog, "file", note=f"⚠️ Файл {_size(size)}: бот принимает до "
                                           f"{config.TG_MATERIALS_MAX_FILE_BYTES // 1024 // 1024} МБ.")
            return
        dialog.data.update({"file_id": file_id, "file_name": name, "file_size": size,
                            "_file_message": message.get("message_id")})
        dialog.step = "file_taken"

    def _show_card(self, dialog: Dialog) -> None:
        uid = dialog.user_id
        markup = {"inline_keyboard": [
            [_button("✅ Отправить", f"m:{uid}:send:")],
            [_button("↺ Заново", f"m:{uid}:restart:"), _button("✖ Отмена", f"m:{uid}:cancel:")],
        ]}
        text = card_text(dialog.data, author=dialog.author, header="Проверьте карточку")
        sent = self._send(dialog.chat_id, text, markup=markup,
                          reply_to=dialog.data.get("_file_message") or dialog.data.get("_last_user_message"))
        dialog.step = "confirm"
        dialog.prompt_id = None
        dialog.card_id = sent.get("message_id") if sent else None

    # --- кнопки -------------------------------------------------------------------------------

    def _on_callback(self, query: dict[str, Any]) -> None:
        parts = str(query.get("data") or "").split(":", 3)
        user = query.get("from") or {}
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        if len(parts) != 4 or parts[0] != "m" or not chat:
            self._ack(query)
            return
        _, owner, action, value = parts
        if str(user.get("id")) != owner:
            self._ack(query, "Это меню другого участника. Чтобы внести свой материал — /add.", alert=True)
            return
        if action == "history":  # листание истории — не ввод, диалога у него нет
            self._ack(query)
            self._history(chat, user, {}, offset=int(value) if value.isdigit() else 0,
                          edit_id=message.get("message_id"))
            return
        key = (int(chat["id"]), int(user["id"]))
        dialog = self._dialog(key)
        if dialog is None:
            self._ack(query, "Ввод устарел или уже отправлен. Начните заново — /add.", alert=True)
            return
        dialog.touched = self.clock()
        if action == "cancel":
            self._ack(query, "Ввод отменён")
            self.dialogs.pop(key, None)
            self._cleanup(dialog)
        elif action == "restart":
            self._ack(query)
            self._start(chat, user, {"message_id": dialog.data.get("_last_user_message")}, own=False)
        elif action == "kind" and dialog.step == "kind" and value in cm.KINDS:
            self._ack(query, cm.KINDS[value])
            dialog.data["kind"] = value
            self._ask(dialog, {"link": "url", "file": "file", "event": "title"}[value])
        elif action == "verdict" and dialog.step == "verdict" and value in cm.VERDICT_LABELS:
            self._ack(query, cm.VERDICT_LABELS[value])
            dialog.data["verdict"] = value
            self._ask(dialog, "comment", note=f"Оценка: {cm.VERDICT_LABELS[value]}.")
        elif action == "send" and dialog.step == "confirm" and message.get("message_id") == dialog.card_id:
            self._submit(dialog, query)
        else:
            self._ack(query)

    def _submit(self, dialog: Dialog, query: dict[str, Any]) -> None:
        data = dialog.data
        material = {
            "kind": data.get("kind"), "title": data.get("title"), "description": data.get("description"),
            "url": data.get("url"), "companies": data.get("companies") or [], "verdict": data.get("verdict"),
            "verdict_comment": data.get("verdict_comment"),
            # Ключ повтора — сообщение с карточкой: двойное нажатие и повтор после сбоя не двоят материал.
            "tg_chat_id": dialog.chat_id, "tg_message_id": dialog.card_id, "tg_user_id": dialog.user_id,
            "tg_username": dialog.username, "tg_author": dialog.author,
        }
        try:
            file = None
            if data.get("kind") == "file":
                file = (data.get("file_name") or "файл", self.tg.download(data["file_id"]))
            saved = self.core.save(material, file)
        except (TelegramError, CoreError) as exc:
            retry = not isinstance(exc, CoreError) or exc.retryable
            hint = "Нажмите «Отправить» ещё раз чуть позже." if retry else "Нажмите «Заново» и поправьте материал."
            self._ack(query, f"Не сохранено: {exc}. {hint}"[:200], alert=True)
            logger.warning("tg_materials_submit_failed chat=%s user=%s error=%s", dialog.chat_id, dialog.user_id, exc)
            return
        self._ack(query, "Материал принят")
        self.dialogs.pop((dialog.chat_id, dialog.user_id), None)
        final = card_text(data, author=dialog.author, header=f"Материал №{saved['id']} принят на платформу")
        self.tg.call("editMessageText", chat_id=dialog.chat_id, message_id=dialog.card_id, text=final,
                     parse_mode="HTML", disable_web_page_preview=True, reply_markup={"inline_keyboard": []})
        if dialog.private:
            self._post_to_groups(dialog.user_id, final)
        # Остаются итоговая карточка и у материала «файл» сообщение с файлом: коллеги открывают его в группе.
        self._cleanup(dialog, keep={dialog.card_id, data.get("_file_message") if data.get("kind") == "file" else None})

    def _post_to_groups(self, user_id: int, text: str) -> None:
        """Материал из лички — и в рабочие группы автора: группа видит всё, что внесено, как бы ни вносили."""
        for chat_id in sorted(self.chat_ids):
            try:
                info = self.tg.call("getChatMember", chat_id=chat_id, user_id=user_id) or {}
                if info.get("status") in {"creator", "administrator", "member"} or info.get("is_member"):
                    self._send(chat_id, text)
            except TelegramError as exc:
                # Материал уже на платформе: сбой показа в группе ввод не ломает.
                logger.warning("tg_materials_group_post_failed chat=%s error=%s", chat_id, exc)

    # --- прочие команды -----------------------------------------------------------------------

    def _cancel(self, key: tuple[int, int], message: dict[str, Any]) -> None:
        dialog = self.dialogs.pop(key, None)
        if dialog is None:
            self._send(key[0], "Отменять нечего: ввода нет. Внести материал — /add.", reply_to=message.get("message_id"))
            return
        self._cleanup(dialog)
        self._send(key[0], "Ввод отменён.", reply_to=message.get("message_id"))

    def _history(self, chat: dict[str, Any], user: dict[str, Any], message: dict[str, Any], *,
                 offset: int = 0, edit_id: int | None = None) -> None:
        """История и статус материалов автора, по _HISTORY_PAGE; «Ещё» листает тем же сообщением."""
        chat_id, uid = int(chat["id"]), int(user["id"])
        try:
            rows, total = self.core.history(uid, limit=_HISTORY_PAGE, offset=offset)
        except requests.RequestException:
            self._send(chat_id, "Платформа сейчас недоступна, попробуйте позже.", reply_to=message.get("message_id"))
            return
        more = offset + len(rows) < total
        buttons = [[_button("Ещё", f"m:{uid}:history:{offset + _HISTORY_PAGE}")]] if more else []
        text = history_text(rows, total, offset)
        if edit_id:
            self.tg.call("editMessageText", chat_id=chat_id, message_id=edit_id, text=text, parse_mode="HTML",
                         disable_web_page_preview=True, reply_markup={"inline_keyboard": buttons})
            return
        sent = self._send(chat_id, text, reply_to=message.get("message_id"),
                          markup={"inline_keyboard": buttons} if buttons else None)
        if chat.get("type") != "private":
            self._delete_later(chat_id, [message.get("message_id"), sent.get("message_id")])

    def _delete_later(self, chat_id: int, message_ids: list[int | None]) -> None:
        due = self.clock() + _GROUP_REPLY_SECONDS
        self._later += [(due, chat_id, int(message_id)) for message_id in message_ids if message_id]

    def tick(self) -> None:
        """Отложенная уборка группы; цикл зовёт её между опросами Telegram (опрос длится до 50 с)."""
        now = self.clock()
        due = [item for item in self._later if item[0] <= now]
        self._later = [item for item in self._later if item[0] > now]
        for _when, chat_id, message_id in due:
            try:
                self.tg.call("deleteMessage", chat_id=chat_id, message_id=message_id)
            except TelegramError:
                pass  # уже удалено руками или старше 48 ч

    # --- Telegram -----------------------------------------------------------------------------

    def _send(self, chat_id: int, text: str, *, reply_to: int | None = None, markup: dict | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                  "disable_web_page_preview": True, "reply_markup": markup}
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        return self.tg.call("sendMessage", **params) or {}

    def _ack(self, query: dict[str, Any], text: str | None = None, *, alert: bool = False) -> None:
        try:
            self.tg.call("answerCallbackQuery", callback_query_id=query.get("id"), text=text,
                         show_alert=alert or None)
        except TelegramError:
            pass  # кнопка «повисит» пару секунд — не повод терять ход диалога

    def _remember(self, dialog: Dialog, sent: dict[str, Any] | None) -> None:
        if sent and sent.get("message_id"):
            dialog.bot_messages.append(int(sent["message_id"]))

    def _cleanup(self, dialog: Dialog, keep: set[int | None] | None = None) -> None:
        """Стереть ввод: вопросы бота, /add и ответы автора; keep — что оставить (итоговая карточка, файл)."""
        keep = keep or set()
        for message_id in dialog.bot_messages + ([dialog.card_id] if dialog.card_id else []) + dialog.user_messages:
            if message_id in keep:
                continue
            try:
                self.tg.call("deleteMessage", chat_id=dialog.chat_id, message_id=message_id)
            except TelegramError:
                pass  # старше 48 ч или уже удалено


# --- цикл -------------------------------------------------------------------------------------


def configure_profile(tg: TelegramApi) -> str:
    """Имя, описание и меню команд бота — их видно в профиле и в пустом чате до /start."""
    me = tg.call("getMe") or {}
    for method, params in (
        ("setMyDescription", {"description": DESCRIPTION}),
        ("setMyShortDescription", {"short_description": SHORT_DESCRIPTION}),
        ("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in COMMANDS]}),
    ):
        try:
            tg.call(method, **params)
        except TelegramError as exc:
            logger.warning("tg_materials_profile_failed method=%s error=%s", method, exc)
    return str(me.get("username") or "")


def run(*, core_api_url: str | None = None, token: str | None = None, worker_id: str | None = None) -> None:
    core = CoreClient(core_api_url or config.CORE_API_URL, token or config.EXTERNAL_WORKER_TOKEN,
                      worker_id or config.EXTERNAL_WORKER_ID)
    stop = {"flag": False}

    def _stop(*_args: Any) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    last_hello: float | None = None

    def hello() -> None:
        nonlocal last_hello
        if last_hello is not None and time.monotonic() - last_hello < _HELLO_EVERY_SECONDS:
            return
        try:
            core.hello()
            last_hello = time.monotonic()
        except requests.RequestException as exc:
            logger.warning("tg_materials_hello_failed error=%s", type(exc).__name__)

    if not config.TG_MATERIALS_BOT_TOKEN or not config.TG_MATERIALS_CHAT_IDS:
        # Не падаем: иначе restart-петля, и выкат NL встал бы на этом контейнере. Бот молчит,
        # но отмечается в ядре — check-lanes и worker-versions покажут, что он жив и чей сборки.
        logger.error("tg_materials_bot_disabled: задайте TG_MATERIALS_BOT_TOKEN и TG_MATERIALS_CHAT_IDS")
        while not stop["flag"]:
            hello()
            time.sleep(5)
        return

    tg = TelegramApi(config.TG_MATERIALS_BOT_TOKEN)
    bot = MaterialsBot(tg, core, chat_ids=config.TG_MATERIALS_CHAT_IDS)
    while not stop["flag"] and not bot.username:
        hello()
        try:
            bot.username = configure_profile(tg)
        except TelegramError as exc:
            logger.warning("tg_materials_getme_failed error=%s", exc)
            time.sleep(15)
    logger.info("tg_materials_bot_started username=%s chats=%s", bot.username, sorted(bot.chat_ids))
    offset: int | None = None
    pause = 1.0
    while not stop["flag"]:
        hello()
        bot.tick()
        try:
            updates = tg.call("getUpdates", http_timeout=_POLL_SECONDS + 15, timeout=_POLL_SECONDS,
                              offset=offset, allowed_updates=["message", "callback_query"])
            pause = 1.0
        except TelegramError as exc:
            logger.warning("tg_materials_poll_failed error=%s", exc)
            time.sleep(pause)
            pause = min(pause * 2, 60)
            continue
        for update in updates or []:
            offset = int(update["update_id"]) + 1
            bot.handle(update)
    logger.info("tg_materials_bot_stopped")
