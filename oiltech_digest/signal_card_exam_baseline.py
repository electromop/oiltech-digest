"""Базовая линия экзамена без ИИ (#98, спека #93): компании и регион — словарём.

Нужна, чтобы видеть, что модель здесь действительно что-то добавляет. Компании — все компании
справочника алиасов, названные в заголовке или тексте (слова — с учётом регистра: «Nov.» — не NOV,
«total» — не Total). Регион — по словарю мест: регион, места которого текст называет чаще (при
равенстве — названный раньше); мест нет — «не определено».
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from oiltech_digest.signal_card_exam_score import same_word, text_tokens
from oiltech_digest.signal_card_pack import UNDEFINED

# Места — по-русски и по-английски; русские слова сверяются по основе (падежи), составные — целиком.
_PLACES: dict[str, tuple[str, ...]] = {
    "Россия": ("Россия", "РФ", "российский", "Russia", "Russian", "Москва", "Moscow", "Сибирь", "Siberia", "Тюмень",
               "Тюменская", "ЯНАО", "ХМАО", "Югра", "Ямал", "Yamal", "Татарстан", "Tatarstan", "Башкортостан",
               "Башкирия", "Пермь", "Пермский край", "Самара", "Самарская", "Оренбург", "Сахалин", "Sakhalin",
               "Санкт-Петербург", "Новосибирск", "Томск", "Красноярск", "Иркутск", "Якутия", "Ханты-Мансийск",
               "Сургут", "Нижневартовск", "Ноябрьск", "Новый Уренгой", "Мурманск", "Уфа", "Казань"),
    "СНГ и Центральная Азия": ("СНГ", "Центральная Азия", "Central Asia", "Казахстан", "Kazakhstan", "Узбекистан",
                               "Uzbekistan", "Туркменистан", "Turkmenistan", "Азербайджан", "Azerbaijan", "Беларусь",
                               "Belarus", "Кыргызстан", "Kyrgyzstan", "Таджикистан", "Tajikistan", "Армения",
                               "Armenia", "Ташкент", "Tashkent", "Баку", "Baku", "Атырау", "Atyrau", "Тенгиз",
                               "Tengiz", "Кашаган", "Kashagan"),
    "Европа": ("Европа", "Europe", "European", "Евросоюз", "Норвегия", "Norway", "Norwegian", "Великобритания",
               "Britain", "British", "UK", "United Kingdom", "England", "Англия", "Scotland", "Шотландия", "Aberdeen",
               "Абердин", "Северное море", "North Sea", "Нидерланды", "Netherlands", "Dutch", "Германия", "Germany",
               "German", "Франция", "France", "French", "Италия", "Italy", "Italian", "Испания", "Spain", "Дания",
               "Denmark", "Danish", "Польша", "Poland", "Румыния", "Romania", "Австрия", "Austria", "Швеция",
               "Sweden", "Финляндия", "Finland", "Ирландия", "Ireland", "Сербия", "Serbia", "Венгрия", "Hungary",
               "Греция", "Greece", "Кипр", "Cyprus", "Португалия", "Portugal", "Бельгия", "Belgium", "Stavanger",
               "Ставангер"),
    "Ближний Восток": ("Ближний Восток", "Middle East", "Саудовская Аравия", "Saudi", "ОАЭ", "UAE", "Emirates",
                       "Абу-Даби", "Abu Dhabi", "Дубай", "Dubai", "Катар", "Qatar", "Оман", "Oman", "Кувейт",
                       "Kuwait", "Ирак", "Iraq", "Иран", "Iran", "Бахрейн", "Bahrain", "Израиль", "Israel",
                       "Курдистан", "Kurdistan"),
    "Северная Америка": ("Северная Америка", "North America", "США", "USA", "U.S.", "United States", "American",
                         "Техас", "Texas", "Хьюстон", "Houston", "Permian", "Пермский бассейн", "Gulf of Mexico",
                         "Мексиканский залив", "Oklahoma", "Оклахома", "Tulsa", "North Dakota", "Bakken", "Alaska",
                         "Аляска", "Louisiana", "Colorado", "New Mexico", "Pennsylvania", "Eagle Ford",
                         "Haynesville", "Канада", "Canada", "Canadian", "Alberta", "Альберта", "Calgary", "Калгари"),
    "Латинская Америка": ("Латинская Америка", "Latin America", "Бразилия", "Brazil", "Brazilian", "Аргентина",
                          "Argentina", "Мексика", "Mexico", "Mexican", "Колумбия", "Colombia", "Венесуэла",
                          "Venezuela", "Гайана", "Guyana", "Суринам", "Suriname", "Перу", "Peru", "Чили", "Chile",
                          "Эквадор", "Ecuador", "Тринидад", "Trinidad", "Боливия", "Bolivia", "Vaca Muerta"),
    "Китай": ("Китай", "China", "Chinese", "КНР", "PRC", "Пекин", "Beijing", "Шанхай", "Shanghai", "Синьцзян",
              "Xinjiang", "Сычуань", "Sichuan", "Тарим", "Tarim", "Bohai"),
    "Индия": ("Индия", "India", "Indian", "Мумбаи", "Mumbai", "Gujarat", "Rajasthan", "Assam"),
    "Юго-Восточная Азия": ("Юго-Восточная Азия", "Southeast Asia", "South-East Asia", "Малайзия", "Malaysia",
                           "Malaysian", "Индонезия", "Indonesia", "Indonesian", "Таиланд", "Thailand", "Thai",
                           "Вьетнам", "Vietnam", "Сингапур", "Singapore", "Филиппины", "Philippines", "Бруней",
                           "Brunei", "Мьянма", "Myanmar", "Sarawak", "Sabah"),
    "другое": ("Африка", "Africa", "African", "Нигерия", "Nigeria", "Ангола", "Angola", "Намибия", "Namibia",
               "Мозамбик", "Mozambique", "Алжир", "Algeria", "Ливия", "Libya", "Египет", "Egypt", "Гана", "Ghana",
               "Зимбабве", "Zimbabwe", "ЮАР", "South Africa", "Австралия", "Australia", "Australian",
               "Новая Зеландия", "New Zealand", "Япония", "Japan", "Japanese", "Корея", "Korea", "Korean",
               "Пакистан", "Pakistan", "Монголия", "Mongolia", "Турция", "Turkey"),
    "глобально": ("worldwide", "globally", "по всему миру"),
}
# Длинное место — раньше короткого: «Gulf of Mexico» — это не «Mexico», «New Mexico» — тоже.
_FORMS = sorted(((region, tuple(text_tokens(place))) for region, places in _PLACES.items() for place in places),
                key=lambda pair: -len(pair[1]))


def finals(items: list[dict[str, Any]], dictionaries: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Значения «карточки» без ИИ — в той же форме, что у агента (один прогон)."""
    companies = [(entry["name"], [tuple(text_tokens(name, keep_case=True)) for name in (entry["name"], *entry["aliases"])])
                 for entry in dictionaries["companies"]]
    out: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        text = f"{item['title']}\n{item['text']}"
        cased = text_tokens(text, keep_case=True)
        named = [name for name, forms in companies
                 if any(form and _stands_at(cased, index, form) for form in forms for index in range(len(cased)))]
        out[item["id"]] = [{"participants_json": [{"name": name} for name in named], "region": _region(text_tokens(text))}]
    return out


def _stands_at(tokens: list[str], index: int, form: tuple[str, ...]) -> bool:
    """Стоит ли имя (слова подряд) в тексте с этого слова."""
    return len(form) <= len(tokens) - index and all(
        same_word(word, tokens[index + offset]) for offset, word in enumerate(form))


def _region(tokens: list[str]) -> str:
    counts: Counter[str] = Counter()
    first: dict[str, int] = {}
    index = 0
    while index < len(tokens):
        for region, form in _FORMS:
            if _stands_at(tokens, index, form):
                counts[region] += 1
                first.setdefault(region, index)
                index += len(form)
                break
        else:
            index += 1
    if not counts:
        return UNDEFINED
    return max(counts, key=lambda region: (counts[region], -first[region]))
