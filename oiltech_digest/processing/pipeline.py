"""Обработка статей ленты: суть и перевод заголовка; ключевые слова и ступень балла критерия, общие с баллами сигнала.

Ленту решает «Отбор» (#130), тему и баллы ставит «Карточка» (#132): гейт ленты, тег и балл статьи сняты 08.11."""

from __future__ import annotations

import json
import math
import re
from typing import Any

import logging

from oiltech_digest import config, expert_rules
from oiltech_digest.db import repository
from oiltech_digest.ingestion import article_fetcher
from oiltech_digest.processing.domain_glossary import enforce_glossary_text, glossary_prompt_block, mixed_script_words
from oiltech_digest.processing.openai_client import (
    AIResponse,
    OfflineAIClient,
    OpenAIResponsesClient,
    output_budget,
)
from oiltech_digest.processing.prompts import (
    EDIT_TITLE_INSTRUCTIONS,
    EDIT_TITLE_SCHEMA,
    SUMMARY_INSTRUCTIONS,
    SUMMARY_SCHEMA,
    TITLE_MAX_CHARS,
    TRANSLATE_INSTRUCTIONS,
    TRANSLATE_SCHEMA,
)


logger = logging.getLogger(__name__)


def make_client(offline: bool = False):
    return OfflineAIClient() if offline else OpenAIResponsesClient()


def process_summaries(limit: int = 20, offline: bool = False) -> dict:
    client = make_client(offline)
    return process_summary_articles(repository.get_articles_needing_summary(limit), client)


def process_summary_articles(articles: list[dict], client) -> dict:
    stats = {"processed": 0, "errors": 0}
    for article in articles:
        try:
            response = summarize_article(article, client)
            repository.upsert_article_card(article["id"], response.data["summary"], response.model)
            _record_run(article, "summary", client, response)
            stats["processed"] += 1
        except Exception as exc:  # noqa: BLE001 - batch should continue
            _record_error(article, "summary", client, exc)
            stats["errors"] += 1
    return stats


def process_translations(limit: int = 20, offline: bool = False) -> dict:
    client = make_client(offline)
    return process_translation_articles(repository.get_articles_needing_title_ru(limit), client)


def process_translation_articles(articles: list[dict], client) -> dict:
    """Отдельная стадия перевода заголовков: проставляет title_ru статьям без него.

    Русские заголовки переводить не нужно: берём их как есть (без AI-вызова). AI
    дёргается ТОЛЬКО для иностранных заголовков. Так бэкфилл по всей базе дешёвый:
    платим лишь за реальный перевод. `ai` в статистике: сколько раз ходили в модель."""
    stats = {"processed": 0, "ai": 0, "errors": 0}
    for article in articles:
        try:
            title_ru, response = title_ru_for_article(article, client)
            if title_ru is None:
                stats["processed"] += 1
                continue
            repository.set_article_title_ru(article["id"], title_ru)
            if response is not None:
                _record_run(article, "translation", client, response)
                stats["ai"] += 1
            stats["processed"] += 1
        except Exception as exc:  # noqa: BLE001 - batch should continue
            _record_error(article, "translation", client, exc)
            stats["errors"] += 1
    return stats


def process_full(limit: int = 20, offline: bool = False) -> dict:
    """Запустить по-статейный конвейер на статьях с незавершённым AI pipeline."""
    client = make_client(offline)
    return process_pipeline_articles(repository.get_articles_needing_pipeline(limit), client)


