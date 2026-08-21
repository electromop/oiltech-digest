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

Смотри только на переданные факты: сколько материалов проверено, сколько релевантных,
какая средняя оценка, сколько шума и дублей. Не придумывай факты.

Верни действие:
- add: источник явно полезен;
- test_more: данных мало или качество среднее;
- reject: источник шумный или бесполезный;
- human_review: нужна ручная проверка.

reason — коротко по-русски, почему такое решение. Ответ строго по JSON Schema."""


SOURCE_RECOMMENDATION_SCHEMA = {
    "name": "source_candidate_recommendation",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["recommended_action", "reason"],
        "properties": {
            "recommended_action": {
                "type": "string",
                "enum": ["add", "test_more", "reject", "human_review"],
            },
            "reason": {"type": "string"},
        },
    },
}

