"""Гейт, стоп-слова, тег и балл статьи: только для агента источников (с ленты сняты 08.11).

Ленту с 08.11 решает только «Отбор» (#130, спека #128), тему и баллы ставит «Карточка» (#132): гейт ленты, его
перепроверка, тег и балл статьи сняты. Агент источников выключен в коде, но его код не удаляется (решение владельца
08.11): песочница на ядре (sandbox.process_candidate_articles) и оценка кандидата на NL
(external_ai.process_source_candidate_payload) по-прежнему зовут эти стадии на статьях кандидата. Сюда они перенесены
без изменения поведения. Свои ключи моделей у стадий сняты: модель config.OPENAI_MODEL, уровни рассуждения
прежние, как у умолчаний снятых ключей (гейт minimal, тег как у основной модели, балл medium).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from oiltech_digest import config
from oiltech_digest.db import repository
from oiltech_digest.processing.openai_client import AIClientError, AIResponse, output_budget
from oiltech_digest.processing.pipeline import _article_prompt, _compact, _contains_keyword, _search_text

logger = logging.getLogger(__name__)

# Уровни рассуждения стадий: прежние умолчания OPENAI_RELEVANCE_REASONING и OPENAI_SCORE_REASONING (ключи сняты
# 08.11). Гейт отвечает строгим JSON, и на medium Responses API иногда тратил весь ответ на рассуждение; балл на
# minimal систематически занижался (инцидент 2026-06).
RELEVANCE_REASONING = "minimal"
SCORE_REASONING = "medium"


TAGGING_INSTRUCTIONS = """Ты классифицируешь нефтесервисные новости по дереву тегов. Во входе —
список тегов в формате `id: Родитель / Дочерний | EN: ... | keywords: ...`.

- Выбери РОВНО один тег по сути статьи и верни его числовой id из списка.
- Бери самый КОНКРЕТНЫЙ подходящий тег. Если в списке есть иерархия `Родитель / Дочерний` —
  предпочитай дочерний; иначе выбирай из плоского списка.
- confidence (0..1): высокая при прямом совпадении смысла и ключевых слов, низкая если тег
  лишь приблизительный.
- rationale — кратко по-русски, почему этот тег, по СУТИ статьи, а не по источнику.
- Если во входе есть блок rules — следуй ему.

Ответ строго по JSON Schema."""

SCORING_INSTRUCTIONS = """Ты оцениваешь нефтесервисную новость для дайджеста. Оцени КАЖДЫЙ
критерий (ai_score) от 0 до 100 по его описанию и ключевым словам. Используй ВСЮ шкалу.

Якоря ai_score:
- 85-100: критерий выражен прямо и сильно (промышленное внедрение, измеримый эффект, готовое
  решение для нефтегаза);
- 65-84: выражен явно, но с оговорками (пилот, ранняя стадия, эффект ожидаемый);
- 40-64: затронут косвенно или слабо;
- 10-39: почти не выражен;
- 0-9: к статье не относится.

Оценивай критерий по ЕГО описанию, а не по общей «важности» новости. Прямо применимая
нефтесервисная статья должна уверенно брать 70+ по профильным критериям — высоких баллов не бойся.

Инциденты. Нефтяные ЧП (атака на танкер или терминал, взрыв, авария, пожар, срыв поставок,
геополитика вокруг цен) остаются в выборке, но это ФОН рынка, а не приоритетный сигнал —
оценивай их НИЗКО. «Бизнес-эффект» вознаграждает ценность описанного РЕШЕНИЯ или ДЕЙСТВИЯ
(технология, сервис, метод, оборудование, контракт, внедрение), улучшающего добычу, сроки,
затраты или безопасность. Масштаб происшествия, ущерб, число жертв, влияние срыва на цены —
это НЕ бизнес-эффект: статья описывает ПРОБЛЕМУ, а не улучшение, и набирает мало по всем
критериям. Исключение: статья по сути про РЕШЕНИЕ, вызванное инцидентом (новая технология,
регламент, оборудование по итогам аварии) — оценивай решение по существу, оно может быть высоким.

incident_without_solution = true, только если статья про само происшествие — удар или атаку,
взрыв, аварию, пожар, срыв поставок — и решения (технологии, регламента, оборудования,
контракта) в ней нет. Тогда каждый критерий 0–30. Статья про решение, вызванное инцидентом, —
false; геополитика и цены без самого происшествия — тоже false.

