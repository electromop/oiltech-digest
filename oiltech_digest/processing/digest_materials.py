"""Материалы выпуска в формате эксперта (#161, спека #160, ADR 0006): раздел, рубрика, подпись, критерии, связь,
картинка и порядок карточек PDF.

Поля материала живут на его сигнале (ADR 0006): у находки радара и материала коллеги это сам сигнал, у статьи ленты
её сигнал ленты. Статья без сигнала ленты (собрана до 01.08.2026, бывает только в закрытых месяцах) идёт как сигнал
без категории, рубрика по её корневому тегу. Порядок PDF считается: раздел, рубрика по таблице, итоговый балл по
убыванию; черновик выпуска его не задаёт (письмо и DOCX работают с черновиком, как раньше).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from functools import cache
import math

from oiltech_digest import config, event_dates, feed_window
from oiltech_digest.db import issue_edits_store, repository
from oiltech_digest.db.issue_edits_store import PICTURE_OWN, PICTURE_PLACEHOLDER
from oiltech_digest.otbor_pack_format import load_dictionaries as load_otbor_dictionaries
from oiltech_digest.scoring_profiles import BUSINESS as BUSINESS_PROFILE, SCORING_PROFILES, TECH_RADAR
from oiltech_digest.signal_card_pack import UNDEFINED

BUSINESS, TECHNOLOGY = "business", "technology"
# Своя картинка публикации годна, если она не уже 400 px и не вытянута сильнее 3:1: иначе это логотип или полоска.
MIN_IMAGE_WIDTH, MAX_IMAGE_RATIO = 400, 3.0


@dataclass(frozen=True)
class Section:
    """Раздел выпуска: ключ (категория сигнала), номер и заголовок листа; профиль набора критериев (ADR 0002), его
    имена стоят над «нет оценки по критериям» у карточки без разбивки; подпись карточки без события; заглушка
    карточки без темы из 13; формы слова для темы документа (один, два, пять)."""

    key: str
    number: str
    title: str
    profile: str
    no_event_label: str
    fallback_placeholder: str
    nouns: tuple[str, str, str]


# Разделы по порядку листов: сначала бизнес. Подписи «нет события»: из таблицы спеки #160.
SECTIONS = (
    Section(BUSINESS, "01", "Бизнес-сигналы", BUSINESS_PROFILE, "Рыночный контекст", "market",
            ("бизнес-сигнал", "бизнес-сигнала", "бизнес-сигналов")),
    Section(TECHNOLOGY, "02", "Технологические сигналы", TECH_RADAR, "Обзор практики", "automation",
            ("технологический сигнал", "технологических сигнала", "технологических сигналов")),
)
_SECTION = {section.key: section for section in SECTIONS}
_SECTION_PLACE = {section.key: place for place, section in enumerate(SECTIONS)}


@dataclass(frozen=True)
class Theme:
    """Тема выпуска: начало имени корневого тега (тема сигнала равна корневому тегу; имена тем: данные, их правят на
    экране «Теги», поэтому по началу имени, как signal_themes.MARKET_PREFIX), рубрика карточки, она же строчными для
    «Главного» и заглушка картинки (assets/placeholders)."""

    prefix: str
    rubric: str
    phrase: str
    placeholder: str
    # Рубрика темы в бизнес-разделе, если она там другая (рубрика и её строчная форма).
    in_business: tuple[str, str] | None = None


# Тема автоматизации в бизнес-разделе: обзор рынка, заказ, сделка, смена поставщиков (эксперт 03.11).
AUTOMATION_MARKET = ("РЫНОК АВТОМАТИЗАЦИИ", "рынок автоматизации")
# Таблица рубрик спеки #160 в порядке рубрик выпуска; три имени (геологоразведка, заканчивание, автоматизация)
# предложены нами и подтверждены экспертом 03.11.
THEMES = (
    Theme("Рынок", "РЫНОК И СЕРВИСНЫЕ МОДЕЛИ", "рынок и сервисные модели", "market"),
    Theme("Геологоразведка", "ГЕОЛОГОРАЗВЕДКА", "геологоразведка", "geology"),
    Theme("Бурение", "БУРЕНИЕ", "бурение", "drilling"),
    Theme("Заканчивание", "ЗАКАНЧИВАНИЕ СКВАЖИН", "заканчивание скважин", "completion"),
    Theme("Добыча", "ДОБЫЧА", "добыча", "production"),
    Theme("Химия", "ХИМИЯ И ВОДА", "химия и вода", "chemistry"),
    Theme("HSE", "БЕЗОПАСНОСТЬ", "безопасность", "hse"),
    Theme("ГРП", "ГРП", "ГРП", "fracturing"),
    Theme("КРС", "КРС И ВНУТРИСКВАЖИННЫЕ РАБОТЫ", "КРС и внутрискважинные работы", "workover"),
    Theme("Логистика", "ЛОГИСТИКА", "логистика", "logistics"),
    Theme("Энергетика", "ЭНЕРГЕТИКА", "энергетика", "energy"),
    Theme("Промысловая", "ДИАГНОСТИКА И ЦЕЛОСТНОСТЬ", "диагностика и целостность", "integrity"),
    Theme("Автоматизация", "АВТОМАТИЗАЦИЯ", "автоматизация", "automation", in_business=AUTOMATION_MARKET),
)
# Порядок рубрик: рынок автоматизации сразу за рынком и сервисными моделями, дальше темы по таблице.
RUBRIC_ORDER = (THEMES[0].rubric, AUTOMATION_MARKET[0], *(theme.rubric for theme in THEMES[1:]))
# Место рубрики; рубрики не из порядка (тема не из 13): после всех.
_RUBRIC_PLACE = {rubric: place for place, rubric in enumerate(RUBRIC_ORDER)}
# Давнее событие: своя рубрика и подпись, место сразу за рубрикой своей темы.
OLD_PRACTICE_RUBRIC, OLD_PRACTICE_LABEL = "СПРАВОЧНАЯ ПРАКТИКА", "Практика прошлых периодов"

# Подпись после рубрики по типу события «Карточки» (таблица спеки #160, значения справочника event_type
# signal_card_dictionaries.json): говорит о стадии события. Тип не определён: подписи нет.
LABELS = {
    "регулирование": "Регуляторная инициатива",
    "инвестиции": "Программа заказчика",
    "контракт": "Контракт",
    "сделка (M&A)": "Сделка",
    "партнёрство": "Развитие сервиса",
    "исследование": "Лабораторное исследование",
    "патент": "Патент",
    "пилот": "Пилот",
    "промысловые испытания": "Промысловый кейс",
    "запуск продукта": "Анонс продукта",
    "внедрение": "Применение на промысле",
    "масштабирование": "Масштабирование",
    "коммерциализация": "Выход на рынок",
    "другое событие": "Событие",
}
# Особые случаи таблицы: масштабирование при зрелости «масштабное внедрение»; «нет события»: по разделу (Section).
SCALING, SCALED_MATURITY, SCALED_LABEL = "масштабирование", "масштабное внедрение", "Масштабное применение"
NO_EVENT = "нет события"

# Месяц выпуска в «Главное в октябрьском выпуске».
_MONTH_ADJECTIVES = ("январском", "февральском", "мартовском", "апрельском", "майском", "июньском", "июльском",
                     "августовском", "сентябрьском", "октябрьском", "ноябрьском", "декабрьском")


def plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение существительного по числу (1 новость / 2 новости / 5 новостей)."""
    n = abs(int(n)) % 100
    if 10 < n < 20:
        return many
    n1 = n % 10
    if n1 == 1:
        return one
    if 2 <= n1 <= 4:
        return few
    return many


