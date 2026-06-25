"""Article processing services: summary, tagging, scoring and cost reports."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from oiltech_digest import config
from oiltech_digest.db import repository
from oiltech_digest.ingestion import article_fetcher
from oiltech_digest.processing.openai_client import AIClientError, AIResponse, OfflineAIClient, OpenAIResponsesClient
from oiltech_digest.processing.prompts import (
    RELEVANCE_INSTRUCTIONS,
    RELEVANCE_SCHEMA,
    SCORING_INSTRUCTIONS,
    SCORE_SCHEMA,
    SUMMARY_INSTRUCTIONS,
    SUMMARY_SCHEMA,
    TAG_SCHEMA,
    TAGGING_INSTRUCTIONS,
)


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
            repository.upsert_article_card(article["id"], response.data["summary"], response.model, response.data.get("title_ru"))
            _record_run(article, "summary", client, response)
            stats["processed"] += 1
        except Exception as exc:  # noqa: BLE001 - batch should continue
            _record_error(article, "summary", client, exc)
            stats["errors"] += 1
    return stats


def process_relevance(limit: int = 20, offline: bool = False) -> dict:
    client = make_client(offline)
    return process_relevance_articles(repository.get_articles_needing_relevance(limit), client)


def process_relevance_articles(articles: list[dict], client) -> dict:
    """AI-фильтр: помечает статьи как релевантные/нерелевантные нефтесервису.
    Нерелевантные получают status='rejected' и дальше не тегируются/не скорятся.
    Перед AI-вызовом отсекаем статьи со стоп-словами родительских тегов (бэклог #6)."""
    tags = repository.list_enabled_tags()
    stats = {"processed": 0, "relevant": 0, "rejected": 0, "errors": 0}
    for article in articles:
        try:
            blocked_reason = _negative_keyword_block(article, tags)
            if blocked_reason:
                repository.set_article_relevance(article["id"], False, blocked_reason, "negative-keyword")
                stats["processed"] += 1
                stats["rejected"] += 1
                continue
            response = relevance_article(article, client)
            relevant = bool(response.data.get("relevant"))
            repository.set_article_relevance(
                article["id"], relevant, response.data.get("reason"), response.model
            )
            _record_run(article, "relevance", client, response)
            stats["processed"] += 1
            stats["relevant" if relevant else "rejected"] += 1
        except Exception as exc:  # noqa: BLE001 - batch should continue
            _record_error(article, "relevance", client, exc)
            stats["errors"] += 1
    return stats


def process_tags(limit: int = 20, offline: bool = False) -> dict:
    client = make_client(offline)
    return process_tag_articles(repository.get_articles_needing_tags(limit), client)


def process_tag_articles(articles: list[dict], client) -> dict:
    tags = repository.list_enabled_tags()
    stats = {"processed": 0, "errors": 0}
    if not tags:
        raise ValueError("Нет активных тегов. Запустите seed-tags.")
    for article in articles:
        try:
            response = tag_article(article, tags, client)
            tag_id = _valid_tag_id(response.data.get("tag_id"), tags)
            if tag_id == 0:
                tag_id = keyword_tag(article, tags)["tag_id"]
            repository.upsert_article_tag(
                article["id"],
                tag_id,
                _clamp(float(response.data.get("confidence") or 0), 0, 1),
                response.data.get("rationale"),
                response.model,
            )
            _record_run(article, "tagging", client, response)
            stats["processed"] += 1
        except Exception as exc:  # noqa: BLE001
            _record_error(article, "tagging", client, exc)
            stats["errors"] += 1
    return stats


def process_scores(limit: int = 20, offline: bool = False) -> dict:
    client = make_client(offline)
    return process_score_articles(repository.get_articles_needing_scores(limit), client)


def process_score_articles(articles: list[dict], client) -> dict:
    criteria = repository.list_enabled_scoring_criteria()
    _validate_weights(criteria)
    stats = {"processed": 0, "errors": 0}
    for article in articles:
        try:
            response = score_article(article, criteria, client)
            payload = normalize_score_payload(article, criteria, response.data)
            repository.replace_article_score(
                article["id"],
                payload["total_score"],
                payload["score_label"],
                payload["explanation"],
                payload["items"],
                response.model,
            )
            _record_run(article, "scoring", client, response)
            stats["processed"] += 1
        except Exception as exc:  # noqa: BLE001
            _record_error(article, "scoring", client, exc)
            stats["errors"] += 1
    return stats


def process_full(limit: int = 20, offline: bool = False) -> dict:
    """Запустить по-статейный конвейер на статьях, у которых ещё нет сути."""
    client = make_client(offline)
    return process_pipeline_articles(repository.get_articles_needing_summary(limit), client)


def process_pipeline_articles(articles: list[dict], client, fetch_full: bool = True) -> dict:
    """Полный конвейер по одной статье целиком: full-text → суть → релевантность → тег → скоринг.

    Каждая статья проходит все этапы до конца, прежде чем берётся следующая, —
    готовые карточки появляются по мере обработки, не нужно ждать прогона всего
    батча на каждом этапе. Нерелевантные дальше не тегируются и не скорятся.
    """
    tags = repository.list_enabled_tags()
    if not tags:
        raise ValueError("Нет активных тегов. Запустите seed-tags.")
    criteria = repository.list_enabled_scoring_criteria()
    _validate_weights(criteria)
    stats = {"processed": 0, "fulltext": 0, "summary": 0, "relevant": 0,
             "rejected": 0, "tagged": 0, "scored": 0, "errors": 0}
    for article in articles:
        stats["processed"] += 1
        try:
            # 1. Полный текст из HTML, если RSS отдал только сниппет.
            if fetch_full and article.get("text_truncated") and article.get("url"):
                result = article_fetcher.fetch_article_text(article)
                if result.status == "ok":
                    repository.update_article_full_text(
                        int(article["id"]), raw_text=result.text, text_truncated=False,
                        status="ok", method=result.method, error=None,
                    )
                    article["raw_text"] = result.text
                    article["text_truncated"] = False
                    stats["fulltext"] += 1

            # 2. Релевантность ПЕРВОЙ — на сыром тексте, до сути (без bias и без лишних
            #    AI-вызовов на нерелевантном).
            rel_resp = relevance_article(article, client)
            relevant = bool(rel_resp.data.get("relevant"))
            repository.set_article_relevance(article["id"], relevant, rel_resp.data.get("reason"), rel_resp.model)
            _record_run(article, "relevance", client, rel_resp)
            stats["relevant" if relevant else "rejected"] += 1
            if not relevant:
                continue

            # 3. Суть.
            summary_resp = summarize_article(article, client)
            repository.upsert_article_card(article["id"], summary_resp.data["summary"], summary_resp.model, summary_resp.data.get("title_ru"))
            _record_run(article, "summary", client, summary_resp)
            article["summary"] = summary_resp.data["summary"]
            stats["summary"] += 1

            # 4. Тег.
            tag_resp = tag_article(article, tags, client)
            tag_id = _valid_tag_id(tag_resp.data.get("tag_id"), tags)
            if tag_id == 0:
                tag_id = keyword_tag(article, tags)["tag_id"]
            repository.upsert_article_tag(
                article["id"], tag_id,
                _clamp(float(tag_resp.data.get("confidence") or 0), 0, 1),
                tag_resp.data.get("rationale"), tag_resp.model,
            )
            _record_run(article, "tagging", client, tag_resp)
            stats["tagged"] += 1

            # 5. Скоринг.
            score_resp = score_article(article, criteria, client)
            payload = normalize_score_payload(article, criteria, score_resp.data)
            repository.replace_article_score(
                article["id"], payload["total_score"], payload["score_label"],
                payload["explanation"], payload["items"], score_resp.model,
            )
            _record_run(article, "scoring", client, score_resp)
            stats["scored"] += 1
        except Exception as exc:  # noqa: BLE001 - batch should continue
            _record_error(article, "pipeline", client, exc)
            stats["errors"] += 1
    return stats


def summarize_article(article: dict, client) -> AIResponse:
    return client.complete_json(
        SUMMARY_INSTRUCTIONS,
        _article_prompt(article),
        SUMMARY_SCHEMA,
        max_output_tokens=1200,
    )


def relevance_article(article: dict, client) -> AIResponse:
    # Гейт судит по СЫРОМУ тексту (title+source+text), БЕЗ AI-сути: суммаризатор
    # обязан притягивать любую статью к нефтегазу, и подача его сути на вход гейта
    # давала самосбывающуюся релевантность (мусор проходил). Модель/effort — отдельные,
    # обычно сильнее основных: вызов дешёвый, цена ошибки высокая.
    return client.complete_json(
        RELEVANCE_INSTRUCTIONS,
        _relevance_prompt(article),
        RELEVANCE_SCHEMA,
        max_output_tokens=400,
        model=config.OPENAI_RELEVANCE_MODEL,
        reasoning_effort=config.OPENAI_RELEVANCE_REASONING,
    )


def tag_article(article: dict, tags: list[dict], client) -> AIResponse:
    tag_lines = []
    for tag in tags:
        path = f"{tag.get('parent_name')} / {tag['name']}" if tag.get("parent_name") else tag["name"]
        tag_lines.append(
            f"{tag['id']}: {path} | EN: {tag.get('name_en') or ''} | "
            f"keywords: {', '.join((tag.get('keywords_en_json') or [])[:12])}"
        )
    return client.complete_json(
        TAGGING_INSTRUCTIONS,
        _article_prompt(article) + "\n\navailable_tags:\n" + "\n".join(tag_lines),
        TAG_SCHEMA,
        max_output_tokens=1000,
    )


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
    )


