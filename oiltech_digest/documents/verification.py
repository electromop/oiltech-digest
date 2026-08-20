"""Сверка чисел, названных моделью, с текстом якоря, откуда она их взяла.

Модель отдаёт факт как тройку «значение · единица · номер якоря». Мы не верим ей
на слово: число обязано найтись в тексте именно этого якоря, причём в том же
масштабе и с той же единицей. Не нашлось — факт помечается неподтверждённым и
в сравнения не идёт.

Почему масштаб сверяется наравне с цифрами: без этого проверка вырождается в
поиск подстроки. Значение «1,5» модель вытащила из «1,5 млн тонн», а выдала как
«1,5 млрд тонн» — подстрока на месте, факт при этом ложный в тысячу раз.
Поэтому сравниваются не строки, а величины: число, умноженное на масштаб,
плюс канонизированная единица.

Модуль — чистые функции: ни базы, ни сети, ни обращений к модели.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

__all__ = ["verify_value", "normalize_number"]


# Пробелы-разделители разрядов: обычный, неразрывный, узкий неразрывный,
# тонкий и цифровой. В PDF и DOCX встречаются все пять.
_SPACE_CHARS = "     "
_APOSTROPHES = "'’ʼ`"

# Число целиком, вместе с разделителями разрядов. Порядок веток важен: сначала
# самые длинные формы, иначе «1.234.567» распадётся на три числа.
# Разбор идёт по непересекающимся совпадениям слева направо — поэтому «15»
# внутри «2015» отдельным числом не всплывает: «2015» съедается целиком.
_NUMBER_RE = re.compile(
    r"\d{1,3}(?:[" + _SPACE_CHARS + r"]\d{3})+(?:[.,]\d+)?"  # 1 234 567,89
    r"|\d{1,3}(?:\.\d{3})+(?:,\d+)?"                          # 1.234.567,89
    r"|\d{1,3}(?:,\d{3})+(?:\.\d+)?"                          # 1,234,567.89
    r"|\d+(?:[.,]\d+)?"                                       # 1234 · 1,5 · 1.5
)

# Токены хвоста после числа: символ валюты/процента, слово (возможно с индексом
# вроде «м3»/«м³») либо слеш составной единицы («барр/сут»).
_TOKEN_RE = re.compile(r"[%$₽€]|[A-Za-zА-Яа-яЁё]+[0-9²³]?|/")

# Масштаб — степень десятки. Значение и текст приводятся к одной величине,
# поэтому «1,5 млн» и «1 500 000» совпадут, а «1,5 млн» и «1,5 млрд» — нет.
_SCALES = {
    "тыс": 3, "тыся": 3, "тысяч": 3, "тысяча": 3, "тысячи": 3, "тысячах": 3,
    "млн": 6, "миллион": 6, "миллиона": 6, "миллионов": 6, "миллионах": 6,
    "млрд": 9, "миллиард": 9, "миллиарда": 9, "миллиардов": 9, "миллиардах": 9,
    "трлн": 12, "триллион": 12, "триллиона": 12, "триллионов": 12,
}

# Единица приводится к каноническому виду, чтобы «тонн», «тонны» и «т» считались
# одним и тем же, а «тонн» и «барр» — разным. Незнакомое слово канона не имеет
# и сравнивается как есть: это строже, чем игнорировать его.
_UNIT_ALIASES = {
    "%": "%", "процент": "%", "процента": "%", "процентов": "%", "проц": "%", "пп": "%",
    "т": "т", "тн": "т", "тонн": "т", "тонна": "т", "тонны": "т", "тонну": "т", "тоннах": "т",
    "барр": "барр", "баррель": "барр", "барреля": "барр", "баррелей": "барр", "bbl": "барр",
    "м3": "м3", "м³": "м3", "нм3": "м3", "куб": "м3", "кубометр": "м3", "кубометров": "м3",
    "руб": "руб", "рубль": "руб", "рубля": "руб", "рублей": "руб", "₽": "руб", "rub": "руб",
    "$": "$", "долл": "$", "доллар": "$", "доллара": "$", "долларов": "$", "usd": "$",
    "€": "€", "евро": "€", "eur": "€",
    "км": "км", "километр": "км", "километра": "км", "километров": "км",
    "сут": "сут", "сутки": "сут", "суток": "сут", "сутках": "сут",
    "год": "год", "года": "год", "лет": "год", "г": "год",
    "шт": "шт", "штук": "шт", "штуки": "шт",
}

# Сколько символов после числа считаем его хвостом. Дальше начинается фраза,
# а не единица.
_TAIL_WINDOW = 30


def normalize_number(raw: str) -> str | None:
    """Приводит записанное число к канонической строке или отдаёт None.

    «1 234 567», «1 234 567» (неразрывный пробел), «1.234.567» и «1,234,567» —
    одно и то же число. «1,5» и «1.5» — тоже одно.

    Правило развилки для одиночного разделителя: ровно три цифры после него —
    разряды («1,234» → 1234), иначе десятичная часть («1,5» → 1.5). Для сверки
    важнее не «угадать», а обработать обе стороны одинаково: и значение модели,
    и текст документа проходят через эту же функцию.

    Знак не разбирается намеренно: в документах сплошь диапазоны вида
    «2024-2025», и минус там — тире, а не отрицательное число.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    text = text.lstrip("+-−–—")
    for ch in _SPACE_CHARS + _APOSTROPHES:
        text = text.replace(ch, "")
    if not text or not re.fullmatch(r"[\d.,]+", text):
        return None
    if not any(ch.isdigit() for ch in text):
        return None

    dots, commas = text.count("."), text.count(",")
    if dots and commas:
        # Оба разделителя сразу: десятичный — тот, что правее.
        decimal_sep = "." if text.rfind(".") > text.rfind(",") else ","
        grouping_sep = "," if decimal_sep == "." else "."
        text = text.replace(grouping_sep, "").replace(decimal_sep, ".")
    elif dots or commas:
        sep = "." if dots else ","
        count = dots or commas
        tail = text.rsplit(sep, 1)[1]
        if count > 1 or len(tail) == 3:
            text = text.replace(sep, "")
        else:
            text = text.replace(sep, ".")

    if text.count(".") > 1:
        return None
    int_part, _, frac_part = text.partition(".")
    int_part = int_part.lstrip("0")
    frac_part = frac_part.rstrip("0")
    if not int_part and not frac_part:
        return "0"
    int_part = int_part or "0"
    return f"{int_part}.{frac_part}" if frac_part else int_part


