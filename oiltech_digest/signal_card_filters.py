"""Фильтры экрана по атрибутам карточки сигнала (#97): значения справочников для экрана, проверка значения
фильтра и все написания компании для «всё про компанию». Агент «Карточка сигнала» — в signal_card.py."""

from __future__ import annotations

from oiltech_digest.signal_card import CHOICE_COLUMNS, _plain
from oiltech_digest.signal_card_pack import UNDEFINED, load_dictionaries


def company_names(name: str) -> list[str]:
    """Все написания компании для фильтра «всё про компанию» (#97) — в форме сверки с базой (_name_key):
    по справочнику алиасов — каноническое имя и алиасы, компании нет в справочнике — её имя. Компания —
    целиком, а не подстрокой: «Газпром» — не «Газпром нефть». Пустое имя — пустой список."""
    wanted = _plain(name)
    if not wanted:
        return []
    for entry in load_dictionaries().get("companies") or []:
        names = (entry["name"], *entry["aliases"])
        if wanted in {_plain(item) for item in names}:
            return sorted({_name_key(item) for item in names})
    return [_name_key(name)]


def _name_key(value: str) -> str:
    """Имя участника в форме сверки с базой (repository._PARTICIPANT_NAMED_SQL): без различий регистра,
    «ё» и пробелов, в том числе неразрывных. lower, а не casefold: как lower() в Postgres."""
    return " ".join(str(value or "").replace("ё", "е").replace("Ё", "Е").lower().split())


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
