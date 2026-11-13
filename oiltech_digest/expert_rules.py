"""Правила эксперта (#190, спека #187, ADR 0007): надстройка в базе поверх инструкций агентов в коде.

Здесь то, что одинаково понимают ядро и воркер NL без базы: шесть агентов и их шаги, проверка правила, блок
«Правила эксперта, версия N» во входе агента. Хранение, версии набора и откат: db/expert_rules_store.py.

Ядро кладёт в задачу агента блок его действующего набора (expert_rules_store.payload_block): номер версии, правила
с шагом, категорией и темой поиска. Воркер дописывает к инструкции только правила своего шага и категории
(with_rules); у «Отбора» и «Дедупа» категорию решает сам агент или её у находки ещё нет, поэтому они видят все правила
с пометкой категории; поиск получает подсказки только своей темы (search_hints). Правил нет: инструкция прежняя слово
в слово, и задаче хватает прежнего контракта воркера (contract.required_contract).

Правило поиска радара: только подсказки по теме, без свободного текста (граница О-7 от 03.11): список коротких
поисковых терминов через «;», без ссылок, сайтов, запретов и указаний; иначе правило не пишется.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class Agent:
    label: str
    steps: dict[str, str]


# Шаги агентов по спеке #187 (раздел «Правила эксперта»).
AGENTS: dict[str, Agent] = {
    "otbor": Agent("«Отбор»", {"industry": "отрасль", "occasion": "повод", "relevance": "связь",
                              "decision": "решение и причина", "category": "категория"}),
    "summary": Agent("«Суть и перевод»", {"summary": "суть", "title": "заголовок", "terms": "термины"}),
    "card": Agent("«Карточка»", {"theme": "тема", "event_date": "дата события", "participants": "компании и страны",
                                "event_type": "тип события", "region": "регион",
                                "technology_class": "класс технологии", "maturity": "стадия"}),
    "scores": Agent("Баллы", {"criteria": "по критериям"}),
    "dedup": Agent("«Дедуп»", {"same_event": "одно ли событие"}),
    "search": Agent("Поиск радара", {"topics": "подсказки по темам"}),
}
SEARCH = "search"
# Не больше стольких действующих правил у агента (решение 8 гриля).
LIMIT = 20
CATEGORIES = {"business": "бизнес", "technology": "технология"}
# Правило короткое: в среднем около 25 токенов (расчёт 12.11); потолок длины текста.
TEXT_LIMIT = 400
# Подсказки поиска: до стольких терминов, каждый до стольких слов и знаков.
SEARCH_TERMS, TERM_WORDS, TERM_CHARS = 5, 6, 60

_TERM_RE = re.compile(r"[\w][\w +./-]*", re.UNICODE)
_DOMAIN_RE = re.compile(r"(?:://|www\.|\w\.(?:ru|рф|su|com|net|org|info|io|gov|edu|uk|de|kz|by)\b)", re.IGNORECASE)
# Слова запрета и указания: подсказка поиска только добавляет, а не ограничивает и не командует.
_COMMAND_WORDS = frozenset({
    "не", "нет", "кроме", "только", "никогда", "всегда", "без", "исключая", "исключить", "игнорировать", "запрещено",
    "нельзя", "сайт", "сайты", "not", "only", "except", "never", "always", "without", "ignore", "exclude", "site",
})


class RuleProblem(ValueError):
    """Правило не годится: причина словами для журнала и ответа администратору."""


def agent_label(agent: str) -> str:
    return AGENTS[agent].label if agent in AGENTS else agent


def step_label(agent: str, step: str) -> str:
    return AGENTS[agent].steps.get(step, step) if agent in AGENTS else step


def checked_rule(agent: Any, step: Any, category: Any, topic: Any, text: Any, *,
                 topics: Iterable[str]) -> dict[str, Any]:
    """Правило, приведённое к виду хранения: агент и шаг из списка, категория (business, technology или нет), тема у
    поиска из тем радара, текст одной строкой. RuleProblem: что не так."""
    agent, step = str(agent or "").strip(), str(step or "").strip()
    if agent not in AGENTS:
        raise RuleProblem(f"агент «{agent[:40]}» не из списка")
    if step not in AGENTS[agent].steps:
        raise RuleProblem(f"шаг «{step[:40]}» не из шагов агента {AGENTS[agent].label}")
    category = category_code(category)
    words = " ".join(str(text or "").split())
    if agent == SEARCH:
        if category:
            raise RuleProblem("у правил поиска нет категории: подсказки только по темам")
        topic = " ".join(str(topic or "").split())
        if topic not in set(topics):
            raise RuleProblem(f"тема поиска «{topic[:60]}» не из тем радара")
        return {"agent": agent, "step": step, "category": None, "topic": topic, "text": "; ".join(search_terms(words))}
    if not words:
        raise RuleProblem("пустое правило")
    if len(words) > TEXT_LIMIT:
        raise RuleProblem(f"правило длиннее {TEXT_LIMIT} знаков")
    return {"agent": agent, "step": step, "category": category, "topic": None, "text": words}


def category_code(value: Any) -> str | None:
    """Категория правила: business, technology или без категории (пусто). RuleProblem: другое значение."""
    text = " ".join(str(value or "").split()).casefold()
    if text in ("", "нет", "любая", "без категории", "none", "any"):
        return None
    for code, label in CATEGORIES.items():
        if text in (code, label):
            return code
    raise RuleProblem("категория: бизнес, технология или без категории")


def category_of(value: Any) -> str | None:
    """Категория сигнала (код или подпись во входе агента) → категория правила; другое или «не определено»: None."""
    text = " ".join(str(value or "").split()).casefold()
    return next((code for code, label in CATEGORIES.items() if text in (code, label)), None)


def search_terms(text: str) -> list[str]:
    """Подсказка поиска: термины через «;», каждый: несколько слов без ссылок, сайтов, запретов и указаний.
    RuleProblem: свободный текст (граница О-7)."""
    terms = [" ".join(part.split()) for part in str(text or "").split(";") if part.strip()]
    if not 1 <= len(terms) <= SEARCH_TERMS:
        raise RuleProblem(f"правило поиска: от 1 до {SEARCH_TERMS} поисковых терминов через «;», без свободного текста")
    for term in terms:
        words = term.split()
        if (len(term) > TERM_CHARS or len(words) > TERM_WORDS or not _TERM_RE.fullmatch(term)
                or _DOMAIN_RE.search(term) or any(word.casefold() in _COMMAND_WORDS for word in words)):
            raise RuleProblem(f"правило поиска: «{term[:60]}» не поисковый термин (до {TERM_WORDS} слов, без ссылок, "
                              "сайтов, запретов и указаний): свободный текст в правила поиска не пишется")
    return list(dict.fromkeys(terms))


# --- Блок во входе агента (ядро собирает, воркер читает) ------------------------------------------


def block(agent: str, version: int, rules: list[dict[str, Any]], *, priority: bool = True) -> dict[str, Any]:
    """Блок задачи агента: действующий набор правил (версия и правила с шагом, категорией и темой поиска)."""
    return {"agent": agent, "version": int(version), "priority": bool(priority),
            "rules": [{key: rule.get(key) for key in ("id", "step", "category", "topic", "text")} for rule in rules]}


def version(rules_block: Any) -> int:
    """Номер версии набора в блоке; нет блока или он битый: 0 (правил нет)."""
    try:
        return max(0, int((rules_block or {}).get("version") or 0)) if isinstance(rules_block, dict) else 0
    except (TypeError, ValueError):
        return 0


def has_rules(value: Any) -> bool:
    """В задаче есть хоть одно правило: блок агента или блоки по агентам (прогон радара: поиск и «Дедуп»)."""
    if not isinstance(value, dict):
        return False
    if "rules" in value:
        return bool(value.get("rules"))
    return any(has_rules(one) for one in value.values())


def select(rules_block: Any, *, steps: Iterable[str] | None = None, category: str | None = None,
           topic: str | None = None, every_category: bool = False) -> list[dict[str, Any]]:
    """Правила блока для вызова: своего шага (steps; None: все шаги агента), своей категории (правило без категории
    действует всегда; every_category: все, с пометкой категории) и, у поиска, своей темы."""
    if not isinstance(rules_block, dict):
        return []
    wanted = None if steps is None else set(steps)
    found = []
    for rule in rules_block.get("rules") or []:
        if not isinstance(rule, dict) or not str(rule.get("text") or "").strip():
            continue
        if wanted is not None and rule.get("step") not in wanted:
            continue
        if topic is not None and rule.get("topic") != topic:
            continue
        if rule.get("category") and not every_category and rule.get("category") != category:
            continue
        found.append(rule)
    return found


def rules_text(rules_block: Any, *, steps: Iterable[str] | None = None, category: str | None = None,
               every_category: bool = False) -> str:
    """Блок «Правила эксперта, версия N» для инструкции агента; правил своего шага и категории нет: пусто."""
    rules = select(rules_block, steps=steps, category=category, every_category=every_category)
    if not rules:
        return ""
    agent = str(rules_block.get("agent") or "")
    order = ("При противоречии правило эксперта главнее основной инструкции выше." if rules_block.get("priority", True)
             else "При противоречии действует основная инструкция выше.")
    lines = [f"Правила эксперта, версия {version(rules_block)}. Их согласовал администратор платформы по обратной связи "
             f"экспертов. {order}"]
    for number, rule in enumerate(rules, 1):
        where = step_label(agent, str(rule.get("step") or ""))
        if rule.get("category"):
            where += f"; только для категории «{CATEGORIES.get(rule['category'], rule['category'])}»"
        lines.append(f"{number}. [{where}] {' '.join(str(rule['text']).split())}")
    return "\n".join(lines)


def with_rules(instructions: str, rules_block: Any, **filters: Any) -> str:
    """Инструкция агента с блоком правил своего шага и категории; правил нет: инструкция как есть."""
    text = rules_text(rules_block, **filters)
    return f"{instructions}\n\n{text}" if text else instructions


def search_hints(rules_block: Any, topic: str) -> str:
    """Подсказки эксперта поиску по теме: только добавляют, что искать ещё (граница О-7); нет: пусто."""
    rules = select(rules_block, topic=topic)
    terms = list(dict.fromkeys(term for rule in rules for term in str(rule["text"]).split("; ") if term.strip()))
    if not terms:
        return ""
    return (f"Подсказки эксперта по теме (правила эксперта, версия {version(rules_block)}): ищи также материалы про: "
            + "; ".join(terms) + ".")
