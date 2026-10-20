"""Формы русских слов и имена компаний для сверки (#96, #98): одно ли это слово в другом падеже и названа ли компания.

Общее начало — не падеж: «Роснефтью» — это «Роснефть», а «Роснефтегаз», «Газпромнефть» и «Сибнефть» при «Сибуре» —
другие слова. Слово — основа не короче трёх букв плюс падежное окончание из списка. Компания названа, когда названы
все слова её имени, кроме формы собственности: одно общее слово («and», «Oil Company», «ПАО») — не имя. Короткое имя
внутри длинного имени другой компании компанию не называет: в «Газпром нефть начала бурение» «Газпром» не назван, и
цитата «Газпром» из этого текста его не называет — длинное имя видно и за краем цитаты. Правило одно у ядра (названа
ли компания в цитате участника — named_in_quote, его зовёт signal_card._participants) и у экзамена агента (имена
агента и эксперта, есть ли компания в тексте, signal_card_exam_score): экзамен мерит то, что ядро запишет в карточку.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
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
    required = _required(name)
    if not required:
        return False
    if all(len(word) < 3 for word in required):
        return any(all(_named_word(word, text[start + offset]) for offset, word in enumerate(required))
                   for start in range(len(text) - len(required) + 1))
    return all(any(_named_word(word, token) for token in text) for word in required)


def named_among(names: Sequence[Sequence[str]], text: Sequence[str], others: Iterable[Sequence[str]], *,
                before: Sequence[str] = (), after: Sequence[str] = ()) -> bool:
    """Названа ли компания (names — words каждого её имени) по правилу named среди слов текста, где стоят и другие
    компании (others — words их имён). Длинное имя закрывает короткое: вхождение имени другой компании, внутри
    которого стоит имя этой, не в счёт — «Газпром» не назван в «Газпром нефть начала бурение» и в «Gazprom Neft
    reported», а в «Газпром и Газпром нефть подписали» назван. Для цитаты before и after — слова текста до и после
    неё: закрывает и вхождение, которое выходит за край цитаты, — «подписали Газпром» из «подписали Газпром нефть»
    «Газпром» не называет."""
    closing = [other for other in others if any(_inside(name, other) for name in names)]
    rest = _closed([*before, *text, *after], closing)[len(before):len(before) + len(text)]
    return any(named(name, rest) for name in names)


def _inside(name: Sequence[str], other: Sequence[str]) -> bool:
    """Имя внутри другого, более длинного: все слова имени названы в другом, а не наоборот."""
    return named(name, other) and not named(other, name)


def _closed(text: Sequence[str], names: Sequence[Sequence[str]]) -> list[str]:
    """Слова текста, где имена (слова подряд, в любом падеже и регистре, с формой собственности и без неё) заменены
    пустыми словами: пустое слово ничего не называет и не даёт соседям имени встать подряд."""
    rest = list(text)
    for name in names:
        for phrase in {tuple(name), tuple(_required(name))}:
            for start in range(len(text) - len(phrase) + 1):
                if all(same_word(text[start + offset].casefold(), word.casefold())
                       for offset, word in enumerate(phrase)):
                    rest[start:start + len(phrase)] = [""] * len(phrase)
    return rest


def _required(name: Sequence[str]) -> list[str]:
    """Слова имени без формы собственности (_OPTIONAL); имя из одной формы собственности — целиком."""
    return [word for word in name if word.casefold() not in _OPTIONAL] or list(name)


def _named_word(word: str, token: str) -> bool:
    """Слово имени в слове текста. Латинское слово имени от четырёх букв с заглавной строчными не названо:
    «total depth» — не Total; короткое — названо и строчными: так пишут «bp»."""
    if len(word) >= 4 and _LATIN.search(word) and word != word.casefold() and token == token.casefold():
        return False
    return same_word(token.casefold(), word.casefold())


# Кавычки и тире модель переписывает на свой лад — для сверки цитаты они одно и то же.
_QUOTE_MARKS = str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "’": "'", "‘": "'",
                              "—": "-", "–": "-"})


def quote_form(text: str) -> str:
    """Текст для сверки цитаты: без различий регистра, «ё», пробелов, кавычек и тире."""
    return " ".join(str(text or "").translate(_QUOTE_MARKS).replace("ё", "е").replace("Ё", "Е").casefold().split())


def named_in_quote(quote: str, text: str, names: Sequence[str], others: Sequence[str]) -> bool:
    """Названа ли компания в цитате из текста публикации (text — в quote_form) — под любым из имён
    (named_among): названы все слова имени, кроме формы собственности, в любом падеже. Одного общего слова
    мало: «Helmerich and Payne» не названа в «JOGMEC and PTTEP», а «Equinor ASA» названа в «Equinor испытала»;
    «Роснефтью» — это «Роснефть», а «Роснефтегаз» — другая компания. Цитата стоит в тексте целыми словами (_spots):
    «Газпром» из «Газпромнефть-Хантос» и «100,000 bp» из «100,000 bpd» — обрывки, а не цитаты. Имя внутри длинного
    имени другой компании справочника (others) не в счёт, в том числе когда длинное имя выходит за край цитаты:
    «Газпром» не назван ни в «Газпром нефть начала бурение», ни в цитате «Газпром» из неё, а в «Газпром и Газпром
    нефть подписали» назван. Цитата стоит в тексте не раз — компания названа, если названа хоть в одном месте."""
    form = quote_form(quote)
    spoken = [words(name) for name in names]
    closing = [words(other) for other in others]
    quoted = words(quote)
    return any(named_among(spoken, quoted, closing, before=words(text[:start]), after=words(text[start + len(form):]))
               for start in _spots(form, text))


def _spots(form: str, text: str) -> list[int]:
    """Где цитата (form — в quote_form) стоит в тексте целыми словами: буква или цифра на её краю не продолжает
    слово текста."""
    left = r"(?<!\w)" if re.match(r"\w", form) else ""
    right = r"(?!\w)" if re.match(r"\w", form[-1:]) else ""
    return [match.start() for match in re.finditer(f"(?={left}{re.escape(form)}{right})", text)]