explanation — краткий общий вывод; rationale по критерию — чем обоснован балл. И то и другое
ТОЛЬКО на грамотном русском: без иностранных слов и случайных символов внутри русских слов,
латиница — лишь для общепринятых аббревиатур и имён собственных. Ответ строго по JSON Schema."""

RELEVANCE_INSTRUCTIONS = """Ты фильтр-редактор нефтегазового дайджеста. Суди по СОДЕРЖАНИЮ статьи, НЕ по названию источника.

ПОРЯДОК РЕШЕНИЯ — строго сверху вниз, первое сработавшее правило и есть ответ.

1. ОБЪЕКТ. Событие (в том числе военное, террористическое или аварийное) ПРЯМО поразило,
повредило, захватило или остановило нефтегазовый объект?
Нефтегазовый объект = инфраструктура углеводородов (скважина, платформа, промысел, НПЗ, ГПЗ,
газохимия, трубопровод, НПС/КС, нефте-/газохранилище, экспортный терминал, наливной причал)
| носитель углеводородов (нефтяной танкер, газовоз) | сам транзит нефти/СПГ через чокпоинт,
если поток РЕАЛЬНО нарушен.
ДА → relevant=true. Это правило ПЕРЕВЕШИВАЕТ исключения пункта 3 (война, жертвы).
Низкий приоритет инцидента расставит скоринг — не отбраковывай его здесь.
НЕ объект: судно в проливе лишь потому, что пролив нефтяной; офис, спонсорство, служебный
транспорт и пассажирские рейсы компании, ДТП сотрудника; удар по войскам, территории, городу
или гражданским — даже с мотивом «в ответ на атаки танкеров».

2. ТЕМА. Суть статьи про добычу, бурение, ГРП, заканчивание, КРС, промысловое оборудование,
энергетику промыслов | технологии, ИИ, роботизацию, R&D для нефтегаза | HSE в нефтегазе
| рынок, цены, контракты, M&A нефтегаза и нефтесервиса | санкции, эмбарго, квоты, ОПЕК+,
сдвиги спроса/предложения по нефти, газу, СПГ | транзит и логистику углеводородов
(чокпоинты, трубопроводы, танкерные маршруты, экспортные терминалы)?
ДА → relevant=true.

2б. ТЕМАТИКИ ЗАКАЗЧИКА. Если во входе есть блок `тематики заказчика`, суть статьи
сопоставь с ним. Попадание в тематику — довод ЗА relevant=true, даже если предмет
выглядит «общим IT», «наукой» или «энергетикой»: заказчик заявил эту область своей.
Попадание в тематику НЕ отменяет пункт 3 в части войны, происшествий, спорта и
корпоративной воды — там исключения сильнее. Совпадение по стоп-слову тематики
(`минус:`) — довод ПРОТИВ.
Промышленная автоматизация, АСУ ТП, робототехника и промышленный ИИ считаются
профильными, если применимы к промыслу, НПЗ, трубопроводу или буровой — даже когда
в самой статье нефтегаз прямо не назван.

3. ИСКЛЮЧЕНИЯ → relevant=false, даже если источник энергетический:
боевые действия, военные сводки, удары, обстрелы, БПЛА, жертвы войны, теракты | происшествия,
криминал, ДТП, ЧП без нефтегазового объекта | спорт, культура, развлечения, светская хроника
| общие IT, стартапы, наука, потребительские стандарты | политика, выборы, международные
отношения, макроэкономика и биржевые сводки без конкретного влияния на добычу, поставки или
цены углеводородов | корпоративная вода вне производства (HR-рейтинги, ESG-рейтинги и награды,
благотворительность, назначения, конкурсы, обучение) | атомная энергетика, теплосети,
электросети, возобновляемая энергетика вне связи с нефтегазом.

4. СОМНЕНИЕ. Если связь с нефтегазом приходится притягивать косвенными рассуждениями →
relevant=false. Исключение — пункт 2б: заявленная заказчиком тематика сомнением не
считается.

Калибраторы: атака на танкер, взрыв ГПЗ, захват нефтеналивного терминала → true. «США бьют по
Ирану в ответ на обстрелы танкеров» → false. «Обстрел сухогруза в Ормузском проливе» без срыва
транзита → false. «Блокада Ормуза, транзит нефти встал» → true. «Крушение вертолёта нефтяной
компании», «ESG-рейтинг компании» → false.