def keyword_tag(article: dict, tags: list[dict]) -> dict:
    text = _search_text(article)
    best = {"tag_id": tags[0]["id"], "confidence": 0.15, "matches": 0}
    for tag in tags:
        keywords = (tag.get("keywords_json") or []) + (tag.get("keywords_en_json") or [])
        matches = sum(1 for keyword in keywords if _contains_keyword(text, keyword))
        if matches > best["matches"]:
            best = {"tag_id": tag["id"], "confidence": min(0.9, 0.35 + matches * 0.08), "matches": matches}
    return best


def normalize_score_payload(article: dict, criteria: list[dict], payload: dict[str, Any]) -> dict:
    by_id = {int(c["id"]): c for c in criteria}
    ai_items = {int(item["criterion_id"]): item for item in payload.get("items", []) if item.get("criterion_id") in by_id}
    items = []
    weighted_total = 0.0
    for criterion in criteria:
        criterion_id = int(criterion["id"])
        weight = float(criterion["weight"])
        keyword_score = keyword_score_for_criterion(article, criterion)
        ai_score = float((ai_items.get(criterion_id) or {}).get("ai_score") or keyword_score)
        final_score = round((keyword_score * 0.35) + (ai_score * 0.65), 2)
        weighted_total += final_score * weight / 100
        items.append(
            {
                "criterion_id": criterion_id,
                "keyword_score": keyword_score,
                "ai_score": ai_score,
                "final_score": final_score,
                "rationale": (ai_items.get(criterion_id) or {}).get("rationale") or "Keyword/AI blended score",
            }
        )
    total_score = round(_clamp(weighted_total, 0, 100), 2)
    return {
        "total_score": total_score,
        "score_label": score_label(total_score),
        "explanation": payload.get("explanation") or "",
        "items": items,
    }


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