def process_pipeline_articles(articles: list[dict], client, fetch_full: bool = True) -> dict:
    """Конвейер ленты по одной статье целиком: полный текст → суть → перевод заголовка.

    Каждая статья проходит все этапы до конца, прежде чем берётся следующая: готовые карточки
    появляются по мере обработки. «Брать или нет» решает только «Отбор» (#130): статью, которую он
    держит (отклонил или ещё не решил), конвейер не берёт, отклонённую до него гейтом тоже:
    гейта, который мог бы передумать, с 08.11 нет. Тег и балл статьи не считаются: тему и баллы
    ставит «Карточка» у сигнала статьи (#132).
    """
    stats = {"processed": 0, "fulltext": 0, "summary": 0, "translated": 0, "rejected": 0, "errors": 0}
    for article in [item for item in articles if not repository.held_by_otbor(item)]:
        stats["processed"] += 1
        if repository.article_rejected(article):
            stats["rejected"] += 1
            continue
        try:
            # 1. Полный текст из HTML, если RSS отдал только сниппет.
            if fetch_full and article.get("text_truncated") and article.get("url"):
                result = article_fetcher.fetch_article_text(article)
                if result.status == "ok":
                    written = repository.update_article_full_text(
                        int(article["id"]), raw_text=result.text, text_truncated=False,
                        status="ok", method=result.method, error=None,
                    )
                    article["raw_text"] = written or result.text
                    article["text_truncated"] = False
                    stats["fulltext"] += 1

            # 2. Суть.
            if not article.get("summary"):
                summary_resp = summarize_article(article, client)
                repository.upsert_article_card(article["id"], summary_resp.data["summary"], summary_resp.model)
                _record_run(article, "summary", client, summary_resp)
                article["summary"] = summary_resp.data["summary"]
                stats["summary"] += 1

            # 3. Перевод заголовка, отдельная стадия: иностранный переводится, русский не по правилам эксперта правится.
            if not article.get("title_ru"):
                title_ru, translate_resp = title_ru_for_article(article, client, edit_russian=True)
                if title_ru is not None:
                    repository.set_article_title_ru(article["id"], title_ru)
                    if translate_resp is not None:
                        _record_run(article, "translation", client, translate_resp)
                        stats["translated"] += 1
        except Exception as exc:  # noqa: BLE001 - batch should continue
            _record_error(article, "pipeline", client, exc)
            stats["errors"] += 1
    return stats


# Шаги «Сути и перевода» в правилах эксперта (#190): вызов сути и вызов заголовка; термины: в обоих.
SUMMARY_RULE_STEPS, TITLE_RULE_STEPS = ("summary", "terms"), ("title", "terms")


def with_expert_rules(instructions: str, article: dict, steps: tuple[str, ...]) -> str:
    """Инструкция вызова с правилами эксперта своего шага и категории публикации (#190): блок правил кладёт в
    публикацию воркер (article["expert_rules"]); нет его или правил: инструкция прежняя."""
    return expert_rules.with_rules(instructions, article.get("expert_rules"), steps=steps,
                                   category=article.get("signal_category"))


def summarize_article(article: dict, client) -> AIResponse:
    prompt = _article_prompt(article, otbor=True)
    instructions = with_expert_rules(SUMMARY_INSTRUCTIONS, article, SUMMARY_RULE_STEPS)
    return _complete_one_script(
        lambda extra: client.complete_json(
            instructions,
            prompt + extra,
            SUMMARY_SCHEMA,
            max_output_tokens=output_budget(1200, config.OPENAI_SUMMARY_REASONING),
            model=config.OPENAI_SUMMARY_MODEL,
            reasoning_effort=config.OPENAI_SUMMARY_REASONING,
        ),
        "summary",
        article,
    )


