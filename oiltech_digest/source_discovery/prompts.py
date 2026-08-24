"""Prompts for the controlled source discovery agent."""

from __future__ import annotations


SEARCH_QUERY_INSTRUCTIONS = """Ты помогаешь искать новые источники для отраслевого нефтегазового дайджеста.

Нужно предложить поисковые запросы для поиска сайтов, пресс-центров, отраслевых медиа,
страниц компаний и исследовательских центров по указанной теме.

Правила:
- запросы должны вести к источникам новостей, а не к одной случайной статье;
- часть запросов дай на английском, часть на русском;
- добавляй слова вроде news, newsroom, press release, technology, oilfield, upstream,
  если они уместны;
- не предлагай слишком общие запросы;
- ответ строго по JSON Schema."""


SEARCH_QUERY_SCHEMA = {
    "name": "source_search_queries",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["queries"],
        "properties": {
            "queries": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
    },
}


SOURCE_RECOMMENDATION_INSTRUCTIONS = """Ты оцениваешь кандидата источника для OilTech Digest.

Смотри только на переданные факты: метрики проверки, примеры материалов, причины
релевантности, теги, краткие сути и оценки. Не придумывай факты и не утверждай,
что источник стабильно полезен, если проверено мало материалов.

Верни действие:
- add: источник явно полезен;
- test_more: данных мало или качество среднее;
- reject: источник шумный или бесполезный;
- human_review: нужна ручная проверка.

reason — коротко по-русски, почему такое решение.
strengths — 1-4 сильные стороны источника по фактам.
risks — 1-4 риска или причины осторожности.
confidence — уверенность от 0 до 1.
Ответ строго по JSON Schema."""


SOURCE_RECOMMENDATION_SCHEMA = {
    "name": "source_candidate_recommendation",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["recommended_action", "reason", "strengths", "risks", "confidence"],
        "properties": {
            "recommended_action": {
                "type": "string",
                "enum": ["add", "test_more", "reject", "human_review"],
            },
            "reason": {"type": "string"},
            "strengths": {
                "type": "array",
                "items": {"type": "string"},
            },
            "risks": {
                "type": "array",
                "items": {"type": "string"},
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
            },
        },
    },
}
