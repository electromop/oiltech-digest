"""Фильтры экрана по атрибутам карточки сигнала (#97): значения справочников для экрана, проверка значения
фильтра и имена участников для «всё про компанию» — с дочерними обществами по списку «дочка → мать» (решение
владельца 20.10, company_parents.py); страна (#137) — по справочнику стран у кода. Агент «Карточка сигнала» — в
signal_card.py."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from oiltech_digest import company_parents, word_forms
from oiltech_digest.signal_card import CHOICE_COLUMNS
from oiltech_digest.signal_card_countries import Countries
from oiltech_digest.signal_card_pack import UNDEFINED, load_dictionaries


def company_rule() -> word_forms.Companies:
    """Правило «одна компания» (word_forms.Companies: форма собственности, «Имя (АББР)», пояснение в скобках,
    падеж) со справочником алиасов ядра: одно у фильтра «всё про компанию» и у проверок списка дочерних обществ
    (обработчики api.py, company_parents.checked_row). Справочник читается при каждом вызове, разбор имён копится
    в правиле (word_forms.companies): правило берут раз на запрос, а не на каждое сравнение (замер 20.10, вставка
    500 строк: 7,4 с против 0,14 с)."""
    return word_forms.companies(load_dictionaries().get("companies") or [])


def company_group(name: str, pairs: Sequence[company_parents.Pair]) -> list[str]:
    """Кого показывает фильтр «всё про компанию» (решение владельца 20.10): сама компания (как спросили, без
    лишних пробелов) и все её дочерние общества по цепочке из списка «дочка → мать» (pairs — пары как написаны,
    company_parents.descendants): Газпром → Газпром нефть → Газпромнефть-Заполярье. Дочки — как написаны в списке,
    ближние первыми; мать дочка не тянет. Список сверяется по правилу «одна компания» (company_rule): мать
    «ПАО «Газпром»» в списке — это и «Газпром» в запросе. Пустое имя — пустой список."""
    wanted = company_parents.clean(name)
    if not wanted:
        return []
    return [wanted, *company_parents.descendants(wanted, pairs, company_rule().same)]


def group_names(group: Sequence[str], known: Iterable[str]) -> list[str]:
    """Имена участников для фильтра «всё про компанию» (#97): из известных имён (known — все разные имена
    участников карточек в базе) — те, что по правилу «одна компания» (company_rule) та же компания, что хоть одна
    компания группы (company_group: сама компания и её дочки), плюс имена группы. Выборка сверяет их с именем
    участника точно: правило работает здесь, при запросе, — в базе ничего не хранится и не пересчитывается.
    Компания — целиком: «Газпром» — не «Газпром нефть», пока список не назовёт её дочкой «Газпрома». Пустая
    группа — пустой список."""
    rule = company_rule()
    return sorted({*group, *(other for other in known
                             if isinstance(other, str) and any(rule.same(member, other) for member in group))})


def checked_choice(column: str, value: str | None) -> str | None:
    """Значение фильтра справочного поля карточки (#97): из справочника или «не определено»; пустое —
    фильтра нет. Чужое — ValueError: опечатка в адресе выглядела бы как «сигналов нет»."""
    if not value:
        return None
    block = load_dictionaries()[CHOICE_COLUMNS[column]]
    if value != UNDEFINED and value not in block["values"]:
        raise ValueError(f"Фильтр «{block['title']}»: значения «{value}» нет в справочнике")
    return value


def checked_country(value: str | None) -> str | None:
    """Значение фильтра «Страна» (#137): имя справочника стран как есть; пустое — фильтра нет. Чужое и написание
    вместо имени («USA») — ValueError, как у справочных полей: значения экран берёт из справочника."""
    if not value:
        return None
    if value not in Countries(load_dictionaries()).names:
        raise ValueError(f"Фильтр «Страна»: страны «{value}» нет в справочнике")
    return value


def screen_dictionaries() -> dict[str, list[str]]:
    """Значения фильтров экрана (#97): справочные поля карточки по колонкам signals, подсказки к полю
    «Компания» — канонические имена справочника алиасов, и страны (#137) — имена справочника стран. «Не
    определено» в справочник не входит: его экран добавляет сам, как и подписи полей."""
    dictionaries = load_dictionaries()
    return {
        **{column: list(dictionaries[key]["values"]) for column, key in CHOICE_COLUMNS.items()},
        "companies": [entry["name"] for entry in dictionaries.get("companies") or []],
        "country": Countries(dictionaries).names,
    }
