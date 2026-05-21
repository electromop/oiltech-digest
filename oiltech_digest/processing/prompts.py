"""Prompts and JSON schemas for article processing."""

from __future__ import annotations


SUMMARY_INSTRUCTIONS = """Ты отраслевой аналитик нефтесервисных технологий.
Составь краткую суть статьи для внутреннего технологического дайджеста.
Пиши по-русски, без маркетинга, 2-3 предложения. Отрази: что произошло,
почему это важно для нефтесервиса/добычи и есть ли практический эффект.
Не добавляй факты, которых нет в статье."""

TAGGING_INSTRUCTIONS = """Ты классифицируешь нефтесервисные новости.
Выбери один самый подходящий тег из списка. Если статья нерелевантна
нефтесервису, выбери ближайший верхнеуровневый тег с низкой уверенностью.
Ответ строго по JSON Schema."""

SCORING_INSTRUCTIONS = """Ты оцениваешь нефтесервисную новость для внутреннего
дайджеста. Оцени каждый критерий от 0 до 100 с учётом смысла статьи,
ключевых слов, источника и применимости. Итоговая оценка должна быть
взвешенной и объяснимой. Ответ строго по JSON Schema."""


SUMMARY_SCHEMA = {
    "name": "article_summary",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary"],
        "properties": {
            "summary": {"type": "string"},
        },
    },
}

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

SCORE_SCHEMA = {
    "name": "article_score",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["total_score", "score_label", "explanation", "items"],
        "properties": {
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
