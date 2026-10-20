"""Фильтры экрана по атрибутам карточки сигнала (#97): значения справочников для экрана, проверка значения
фильтра и имена участников для «всё про компанию». Агент «Карточка сигнала» — в signal_card.py."""

from __future__ import annotations

from collections.abc import Iterable

from oiltech_digest import word_forms
from oiltech_digest.signal_card import CHOICE_COLUMNS
from oiltech_digest.signal_card_pack import UNDEFINED, load_dictionaries


def company_names(name: str, known: Iterable[str]) -> list[str]:
    """Имена участников для фильтра «всё про компанию» (#97): из известных имён (known — все разные имена
    участников карточек в базе) — те, что по правилу «одна компания» (word_forms.Companies: форма собственности,
    «Имя (АББР)», пояснение в скобках, падеж, справочник алиасов) та же компания, плюс само имя без крайних
    пробелов. Выборка сверяет их с именем участника точно: правило работает здесь, при запросе, — в базе ничего не
    хранится и не пересчитывается. Компания — целиком: «Газпром» — не «Газпром нефть». Пустое имя — пустой список."""
    wanted = str(name or "").strip()
    if not wanted:
        return []
    rule = word_forms.companies(load_dictionaries().get("companies") or [])
    return sorted({wanted, *(other for other in known if isinstance(other, str) and rule.same(wanted, other))})


def checked_choice(column: str, value: str | None) -> str | None:
    """Значение фильтра справочного поля карточки (#97): из справочника или «не определено»; пустое —
    фильтра нет. Чужое — ValueError: опечатка в адресе выглядела бы как «сигналов нет»."""
    if not value:
        return None
    block = load_dictionaries()[CHOICE_COLUMNS[column]]
    if value != UNDEFINED and value not in block["values"]:
        raise ValueError(f"Фильтр «{block['title']}»: значения «{value}» нет в справочнике")
    return value


def screen_dictionaries() -> dict[str, list[str]]:
    """Значения фильтров экрана (#97): справочные поля карточки по колонкам signals и подсказки к полю
    «Компания» — канонические имена справочника алиасов. «Не определено» в справочник не входит: его
    экран добавляет сам, как и подписи полей."""
    dictionaries = load_dictionaries()
    return {
        **{column: list(dictionaries[key]["values"]) for column, key in CHOICE_COLUMNS.items()},
        "companies": [entry["name"] for entry in dictionaries.get("companies") or []],
    }
