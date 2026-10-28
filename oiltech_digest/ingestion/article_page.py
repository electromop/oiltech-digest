"""Что взять со страницы статьи: заголовок, дата публикации, текст.

Вынесено из request_parser 28.09, когда правки извлечения вывели его за 500 строк (так же,
как 18.09 выбор ссылок листинга ушёл в listing_cards). Здесь — только разбор уже скачанной
страницы; скачивание, листинг и вставка остаются в request_parser.
"""

from __future__ import annotations

from datetime import datetime
import logging

from oiltech_digest.config import MIN_ARTICLE_TEXT_CHARS
from oiltech_digest.ingestion import dates, normalize
from oiltech_digest.ingestion.article_fetcher import _trafilatura_extract, extract_main_text
from oiltech_digest.ingestion.dates import guess_date_text as _guess_date_from_text
from oiltech_digest.ingestion.dates import parse_datetime as _parse_datetime

logger = logging.getLogger(__name__)


def parse_article_page(content: bytes | str, fallback_title: str = "", url: str | None = None,
                       fallback_date: datetime | None = None) -> tuple[str, datetime | None, str]:
    """Заголовок, дата публикации и текст страницы статьи.

    `fallback_title` и `fallback_date` — заголовок и дата карточки ленты. `url` — адрес статьи:
    если в нём есть дата, дата публикации обязана с ней сходиться (#139, `_published_at`). Без
    адреса — прежний разбор: так зовут ручной импорт, диагностика источника и докачка радара."""
    try:
        doc = normalize.parse_html(content)
    except (ValueError, TypeError):
        return fallback_title, None, ""

    title = first_non_empty(
        doc.xpath("string(//meta[@property='og:title']/@content)"),
        doc.xpath("string(//meta[@name='twitter:title']/@content)"),
        # НЕ string(//h1[1]): XPath string() склеивает текст всех потомков БЕЗ разделителя.
        # У Neftegaz.ru лид лежит внутри того же <h1>, и на выходе получалось
        # «…развивает российские технологии ГРПНа Южно-Приобском месторождении…» —
        # заказчик присылал это дважды (22.08 «текст сливается», 08.09 «потеряли Ва»).
        # Склейка ломала и дедуп: content_hash считается по заголовку, поэтому одна
        # публикация с лидом и без лида давала разные хэши.
        _text_with_separators(doc, "//h1[1]"),
        # Заголовок карточки ленты — раньше <title> страницы. У CNOOC нет ни og:title, ни
        # <h1>, а <title> — название раздела: 18.09 все 7 собранных новостей получили
        # один заголовок «中国海洋石油集团有限公司 公司新闻». Короткая подпись карточки
        # («Подробнее») заголовком не считается.
        fallback_title if len(normalize.clean_html(fallback_title or "")) >= 12 else "",
        doc.xpath("string(//title)"),
        fallback_title,
    )
    title = normalize.clean_html(title)[:500]

    published_at = _published_at(doc, url, fallback_date)

    raw_text = _own_text(content, title, extract_main_text(content, title=title))
    if len(raw_text) < MIN_ARTICLE_TEXT_CHARS:
        raw_text = _visible_text(doc)
    return title, published_at, raw_text


def _published_at(doc, url: str | None, fallback_date: datetime | None) -> datetime | None:
    """Дата публикации со страницы, без неё — дата карточки ленты.

    С датой в адресе статьи берётся только сходящаяся с ней (#139). У Pipeline & Gas Journal
    первый `<time>` несёт `datetime="2025-10-01"` на всех статьях, видимый текст — настоящая дата
    «10/06/2026» (месяц первым), адрес — `/news/2026/october/…`; атрибут выигрывал, и с 30.05
    статьи ложились в октябрь 2025 года, мимо лент своих месяцев. Прежний выбор остаётся, если
    сошёлся с адресом; иначе — первая сошедшаяся из своих мест даты на странице (числовая — в том
    прочтении, что сходится), затем дата карточки ленты. Не сошлась ни одна — пусто: лента
    возьмёт дату поступления, а не дату из противоречащей разметки.
    """
    published_at = _parse_datetime(
        first_non_empty(
            doc.xpath("string(//meta[@property='article:published_time']/@content)"),
            doc.xpath("string(//meta[@name='pubdate']/@content)"),
            doc.xpath("string(//time[1]/@datetime)"),
            _guess_date_from_text(doc.xpath("string(//time[1])")),
        )
    ) or dates.date_from_markup(doc)
    in_url = dates.url_date(url)
    if in_url is None:
        return published_at or fallback_date
    if in_url.agrees(published_at):
        return published_at
    for value in dates.article_date_values(doc):
        for reading in dates.possible_dates(value):
            if in_url.agrees(reading):
                return reading
    return fallback_date if in_url.agrees(fallback_date) else None


def _own_text(content: bytes | str, title: str, text: str) -> str:
    """Текст, который принадлежит заголовку: основное извлечение или, если оно взяло
    чужой блок, второй разбор (trafilatura).

    Страж принадлежности (№24) стоял только в дозагрузке, а первичный разбор принимал
    любой блок длиннее порога. У Eni это окно чат-бота на каждой странице: 3177 одинаковых
    знаков, первая «статья» легла в базу, остальные отбивал рубеж «то же тело» (25.09).
    Второй разбор принимаем, только если он сам про заголовок, — иначе остаётся прежний
    текст: хуже, чем было, не делаем. Короткий текст сюда не относится — для него есть
    запасной путь ниже по стеку.
    """
    if len(text) < MIN_ARTICLE_TEXT_CHARS or normalize.title_matches_body(title, text):
        return text
    alternative = _trafilatura_extract(content)
    if len(alternative) >= MIN_ARTICLE_TEXT_CHARS and normalize.title_matches_body(title, alternative):
        logger.debug("parse_article_page: основное извлечение не про заголовок %r — взят второй разбор", title)
        return alternative
    return text


# Запасной текст страницы длиннее этого — уже не статья, а вся страница целиком.
_FALLBACK_TEXT_LIMIT = 20000


def _visible_text(doc) -> str:
    """Текст страницы без скриптов и стилей — запасной путь, когда статья не выделилась.

    `text_content()` забирает и содержимое `<script>`: у ТеДо в «текст статьи» ложилось
    132 тыс. знаков JSON, у Kuwait Oil — 444 тыс. На проде 18.09 — 42 статьи длиннее
    50 тыс. знаков, 36 из них со скриптами, самая большая 768 тыс. Модель читает
    первые 6000 знаков — то есть судила бы JavaScript вместо новости.
    """
    for node in doc.xpath("//script|//style|//noscript|//template|//svg"):
        node.drop_tree()
    return normalize.clean_html(" ".join(doc.itertext()))[:_FALLBACK_TEXT_LIMIT]


def _text_with_separators(doc, xpath: str) -> str:
    """Текст узла с ПРОБЕЛОМ между вложенными элементами.

    Замена XPath `string(...)`, который склеивает соседние текстовые узлы вплотную.
    Пустые узлы отбрасываем, остальное соединяем одним пробелом; схлопывание
    повторов делает `normalize.clean_html` выше по стеку.
    """
    try:
        parts = [str(part).strip() for part in doc.xpath(f"{xpath}//text()")]
    except Exception:  # noqa: BLE001 — битый XPath не должен ронять разбор статьи
        return ""
    return " ".join(part for part in parts if part)


def first_non_empty(*values: str) -> str:
    for value in values:
        if value and str(value).strip():
            return str(value).strip()
    return ""