def _complete_one_script(call, field: str, article: dict) -> AIResponse:
    """Вызов модели и словарь; слово из двух алфавитов: одна повторная попытка.

    Двойники и склейку словарь чинит сам (normalize_scripts), а полуперевод
    («управляego», «наshore», «электроэнergyю»: 56 статей на 25.09), нет: такой
    ответ переспрашиваем, назвав слова. Берём вариант, где смешанных слов меньше;
    токены обоих вызовов: в один итог, чтобы расход в ai_processing_runs сошёлся.
    """
    response = call("")
    text = enforce_glossary_text(str(response.data.get(field) or ""), article)
    mixed = mixed_script_words(text)
    data, input_tokens, output_tokens = response.data, response.input_tokens, response.output_tokens
    if mixed:
        try:
            retry = call(_mixed_script_note(mixed))
        except Exception:  # noqa: BLE001 - сбой повтора не должен губить оплаченный первый ответ
            logger.warning("повтор при смешанном алфавите не удался, оставляю первый ответ", exc_info=True)
        else:
            input_tokens += retry.input_tokens
            output_tokens += retry.output_tokens
            retry_text = enforce_glossary_text(str(retry.data.get(field) or ""), article)
            if retry_text and len(mixed_script_words(retry_text)) < len(mixed):
                data, text = retry.data, retry_text
    return AIResponse(
        data={**data, field: text},
        model=response.model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _mixed_script_note(words: list[str]) -> str:
    listed = ", ".join(f"«{word}»" for word in words[:5])
    return (
        "\n\nВ прошлом ответе были слова, где латиница смешана с кириллицей: "
        f"{listed}. Ответь заново: каждое слово — целиком по-русски, имена собственные "
        "и аббревиатуры — целиком латиницей."
    )


def translate_article(article: dict, client) -> AIResponse:
    """AI-перевод заголовка на русский. Отдельная стадия (раньше был частью summary).
    Модель/effort, собственные (обычно дешёвые: ответ короткий), фолбэк на основные."""
    return _title_call(article, client, TRANSLATE_INSTRUCTIONS, TRANSLATE_SCHEMA)


def edit_title_article(article: dict, client) -> AIResponse:
    """Правка русского заголовка по правилам эксперта (#150, решение владельца 03.11): вход и модель: перевода."""
    return _title_call(article, client, EDIT_TITLE_INSTRUCTIONS, EDIT_TITLE_SCHEMA)


def _title_call(article: dict, client, instructions: str, schema: dict) -> AIResponse:
    """Вызов модели заголовка (перевод или правка): вход _title_prompt, модель и уровень перевода, словарь; правила
    эксперта шага «заголовок» (#190)."""
    prompt = _title_prompt(article)
    instructions = with_expert_rules(instructions, article, TITLE_RULE_STEPS)
    return _complete_one_script(
        lambda extra: client.complete_json(
            instructions,
            prompt + extra,
            schema,
            max_output_tokens=output_budget(300, config.OPENAI_TRANSLATE_REASONING),
            model=config.OPENAI_TRANSLATE_MODEL,
            reasoning_effort=config.OPENAI_TRANSLATE_REASONING,
        ),
        "title_ru",
        article,
    )


def title_ru_for_article(article: dict, client, *, edit_russian: bool = False) -> tuple[str | None, AIResponse | None]:
    """Вернуть (title_ru, ai_response). Иностранный заголовок переводим через AI. Русский не переводим
    (ai_response=None); с edit_russian, у принятой публикации, правим по правилам эксперта, если он
    им не отвечает (title_follows_rules). Правка не берётся, остаётся заголовок издателя, если в ней число, которого
    нет в заголовке и тексте публикации, или имя, которого нет во входе модели (_title_prompt: заголовок, источник,
    начало текста, термины словаря). Пустой заголовок → (None, None)."""
    title = (article.get("title") or "").strip()
    if not title:
        return None, None
    if not _needs_translation(title):
        if not edit_russian or title_follows_rules(title, article.get("source_name")):
            return title[:200], None
        response = edit_title_article(article, client)
        edited = (response.data.get("title_ru") or "").strip()
        names = _new_names(edited, _title_prompt(article))
        if names:
            logger.info("правка заголовка статьи %s не взята, имён нет во входе модели %s: %s",
                        article.get("id"), names, edited)
        taken = (edited and not names
                 and _numbers(edited) <= _numbers(f"{title} {article.get('raw_text') or article.get('summary') or ''}"))
        return (edited if taken else title)[:200], response
    response = translate_article(article, client)
    translated = (response.data.get("title_ru") or title).strip()
    return (translated or title)[:200], response


# Правила заголовка эксперта (#150), которые видно кодом; предел длины: prompts.TITLE_MAX_CHARS, тот же, что в
# инструкциях. Русский заголовок, который им отвечает, модель не правит (решение владельца 03.11): называет ли он
# событие, а не публикацию, код не видит.
# Тире между частями (кроме тире диапазона между числами: «2027-2029»), дефис с пробелами, черта, скобки.
_TITLE_PARTS = re.compile(r"(?<!\d)[–—]|[–—](?!\d)|\s-\s|[|()\[\]]")
# Издание доменом: «Вести.ru», «Нефтегаз.РУ».
_TITLE_DOMAIN = re.compile(r"\w\.(?:ru|рф|ру|su|com|net|org|info|io|media|tv|kz|by|uz)\b", re.IGNORECASE)
# Обрыв: многоточие или двоеточие в конце.
_TITLE_CUT = re.compile(r"(?:…|\.\.\.|:)$")
# Эмодзи (пиктограммы, флаги, знак варианта) и хэштег; «№» и «°» эмодзи не считаются.
_TITLE_EMOJI = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]|(?:^|\s)#\w")


