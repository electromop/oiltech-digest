"""Профили скоринга (сессия G, ADR 0002): «Бизнес-сигналы» и «Технологический радар».

Профиль — свой набор критериев со своими весами, сумма в каждом — 100. Профиль — данные
(`scoring_criteria.profile`), а не код: заказчик правит оба набора на экране «Скоринг».

Статья ленты всегда оценивается профилем business: лента и есть бизнес-сигналы, выбора
профиля по статье нет. Поэтому у всех читателей, которые профилей не знают (конвейер, пакет
для NL, песочница агента источников), умолчание — business, и их поведение не меняется.
Профиль tech_radar хранится и правится на своей вкладке, но к оценке радара пока не подключён:
подключение — зона Германа (ADR 0002, вариант Б1).
"""

from __future__ import annotations

BUSINESS = "business"
TECH_RADAR = "tech_radar"
SCORING_PROFILES = (BUSINESS, TECH_RADAR)

# Чем оценивается статья ленты.
ARTICLE_SCORING_PROFILE = BUSINESS


def check_profile(value: object) -> str:
    """Граница: профиль приходит из запроса, CLI и итога воркера. Чужое — ValueError."""
    if value not in SCORING_PROFILES:
        raise ValueError(f"Неизвестный профиль скоринга {value!r}: есть {', '.join(SCORING_PROFILES)}")
    return str(value)
