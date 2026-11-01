"""Тема сигнала у «Карточки» (#132, спека #128): 13 тематик заказчика и темы-кандидаты по ключам.

Тему ставит основной вызов «Карточки» по содержанию публикации, одну из 13 корневых тематик заказчика (теги без
служебного приёмника, как у радара: signal_discovery.topics_from_tags). Кандидатов ядро считает бесплатно, по ключам
тематики и её подтегов (то же правило совпадения, что у темы радара: signal_discovery._content_theme): модель получает
имена всех 13 и три кандидата, а выбирает сама.
"""

from __future__ import annotations

from typing import Any

# Сколько тем-кандидатов по ключам получает модель (спека #128).
CANDIDATES = 3


def names(tags: list[dict[str, Any]]) -> list[str]:
    """Имена 13 тематик заказчика — корневые теги без служебного приёмника, по порядку справочника тегов."""
    from oiltech_digest.signal_discovery import topics_from_tags

    return [topic["name"] for topic in topics_from_tags(tags)]


def candidates(text: str, tags: list[dict[str, Any]]) -> list[str]:
    """До трёх тематик, чьи ключи (свои и подтегов, русские и английские) нашлись в тексте, — больше совпадений
    выше, при равенстве по порядку справочника. Совпадение — целым словом, как у темы радара."""
    from oiltech_digest.signal_discovery import (_contains_negative_keyword, _dedupe, _flatten_tag_values,
                                                 _norm_match_text, _select_topic_tags)

    normalized = _norm_match_text(text)
    hits = []
    for order, name in enumerate(names(tags)):
        selected = _select_topic_tags(name, tags)
        keywords = _dedupe([*_flatten_tag_values(selected, "keywords_json"),
                            *_flatten_tag_values(selected, "keywords_en_json")])
        found = sum(1 for keyword in keywords if _contains_negative_keyword(normalized, keyword))
        if found:
            hits.append((-found, order, name))
    return [name for _, _, name in sorted(hits)[:CANDIDATES]]