def _article_prompt(article: dict) -> str:
    return "\n".join(
        [
            f"title: {article.get('title') or ''}",
            f"source: {article.get('source_name') or ''}",
            f"url: {article.get('url') or ''}",
            f"language: {article.get('language') or 'unknown'}",
            f"published_at: {article.get('published_at') or ''}",
            f"summary: {article.get('summary') or ''}",
            f"text: {_compact(article.get('raw_text') or '', 6000)}",
        ]
    )


def _relevance_prompt(article: dict) -> str:
    """Вход гейта релевантности — БЕЗ AI-сути (намеренно): только сырые поля статьи,
    чтобы суждение шло по реальному содержанию, а не по подкрученной нефтегаз-сути."""
    return "\n".join(
        [
            f"title: {article.get('title') or ''}",
            f"source: {article.get('source_name') or ''}",
            f"url: {article.get('url') or ''}",
            f"language: {article.get('language') or 'unknown'}",
            f"published_at: {article.get('published_at') or ''}",
            f"text: {_compact(article.get('raw_text') or '', 6000)}",
        ]
    )


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


def _valid_tag_id(value, tags: list[dict]) -> int:
    try:
        tag_id = int(value)
    except (TypeError, ValueError):
        return 0
    return tag_id if any(int(t["id"]) == tag_id for t in tags) else 0


def _validate_weights(criteria: list[dict]) -> None:
    if not criteria:
        raise ValueError("Нет активных критериев скоринга. Запустите seed-scoring.")
    total = sum(float(c["weight"]) for c in criteria)
    if round(total, 2) != 100:
        raise ValueError(f"Сумма весов критериев должна быть 100, сейчас {total}")


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
