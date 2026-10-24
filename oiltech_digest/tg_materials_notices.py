"""Итоги «Отбора» авторам материалов коллег (#131): бот на NL забирает у ядра несообщённые итоги и отвечает в чате
материала на его карточку, в группе — с упоминанием автора. Текст и итог считает ядро (material_signals), здесь —
доставка: разметка, повтор при сбое Telegram и отметка доставленного."""

from __future__ import annotations

import html
import logging
import re
from typing import Any, Callable

import requests

logger = logging.getLogger(__name__)

# Итоги бот забирает у ядра не чаще раза в минуту: решение приходит минутами, а не секундами.
NOTICES_EVERY_SECONDS = 60
# Чат, куда бот писать не может (удалён, бот исключён или заблокирован): повтор уведомления не поможет.
_UNDELIVERABLE_RE = re.compile(r"Telegram \w+: (?:400|403)\b")


def notice_text(notice: dict[str, Any]) -> str:
    """Итог автору: текст ядра; в группе — с упоминанием автора, в личке — с заглавной буквы."""
    text = html.escape(str(notice.get("text") or ""))
    if int(notice["chat_id"]) == int(notice["user_id"]):
        return text[:1].upper() + text[1:]
    author = html.escape(str(notice.get("author") or "автор"))
    return f'<a href="tg://user?id={int(notice["user_id"])}">{author}</a>, {text}'


def deliver(core: Any, send: Callable[..., Any], telegram_error: type[Exception]) -> int:
    """Доставить несообщённые итоги: ответ на карточку материала (send — отправка бота). Доставленное ядро помнит.
    Сбой Telegram — повтор в следующий раз; чат, куда писать нельзя (400, 403), — без повтора: иначе одно
    уведомление слали бы вечно. Сколько итогов разобрано."""
    try:
        notices = core.notices()
    except requests.RequestException as exc:
        logger.warning("tg_materials_notices_failed error=%s", type(exc).__name__)
        return 0
    handled = 0
    for notice in notices:
        try:
            send(int(notice["chat_id"]), notice_text(notice), reply_to=int(notice["reply_to"]))
        except telegram_error as exc:
            logger.warning("tg_materials_notice_failed material=%s error=%s", notice.get("material_id"), exc)
            if not _UNDELIVERABLE_RE.search(str(exc)):
                continue
        try:
            core.notice_sent(int(notice["material_id"]), str(notice["outcome"]))
            handled += 1
        except requests.RequestException as exc:
            logger.warning("tg_materials_notice_ack_failed material=%s error=%s", notice.get("material_id"),
                           type(exc).__name__)
    return handled