def verify_value(value: str, unit: str | None, anchor_text: str) -> bool:
    """True, если число с таким же масштабом и единицей есть в тексте якоря.

    Пустое значение или пустой текст якоря — это False, а не исключение:
    сверка не подтвердила факт, вызывающему нечего ловить.

    Единица не указана (None или пусто) — сверяется только само число: модель
    ничего не заявила про масштаб, значит и спрашивать с неё нечего.
    """
    if not value or not str(value).strip():
        return False
    if not anchor_text or not str(anchor_text).strip():
        return False

    raw_value = str(value).strip()
    head = _NUMBER_RE.match(raw_value)
    if head is None:
        return False
    normalized = normalize_number(head.group())
    if normalized is None:
        return False

    # Модель нередко кладёт масштаб в само значение: value="1,5 млн", unit="тонн".
    # Хвост значения используется только там, где поле unit молчит, — иначе
    # масштаб посчитался бы дважды.
    tail_exp, tail_unit = _parse_spec(raw_value[head.end():])
    claimed_exp, claimed_unit = _parse_spec(unit or "")
    if claimed_exp == 0:
        claimed_exp = tail_exp
    if claimed_unit is None:
        claimed_unit = tail_unit

    check_context = bool((unit or "").strip()) or tail_unit is not None or tail_exp != 0
    target = _magnitude(normalized, claimed_exp)
    if target is None:
        return False

    text = str(anchor_text)
    for match in _NUMBER_RE.finditer(text):
        found = normalize_number(match.group())
        if found is None:
            continue
        if not check_context:
            if found == normalized:
                return True
            continue

        found_exp, found_unit = _parse_spec(text[match.end():match.end() + _TAIL_WINDOW])
        if found_unit is None:
            # «$1,5 млрд» — валюта стоит перед числом.
            found_unit = _currency_prefix(text[:match.start()])
        found_magnitude = _magnitude(found, found_exp)
        if found_magnitude is None or found_magnitude != target:
            continue
        if claimed_unit is not None and found_unit != claimed_unit:
            continue
        return True
    return False


def _parse_spec(text: str) -> tuple[int, str | None]:
    """Разбирает хвост «млн тонн» / «млрд долл./барр.» в (степень десятки, единица).

    Читается не больше одного слова масштаба и одной единицы (плюс части через
    слеш) — дальше идёт обычная речь: «1,5 млн тонн нефти в 2024 году».
    """
    tokens = _TOKEN_RE.findall(text or "")
    if not tokens:
        return 0, None

    exp = 0
    index = 0
    if _fold(tokens[0]) in _SCALES:
        exp = _SCALES[_fold(tokens[0])]
        index = 1
    if index >= len(tokens):
        return exp, None

    parts = [_canon_unit(tokens[index])]
    index += 1
    while index + 1 < len(tokens) and tokens[index] == "/":
        parts.append(_canon_unit(tokens[index + 1]))
        index += 2
    return exp, "/".join(parts)


def _currency_prefix(before: str) -> str | None:
    """Символ валюты вплотную перед числом, если он там есть."""
    stripped = before.rstrip(_SPACE_CHARS)
    if stripped and stripped[-1] in "$₽€":
        return _canon_unit(stripped[-1])
    return None


def _canon_unit(token: str) -> str:
    folded = _fold(token)
    return _UNIT_ALIASES.get(folded, folded)


def _fold(token: str) -> str:
    return token.strip().lower().replace("ё", "е")


def _magnitude(normalized: str, exp: int) -> Decimal | None:
    try:
        return Decimal(normalized).scaleb(exp)
    except (InvalidOperation, ValueError):
        return None