def cover(month: str | None, sections: list[dict]) -> dict:
    """«Главное в выпуске» на обложке: строка на раздел. Пишет человек на экране «Дайджест» (#162); не написано:
    перечень рубрик раздела (у давнего события его тема; решение владельца 04.11). Раздел без материалов строки не
    даёт, даже написанной: она говорила бы о том, чего в выпуске нет. default и кто правил нужны экрану."""
    adjective = f"{_MONTH_ADJECTIVES[feed_window.parse_month(month).month - 1]} " if month else ""
    written = issue_edits_store.highlights(month) if month else {}
    rows = []
    for section in sections:
        if not section["materials"]:
            continue
        topics = list(dict.fromkeys(material["topic"] for material in section["materials"] if material.get("topic")))
        joined = ", ".join(topics)
        default = f"{joined[0].upper()}{joined[1:]}." if joined else ""
        human = written.get(section["key"])
        text = human["text"] if human else default
        if text:
            rows.append({"key": section["key"], "label": section["title"], "text": text, "default": default,
                         "edited": human is not None, "edited_by": human["updated_by_email"] if human else None,
                         "edited_at": human["updated_at"] if human else None})
    return {"highlights_title": f"Главное в {adjective}выпуске", "highlights": rows}


def document(month: str | None, sections: list[dict], *, name: str) -> dict:
    """Название и тема PDF («Нефтесервисный дайджест. Октябрь 2026», «21 материал: 5 бизнес-сигналов и 16
    технологических сигналов») и подписи колонтитула: название выпуска и месяц."""
    month_label = feed_window.month_label(feed_window.parse_month(month)).capitalize() if month else ""
    counts = {section["key"]: len(section["materials"]) for section in sections}
    total = sum(counts.values())
    parts = [f"{counts[section.key]} {plural(counts[section.key], *section.nouns)}"
             for section in SECTIONS if counts.get(section.key)]
    subject = f"{total} {plural(total, 'материал', 'материала', 'материалов')}"
    return {
        "name": name,
        "month_label": month_label,
        "title": f"{name}. {month_label}" if month_label else name,
        "subject": f"{subject}: {' и '.join(parts)}" if parts else subject,
    }