_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers(text: str) -> set[str]:
    return {value.replace(",", ".") for value in _NUMBER.findall(text)}


# Слово из букв любого алфавита; дефис делит слово («ИТ-патентов»: «ИТ» и «патентов»), цифры сверяет _numbers.
_WORD = re.compile(r"[^\W\d_]+")


def _new_names(edited: str, seen: str) -> list[str]:
    """Слова правки с заглавной буквы, которых модель не видела: имя или название, которого во входе нет (06.11, ФИПС:
    «мастер-класс Олега Ена» стало «Олег Ен провёл»). Падеж менять можно: слову хватает общей основы с каким-нибудь
    словом входа, у слова до четырёх букв это все буквы, кроме последней («Ухта» → «Ухте»), у длинного все, кроме двух
    последних, но не меньше четырёх («Ставропольского» → «Ставропольский»). Слово из двух букв должно стоять во входе
    как есть: так ловятся «Ен» и сокращение «РФ», которого в публикации нет.

    Первое слово не проверяется: с заглавной там и обычное слово в новой форме («Создана», «Началась»), на правках прода
    06.11 его проверка отбросила бы только верные правки. Цена: искажённое имя первым словом и усечённое длинное имя
    («Гарсиа» → «Гарси») код не видит."""
    known = {word.casefold() for word in _WORD.findall(seen)}
    found = []
    for index, word in enumerate(_WORD.findall(edited)):
        folded = word.casefold()
        if index == 0 or not word[:1].isupper() or folded in known:
            continue
        stem = folded[:len(folded) - 1 if len(folded) <= 4 else max(4, len(folded) - 2)]
        if len(folded) <= 2 or not any(other.startswith(stem) for other in known):
            found.append(word)
    return found


def title_follows_rules(title: str, source_name: str | None = None) -> bool:
    """Отвечает ли заголовок правилам эксперта, которые видно кодом: не длиннее TITLE_MAX_CHARS знаков, без тире,
    черты и скобок, без издания (доменом или именем источника в конце: издатель: автор события, его имя в начале
    и в середине не помеха), без обрыва, эмодзи и хэштегов."""
    text = " ".join(title.split())
    source = " ".join(re.sub(r"\([^)]*\)", " ", source_name or "").split()).casefold()
    return not (
        len(text) > TITLE_MAX_CHARS
        or _TITLE_PARTS.search(text)
        or _TITLE_DOMAIN.search(text)
        or _TITLE_CUT.search(text)
        or _TITLE_EMOJI.search(text)
        or (len(source) >= 3 and text.rstrip(" .:!?»\"'").casefold().endswith(source))
    )


def _needs_translation(title: str) -> bool:
    """Заголовок считаем требующим перевода, если кириллицы в нём меньше половины букв."""
    letters = [ch for ch in title if ch.isalpha()]
    if not letters:
        return False
    cyrillic = sum(1 for ch in letters if "Ѐ" <= ch <= "ӿ")
    return cyrillic / len(letters) < 0.5


# Блендинг балла критерия. Оценка модели (ai_score), ОСНОВНОЙ сигнал и НИЖНЯЯ граница:
# модель читает суть статьи. Ключевые слова (keyword_score) могут только ПОДНЯТЬ балл, но
# никогда не топят: узкие списки ключей (нужно 3+ точных совпадения из ~10 на критерий)
# почти всегда дают низкий keyword_score, и в прежней схеме (0.35·kw + 0.65·ai) он тянул
# итог вниз: статья с ai=80 давала total ~57 (< порога 65), а 70+ требовал ai≈100: поэтому
# до 65+ дотягивали единицы (инцидент 2026-06). Теперь:
#   blended = SCORE_KEYWORD_WEIGHT·kw + SCORE_AI_WEIGHT·ai
#   final   = max(ai, blended)   ← keyword добавляет вес только когда совпадений много (kw>ai)
# Веса вынесены в константы: подстраивай после пилота на проде.
SCORE_KEYWORD_WEIGHT = 0.2
SCORE_AI_WEIGHT = 0.8


