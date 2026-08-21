"""Controlled source discovery agent tools.

MVP design: the agent can plan, generate queries, inspect seed URLs and write
source candidates. It does not autonomously crawl the web or activate sources.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import time
from typing import Any

import requests

from oiltech_digest import config as app_config
from oiltech_digest.db import repository
from oiltech_digest.ingestion import request_parser
from oiltech_digest.ingestion.relevance_filter import should_keep_article
from oiltech_digest.ingestion.source_diagnostics import probe_url
from oiltech_digest.processing.pipeline import make_client
from oiltech_digest.source_discovery.prompts import (
    SEARCH_QUERY_INSTRUCTIONS,
    SEARCH_QUERY_SCHEMA,
    SOURCE_RECOMMENDATION_INSTRUCTIONS,
    SOURCE_RECOMMENDATION_SCHEMA,
)


DEFAULT_MAX_QUERIES = 8


@dataclass(frozen=True)
class DiscoveryConfig:
    topic: str
    limit: int = 20
    seed_urls: tuple[str, ...] = ()
    offline: bool = False
    dry_run: bool = True
    fetch_inspection: bool = False
    test_parse: bool = False
    run_id: int | None = None
    query_strategy: str = "balanced"


def discover_sources(config: DiscoveryConfig) -> dict[str, Any]:
    """Run one safe source-discovery iteration.

    Without a configured search provider this command is still useful: it creates
    a concrete search plan and can turn explicit seed URLs into source candidates.
    """
    started = time.monotonic()
    task_id = None
    if not config.dry_run:
        task_id = repository.create_agent_task(
            "discover_sources",
            topic=config.topic,
            payload={
                "limit": config.limit,
                "seed_urls": list(config.seed_urls),
                "fetch_inspection": config.fetch_inspection,
                "test_parse": config.test_parse,
                "query_strategy": config.query_strategy,
            },
            budget={
                "max_queries": DEFAULT_MAX_QUERIES,
                "max_candidates": config.limit,
                "requires_approval_for_activation": True,
            },
            status="running",
        )

    gaps = get_topic_gaps(limit=10)
    queries = generate_search_queries(
        config.topic,
        offline=config.offline,
        limit=DEFAULT_MAX_QUERIES,
        strategy=config.query_strategy,
    )
    search_results = search_web(queries, limit=config.limit)

    candidates = []
    rejected_domains = _rejected_memory_domains()
    candidate_urls = _candidate_urls(
        config.seed_urls,
        search_results.get("results") or [],
        config.limit,
        rejected_domains=rejected_domains,
    )
    for url_info in candidate_urls:
        url = url_info["url"]
        inspection = inspect_source(url, fetch=config.fetch_inspection)
        parse_result = test_parse_source(url, article_limit=5) if config.test_parse else None
        metrics = parse_result["metrics"] if parse_result else {
            "tested_articles": 0,
            "relevant_articles": 0,
            "avg_score": None,
            "duplicate_count": 0,
            "noise_count": 0,
        }
        recommendation = recommend_source_action({**metrics, "inspection": inspection}, offline=config.offline)
        candidate = {
            "url": url,
            "normalized_domain": repository.normalize_domain(url),
            "name": inspection.get("name"),
            "candidate_type": inspection.get("candidate_type"),
            "status": "needs_human_review",
            "discovered_by": "source-discovery-agent",
            "discovery_reason": url_info.get("reason") or f"Candidate for topic: {config.topic}",
            "topic": config.topic,
            "confidence": inspection.get("confidence"),
            "tested_articles": metrics.get("tested_articles", 0),
            "relevant_articles": metrics.get("relevant_articles", 0),
            "avg_score": metrics.get("avg_score"),
            "duplicate_count": metrics.get("duplicate_count", 0),
            "noise_count": metrics.get("noise_count", 0),
            "recommended_action": recommendation["recommended_action"],
            "review_comment": recommendation["reason"],
            "inspection": inspection,
            "test_parse": parse_result,
        }
        if not config.dry_run:
            candidate_id = repository.upsert_source_candidate(candidate)
            repository.record_agent_action(
                task_id,
                "create_source_candidate",
                input_payload={"url": url, "topic": config.topic},
                output_payload={"candidate_id": candidate_id, **candidate},
            )
            candidate["id"] = candidate_id
        candidates.append(candidate)

    result = {
        "dry_run": config.dry_run,
        "task_id": task_id,
        "topic": config.topic,
        "topic_gaps": gaps,
        "queries": queries,
        "query_strategy": config.query_strategy,
        "search": search_results,
        "rejected_domains_skipped": sorted(rejected_domains),
        "candidates": candidates,
        "limits": {
            "max_queries": DEFAULT_MAX_QUERIES,
            "max_candidates": config.limit,
            "activation_requires_approval": True,
        },
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    if not config.dry_run:
        _persist_query_memory(config.topic, queries, search_results.get("status"), candidate_urls, candidates)
        repository.record_agent_action(
            task_id,
            "discover_sources_finished",
            run_id=config.run_id,
            input_payload={"topic": config.topic},
            output_payload=result,
            duration_ms=result["duration_ms"],
        )
    return result


def get_topic_gaps(*, days: int = 30, target_per_topic: int = 10, limit: int = 10) -> list[dict]:
    period_to = datetime.now(timezone.utc)
    period_from = period_to - timedelta(days=days)
    return repository.compute_topic_gap_rows(period_from, period_to, target_per_topic, limit)


def generate_search_queries(
    topic: str,
    *,
    offline: bool = False,
    limit: int = DEFAULT_MAX_QUERIES,
    strategy: str = "balanced",
) -> list[str]:
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("topic is required")
    remembered = _remembered_queries(topic, limit=limit)
    muted = _muted_queries(topic)
    if offline:
        return _merge_queries(remembered, _offline_queries(topic, DEFAULT_MAX_QUERIES, strategy=strategy), limit=limit, exclude=muted)
    client = make_client(False)
    response = client.complete_json(
        SEARCH_QUERY_INSTRUCTIONS,
        f"topic: {topic}\nstrategy: {strategy}\nlimit: {limit}",
        SEARCH_QUERY_SCHEMA,
        max_output_tokens=600,
    )
    queries = [str(item).strip() for item in response.data.get("queries") or [] if str(item).strip()]
    generated = _merge_queries(queries, _offline_queries(topic, DEFAULT_MAX_QUERIES, strategy=strategy), limit=DEFAULT_MAX_QUERIES)
    return _merge_queries(remembered, generated, limit=limit, exclude=muted)


def search_web(queries: list[str], *, limit: int = 20) -> dict[str, Any]:
    """Search candidate sources through a configured provider.

    Supported providers:
    - none: explicit no-op for seed-url MVP checks;
    - brave: Brave Search API;
    - serpapi: SerpAPI Google results.
    """
    provider = app_config.SOURCE_DISCOVERY_SEARCH_PROVIDER
    if provider in {"", "none", "disabled"}:
        return {
            "status": "not_configured",
            "provider": "none",
            "reason": "search provider is not connected yet; use --seed-url for MVP checks",
            "queries": queries,
            "limit": limit,
            "results": [],
        }
    if provider == "brave":
        return _search_brave(queries, limit=limit)
    if provider == "serpapi":
        return _search_serpapi(queries, limit=limit)
    return {
        "status": "unsupported_provider",
        "provider": provider,
        "reason": f"unsupported SOURCE_DISCOVERY_SEARCH_PROVIDER={provider}",
        "queries": queries,
        "limit": limit,
        "results": [],
    }


def inspect_source(url: str, *, fetch: bool = False) -> dict[str, Any]:
    domain = repository.normalize_domain(url)
    candidate_type = _candidate_type(url)
    result: dict[str, Any] = {
        "url": url,
        "domain": domain,
        "name": _name_from_domain(domain),
        "candidate_type": candidate_type,
        "confidence": _inspection_confidence(candidate_type),
        "fetch_checked": False,
    }
    if not fetch:
        return result

    probe, content = probe_url(url)
    result["fetch_checked"] = True
    result["probe"] = {
        "status": probe.status,
        "bytes": probe.bytes,
        "seconds": probe.seconds,
        "error": probe.error,
    }
    if content:
        text = content[:5000].decode("utf-8", errors="ignore").lower()
        result["has_rss_hint"] = "rss" in text or "application/rss+xml" in text
        result["has_news_hint"] = any(word in text for word in ("news", "press release", "новости", "пресс-релиз"))
    return result


def test_parse_source(url: str, *, article_limit: int = 5) -> dict[str, Any]:
    """Read-only test parse of a source candidate.

    This does not insert articles. It only checks whether the page looks like a
    usable listing and whether candidate article pages produce meaningful text.
    """
    source = {
        "id": 0,
        "name": _name_from_domain(repository.normalize_domain(url)),
        "url": url,
        "listing_url": url,
        "category": "source-discovery",
        "source_type": "candidate",
    }
    listing_probe, listing_content = probe_url(url)
    result: dict[str, Any] = {
        "url": url,
        "listing_probe": {
            "status": listing_probe.status,
            "bytes": listing_probe.bytes,
            "seconds": listing_probe.seconds,
            "error": listing_probe.error,
        },
        "candidates": [],
        "metrics": {
            "tested_articles": 0,
            "relevant_articles": 0,
            "avg_score": None,
            "duplicate_count": 0,
            "noise_count": 0,
        },
    }
    if listing_content is None:
        result["verdict"] = "listing_fetch_failed"
        return result

    candidates = request_parser.extract_candidate_links(source, url, listing_content, limit=article_limit)
    checks = []
    seen_urls: set[str] = set()
    relevant_count = 0
    noise_count = 0
    duplicate_count = 0
    pseudo_scores = []
    for candidate in candidates[:article_limit]:
        if candidate.url in seen_urls:
            duplicate_count += 1
            continue
        seen_urls.add(candidate.url)
        article_probe, article_content = probe_url(candidate.url)
        check: dict[str, Any] = {
            "url": candidate.url,
            "title": candidate.title,
            "listing_score": candidate.score,
            "probe_status": article_probe.status,
            "probe_error": article_probe.error,
        }
        if article_content is None:
            check["verdict"] = "article_fetch_failed"
            checks.append(check)
            continue
        title, published_at, raw_text = request_parser.parse_article_page(article_content, candidate.title)
        pre_filter = should_keep_article(title, raw_text, source)
        text_chars = len(raw_text or "")
        is_relevant_like = pre_filter.keep and text_chars >= app_config.MIN_ARTICLE_TEXT_CHARS
        if is_relevant_like:
            relevant_count += 1
            pseudo_scores.append(min(85, 45 + candidate.score * 5))
        else:
            noise_count += 1
        check.update({
            "verdict": "ok" if is_relevant_like else "not_useful",
            "title": title,
            "published_at": published_at,
            "text_chars": text_chars,
            "prefilter_keep": pre_filter.keep,
            "prefilter_reason": pre_filter.reason,
            "prefilter_noise": list(pre_filter.matched_noise[:5]),
            "prefilter_keywords": list(pre_filter.matched_keywords[:5]),
        })
        checks.append(check)

    tested = len(checks)
    avg_score = round(sum(pseudo_scores) / len(pseudo_scores), 2) if pseudo_scores else None
    result["candidates"] = checks
    result["metrics"] = {
        "tested_articles": tested,
        "relevant_articles": relevant_count,
        "avg_score": avg_score,
        "duplicate_count": duplicate_count,
        "noise_count": noise_count,
    }
    result["verdict"] = "ok" if relevant_count else ("no_candidates" if not candidates else "no_useful_articles")
    return result


def test_source_candidate(
    candidate_id: int,
    *,
    article_limit: int = 5,
    offline: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Test an already saved source candidate and update its assessment."""
    candidate = repository.get_source_candidate(candidate_id)
    if candidate is None:
        raise ValueError(f"source candidate id={candidate_id} not found")

    started = time.monotonic()
    parse_result = test_parse_source(str(candidate["url"]), article_limit=article_limit)
    metrics = parse_result["metrics"]
    recommendation = recommend_source_action(metrics, offline=offline)
    next_status = _status_for_recommendation(recommendation["recommended_action"])
    result = {
        "candidate_id": candidate_id,
        "url": candidate["url"],
        "parse": parse_result,
        "metrics": metrics,
        "recommended_action": recommendation["recommended_action"],
        "review_comment": recommendation["reason"],
        "next_status": next_status,
        "dry_run": dry_run,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    if not dry_run:
        repository.update_source_candidate_assessment(
            candidate_id,
            status=next_status,
            tested_articles=metrics.get("tested_articles"),
            relevant_articles=metrics.get("relevant_articles"),
            avg_score=metrics.get("avg_score"),
            duplicate_count=metrics.get("duplicate_count"),
            noise_count=metrics.get("noise_count"),
            recommended_action=recommendation["recommended_action"],
            review_comment=recommendation["reason"],
        )
        repository.record_agent_action(
            None,
            "test_source_candidate",
            input_payload={"candidate_id": candidate_id, "article_limit": article_limit},
            output_payload=result,
            duration_ms=result["duration_ms"],
        )
    return result


def score_source_candidate(metrics: dict[str, Any]) -> dict[str, Any]:
    tested = int(metrics.get("tested_articles") or 0)
    relevant = int(metrics.get("relevant_articles") or 0)
    avg_score = float(metrics.get("avg_score") or 0)
    duplicate = int(metrics.get("duplicate_count") or 0)
    noise = int(metrics.get("noise_count") or 0)
    denominator = max(tested, 1)
    quality_score = (
        (relevant / denominator) * 45
        + (avg_score / 100) * 30
        - (duplicate / denominator) * 10
        - (noise / denominator) * 15
    )
    return {
        "tested_articles": tested,
        "relevant_articles": relevant,
        "avg_score": avg_score if metrics.get("avg_score") is not None else None,
        "duplicate_count": duplicate,
        "noise_count": noise,
        "quality_score": round(max(0, min(100, quality_score)), 2),
    }


def recommend_source_action(metrics: dict[str, Any], *, offline: bool = True) -> dict[str, str]:
    score = score_source_candidate(metrics)
    tested = score["tested_articles"]
    relevant = score["relevant_articles"]
    quality = score["quality_score"]
    if tested == 0:
        fallback = {
            "recommended_action": "human_review",
            "reason": "Источник ещё не проверялся на реальных материалах; нужна ручная проверка или пробный парсинг.",
        }
    elif tested < 5:
        fallback = {
            "recommended_action": "test_more",
            "reason": "Материалов мало для уверенного решения; источник нужно протестировать ещё.",
        }
    elif quality >= 55 and relevant >= max(3, tested // 2):
        fallback = {
            "recommended_action": "add",
            "reason": "Источник дал достаточно релевантных материалов и выглядит полезным.",
        }
    elif quality <= 20:
        fallback = {
            "recommended_action": "reject",
            "reason": "Источник даёт мало релевантных материалов или слишком много шума.",
        }
    else:
        fallback = {
            "recommended_action": "human_review",
            "reason": "Качество неоднозначное; решение лучше принять человеку.",
        }
    if offline:
        return fallback

    client = make_client(False)
    response = client.complete_json(
        SOURCE_RECOMMENDATION_INSTRUCTIONS,
        json.dumps({"metrics": metrics, "score": score}, ensure_ascii=False),
        SOURCE_RECOMMENDATION_SCHEMA,
        max_output_tokens=400,
    )
    return {
        "recommended_action": response.data.get("recommended_action") or fallback["recommended_action"],
        "reason": response.data.get("reason") or fallback["reason"],
    }


def _offline_queries(topic: str, limit: int, *, strategy: str = "balanced") -> list[str]:
    strategy = (strategy or "balanced").strip().lower()
    strategy_queries = {
        "newsroom": [
            f"{topic} newsroom press release oil gas",
            f"{topic} media center upstream company",
            f"{topic} новости компании нефтегаз",
            f"{topic} пресс-релиз нефтесервис",
        ],
        "technical": [
            f"{topic} technology case study oilfield",
            f"{topic} technical paper upstream",
            f"{topic} SPE drilling production technology",
            f"{topic} patent oil gas technology",
        ],
        "company": [
            f"{topic} vendor oilfield solution",
            f"{topic} service company launch",
            f"{topic} нефтесервис технология компания",
            f"{topic} industrial supplier oil gas news",
        ],
    }
    base = strategy_queries.get(strategy, []) + [
        f"{topic} нефтегаз новости",
        f"{topic} нефтесервис пресс-релиз",
        f"{topic} технологии добычи нефти",
        f"{topic} upstream news",
        f"{topic} oilfield technology news",
        f"{topic} press release oil gas",
        f"{topic} drilling production technology",
        f"{topic} energy industry newsroom",
    ]
    return base[:limit]


def _candidate_type(url: str) -> str:
    lowered = url.lower()
    if "rss" in lowered or lowered.endswith(".xml"):
        return "rss"
    if any(part in lowered for part in ("newsroom", "press", "media", "releases")):
        return "newsroom"
    if any(part in lowered for part in ("news", "novosti", "новости")):
        return "media"
    return "unknown"


def _name_from_domain(domain: str) -> str:
    if not domain:
        return "Unknown source"
    return domain.split(".")[0].replace("-", " ").title()


def _inspection_confidence(candidate_type: str) -> float:
    if candidate_type == "rss":
        return 0.75
    if candidate_type == "newsroom":
        return 0.65
    if candidate_type == "media":
        return 0.55
    return 0.35


def _status_for_recommendation(action: str) -> str:
    if action == "add":
        return "needs_human_review"
    if action == "test_more":
        return "test_parsing"
    if action == "reject":
        return "rejected"
    return "needs_human_review"


def _search_brave(queries: list[str], *, limit: int) -> dict[str, Any]:
    if not app_config.BRAVE_SEARCH_API_KEY:
        return {
            "status": "missing_api_key",
            "provider": "brave",
            "reason": "BRAVE_SEARCH_API_KEY is empty",
            "queries": queries,
            "limit": limit,
            "results": [],
        }
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    per_query = max(1, min(10, limit))
    for query in queries:
        try:
            response = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": app_config.BRAVE_SEARCH_API_KEY,
                },
                params={"q": query, "count": per_query},
                timeout=app_config.SOURCE_DISCOVERY_SEARCH_TIMEOUT,
            )
            if response.status_code >= 400:
                errors.append(f"{query}: HTTP {response.status_code} {response.text[:200]}")
                continue
            for item in ((response.json().get("web") or {}).get("results") or []):
                url = item.get("url")
                if not url:
                    continue
                results.append({
                    "query": query,
                    "url": url,
                    "title": item.get("title"),
                    "snippet": item.get("description"),
                    "provider": "brave",
                })
        except Exception as exc:  # noqa: BLE001 - one query must not kill the run
            errors.append(f"{query}: {exc}")
    return _search_payload("brave", queries, limit, results, errors)