def section_of(category: str | None) -> str:
    """Раздел по категории сигнала. Без категории и «другое» идут в технологии, как на экране радара."""
    return BUSINESS if category == BUSINESS else TECHNOLOGY


def theme_of(name: str | None) -> Theme | None:
    """Тема выпуска по имени темы сигнала (корневого тега). Чужое имя («не классифицировано», тема радара до 13
    тематик), None: у карточки нет рубрики, и она идёт после всех рубрик своего раздела."""
    folded = (name or "").casefold()
    return next((theme for theme in THEMES if folded.startswith(theme.prefix.casefold())), None)


def label_of(event_type: str | None, maturity: str | None, section: str) -> str:
    """Подпись карточки по типу события и зрелости «Карточки»; не определено: пусто."""
    if event_type == NO_EVENT:
        return _SECTION[section].no_event_label
    if event_type == SCALING and maturity == SCALED_MATURITY:
        return SCALED_LABEL
    return LABELS.get(event_type or "", "")


def criteria_of(fields: dict) -> list[dict]:
    """Баллы по критериям, как на карточке витрины (scoreBreakdown экрана радара): разбивка балла сигнала, имя критерия
    из снимка набора на момент оценки, целые от 0 до 100 (округление как у экрана); «Свежесть» без даты события в итог
    не входит. Балл посчитан неизвестным профилем: разбивки нет, как на витрине."""
    if fields.get("score_profile") not in SCORING_PROFILES:
        return []
    names = {}
    for criterion in fields.get("criteria_snapshot") or []:
        try:
            names[int(criterion["id"])] = criterion.get("name")
        except (KeyError, TypeError, ValueError):
            continue
    rows = []
    for item in fields.get("score_items_json") or []:
        try:
            criterion_id = int(item["criterion_id"])
            score = min(100, max(0, math.floor(float(item.get("final_score") or 0) + 0.5)))
        except (KeyError, TypeError, ValueError):
            continue
        excluded = bool(item.get("excluded"))
        rows.append({"name": names.get(criterion_id) or f"Критерий {criterion_id}",
                     "score": None if excluded else score, "excluded": excluded})
    return rows


@cache
def _relevance_labels() -> dict[str, str]:
    return {value["code"]: value["label"] for value in load_otbor_dictionaries()["oilfield_relevance"]["values"]}


def relevance_of(code: str | None) -> str:
    """Связь с нефтесервисом словами справочника «Отбора» («прямая», «переносимая»); пусто: «не определено»."""
    return _relevance_labels().get(code or "", UNDEFINED)


def is_old_event(fields: dict, month: date | None) -> bool:
    """Давнее событие: дата события старше SIGNAL_RADAR_MAX_EVENT_AGE_DAYS (180, порог экрана радара) до начала месяца
    выпуска. Дата с точностью до месяца или года: по последнему дню своего периода, как у «Отбора» (event_dates)."""
    days = int(config.SIGNAL_RADAR_MAX_EVENT_AGE_DAYS)
    event = fields.get("event_date")
    if month is None or days <= 0 or not isinstance(event, date):
        return False
    return event_dates.period_end(event, fields.get("event_date_precision")) < month - timedelta(days=days)


def sections(items: list[dict], *, month: str | None) -> list[dict]:
    """Разделы PDF выпуска: каждый материал с полями своего сигнала, в порядке карточек. `items`: позиции выпуска от
    сборщика: номер статьи или сигнала, заголовок, суть целиком, ссылка на главную публикацию, своя картинка, балл,
    корневой тег статьи."""
    month_start = feed_window.parse_month(month) if month else None
    by_article, by_signal = repository.issue_material_fields(
        [item["article_id"] for item in items if item.get("article_id")],
        [item["signal_id"] for item in items if item.get("signal_id")],
    )
    keyed = [
        _material(item, by_article.get(item["article_id"]) if item.get("article_id")
                  else by_signal.get(item["signal_id"]), month_start)
        for item in items
    ]
    # Сортировка устойчивая: при равных разделе, рубрике и балле: порядок сборщика.
    materials = [material for _, material in sorted(keyed, key=lambda pair: pair[0])]
    result = []
    for section in SECTIONS:
        members = [material for material in materials if material["section"] == section.key]
        names = ([row["name"] for row in repository.list_enabled_scoring_criteria(section.profile)]
                 if members else [])
        result.append({"key": section.key, "number": section.number, "title": section.title,
                       "criteria_names": names, "materials": members})
    return result


