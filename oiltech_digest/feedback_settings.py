"""Настройки контура ОС (#189, спека #187): все 20 настроек «Настроек контура ОС» (решение владельца 12.11, приняты как
предложено) хранятся в базе и действуют без выката; правит их администратор в разделе «Настройки» вкладки «Обратная
связь».

Хранение: строка на сохранение (feedback_settings), действует последняя поверх умолчаний кода; прежние строки остаются
историей, кто и когда что менял. Журнал ОС и недельный срез читают расписание, окно похожих отметок, порог улучшения
карточки, срок жизни предложения, выборку отклонённого, кто видит вкладку, значок, модель разбора и потолок расходов;
правила эксперта и автопроверка (#190): пороги правила, объём проверки, совет, «мало данных», проверки «Сути и
перевода» и «Поиска», проверку набора и «правило эксперта главнее инструкции».

ВНИМАНИЕ про тесты: соединение: `connection.get_connection()` атрибутом модуля: изоляция тестов держится на подмене
этого атрибута.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Json

from oiltech_digest import config
from oiltech_digest.db import connection

# Модель разбора ОС: только линейка gpt-6 (правило владельца 07.11) и только с известной ставкой, иначе потолок
# расходов считал бы её по ставке по умолчанию.
_MODEL_RE = re.compile(r"gpt-6(\.\d+)?(-[a-z0-9]+)*")
_TIME_RE = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    kind: str  # int, number, bool, time, choice, model
    default: Any
    low: float | None = None
    high: float | None = None
    choices: tuple[tuple[str, str], ...] = ()
    # Вступает в силу позже, с будущей частью контура: до тех пор только хранится (с #190 таких нет).
    later: bool = False


WEEKDAYS = (("1", "понедельник"), ("2", "вторник"), ("3", "среда"), ("4", "четверг"), ("5", "пятница"),
            ("6", "суббота"), ("7", "воскресенье"))

SETTINGS: tuple[Setting, ...] = (
    Setting("slice_weekday", "День недельного среза", "choice", "1", choices=WEEKDAYS),
    Setting("slice_time", "Время недельного среза, МСК", "time", "09:00"),
    Setting("manual_slice", "Кнопка «собрать сейчас» у администратора", "bool", True),
    Setting("similar_window_days", "Окно похожих отметок, дней", "int", 30, 1, 365),
    Setting("rule_threshold_marks", "Порог правила агенту: отметок об одном и том же", "int", 2, 1, 20),
    Setting("rule_explicit_request", "Правило агенту по одной явной просьбе («переводить X как Y», «всегда…»)", "bool",
            True),
    Setting("card_threshold_marks", "Порог улучшения карточки: отметок", "int", 1, 1, 20),
    Setting("proposal_lifetime_days", "Срок жизни предложения без решения, дней", "int", 30, 1, 365),
    Setting("autocheck_rows", "Объём автопроверки: строк эталона ОС", "int", 150, 10, 1000),
    Setting("autocheck_advice", "Когда система советует «согласовать»", "choice", "more_toward",
            choices=(("more_toward", "к эталону сдвинулось больше, чем от него"),
                     ("none_away", "ни одного решения не сдвинулось от эталона"))),
    Setting("little_data_rows", "«Мало данных»: меньше строк эталона", "int", 30, 1, 1000),
    Setting("summary_check_edits", "Проверка «Сути и перевода»: последних правок «было / стало»", "int", 20, 1, 200),
    Setting("search_check_topics", "Проверка «Поиска»: тем повторного поиска", "int", 1, 1, 13),
    Setting("search_check_days", "Проверка «Поиска»: дней повторного поиска", "int", 7, 1, 30),
    Setting("set_check_monthly", "Проверка всего набора правил в первый понедельник месяца", "bool", True),
    Setting("rejected_sample_size", "Выборка отклонённого «Отбором» в первый понедельник месяца, публикаций", "int",
            20, 0, 200),
    Setting("expert_rule_priority", "При противоречии правило эксперта главнее основной инструкции", "bool", True),
    Setting("tab_visible_to", "Кто видит вкладку «Обратная связь»", "choice", "all",
            choices=(("all", "все допущенные"), ("admin", "только администратор"))),
    Setting("card_badge", "Значок «ждёт решения в журнале» на карточке", "bool", True),
    Setting("removed_visible_to", "Кто видит раздел «Убрано людьми»", "choice", "admin",
            choices=(("admin", "только администратор"), ("all", "все допущенные"))),
    Setting("review_model", "Модель разбора ОС", "model", "gpt-6-sol"),
    Setting("monthly_ceiling_usd", "Потолок расходов контура в месяц, $", "number", 50, 0, 10000),
)
_BY_KEY = {setting.key: setting for setting in SETTINGS}


def defaults() -> dict[str, Any]:
    return {setting.key: setting.default for setting in SETTINGS}


def current() -> dict[str, Any]:
    """Действующие настройки: последняя сохранённая строка поверх умолчаний. Ключ, которого код уже не знает, и
    значение, которое стало неверным, не действуют: вместо них умолчание."""
    with connection.get_connection() as conn:
        row = conn.execute("SELECT values_json FROM feedback_settings ORDER BY id DESC LIMIT 1").fetchone()
    stored = row[0] if row and isinstance(row[0], dict) else {}
    values = defaults()
    for key, value in stored.items():
        setting = _BY_KEY.get(key)
        if setting is None:
            continue
        try:
            values[key] = _checked(setting, value)
        except ValueError:
            continue
    return values


def save(changes: dict[str, Any], *, author: str | None) -> dict[str, Any]:
    """Сохранить правку администратора поверх действующих: новой строкой. ValueError: незнакомая настройка или неверное
    значение, словами для ответа; ничего не сохранено."""
    unknown = sorted(set(changes) - set(_BY_KEY))
    if unknown:
        raise ValueError(f"Нет такой настройки контура: {', '.join(unknown)}")
    values = current()
    for key, value in changes.items():
        values[key] = _checked(_BY_KEY[key], value)
    with connection.get_connection() as conn:
        conn.execute("INSERT INTO feedback_settings (values_json, author) VALUES (%s, %s)", (Json(values), author))
        conn.commit()
    return values


def describe() -> list[dict[str, Any]]:
    """Состав настроек для экрана: подпись, вид значения, пределы, варианты, умолчание и вступает ли в силу позже."""
    return [{"key": setting.key, "label": setting.label, "kind": setting.kind, "default": setting.default,
             "low": setting.low, "high": setting.high, "later": setting.later,
             "choices": [{"value": value, "label": label} for value, label in setting.choices]}
            for setting in SETTINGS]


def _checked(setting: Setting, value: Any) -> Any:
    label = f"«{setting.label}»"
    if setting.kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{label}: да или нет")
        return value
    if setting.kind in ("int", "number"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{label}: нужно число")
        if setting.kind == "int" and float(value) != int(value):
            raise ValueError(f"{label}: нужно целое число")
        number = int(value) if setting.kind == "int" else round(float(value), 2)
        if (setting.low is not None and number < setting.low) or (setting.high is not None and number > setting.high):
            raise ValueError(f"{label}: от {setting.low:g} до {setting.high:g}")
        return number
    text = " ".join(str(value or "").split())
    if setting.kind == "time":
        if not _TIME_RE.fullmatch(text):
            raise ValueError(f"{label}: время ЧЧ:ММ, например 09:00")
        return text
    if setting.kind == "choice":
        if text not in {choice for choice, _ in setting.choices}:
            raise ValueError(f"{label}: одно из {', '.join(label for _, label in setting.choices)}")
        return text
    if setting.kind == "model":
        if not _MODEL_RE.fullmatch(text) or not any(text.startswith(prefix) for prefix in config.OPENAI_MODEL_PRICES
                                                     if prefix.startswith("gpt-6")):
            raise ValueError(f"{label}: только модель линейки gpt-6 с известной ценой, например gpt-6-sol")
        return text
    raise ValueError(f"{label}: неизвестный вид настройки")
