"""Вердикты сбора: чем кончился кандидат ленты или листинга.

Две группы — по тому, кто решает.

Рубежи сбора ДО вставки (строчными) проходит парсер источника: `known` — адрес уже
в базе (или сбор считает пост знакомым), `old` — старее окна свежести или даты начала сбора
источника (`old_cutoff`), `fetch_failed` —
страница статьи не скачалась, `redirected_home` — скачалась, но сайт увёл со статьи на
свою главную (переехал или снял статью: лечится сменой ленты или архивом, а не маршрутом
через NL или браузером — поэтому не `fetch_failed`), `too_short` — нет заголовка или
текст короче порога, `prefilter` — отсеял предфильтр, `ready` — дошёл до вставки.

Рубежи самой вставки (заглавными) решает `repository.insert_verdict` — единственная их
реализация, её зовёт и insert_article: `DUP_URL_KEY` — ключ адреса занят видимой статьёй,
`DUP_BODY_HASH` — такое же тело у видимой статьи источника, `known` — тот же адрес у
скрытой строки, `WOULD_INSERT` — вставилась бы.

`DUP_URL_KEY` проба делит на `DUP_URL_KEY_SAME` (ключ занят этой же статьёй) и
`DUP_URL_KEY_OTHER` (ДРУГОЙ статьёй — склейка, потеря). Вставке это различие не нужно,
поэтому его считает только проба (source_probe.key_verdict).

Шаги (`Step`) общие для сбора и пробы источника (`source-probe`): сбор по ним считает
сводку и вставляет `ready`, проба печатает вердикт. Одна реализация — один ответ.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from oiltech_digest.ingestion import dates

KNOWN = "known"
OLD = "old"
FETCH_FAILED = "fetch_failed"
REDIRECTED_HOME = "redirected_home"
TOO_SHORT = "too_short"
PREFILTER = "prefilter"
READY = "ready"

DUP_URL_KEY = "DUP_URL_KEY"
DUP_URL_KEY_SAME = "DUP_URL_KEY_SAME"
DUP_URL_KEY_OTHER = "DUP_URL_KEY_OTHER"
DUP_BODY_HASH = "DUP_BODY_HASH"
WOULD_INSERT = "WOULD_INSERT"

# Порядок в сводке: сперва ответ на вопрос «даёт ли источник новое», дальше — по пути сбора.
ORDER = (WOULD_INSERT, KNOWN, OLD, FETCH_FAILED, REDIRECTED_HOME, TOO_SHORT, PREFILTER,
         DUP_URL_KEY_SAME, DUP_URL_KEY_OTHER, DUP_BODY_HASH)


@dataclass(frozen=True)
class ArticleFetch:
    """Чем кончилась загрузка статьи-кандидата: запись для вставки — или почему её нет.

    `failure` — FETCH_FAILED (страница не получена), REDIRECTED_HOME (сайт увёл со статьи
    на главную) или TOO_SHORT (нет заголовка или текст короче MIN_ARTICLE_TEXT_CHARS). Для
    сбора все причины — просто «статьи нет», для пробы источника — разные ответы на вопрос
    «почему молчит».
    """

    article: dict | None
    failure: str | None = None
    title: str = ""
    text_chars: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class Step:
    """Чем для одного кандидата кончились рубежи сбора до вставки.

    `record` — запись для insert_article: у READY всегда, у PREFILTER и OLD страницы
    статьи — если она уже скачана. `detail` — почему: слова шума предфильтра, статус
    загрузки, правило «последнего увиденного поста».
    """

    stage: str
    url: str
    title: str = ""
    published_at: datetime | None = None
    text_chars: int | None = None
    record: dict | None = None
    detail: str = ""


def old_cutoff(source: dict, max_age_days: int | None) -> datetime | None:
    """Граница рубежа OLD: что опубликовано раньше, сбор не берёт. Поздняя из двух: окно свежести прогона
    (`max_age_days`) и дата начала сбора источника (`sources.collect_since`, решение владельца 06.11: новый источник
    не тащит архив своей ленты, база ставит дату на две недели раньше заведения). Нет ни той, ни другой: границы нет.
    Одна граница на все стратегии сбора, на воркер NL и на пробу источника. Статью без даты граница не отсекает:
    судить её не по чему."""
    bounds = []
    if max_age_days is not None:
        bounds.append(datetime.now(timezone.utc) - timedelta(days=int(max_age_days)))
    since = dates.as_utc(source.get("collect_since"))
    if since is not None:
        bounds.append(since)
    return max(bounds) if bounds else None
