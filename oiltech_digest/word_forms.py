"""Формы русских слов и имена компаний для сверки (#96, #98): одно ли это слово в другом падеже и названа ли компания.

Общее начало — не падеж: «Роснефтью» — это «Роснефть», а «Роснефтегаз», «Газпромнефть» и «Сибнефть» при «Сибуре» —
другие слова. Слово — основа не короче трёх букв плюс падежное окончание из списка. Компания названа, когда названы
все слова её имени, кроме формы собственности: одно общее слово («and», «Oil Company», «ПАО») — не имя. Правило одно
у ядра (названа ли компания в цитате участника, signal_card._named_in) и у экзамена агента (имена агента и эксперта,
есть ли компания в тексте, signal_card_exam_score): экзамен мерит то, что ядро запишет в карточку.
"""

from __future__ import annotations

from collections.abc import Sequence
import re

_CYRILLIC = re.compile(r"[а-яА-Я]")
_LATIN = re.compile(r"[a-zA-Z]")
# Падежные окончания русских существительных и прилагательных (ед. и мн. число): слово — основа плюс одно из них.
_ENDINGS = ("", "а", "я", "у", "ю", "е", "и", "ы", "о", "ь", "й", "ом", "ем", "ой", "ей", "ью", "ам", "ям", "ах", "ях",
            "ов", "ев", "ую", "юю", "ая", "яя", "ое", "ее", "ие", "ые", "ий", "ый", "их", "ых", "им", "ым", "ого", "его",
            "ому", "ему", "ами", "ями", "ими", "ыми")
# Форма собственности и вид организации — слова имени, без которых компания всё равно названа: «Equinor ASA» — в
# «Equinor испытала», «ПАО «Газпром»» — в «Газпром начал», «Expro Group» — в «Expro выиграла». Названа ли компания
# по одному такому слову — нет: оно у всех.
_OPTIONAL = frozenset({
    "пао", "оао", "зао", "ао", "нао", "ооо", "нк", "гк", "нпо", "ук", "группа", "компания",
    "pjsc", "ojsc", "jsc", "cjsc", "inc", "ltd", "llc", "plc", "asa", "as", "ag", "sa", "nv", "bv", "se", "spa",
    "gmbh", "lp", "llp", "sdn", "bhd", "pty", "co", "corp", "corporation", "company", "group", "holding", "holdings",
    "limited"})


def same_word(one: str, other: str) -> bool:
    """Одно слово, в том числе в другом падеже: у русских слов от четырёх букв — общая основа не короче трёх букв
    и падежные окончания (_ENDINGS). Латиница склонений не знает — только целиком. Регистр и «ё» сводит к одному
    вызывающий."""
    if one == other:
        return True
    if min(len(one), len(other)) < 4 or not (_CYRILLIC.match(one) and _CYRILLIC.match(other)):
        return False
    return bool(_stems(one) & _stems(other))


def _stems(word: str) -> set[str]:
    return {word[:len(word) - len(ending)] for ending in _ENDINGS
            if word.endswith(ending) and len(word) - len(ending) >= 3}


def words(text: str) -> list[str]:
    """Слова имени, цитаты или текста для сверки имён: регистр — как написано, «ё» — «е»."""
    return re.findall(r"\w+", str(text or "").replace("ё", "е").replace("Ё", "Е"))


def named(name: Sequence[str], text: Sequence[str]) -> bool:
    """Названо ли имя (его words) среди слов цитаты или текста: названо каждое слово имени, кроме формы
    собственности (_OPTIONAL), — в любом падеже и порядке. Одного общего слова мало: «Helmerich and Payne» не
    названа в «JOGMEC and PTTEP», «ПАО «Газпром»» — в «ПАО «Татнефть»». Имя из одних коротких слов (BP, H&P) —
    целиком, подряд."""
    required = [word for word in name if word.casefold() not in _OPTIONAL] or name
    if not required:
        return False
    if all(len(word) < 3 for word in required):
        return any(all(_named_word(word, text[start + offset]) for offset, word in enumerate(required))
                   for start in range(len(text) - len(required) + 1))
    return all(any(_named_word(word, token) for token in text) for word in required)


def _named_word(word: str, token: str) -> bool:
    """Слово имени в слове текста. Латинское слово имени от четырёх букв с заглавной строчными не названо:
    «total depth» — не Total; короткое — названо и строчными: так пишут «bp»."""
    if len(word) >= 4 and _LATIN.search(word) and word != word.casefold() and token == token.casefold():
        return False
    return same_word(token.casefold(), word.casefold())
