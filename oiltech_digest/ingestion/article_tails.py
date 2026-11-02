"""Где кончается материал издателя: хвосты страницы в тексте статьи (#140).

Вместе с текстом сохранялись соседние новости, комментарии читателей, реклама сайта и подвал: за
30 дней на проде у 1 319 статей из 5 740. Текст идёт во вход «Отбора», «Карточки» и сути, и чужие
события в хвосте давали «разные события», чужие компании, даты и тему.

Два правила. Общее работает при извлечении со страницы: чужие блоки по классу или id и всё, что идёт
за заголовком хвоста (`strip_candidates`, `cut_text_at_heading`). Правило повторов работает при записи:
хвост, который есть у других статей источника (`find_repeated_tail`, запись в db/article_tail_store.py).

Здесь только разбор, без базы.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
import re
from typing import Any, NamedTuple

from oiltech_digest import config
from oiltech_digest.ingestion import normalize

# Слова класса или id, которыми сайты помечают чужие блоки. Сравнение по целым словам, а не по
# подстроке, как у article_fetcher._BAD_CLASS_RE: здесь пропадает весь блок, а подстрока «ads»
# есть в uploads, «share» в shareholders.
# Соседние материалы и комментарии: метка решает, такой блок бывает длиннее самой новости.
_FOREIGN_WORDS = frozenset({
    "related", "similar", "recommended", "recommendations", "trending", "popular",
    "comment", "comments", "commentlist", "commentform", "disqus",
})
_FOREIGN_PAIRS = frozenset({
    ("more", "news"), ("more", "stories"), ("more", "articles"), ("more", "posts"),
    ("other", "news"), ("other", "articles"), ("other", "posts"),
    ("latest", "news"), ("last", "news"), ("news", "list"), ("most", "read"),
    ("read", "also"), ("also", "read"), ("see", "also"), ("check", "also"), ("prev", "next"),
})
# Подписка, реклама, подвал, колонка: такими словами помечают и обёртки вёрстки (content-sidebar-wrap).
_SITE_WORDS = frozenset({
    "subscribe", "subscription", "newsletter", "signup", "register", "registration",
    "advert", "adverts", "advertisement", "advertising", "ad", "ads", "adsbygoogle", "promo", "sponsor", "sponsored",
    "banner", "footer", "sidebar", "share", "social", "copyright",
})
_SITE_PAIRS = frozenset({("sign", "up")})
# has-sidebar, with-comments, no-related, comments-open: это настройка вёрстки, а не сам блок.
_MODIFIER_WORDS = frozenset({
    "has", "with", "without", "no", "show", "hide", "enable", "enabled", "disable", "disabled",
    "open", "closed", "allow", "status",
})
_CLASS_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")


def _class_tokens(node) -> list[list[str]]:
    """Слова каждого значения class и id: relatedNews и related-news дают одно и то же."""
    return [
        [word.lower() for word in _CLASS_WORD_RE.findall(token)]
        for value in (node.get("class"), node.get("id"))
        for token in (value or "").split()
    ]


def _has_mark(node, words: frozenset[str], pairs: frozenset[tuple[str, str]]) -> bool:
    tokens = _class_tokens(node)
    if any("expert" in token for token in tokens):
        return False  # врезка с мнением эксперта это материал, хотя бы и class="comment expert-comment"
    for token in tokens:
        if _MODIFIER_WORDS.intersection(token):
            continue
        if words.intersection(token) or pairs.intersection(zip(token, token[1:])):
            return True
    return False


def _text_len(node) -> int:
    return len(normalize.clean_html(node.text_content()))


def _unlinked_len(node) -> int:
    linked = sum(len(normalize.clean_html(link.text_content())) for link in node.iter("a"))
    return max(0, _text_len(node) - linked)


def _can_drop(block_root, block) -> bool:
    """Снаружи блока остаётся текст: блок не обёртка статьи.

    Обёртку с меткой снимать нельзя, в ней весь материал, поэтому такой блок не выбрасывается, а чистится
    изнутри. У соседних материалов и комментариев метка решает: у АНГИ комментариев 2,7 тыс. знаков при
    новости в тысячу. У прочих меток сверяемся с текстом вне ссылок: колонка почти вся из ссылок, а статья нет."""
    outside = _text_len(block_root) - _text_len(block)
    if outside < config.MIN_ARTICLE_TEXT_CHARS or block.xpath(".//h1|.//main"):
        return False
    return _has_mark(block, _FOREIGN_WORDS, _FOREIGN_PAIRS) or outside >= _unlinked_len(block)


def _strip_tail_blocks(block_root, node) -> None:
    for child in list(node):
        if not isinstance(child.tag, str):
            continue
        marked = _has_mark(child, _FOREIGN_WORDS, _FOREIGN_PAIRS) or _has_mark(child, _SITE_WORDS, _SITE_PAIRS)
        if marked and _can_drop(block_root, child):
            child.drop_tree()  # в отличие от remove() оставляет текст, идущий за блоком
        else:
            _strip_tail_blocks(block_root, child)


def strip_block(block):
    """Выбросить из блока чужие блоки и всё, что идёт за заголовком хвоста. Правит документ на месте."""
    _strip_tail_blocks(block, block)
    _cut_at_heading(block)
    return block


def strip_candidates(candidates: Iterable, within) -> Iterator:
    """Почистить блоки-кандидаты по очереди и отдать те, что остались внутри `within`.

    Прямо в документе, а не в копии, и внешние раньше вложенных: кандидат внутри выброшенного уходит
    вместе с ним. У Белоруснефти сам список соседних новостей оказался кандидатом (класс list-content)
    и без статьи вокруг становился самым длинным текстом страницы, а у Pipeline & Gas Journal соседние
    новости это тоже <article>."""
    for node in candidates:
        if node is within or any(ancestor is within for ancestor in node.iterancestors()):
            yield strip_block(node)


# Заголовки, которые сайты ставят над чужими блоками. Замер по текстам прода 02.11: в связном тексте
# такие слова тоже встречаются («Related Companies», «Comments are due», «Recommended Practice»),
# а «Читайте также: ссылка» бывает вставкой посреди статьи. Поэтому заголовок должен стоять отдельно,
# а за ним не должно идти связного текста.
_NEIGHBOUR_HEADINGS = (
    "читайте также", "читать также", "смотрите также", "см. также", "читайте ещё", "читайте еще",
    "читайте по теме", "новости по теме", "статьи по теме", "материалы по теме", "публикации по теме",
    "все новости по теме", "другие новости", "другие статьи", "другие материалы", "другие публикации",
    "похожие новости", "похожие статьи", "похожие материалы", "похожие публикации",
    "ещё новости", "еще новости", "ещё по теме", "еще по теме", "больше новостей", "больше материалов",
    "последние новости", "свежие новости", "самое читаемое", "самое популярное",
    "популярные новости", "популярные материалы", "рекомендуем прочитать", "рекомендуем почитать",
    "вам может быть интересно", "вам также может быть интересно", "вам может понравиться",
    "related articles", "related news", "related posts", "related stories", "related content",
    "related reading", "related links", "more news", "more stories", "more articles",
    "read next", "read also", "also read", "see also", "you may also like", "you might also like",
    "you may also be interested in", "you might be interested in",
    "recommended for you", "recommended articles", "recommended stories", "recommended reading",
    "trending now", "trending news", "trending articles", "latest news", "latest articles", "latest stories",
    "top stories", "most read", "most popular", "popular posts", "popular articles",
)
_COMMENT_HEADINGS = (
    "последние комментарии", "последние комментарии к новостям", "оставить комментарий",
    "оставьте комментарий", "добавить комментарий", "написать комментарий",
    "leave a comment", "leave a reply", "post a comment", "add a comment", "join the conversation",
    "what do you think",
)
# Эти считаются заголовком, только когда стоят отдельным куском разметки, отдельной строкой или с двоеточием.
_SHORT_NEIGHBOUR_HEADINGS = ("related", "recommended", "trending", "по теме", "популярное", "рекомендуем")
_SHORT_COMMENT_HEADINGS = ("comments", "комментарии")
_SHORT_HEADINGS = _SHORT_NEIGHBOUR_HEADINGS + _SHORT_COMMENT_HEADINGS


def _phrases(items: tuple[str, ...]) -> str:
    # Длинные первыми: иначе «последние комментарии» заберёт короткое и не дойдёт до «к новостям».
    return "|".join(re.escape(item).replace(r"\ ", r"\s+") for item in sorted(items, key=len, reverse=True))


_HEADING_END = r"(?:\s*\(\s*\d+\s*\))?[\s:.!?…›»→>–-]*"
_HEADING_END_RE = re.compile(_HEADING_END)
_COMMENT_COUNT = r"\d+\s+(?:comments?|комментари\w*)"
_COMMENT_HEADING_RE = re.compile(
    rf"(?:{_phrases(_COMMENT_HEADINGS + _SHORT_COMMENT_HEADINGS)}|{_COMMENT_COUNT}){_HEADING_END}", re.I)
_WHOLE_HEADING_RE = re.compile(
    rf"(?:{_phrases(_NEIGHBOUR_HEADINGS + _COMMENT_HEADINGS + _SHORT_HEADINGS)}|{_COMMENT_COUNT}){_HEADING_END}", re.I)
_HEADING_IN_TEXT_RE = re.compile(
    rf"(?<![\w-])(?:(?:{_phrases(_NEIGHBOUR_HEADINGS + _COMMENT_HEADINGS)})(?![\w-])"
    rf"|(?:{_phrases(_SHORT_HEADINGS)})(?=\s*:))", re.I)
_SHORT_HEADING_LINE_RE = re.compile(rf"^[ \t]*(?:{_phrases(_SHORT_HEADINGS)}){_HEADING_END}$", re.I | re.M)
_SENTENCE_BREAK_RE = re.compile(r"[.!?…][\"”»)]?\s+[A-ZА-ЯЁ«\"“]|[。！？]")


def _reads_like_list(text: str) -> bool:
    """Нет связного текста: у хвостов прода 0 границ предложений, у статей 3-8 на тысячу знаков."""
    return len(_SENTENCE_BREAK_RE.findall(text)) * 1000 <= len(text)


class _Fragment(NamedTuple):
    """Кусок текста блока: element.text или element.tail."""

    element: Any
    is_tail: bool
    text: str
    in_link: bool


def _fragments(element, in_link: bool = False) -> Iterator[_Fragment]:
    """Куски текста блока по порядку документа."""
    in_link = in_link or element.tag == "a"
    if element.text and isinstance(element.tag, str):
        yield _Fragment(element, False, normalize.clean_html(element.text), in_link)
    for child in element:
        yield from _fragments(child, in_link)
        if child.tail:
            yield _Fragment(child, True, normalize.clean_html(child.tail), in_link)


def _drop_from(block_root, fragment: _Fragment) -> None:
    """Выбросить из блока кусок текста и всё, что идёт за ним по порядку документа."""
    element = fragment.element
    if not fragment.is_tail:
        element.text = None
        for child in list(element):
            element.remove(child)
    element.tail = None
    node = element
    while node is not block_root:
        parent = node.getparent()
        following = node.getnext()
        while following is not None:
            next_one = following.getnext()
            parent.remove(following)
            following = next_one
        if parent is not block_root:
            parent.tail = None
        node = parent


def _tail_follows(heading: str, before_chars: int, after: list[_Fragment]) -> bool:
    """За заголовком чужой блок, а не продолжение материала.

    Новость бывает короче списка соседних (у Белоруснефти 360 знаков против тысячи), поэтому положение
    заголовка само ничего не решает: смотрим, что за ним. Почти одни ссылки или связный текст (куски с
    границей предложения или точкой в конце) меньше половины: список. Заголовки с датами и подводками
    у Automation.com так и выглядят. Комментарии связны, и для них считается доля: «Читайте также: ссылка»
    со связным текстом за ней это вставка посреди статьи, сколько бы его ни осталось."""
    chars = sum(len(fragment.text) for fragment in after)
    linked = sum(len(fragment.text) for fragment in after if fragment.in_link)
    connected = sum(len(fragment.text) for fragment in after
                    if fragment.text.endswith(".") or _SENTENCE_BREAK_RE.search(fragment.text))
    if linked * 2 >= chars or connected * 2 <= chars:
        return True
    return bool(_COMMENT_HEADING_RE.fullmatch(heading)) and chars * 3 <= before_chars


def _cut_at_heading(block_root) -> None:
    """Обрезать блок по заголовку хвоста, который стоит после материала: перед ним есть свой текст
    (ссылки хлебных крошек не в счёт) и за ним чужой блок."""
    fragments = [fragment for fragment in _fragments(block_root) if fragment.text]
    for index, fragment in enumerate(fragments):
        if not _WHOLE_HEADING_RE.fullmatch(fragment.text):
            continue
        before_chars = sum(len(earlier.text) for earlier in fragments[:index] if not earlier.in_link)
        if before_chars >= config.MIN_ARTICLE_TEXT_CHARS and _tail_follows(fragment.text, before_chars,
                                                                           fragments[index + 1:]):
            _drop_from(block_root, fragment)
            return


def heading_offset(text: str) -> int | None:
    """Где в готовом тексте начинается хвост с заголовком; разметки здесь уже нет.

    Для текста одним узлом, тела из JSON-LD, второго разбора и сохранённых в базе текстов. Заголовок
    с заглавной буквы, за ним не строчная, дальше не связный текст и не больше, чем до него."""
    matches = [*_HEADING_IN_TEXT_RE.finditer(text), *_SHORT_HEADING_LINE_RE.finditer(text)]
    for match in sorted(matches, key=lambda found: found.start()):
        rest = text[_HEADING_END_RE.match(text, match.end()).end():]
        if not match.group().strip()[0].isupper() or (rest and rest[0].islower()):
            continue
        before = text[:match.start()].rstrip()
        if len(before) >= config.MIN_ARTICLE_TEXT_CHARS and len(rest) <= len(before) and _reads_like_list(rest):
            return len(before)
    return None


def cut_text_at_heading(text: str) -> str:
    offset = heading_offset(text)
    return text if offset is None else text[:offset]


# ---------------------------------------------------------------------------
#  Правило повторов: хвост, который есть и у других статей источника
# ---------------------------------------------------------------------------
# Общее правило не видит блоков без метки и заголовка (у Росатома и Rigzone класс ни о чём не
# говорит), поэтому при записи конец текста сверяется с концами недавних статей того же источника.
# Числа подобраны на всех статьях прода за 45 дней (8 931, снимок 02.11), сверка глазами по источникам.
END_WINDOW_CHARS = 6000
# Повтором считается кусок, который есть хотя бы у двух других статей. С порогом в одну статью на
# снимке добавилось 202 среза, и немалая их часть это настоящий текст: новость и её продолжение
# у Neftegaz.ru и OilCapital кончаются одним абзацем.
MIN_OTHER_ARTICLES = 2
SHINGLE_WORDS = 5
MIN_TAIL_WORDS = 12
MIN_TITLE_WORDS = 4
# Неповторённое слово тяжелее повторённого вдвое: промежуток внутри хвоста (короткий пункт, дата)
# не рвёт его, а общий абзац с заметным текстом после него хвостом не становится.
_GAP_PENALTY = 2
# Конец текста сам должен повторяться: допускаются дата, теги, строка с автором. Без этого общий абзац
# ближе к концу уносил свой вывод статьи.
_MAX_UNREPEATED_END_WORDS = 8

_PAGE_GAP_CHARS = 300
_END_MATCH_WORDS = 8

_WORD_RE = re.compile(r"\d+|[^\W\d_]+")
_REPEATED, _TITLE = 2, 1


@dataclass(frozen=True)
class RepeatedTail:
    """Повторяющийся хвост: `kept + tail` равно исходному тексту до знака.

    `blocked`: резать нельзя, сработал предохранитель."""

    kept: str
    tail: str
    blocked: bool


def _words(text: str) -> list[tuple[int, str]]:
    return [(match.start(), match.group().lower()) for match in _WORD_RE.finditer(text)]


def _word_levels(words: list[str], neighbours: Iterable[tuple[str, str]], own_title: str) -> list[int]:
    """Какие слова конца текста повторяются: _REPEATED, _TITLE или 0.

    Второй уровень нужен списку соседних новостей, где пункты переходят из статьи в статью: два самых
    свежих пункта ещё не успели попасть в концы двух других статей, зато это заголовки статей того же
    источника (а иногда и самой этой статьи). Сам по себе заголовок хвоста не делает."""
    positions: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for index in range(len(words) - SHINGLE_WORDS + 1):
        positions[tuple(words[index:index + SHINGLE_WORDS])].append(index)
    hits: Counter[tuple[str, ...]] = Counter()
    titles = [own_title]
    # Копии одной статьи считаются за одну, а копия этой самой не считается: дубли дело рубежа «то же тело».
    seen = {tuple(words)}
    for title, end in neighbours:
        titles.append(title)
        other = [word for _, word in _words(end)]
        if tuple(other) in seen:
            continue
        seen.add(tuple(other))
        shingles = {tuple(other[index:index + SHINGLE_WORDS]) for index in range(len(other) - SHINGLE_WORDS + 1)}
        hits.update(shingles & positions.keys())

    levels = [0] * len(words)
    _mark_titles(levels, words, titles)
    for shingle, count in hits.items():
        if count >= MIN_OTHER_ARTICLES:
            for index in positions[shingle]:
                levels[index:index + SHINGLE_WORDS] = [_REPEATED] * SHINGLE_WORDS
    return levels


def _mark_titles(levels: list[int], words: list[str], titles: Iterable[str]) -> None:
    starts: dict[str, list[int]] = defaultdict(list)
    for index, word in enumerate(words):
        starts[word].append(index)
    for title in titles:
        title_words = [word for _, word in _words(title)]
        if len(title_words) < MIN_TITLE_WORDS:
            continue
        for index in starts.get(title_words[0], ()):
            if words[index:index + len(title_words)] == title_words:
                levels[index:index + len(title_words)] = [_TITLE] * len(title_words)


def _tail_start(levels: list[int]) -> int | None:
    """Номер слова, с которого начинается хвост: идём с конца и берём лучший счёт."""
    unrepeated_end = next((count for count, level in enumerate(reversed(levels)) if level), len(levels))
    if unrepeated_end > _MAX_UNREPEATED_END_WORDS:
        return None
    score = best = repeated = covered = 0
    start = None
    covered_at_start = 0
    for index in range(len(levels) - 1, -1, -1):
        score += 1 if levels[index] else -_GAP_PENALTY
        repeated += levels[index] == _REPEATED
        covered += bool(levels[index])
        if repeated and score > best:
            best, start, covered_at_start = score, index, covered
    return start if covered_at_start >= MIN_TAIL_WORDS else None


def find_repeated_tail(text: str, neighbours: Iterable[tuple[str, str]], own_title: str = "",
                       already_cut: int = 0) -> RepeatedTail | None:
    """Хвост текста, который повторяется у других статей источника; None, если повтора нет.

    `neighbours`: (заголовок, конец полного текста) других статей источника. `already_cut`: сколько
    знаков у этой статьи уже срезано, предохранитель считает от полного текста.

    Предохранители: от текста должно остаться не меньше порога полного текста, и уйти может не
    больше половины. Хвост длиннее половины не укорачивается, а остаётся целиком."""
    start = max(0, len(text) - END_WINDOW_CHARS)
    tokens = _words(text[start:])
    words = [word for _, word in tokens]
    first = _tail_start(_word_levels(words, neighbours, own_title))
    if first is None:
        return None
    kept = text[:start + tokens[first][0]].rstrip()
    tail = text[len(kept):]
    blocked = (len(kept) < config.MIN_FULL_TEXT_CHARS
               or (len(tail) + already_cut) * 2 > len(text) + already_cut)
    return RepeatedTail(kept, tail, blocked)


def has_tail(text: str, repeated: RepeatedTail | None) -> bool:
    """По самому тексту видно хвост: есть заголовок хвоста или повтор у других статей."""
    return repeated is not None or heading_offset(text or "") is not None


def stored_tail_offset(text: str, repeated: RepeatedTail | None) -> int | None:
    """Где резать сохранённый текст, не заглядывая на страницу. None: по самому тексту нельзя."""
    offsets = []
    heading = heading_offset(text)
    if heading is not None and heading >= config.MIN_FULL_TEXT_CHARS:
        offsets.append(heading)
    if repeated is not None and not repeated.blocked:
        offsets.append(len(repeated.kept))
    return min(offsets, default=None)


def end_of_text_in(stored: str, page_text: str) -> int | None:
    """Где в сохранённом тексте кончается текст страницы. None: конец страницы там не нашёлся."""
    page_words = [word for _, word in _words(page_text)][-_END_MATCH_WORDS:]
    if len(page_words) < _END_MATCH_WORDS:
        return None
    tokens = _words(stored)
    words = [word for _, word in tokens]
    for index in range(len(words) - len(page_words), -1, -1):
        if words[index:index + len(page_words)] == page_words:
            last_start, last_word = tokens[index + len(page_words) - 1]
            end = last_start + len(last_word)
            # Точка и кавычка за последним словом тоже текст страницы.
            while end < len(stored) and not stored[end].isalnum() and not stored[end].isspace():
                end += 1
            return end
    return None


def page_end_is_plausible(text: str, offset: int, repeated: RepeatedTail | None) -> bool:
    """Граница со страницы похожа на конец материала, а не на анонс: сайт мог отдать только начало статьи.

    Хвост виден и по самому тексту: страница не должна кончаться заметно раньше него. На настоящих
    страницах зазор до 100 знаков. Других признаков нет: действуют предохранители правила повторов."""
    known = len(repeated.kept) if repeated is not None else heading_offset(text)
    if known is not None:
        return offset >= known - _PAGE_GAP_CHARS
    return offset >= config.MIN_FULL_TEXT_CHARS and (len(text) - offset) * 2 <= len(text)