def keyword_score_for_criterion(article: dict, criterion: dict) -> float:
    text = _search_text(article)
    keywords = (criterion.get("keywords_json") or []) + (criterion.get("keywords_en_json") or [])
    if not keywords:
        return 0
    matches = sum(1 for keyword in keywords if _contains_keyword(text, keyword))
    return round(_clamp(matches / max(3, math.sqrt(len(keywords))) * 100, 0, 100), 2)


def score_label(score: float) -> str:
    if score >= 80:
        return "Высокая"
    if score >= 65:
        return "Выше средней"
    if score >= 40:
        return "Средняя"
    return "Низкая"


# Сколько знаков текста статьи видит модель сути. По нему же fulltext_recompute решает,
# стоит ли пересчитывать суть после замены обрывка полным текстом.
ARTICLE_PROMPT_TEXT_CHARS = 6000


# Поля «Отбора» во входе «Суть и перевод» (#131, спека #128: передача между агентами): категория, связь с
# нефтесервисом и применение: справочные значения, решения и обоснования «Отбора» нет. Ключ строки во входе: поле
# публикации. У статьи, которую решил гейт, полей нет, и вход прежний.
OTBOR_FIELDS = {"category": "signal_category", "oilfield_relevance": "oilfield_relevance",
                "oilfield_application": "oilfield_application"}


def _otbor_lines(article: dict) -> list[str]:
    return [f"{name}: {article[key]}" for name, key in OTBOR_FIELDS.items() if article.get(key)]


def _article_prompt(article: dict, *, otbor: bool = False) -> str:
    """Вход сути по публикации; otbor: с полями «Отбора» (только «Суть и перевод», #131)."""
    base = "\n".join(
        [
            f"title: {article.get('title') or ''}",
            f"source: {article.get('source_name') or ''}",
            f"url: {article.get('url') or ''}",
            f"language: {article.get('language') or 'unknown'}",
            f"published_at: {article.get('published_at') or ''}",
            *(_otbor_lines(article) if otbor else []),
            f"summary: {article.get('summary') or ''}",
            f"text: {_compact(article.get('raw_text') or '', ARTICLE_PROMPT_TEXT_CHARS)}",
        ]
    )
    glossary = glossary_prompt_block(article)
    return f"{base}\n\n{glossary}" if glossary else base


def _title_prompt(article: dict) -> str:
    """Вход переводчика: только заголовок и контекст источника (дёшево, без полного текста); поля «Отбора», если
    они есть, как у сути (#131)."""
    base = "\n".join(
        [
            f"title: {article.get('title') or ''}",
            f"source: {article.get('source_name') or ''}",
            f"language: {article.get('language') or 'unknown'}",
            *_otbor_lines(article),
            f"context: {_compact(article.get('raw_text') or article.get('summary') or '', 900)}",
        ]
    )
    glossary = glossary_prompt_block(article, limit=8)
    return f"{base}\n\n{glossary}" if glossary else base


def _search_text(article: dict) -> str:
    return " ".join(
        str(article.get(field) or "")
        for field in ("title", "summary", "raw_text", "source_category")
    ).lower()


def _contains_keyword(text: str, keyword: str) -> bool:
    keyword = (keyword or "").strip().lower()
    if not keyword:
        return False
    if len(keyword) <= 4 or re.search(r"\s", keyword):
        return keyword in text
    return re.search(rf"\b{re.escape(keyword)}\b", text, flags=re.IGNORECASE) is not None


def _compact(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _record_run(article: dict, stage: str, client, response: AIResponse) -> None:
    repository.insert_ai_run(
        {
            "article_id": article.get("id"),
            "stage": stage,
            "provider": getattr(client, "provider", "openai"),
            "model": response.model,
            "language": article.get("language"),
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "total_tokens": response.total_tokens,
            "cost_usd": response.cost_usd,
            "status": "ok",
            "error_message": None,
        }
    )


def _record_error(article: dict, stage: str, client, exc: Exception) -> None:
    repository.insert_ai_run(
        {
            "article_id": article.get("id"),
            "stage": stage,
            "provider": getattr(client, "provider", "openai"),
            "model": getattr(client, "model", None),
            "language": article.get("language"),
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0,
            "status": "error",
            "error_message": str(exc)[:1000],
        }
    )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def dumps_for_debug(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)