reason — одна короткая фраза по-русски по сути события. Ответ строго по JSON Schema."""


TAG_SCHEMA = {
    "name": "article_tag",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["tag_id", "confidence", "rationale"],
        "properties": {
            "tag_id": {"type": "integer"},
            "confidence": {"type": "number"},
            "rationale": {"type": "string"},
        },
    },
}

RELEVANCE_SCHEMA = {
    "name": "article_relevance",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["relevant", "reason"],
        "properties": {
            "relevant": {"type": "boolean"},
            "reason": {"type": "string"},
        },
    },
}

SCORE_SCHEMA = {
    "name": "article_score",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["incident_without_solution", "total_score", "score_label", "explanation", "items"],
        "properties": {
            # Первым: модель решает, инцидент ли это, до баллов по критериям (#54).
            "incident_without_solution": {"type": "boolean"},
            "total_score": {"type": "number"},
            "score_label": {
                "type": "string",
                "enum": ["Низкая", "Средняя", "Выше средней", "Высокая"],
            },
            "explanation": {"type": "string"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["criterion_id", "ai_score", "rationale"],
                    "properties": {
                        "criterion_id": {"type": "integer"},
                        "ai_score": {"type": "number"},
                        "rationale": {"type": "string"},
                    },
                },
            },
        },
    },
}


def tags_scope_block(tags: list[dict], *, per_language: int = 6) -> str:
    """Блок «тематики заказчика» для гейта релевантности.

    До 17.09 теги влияли ТОЛЬКО на классификацию уже отобранного: ни предфильтр, ни
    гейт их не читали. Заказчик этого не знал — 07.09 он прямо спросил, влияют ли
    теги на то, попадёт ли статья в список, и расширял их в расчёте на выборку.

    Ключи берём ПОКВОТНО с каждого языка, а не общим срезом: при общем срезе русские
    ключи (их в справочнике 33-52 на тематику) съедали лимит целиком и до промпта не
    доезжало ни одного английского — проверено прогоном 17.09. Гейт судит и
    англоязычные тексты, и это ровно те источники, ради которых включали зарубежный
    контур.

    Служебный приёмник «Не классифицировано» в блок не попадает: по правилу 2б
    попадание в тематику — довод ЗА, и приёмник делал бы доводом ЗА всё непонятое.

    Стоп-слова идут пометкой `минус:` — довод против, а не жёсткий запрет: одно
    неудачное слово в справочнике иначе молча выкосило бы целую тематику.
    """
    lines: list[str] = []
    for tag in tags:
        name = str(tag.get("name") or "").strip()
        if not name or is_catch_all_tag(tag):
            continue
        parts = [name]
        name_en = str(tag.get("name_en") or "").strip()
        if name_en:
            parts.append(f"EN: {name_en}")
        ru = _clean_values(tag.get("keywords_json"))[:per_language]
        en = _clean_values(tag.get("keywords_en_json"))[:per_language]
        keys = _dedupe_keep_order(ru + en)
        if keys:
            parts.append("ключи: " + ", ".join(keys))
        stop = _dedupe_keep_order(_clean_values(tag.get("negative_keywords_json")))[:6]
        if stop:
            parts.append("минус: " + ", ".join(stop))
        lines.append(" | ".join(parts))
    if not lines:
        return ""
    return "тематики заказчика:\n" + "\n".join(f"- {line}" for line in lines)


CATCH_ALL_TAG_MARKERS = ("не классифицировано", "не определено", "новая тема")


def is_catch_all_tag(tag: dict) -> bool:
    """Служебный тег-приёмник, куда падает непонятое. Тематикой заказчика не является."""
    name = str(tag.get("name") or "").casefold()
    return any(marker in name for marker in CATCH_ALL_TAG_MARKERS)


def _clean_values(values: object) -> list[str]:
    return [str(v).strip() for v in (values or []) if str(v).strip()]  # type: ignore[union-attr]


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def relevance_article(article: dict, client, tags: list[dict] | None = None) -> AIResponse:
    # Гейт судит по СЫРОМУ тексту (title+source+text), БЕЗ AI-сути: суммаризатор
    # обязан притягивать любую статью к нефтегазу, и подача его сути на вход гейта
    # давала самосбывающуюся релевантность (мусор проходил).
    try:
        return client.complete_json(
            RELEVANCE_INSTRUCTIONS,
            _relevance_prompt(article, tags=tags),
            RELEVANCE_SCHEMA,
            max_output_tokens=2500,
            model=config.OPENAI_MODEL,
            reasoning_effort=RELEVANCE_REASONING,
        )
    except AIClientError as exc:
        if "max_output_tokens" not in str(exc):
            raise
        return client.complete_json(
            RELEVANCE_INSTRUCTIONS,
            _relevance_prompt(article, tags=tags, text_limit=1500),
            RELEVANCE_SCHEMA,
            max_output_tokens=2500,
            model=config.OPENAI_MODEL,
            reasoning_effort="minimal",
        )


def tag_article(article: dict, tags: list[dict], client) -> AIResponse:
    tag_lines = []
    for tag in tags:
        path = f"{tag.get('parent_name')} / {tag['name']}" if tag.get("parent_name") else tag["name"]
        tag_lines.append(
            f"{tag['id']}: {path} | EN: {tag.get('name_en') or ''} | "
            f"keywords: {', '.join((tag.get('keywords_en_json') or [])[:12])}"
        )
    rules = tag_rules(tags)
    return client.complete_json(
        TAGGING_INSTRUCTIONS,
        _article_prompt(article) + "\n\navailable_tags:\n" + "\n".join(tag_lines)
        + ("\n\nrules:\n" + "\n".join(f"- {rule}" for rule in rules) if rules else ""),
        TAG_SCHEMA,
        max_output_tokens=output_budget(1000, config.OPENAI_REASONING_EFFORT),
        model=config.OPENAI_MODEL,
        reasoning_effort=config.OPENAI_REASONING_EFFORT,
    )


def tag_rules(tags: list[dict]) -> list[str]:
    """Правила выбора тега под списком тегов — с именами из списка, а не из кода (как правило темы «Карточки», #149).

    Переигровка 03.11 (переезд с gpt-5): новые модели клали новости о ценах на сырьё в тег-приёмник, считая, что тега
    для них нет (gpt-6-luna — 26% статей, у прода 3%). Общая фраза «тема рынка, если такой тег есть» luna не убедила:
    «тега рынка в списке нет» — у 26 из 34 таких статей; поэтому правило называет тег. Нет тега в списке — нет правила."""
    from oiltech_digest import signal_themes

    names = [str(tag.get("name") or "") for tag in tags]
    rules = []
    market = signal_themes.market(names)
    if market:
        rules.append(f"Цены и котировки сырья и их прогнозы, спрос и предложение, квоты и ограничения поставок — тег «{market}».")
    if UNCLASSIFIED_TAG_NAME in names:
        rules.append(f"«{UNCLASSIFIED_TAG_NAME}» — только когда не подходит ни один тег.")
    return rules


def score_article(article: dict, criteria: list[dict], client) -> AIResponse:
    criterion_lines = []
    for criterion in criteria:
        criterion_lines.append(
            f"{criterion['id']}: {criterion['name']} | weight={criterion['weight']} | "
            f"description={criterion.get('description') or ''} | "
            f"keywords={', '.join((criterion.get('keywords_en_json') or [])[:12])}"
        )
    return client.complete_json(
        SCORING_INSTRUCTIONS,
        _article_prompt(article) + "\n\ncriteria:\n" + "\n".join(criterion_lines),
        SCORE_SCHEMA,
        max_output_tokens=1800,
        model=config.OPENAI_MODEL,
        reasoning_effort=SCORE_REASONING,
    )


# Куда падает статья, не совпавшая НИ С ОДНИМ ключевым словом. Раньше — в tags[0],
# то есть в первый тег по порядку: при таксономии D01–D18 это была «Сейсморазведка»,
# при 13 тематиках заказчика стала бы «Геологоразведка». Мусор копился в одном
# направлении и выглядел как обычная классификация.
# Заказчик 13.09 сам предложил решение: «если статья не совпадает ни с одной тематикой,
# она должна попадать в Unclassified / потенциально новая тема, чтобы Discovery Agent
# не был ограничен текущей taxonomy».
# Имя одно на всю систему: repository защищает этот тег от выключения, сид его создаёт,
# классификация на него падает. Три копии строки разъехались бы молча.
UNCLASSIFIED_TAG_NAME = repository.SYSTEM_TAG_UNCLASSIFIED


def _fallback_tag(tags: list[dict]) -> dict:
    """Тег-приёмник, если он заведён; иначе — прежнее поведение (первый по порядку)."""
    for tag in tags:
        if tag.get("name") == UNCLASSIFIED_TAG_NAME:
            return tag
    return tags[0]


def keyword_tag(article: dict, tags: list[dict]) -> dict:
    text = _search_text(article)
    best = {"tag_id": _fallback_tag(tags)["id"], "confidence": 0.15, "matches": 0}
    for tag in tags:
        keywords = (tag.get("keywords_json") or []) + (tag.get("keywords_en_json") or [])
        matches = sum(1 for keyword in keywords if _contains_keyword(text, keyword))
        if matches > best["matches"]:
            best = {"tag_id": tag["id"], "confidence": min(0.9, 0.35 + matches * 0.08), "matches": matches}
    return best


_TAGS_SCOPE_CACHE: dict[str, Any] = {"block": None, "at": 0.0}
_TAGS_SCOPE_TTL_SECONDS = 300


def _tags_scope() -> str:
    """Блок тематик заказчика для гейта, с коротким кэшем.

    Гейт зовётся на КАЖДОЙ статье, а справочник тегов меняется редко и руками, так
    что ходить в базу каждый раз незачем. TTL короткий намеренно: заказчик правит
    тематики на экране и ждёт, что новая выборка поедет по ним, а не после деплоя.
    Сбой чтения не должен ронять гейт — тогда просто судим без тематик, как раньше.
    """
    now = time.monotonic()
    cached = _TAGS_SCOPE_CACHE.get("block")
    if cached is not None and now - float(_TAGS_SCOPE_CACHE.get("at") or 0) < _TAGS_SCOPE_TTL_SECONDS:
        return cached
    try:
        block = tags_scope_block(repository.list_enabled_tags())
    except Exception:  # noqa: BLE001 - тематики это подсказка, а не обязательный вход
        logger.warning("не удалось прочитать тематики для гейта релевантности")
        block = ""
    _TAGS_SCOPE_CACHE["block"] = block
    _TAGS_SCOPE_CACHE["at"] = now
    return block


def _relevance_prompt(article: dict, *, tags: list[dict] | None = None, text_limit: int = 6000) -> str:
    """Вход гейта релевантности — БЕЗ AI-сути (намеренно): только сырые поля статьи,
    чтобы суждение шло по реальному содержанию, а не по подкрученной нефтегаз-сути.

    С 17.09 сюда добавлен блок тематик заказчика: до этого теги влияли только на
    классификацию уже отобранного, и заказчик, расширяя их, не менял выборку вообще.
    Блок идёт в пользовательскую часть, а не в инструкции, чтобы не ломать кэш
    префикса: инструкции у всех статей одни и те же.
    """
    lines = [
        f"title: {article.get('title') or ''}",
        f"source: {article.get('source_name') or ''}",
        f"url: {article.get('url') or ''}",
        f"language: {article.get('language') or 'unknown'}",
        f"published_at: {article.get('published_at') or ''}",
        f"text: {_compact(article.get('raw_text') or '', text_limit)}",
    ]
    # Тематики берём из переданного списка, если он есть, и только иначе идём в базу: на
    # зарубежном воркере БАЗЫ НЕТ, и чтение молча вернуло бы пустой блок. Теги в пакет оценки
    # кандидата кладёт ядро (external_ai.build_source_candidate_evaluate_payload).
    scope = tags_scope_block(tags) if tags else _tags_scope()
    if scope:
        lines.append("")
        lines.append(scope)
    return "\n".join(lines)


def _negative_keyword_block(article: dict, tags: list[dict]) -> str | None:
    """Если текст статьи содержит стоп-слово родительского тега — вернуть причину, иначе None.
    Стоп-слова задаются только у родительских тегов (parent_id IS NULL) — бэклог заказчика #6."""
    text = _search_text(article)
    for tag in tags:
        if tag.get("parent_id"):
            continue
        for keyword in tag.get("negative_keywords_json") or []:
            if _contains_keyword(text, keyword):
                return f"стоп-слово «{keyword}» (тег «{tag.get('name')}»)"
    return None


def _valid_tag_id(value: Any, tags: list[dict]) -> int:
    try:
        tag_id = int(value)
    except (TypeError, ValueError):
        return 0
    return tag_id if any(int(tag["id"]) == tag_id for tag in tags) else 0


def _validate_weights(criteria: list[dict]) -> None:
    if not criteria:
        raise ValueError("Нет активных критериев скоринга. Запустите seed-scoring.")
    total = sum(float(c["weight"]) for c in criteria)
    if round(total, 2) != 100:
        raise ValueError(f"Сумма весов критериев должна быть 100, сейчас {total}")