def _rubric(theme: Theme | None, section: str) -> tuple[str, str]:
    """Рубрика карточки и её строчная форма для «Главного»; тема не из 13: пусто."""
    if theme is None:
        return "", ""
    if section == BUSINESS and theme.in_business:
        return theme.in_business
    return theme.rubric, theme.phrase


def _material(item: dict, fields: dict | None, month: date | None) -> tuple[tuple, dict]:
    """Материал выпуска и его место: раздел (сначала бизнес), рубрика по порядку таблицы (давнее событие: сразу за
    рубрикой своей темы, тема не из 13: после всех), балл по убыванию (без балла: последним)."""
    if fields is None:
        # Статья без сигнала ленты идёт как сигнал без категории, тема по её корневому тегу.
        fields = {"theme": item.get("root_tag")}
    section = section_of(fields.get("signal_category"))
    theme = theme_of(fields.get("theme"))
    rubric, topic = _rubric(theme, section)
    rank = 2 * _RUBRIC_PLACE.get(rubric, len(RUBRIC_ORDER))
    label = label_of(fields.get("event_type"), fields.get("technology_maturity"), section)
    if is_old_event(fields, month):
        rubric, label, rank = OLD_PRACTICE_RUBRIC, OLD_PRACTICE_LABEL, rank + 1
    # Балл берётся у позиции выпуска: тот же, по которому идут письмо и DOCX (у статьи тема и балл её публикации,
    # repository.publication_joins, у находки радара и материала её балл). Сигнал, которого «Карточка» ещё не оценила,
    # балла не имеет и идёт последним в своей рубрике, а не по нулю или по баллу, скопированному со статьи.
    score = float(item["score"]) if item.get("score") is not None else None
    # Подпись человека (#162) главнее таблицы, в том числе у давнего события; умолчание идёт экрану подсказкой правки.
    label_default, label = label, fields.get("issue_label") or label
    criteria = criteria_of(fields)
    relevance = relevance_of(fields.get("oilfield_relevance"))
    # Чего не хватает материалу до выпуска (экран «Дайджест», #162): гипотезы, баллов по критериям, связи, своей
    # картинки (заглушку, выбранную человеком, нехваткой не считаем: это решение).
    picture = PICTURE_PLACEHOLDER if fields.get("issue_picture") == PICTURE_PLACEHOLDER else PICTURE_OWN
    missing = [code for code, lacking in (
        ("hypothesis", not fields.get("hypothesis")),
        ("criteria", not criteria),
        ("relevance", relevance == UNDEFINED),
        ("picture", not item["image_url"] and picture == PICTURE_OWN),
    ) if lacking]
    material = {
        "item_type": item["item_type"],
        "article_id": item.get("article_id"),
        "signal_id": item.get("signal_id"),
        # Сигнал, которому принадлежат поля материала (ADR 0006): правка с экрана «Дайджест» идёт ему. У статьи без
        # сигнала ленты (собрана до 01.08.2026) пусто: править нечего.
        "material_signal_id": fields.get("signal_id"),
        "section": section,
        "rubric": rubric,
        "label": label,
        "label_default": label_default,
        "title": item["title"],
        "summary": item["summary"],
        # «Гипотеза для Компании» (#162): кто поставил (human или agent), почта человека и когда.
        "hypothesis": fields.get("hypothesis"),
        "hypothesis_set_by": fields.get("hypothesis_set_by"),
        "hypothesis_author": fields.get("hypothesis_author"),
        "hypothesis_set_at": fields.get("hypothesis_set_at"),
        "criteria": criteria,
        "relevance": relevance,
        "url": item["url"],
        # Своя картинка: у материала есть её адрес. Годна ли она (MIN_IMAGE_WIDTH, MAX_IMAGE_RATIO), видно только на
        # загруженной: это проверяет печать, и негодная или не загрузившаяся меняется на заглушку темы (digest_pdf).
        # picture: выбор человека (#162), placeholder рисует заглушку темы и при своей картинке.
        "image_url": item["image_url"],
        "image_own": bool(item["image_url"]),
        "picture": picture,
        "missing": missing,
        "placeholder": theme.placeholder if theme else _SECTION[section].fallback_placeholder,
        "score": score,
        # Тема карточки строчными для «Главного» по умолчанию: у давнего события: его своя тема.
        "topic": topic,
    }
    order = (_SECTION_PLACE[section], rank, score is None, -(score or 0.0))
    return order, material