def _search_serpapi(queries: list[str], *, limit: int) -> dict[str, Any]:
    if not app_config.SERPAPI_API_KEY:
        return {
            "status": "missing_api_key",
            "provider": "serpapi",
            "reason": "SERPAPI_API_KEY is empty",
            "queries": queries,
            "limit": limit,
            "results": [],
        }
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    per_query = max(1, min(10, limit))
    for query in queries:
        try:
            response = requests.get(
                "https://serpapi.com/search.json",
                params={
                    "engine": "google",
                    "q": query,
                    "api_key": app_config.SERPAPI_API_KEY,
                    "num": per_query,
                },
                timeout=app_config.SOURCE_DISCOVERY_SEARCH_TIMEOUT,
            )
            if response.status_code >= 400:
                errors.append(f"{query}: HTTP {response.status_code} {response.text[:200]}")
                continue
            for item in response.json().get("organic_results") or []:
                url = item.get("link")
                if not url:
                    continue
                results.append({
                    "query": query,
                    "url": url,
                    "title": item.get("title"),
                    "snippet": item.get("snippet"),
                    "provider": "serpapi",
                })
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{query}: {exc}")
    return _search_payload("serpapi", queries, limit, results, errors)


def _search_payload(
    provider: str,
    queries: list[str],
    limit: int,
    results: list[dict[str, Any]],
    errors: list[str],
) -> dict[str, Any]:
    deduped = _dedupe_results(results, limit)
    return {
        "status": "ok" if deduped else ("error" if errors else "empty"),
        "provider": provider,
        "queries": queries,
        "limit": limit,
        "results": deduped,
        "errors": errors[:10],
    }


