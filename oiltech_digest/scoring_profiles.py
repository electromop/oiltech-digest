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

import hashlib
import json
from typing import Any

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


# --- Происхождение балла: снимок критериев ----------------------------------------------------
#
# Балл статьи хранит, каким набором он посчитан: id, имя, вес и хэш текста каждого критерия на
# момент оценки (article_scores.criteria_snapshot). Без снимка вопрос «можно ли пересчитать
# балл без ИИ» не решить: подпункты ссылаются на критерий по id, а его текст и вес с тех пор
# могли поменять на экране. Снимок строит тот, кто считает балл (normalize_score_payload), — на
# NL из критериев пакета, то есть ровно то, что видела модель.


def criterion_text_hash(criterion: dict[str, Any]) -> str:
    """Всё, что критерий даёт оценке, кроме веса: имя и описание (их читает модель), ключевые
    слова RU и EN (по ним считается ключевой балл, часть EN — ещё и во входе модели).

    Вес в хэш не входит: он участвует только в итоге Σ final·вес/100, и при правке одних весов
    балл пересчитывается без ИИ. Порядок ключей важен (во вход модели идут первые), поэтому
    списки хэшируются как есть. Функция одна для ядра и NL — хэши сравнимы между сторонами."""
    blob = json.dumps(
        {
            "name": str(criterion.get("name") or ""),
            "description": str(criterion.get("description") or ""),
            "keywords_json": [str(word) for word in criterion.get("keywords_json") or []],
            "keywords_en_json": [str(word) for word in criterion.get("keywords_en_json") or []],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def criteria_snapshot(criteria: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": int(criterion["id"]),
            "name": str(criterion.get("name") or ""),
            "weight": float(criterion.get("weight") or 0),
            "text_hash": criterion_text_hash(criterion),
        }
        for criterion in criteria
    ]


def profile_of(criteria: list[dict[str, Any]]) -> str | None:
    """Профиль набора, которым считали. Набор — всегда один профиль; поля нет (пакет от ядра
    до профилей) или профили разные — None: тогда его определит ядро по id подпунктов."""
    profiles = {criterion.get("profile") for criterion in criteria}
    if len(profiles) == 1:
        (profile,) = profiles
        if profile in SCORING_PROFILES:
            return str(profile)
    return None
