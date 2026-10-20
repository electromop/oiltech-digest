"""Формы русских слов и имена компаний для сверки (#96, #98): одно ли это слово в другом падеже, названа ли компания
и одна ли это компания.

Общее начало — не падеж: «Роснефтью» — это «Роснефть», а «Роснефтегаз», «Газпромнефть» и «Сибнефть» при «Сибуре» —
другие слова. Слово — основа не короче трёх букв плюс падежное окончание из списка. Компания названа, когда названы
все слова её имени, кроме формы собственности: одно общее слово («and», «Oil Company», «ПАО») — не имя. Короткое имя
внутри длинного имени другой компании компанию не называет: в «Газпром нефть начала бурение» «Газпром» не назван, и
цитата «Газпром» из этого текста его не называет — длинное имя видно и за краем цитаты.

Одна ли это компания (разбор 20.10, Companies):
1. Написания имени (spellings): имя как есть, а у «Имя (АББР)» и «АББР (Имя)», где АББР — аббревиатура имени
   (is_abbreviation), — имя и аббревиатура. Любая другая скобка — пояснение и в имя не входит.
2. Одно имя (same_name): те же слова в том же порядке без формы собственности (и с точками: «S.A.»), без различий
   регистра, «ё», кавычек и дефисов; русское слово — в любом падеже.
3. Справочник: написание совпало с именем записи справочника — к написаниям добавляются все её имена.
4. Две компании — одна, если совпало хоть одно написание одной с написанием другой.
5. Компания — целиком: «Газпром» — не «Газпром нефть», дочка — не мать.

Правило одно у ядра (названа ли компания в цитате участника, повтор участника и издатель — signal_card._participants),
у фильтра «всё про компанию» (signal_card_filters.company_names) и у экзамена агента (имена агента и эксперта, есть
ли компания в тексте — signal_card_exam_score): экзамен мерит то, что ядро запишет в карточку, а фильтр находит её.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
import functools
import re
from typing import Any, NamedTuple

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


def same_name(one: Sequence[str], other: Sequence[str]) -> bool:
    """Одно ли это имя компании (слова — words): те же слова в том же порядке, в любом падеже и регистре, без
    формы собственности (_OPTIONAL, и с точками): «Atlas Energy Solutions Inc.» — это «Atlas Energy Solutions»,
    «Petrobras S.A.» — «Petrobras», «ПАО «Газпром»» — «Газпром», а «ПАО «Татнефть»» — другая компания. Компания —
    целиком: «Газпром» — не «Газпром нефть»."""
    return _same_bare(_bare(one), _bare(other))


def _bare(name: Sequence[str]) -> tuple[str, ...]:
    """Слова имени для сверки (same_name): без формы собственности и регистра."""
    return tuple(word.casefold() for word in _required(name))


def _same_bare(mine: Sequence[str], theirs: Sequence[str]) -> bool:
    return len(mine) == len(theirs) and all(same_word(a, b) for a, b in zip(mine, theirs))


def is_abbreviation(short: str, long: str) -> bool:
    """Аббревиатура ли short имени long: одно слово без строчных букв, от двух букв, не форма собственности, и его
    знаки по порядку — первые буквы слов имени; слова имени можно пропускать: ONGC — Oil and Natural Gas
    Corporation, DOE — U.S. Department of Energy. «ПАО», «США», «поставщик» — не аббревиатуры: это пояснения."""
    found, full = words(short), words(long)
    if len(found) != 1 or len(full) < 2:
        return False
    abbreviation = found[0]
    if sum(char.isalpha() for char in abbreviation) < 2 or abbreviation != abbreviation.upper() \
            or abbreviation.casefold() in _OPTIONAL:
        return False
    initials = iter("".join(word[0] for word in full).casefold())
    return all(char in initials for char in abbreviation.casefold())


# Имя со скобкой: до неё, в ней, после неё. Вложенные и вторые скобки — не разбираются: имя остаётся как есть.
_PARENTHESIS = re.compile(r"(?P<main>[^()]*?)\s*\((?P<inner>[^()]*)\)\s*(?P<tail>[^()]*)")


def spellings(name: str) -> list[str]:
    """Написания имени компании: имя как есть, а у «Имя (АББР)» и «АББР (Имя)» (is_abbreviation) — имя и
    аббревиатура: «Petroleum Development Oman (PDO)» — это и «Petroleum Development Oman», и «PDO». Любая другая
    скобка — пояснение, в имя не входит: «Газпромнефть-Хантос (Газпром нефть)» — это «Газпромнефть-Хантос»,
    «FleetRabbit (поставщик)» — «FleetRabbit», «Газпром (ПАО)» — «Газпром». Пробелы — по одному; полное имя —
    первым; имя без слов — без написаний."""
    text = " ".join(str(name or "").split())
    found = _PARENTHESIS.fullmatch(text)
    if not found:
        return [text] if words(text) else []
    main, inner, tail = found["main"].strip(), found["inner"].strip(), found["tail"].strip()
    outside = " ".join(part for part in (main, tail) if part)
    if not words(outside):
        return [inner] if words(inner) else []
    if is_abbreviation(inner, outside):
        return [outside, inner]
    if not tail and is_abbreviation(main, inner):
        return [inner, main]
    return [outside]


# Разобранных имён в одном Companies — не больше (замер 20.10: 6,3 тыс. имён — 4 МБ): имена приходят и из запроса
# человека. Имён участников в базе станет больше — правило стоит перенести в ключ, записанный в базе (разбор 20.10,
# раздел 4).
_PARSED_LIMIT = 50_000


class Companies:
    """Одна ли это компания — по правилу модуля со справочником записей {name, aliases} (справочник компаний ядра,
    у экзамена — с его алиасами). Записи с общим написанием — одна компания, её имя — имя первой записи. Разбор
    имени (написания, записи справочника) кэшируется по имени: фильтр сверяет компанию с тысячами имён
    участников на каждом запросе."""

    def __init__(self, entries: Iterable[Mapping[str, Any]]):
        groups: list[tuple[int, str, list[tuple[str, ...]]]] = []  # (номер первой записи, её имя, написания)
        for number, entry in enumerate(entries):
            forms = _unique(form for value in (entry["name"], *entry["aliases"]) for form in _spelled(value))
            joined = [group for group in groups if any(same_name(a, b) for a in forms for b in group[2])]
            groups = [group for group in groups if group not in joined]
            first, name = min(((group[0], group[1]) for group in joined), default=(number, str(entry["name"])))
            groups.append((first, name, _unique([*forms, *(form for group in joined for form in group[2])])))
        self._groups = [(name, tuple(forms)) for _, name, forms in sorted(groups)]
        # Слова для сверки написаний справочника — по их числу: имя сверяется только с написаниями той же длины.
        self._by_length: dict[int, list[tuple[tuple[str, ...], int]]] = defaultdict(list)
        for index, (_, forms) in enumerate(self._groups):
            for form in forms:
                bare = _bare(form)
                self._by_length[len(bare)].append((bare, index))
        self._parsed: dict[str, _Parsed] = {}

    def forms(self, name: str) -> tuple[tuple[str, ...], ...]:
        """Написания компании (слова — words, регистр как написан: по ним ищут в тексте) — свои и всех имён её
        записи справочника."""
        return self._parse(name).forms

    def same(self, one: str, other: str) -> bool:
        """Одна ли это компания: хоть одно написание одной — то же имя (same_name), что написание другой."""
        theirs = self._parse(other).bares
        return any(_same_bare(mine, bare) for mine in self._parse(one).bares for bare in theirs)

    def canonical(self, name: str) -> str | None:
        """Имя записи справочника, к которой относится компания; нет её в справочнике — None."""
        return self._parse(name).known

    def key(self, name: str) -> tuple[str, ...]:
        """Одно написание на компанию — для подсчёта по компаниям: слова для сверки имени записи справочника, вне
        справочника — полного имени. «Atlas Energy Solutions Inc.» и «Atlas Energy Solutions», «Роснефтью» и
        «НК «Роснефть»» — один ключ. Отношение same не транзитивно («PDO» — не «Petroleum Development Oman», хотя
        оба — «Petroleum Development Oman (PDO)»), и ключ вне справочника ловит не всё: падеж и аббревиатура — разные
        ключи."""
        known = self.canonical(name)
        forms = _spelled(known) if known else self.forms(name)
        return _bare(forms[0]) if forms else ()

    def _parse(self, name: str) -> _Parsed:
        """Разбор имени, с кэшем по имени: свои написания, написания записей справочника, имя записи."""
        parsed = self._parsed.get(name)
        if parsed is None:
            own = _spelled(name)
            matched = _unique(index for form in own for index in self._matching(_bare(form)))
            forms = _unique([*own, *(form for index in matched for form in self._groups[index][1])])
            parsed = _Parsed(tuple(forms), tuple(_unique(map(_bare, forms))),
                             self._groups[matched[0]][0] if matched else None)
            if len(self._parsed) >= _PARSED_LIMIT:
                self._parsed.clear()
            self._parsed[name] = parsed
        return parsed

    def _matching(self, bare: tuple[str, ...]) -> list[int]:
        """Записи справочника, у которых есть написание с теми же словами (same_name)."""
        return [index for other, index in self._by_length.get(len(bare), ()) if _same_bare(bare, other)]


class _Parsed(NamedTuple):
    forms: tuple[tuple[str, ...], ...]  # написания: слова как написаны
    bares: tuple[tuple[str, ...], ...]  # их слова для сверки (_bare)
    known: str | None  # имя записи справочника


def companies(entries: Iterable[Mapping[str, Any]]) -> Companies:
    """Правило со справочником — один объект на одно его содержимое: разбор имён копится между вызовами (ядро пишет
    карточки пачкой, фильтр сверяет имена участников на каждом запросе)."""
    return _companies(tuple((str(entry["name"]), tuple(map(str, entry["aliases"]))) for entry in entries))


@functools.lru_cache(maxsize=8)
def _companies(entries: tuple[tuple[str, tuple[str, ...]], ...]) -> Companies:
    return Companies({"name": name, "aliases": aliases} for name, aliases in entries)


def _spelled(name: str) -> list[tuple[str, ...]]:
    return [tuple(words(spelling)) for spelling in spellings(name)]


def _unique(items: Iterable[Any]) -> list[Any]:
    return list(dict.fromkeys(items))


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
    """Слова имени без формы собственности (_OPTIONAL), в том числе с точками: «S.A.» words режет на «S» и «A» —
    однобуквенные слова подряд, вместе дающие форму собственности, снимаются тоже. Имя из одной формы
    собственности — целиком."""
    kept: list[str] = []
    index = 0
    while index < len(name):
        end = index
        while end < len(name) and len(name[end]) == 1:
            end += 1
        if end - index >= 2 and "".join(name[index:end]).casefold() in _OPTIONAL:
            index = end
            continue
        if name[index].casefold() not in _OPTIONAL:
            kept.append(name[index])
        index += 1
    return kept or list(name)


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
