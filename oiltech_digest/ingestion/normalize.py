"""Нормализация данных статьи: очистка HTML, парсинг дат, картинка, content_hash.

`clean_html` / `parse_date` / `extract_image` перенесены из прототипа
`oil-tech-digest-bot/parser.py`; `compute_content_hash` — новое.
"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from dateutil import parser as dateparser

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Допуск на часовые пояса и опережающие публикации. Даты дальше этого порога в
# будущем считаем недостоверными: типичный источник — анонсы событий из
# «календаря» на сайте (напр. Equinor «Q3 results — analyst conference»),
# которые скрапер ошибочно принимает за дату публикации.
FUTURE_TOLERANCE_DAYS = 2


def is_future_date(dt: datetime | None, tolerance_days: int = FUTURE_TOLERANCE_DAYS) -> bool:
    """True, если дата заметно в будущем (вероятно, ошибочно распарсенное событие)."""
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt > datetime.now(timezone.utc) + timedelta(days=tolerance_days)


def clean_html(text: str) -> str:
    """Снять HTML-теги, расшифровать entities, схлопнуть пробелы."""
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def parse_date(entry) -> datetime | None:
    """Дата публикации из RSS-entry (published/updated/created) → aware datetime (UTC).

    None, если ни одно поле не распарсилось — статья всё равно сохранится.
    """
    for field in ("published", "updated", "created"):
        raw = entry.get(field, "") if hasattr(entry, "get") else ""
        if raw:
            try:
                dt = dateparser.parse(raw)
            except (ValueError, TypeError, OverflowError):
                continue
            if dt is None:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if is_future_date(dt):
                continue  # дата из будущего — не доверяем, пробуем следующее поле
            return dt
    return None


def extract_image(entry) -> str:
    """URL картинки из media_thumbnail / media_content / enclosures (для будущего #4)."""
    media = entry.get("media_thumbnail", []) if hasattr(entry, "get") else []
    if media and isinstance(media, list):
        return media[0].get("url", "")

    media_content = entry.get("media_content", []) if hasattr(entry, "get") else []
    if media_content and isinstance(media_content, list):
        for mc in media_content:
            if mc.get("medium") == "image" or "image" in mc.get("type", ""):
                return mc.get("url", "")

    enclosures = entry.get("enclosures", []) if hasattr(entry, "get") else []
    if enclosures:
        for enc in enclosures:
            if "image" in enc.get("type", ""):
                return enc.get("href", enc.get("url", ""))
    return ""


def _normalize_title(title: str) -> str:
    return _WS_RE.sub(" ", (title or "").strip().lower())


def _normalize_url(url: str) -> str:
    """host+path в нижнем регистре, без схемы, query (utm и пр.) и хвостового слэша."""
    try:
        parts = urlsplit((url or "").strip().lower())
        if not parts.netloc:
            return (url or "").strip().lower()
        return f"{parts.netloc}{parts.path.rstrip('/')}"
    except ValueError:
        return (url or "").strip().lower()


def compute_content_hash(title: str, url: str) -> str:
    """sha256 от нормализованных title|url. Мягкий сигнал кросс-источниковых дублей."""
    basis = f"{_normalize_title(title)}|{_normalize_url(url)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def compute_body_hash(raw_text: str | None) -> str | None:
    """sha256 от нормализованного ТЕЛА. Ключ для защиты «одно тело — многим статьям» (№24).

    Отличается от compute_content_hash: тот про заголовок+URL (дубль публикации), этот —
    про сам текст. Пробелы схлопываем, чтобы косметика вёрстки не давала разных хэшей.
    """
    if not raw_text:
        return None
    normalized = re.sub(r"\s+", " ", raw_text).strip().lower()
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# Слова, которые есть в любом тексте и потому ничего не доказывают о принадлежности.
_OWNERSHIP_STOPWORDS = frozenset("""
и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее
мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если или быть был для
тем чтобы чем это эта эти этот при над под без до после через между также млн млрд тыс
года году год гг рф сша the a an and or of to in for on at by with from is are was were be
been has have had it that this these those as not but which who will would can could may
might more most new its into
""".split())

# Заголовок короче этого (в значимых словах) судить не позволяет: у «SOCAR» или «May 2026»
# пересечения нет по естественным причинам, а не из-за подмены тела.
_OWNERSHIP_MIN_TITLE_WORDS = 4
# Порог подобран замером на проде 28.07 (400 испорченных против 600 контрольных статей).
_OWNERSHIP_MIN_RATIO = 0.20


def significant_words(text: str) -> list[str]:
    """Значимые слова для сверки принадлежности: без стоп-слов и коротышей.

    Русские слова режем до первых 5 букв — иначе падежи («Роснефти» ↔ «Роснефть»)
    ломали бы сопоставление заголовка с телом.
    """
    out: list[str] = []
    for word in re.findall(r"[а-яёa-z0-9]+", (text or "").lower()):
        if len(word) <= 3 or word in _OWNERSHIP_STOPWORDS:
            continue
        out.append(word[:5] if len(word) > 5 else word)
    return out


def title_matches_body(title: str, body: str) -> bool:
    """Похоже ли, что этот текст принадлежит ЭТОМУ заголовку (защита от подмены, №24).

    Дефект 28.07: у 662 видимых статей (10.4% ленты, у Neftegaz.ru — 37.7%) тело было
    от ДРУГОЙ новости. Никакой проверки принадлежности в коде не существовало: текст
    принимался, если он просто ДЛИННЕЕ прежнего, поэтому листинг, пейвол или «избранный»
    материал из JSON-LD побеждали настоящую статью.

    Проверка намеренно мягкая — цена ложного отказа выше цены пропуска: отвергнутый текст
    означает потерю живой статьи, а пропущенный ловится следующими рубежами. Поэтому:
    судим только по достаточно длинным заголовкам и требуем совпадения всего 20% слов.
    """
    body_words = set(significant_words(body))
    if not body_words:
        return True

    # Заголовок часто несёт хвост с названием издания («… — Новости о нефти и газе»).
    # Такой хвост в теле статьи не встречается и топил бы долю совпадения: замер показал,
    # что из-за него страж отвергал ВЕРНЫЕ статьи OilCapital (19.6% его ленты). Поэтому
    # режем по разделителям и судим по самому «своему» сегменту, а не по строке целиком.
    best_ratio = 0.0
    judged = False
    for segment in re.split(r"\s+[—–|·]\s+|\s+-\s+", title or ""):
        segment_words = set(significant_words(segment))
        if len(segment_words) < _OWNERSHIP_MIN_TITLE_WORDS:
            continue  # слишком короткий кусок судить не позволяет
        judged = True
        best_ratio = max(best_ratio, len(segment_words & body_words) / len(segment_words))

    if not judged:
        # Ни один сегмент не даёт опоры («SOCAR», «May 2026») — не мешаем.
        return True
    return best_ratio >= _OWNERSHIP_MIN_RATIO


# Маркеры «продолжение по ссылке» — типичный признак обрезанной RSS-ленты
_TRUNCATION_TAIL_MARKERS = (
    "read more", "read the full", "read full", "continue reading", "see more",
    "view more", "full story", "[…]", "[...]",
    "читать далее", "читать полностью", "подробнее", "продолжение", "далее по ссылке",
)

# Минимум символов: короче — почти наверняка только анонс, а не полный текст
TRUNCATION_MIN_CHARS = 280


def is_truncated(raw_text: str, min_chars: int = TRUNCATION_MIN_CHARS) -> bool:
    """Эвристика: похоже ли, что RSS отдал сокращённый/обрезанный текст.

    Срабатывает при: пустом тексте; концовке-многоточии; маркерах «читать далее /
    read more»; слишком коротком теле. Это сигнал для ручной проверки/дозагрузки,
    а не строгий критерий.
    """
    text = (raw_text or "").strip()
    if not text:
        return True
    if text.endswith(("…", "...", "[…]", "[...]")):
        return True
    tail = text[-60:].lower()
    if any(marker in tail for marker in _TRUNCATION_TAIL_MARKERS):
        return True
    if len(text) < min_chars:
        return True
    return False