def _dedupe_results(results: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped = []
    for item in results:
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return deduped


def _rejected_memory_domains() -> set[str]:
    rows = repository.list_agent_memory(memory_type="domain", status="rejected", limit=500)
    return {str(row.get("subject") or "").strip().lower() for row in rows if row.get("subject")}


def _remembered_queries(topic: str, *, limit: int) -> list[str]:
    try:
        rows = repository.list_agent_memory(memory_type="query", status="active", limit=100)
    except Exception:  # noqa: BLE001 - query memory must not break source discovery
        return []
    topic_key = topic.strip().lower()
    queries = []
    for row in rows:
        facts = row.get("facts_json") or {}
        if str(facts.get("topic") or "").strip().lower() != topic_key:
            continue
        query = str(row.get("subject") or "").strip()
        if query:
            queries.append(query)
        if len(queries) >= limit:
            break
    return queries


def _muted_queries(topic: str) -> set[str]:
    try:
        rows = repository.list_agent_memory(memory_type="query", status="muted", limit=500)
    except Exception:  # noqa: BLE001 - query memory must not break source discovery
        return set()
    topic_key = topic.strip().lower()
    muted = set()
    for row in rows:
        facts = row.get("facts_json") or {}
        if str(facts.get("topic") or "").strip().lower() != topic_key:
            continue
        query = str(row.get("subject") or "").strip().lower()
        if query:
            muted.add(query)
    return muted


def _merge_queries(primary: list[str], fallback: list[str], *, limit: int, exclude: set[str] | None = None) -> list[str]:
    result = []
    seen = set()
    exclude = exclude or set()
    for query in [*primary, *fallback]:
        normalized = str(query).strip()
        key = normalized.lower()
        if not normalized or key in seen or key in exclude:
            continue
        seen.add(key)
        result.append(normalized)
        if len(result) >= limit:
            break
    return result


def _persist_query_memory(
    topic: str,
    queries: list[str],
    search_status: str | None,
    candidate_urls: list[dict[str, str]],
    candidates: list[dict[str, Any]],
) -> None:
    by_url = {str(item.get("url") or ""): item for item in candidates}
    can_trust_empty_queries = search_status in {"ok", "empty"}
    by_query: dict[str, dict[str, Any]] = {
        str(query).strip(): {
            "found_candidates": 0,
            "relevant_articles": 0,
            "tested_articles": 0,
            "avg_scores": [],
        }
        for query in queries
        if str(query).strip() and can_trust_empty_queries
    }
    for item in candidate_urls:
        query = str(item.get("query") or "").strip()
        if not query:
            continue
        bucket = by_query.setdefault(query, {
            "found_candidates": 0,
            "relevant_articles": 0,
            "tested_articles": 0,
            "avg_scores": [],
        })
        bucket["found_candidates"] += 1
        candidate = by_url.get(str(item.get("url") or "")) or {}
        bucket["relevant_articles"] += int(candidate.get("relevant_articles") or 0)
        bucket["tested_articles"] += int(candidate.get("tested_articles") or 0)
        if candidate.get("avg_score") is not None:
            bucket["avg_scores"].append(float(candidate["avg_score"]))

    for query, facts in by_query.items():
        tested = int(facts["tested_articles"] or 0)
        relevant = int(facts["relevant_articles"] or 0)
        avg_scores = facts.pop("avg_scores")
        avg_score = round(sum(avg_scores) / len(avg_scores), 2) if avg_scores else None
        empty_result = int(facts["found_candidates"] or 0) == 0
        score = 0.0 if empty_result else min(
            100.0,
            facts["found_candidates"] * 10 + (relevant / max(tested, 1)) * 60 + (avg_score or 0) * 0.2,
        )
        try:
            repository.upsert_agent_memory(
                memory_key=_query_memory_key(topic, query),
                memory_type="query",
                subject=query,
                status="muted" if empty_result else "active",
                score=round(score, 2),
                facts={
                    **facts,
                    "topic": topic,
                    "avg_score": avg_score,
                    "empty_result": empty_result,
                },
            )
        except Exception:  # noqa: BLE001 - memory write must not fail discovery
            continue


def _query_memory_key(topic: str, query: str) -> str:
    digest = hashlib.sha1(f"{topic.strip().lower()}::{query.strip().lower()}".encode("utf-8")).hexdigest()[:16]
    return f"query:{digest}"


def _candidate_urls(
    seed_urls: tuple[str, ...],
    search_results: list[dict[str, Any]],
    limit: int,
    *,
    rejected_domains: set[str] | None = None,
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    rejected_domains = rejected_domains or set()
    for url in seed_urls:
        normalized = str(url).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        items.append({"url": normalized, "reason": "Seed URL supplied by operator", "query": ""})
    for result in search_results:
        normalized = str(result.get("url") or "").strip()
        if not normalized or normalized in seen:
            continue
        domain = repository.normalize_domain(normalized).lower()
        if domain and domain in rejected_domains:
            continue
        seen.add(normalized)
        reason = f"Search result for query: {result.get('query')}" if result.get("query") else "Search result"
        items.append({"url": normalized, "reason": reason, "query": str(result.get("query") or "")})
        if len(items) >= limit:
            break
    return items[:limit]
