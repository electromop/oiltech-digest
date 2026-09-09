"""Technology signal radar.

This layer searches for transferable business/HSE signals, not just relevant
articles. It can run fully offline on already collected articles; web evidence
can be fed later through the same evidence shape.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any

from oiltech_digest.db import repository
from oiltech_digest.processing.domain_glossary import enforce_glossary_text, glossary_prompt_block
from oiltech_digest.processing.openai_client import AIResponse
from oiltech_digest.processing.pipeline import make_client


INDUSTRY_CONTEXT_RE = re.compile(
    r"\b("
    r"oil|gas|o&g|lng|refiner|refinery|petrochemical|chemical|drilling|wellsite|wellbore|"
    r"oilfield|pipeline|midstream|upstream|offshore|subsea|frac|fracking|methane|hydrocarbon|"
    r"mining|mine|hazardous area|process safety|industrial safety"
    r")\b|"
    r"(нефт|газ|бурен|скважин|трубопровод|промышленн|опасн|месторожд|добыч|переработк)|"
    r"(油气|石油|天然气|油田|油库|钻井|石化|化工|矿山|管道|炼化|海上平台|防爆|危化|采气|采油)",
    re.IGNORECASE,
)


DEFAULT_RADAR_TOPICS = [
    {
        "name": "HSE robotics / Physical AI",
        "description": "Роботы и physical AI, которые убирают людей из опасных зон и операций.",
        "industry_scope": ["oil and gas", "mining", "metals", "chemicals", "industrial logistics"],
        "query_seeds": [
            "autonomous inspection robot hazardous area",
            "physical AI industrial safety",
            "robotic drilling removes workers from drill floor",
            "remote inspection robot environmental monitoring industrial site",
            "具身智能 油气 巡检机器人",
            "防爆巡检机器人 石油 天然气",
            "油气站场 无人巡检 机器人",
        ],
    },
    {
        "name": "Predictive HSE",
        "description": "Переход от реактивной безопасности к прогнозированию опасного состояния.",
        "industry_scope": ["oil and gas", "mining", "chemicals", "energy"],
        "query_seeds": [
            "predictive safety industrial operations",
            "preventive safety hazardous condition detection",
            "AI predicts safety incidents industrial equipment",
            "predictive maintenance HSE risk reduction",
            "预测性安全 矿山 油气",
            "安全生产 双重预防机制 人工智能",
            "设备状态监测 预测性维护 油田",
        ],
    },
    {
        "name": "Digital PTW / Control of Work",
        "description": "Dynamic PTW, LOTO, SIMOPS и continuous control assurance.",
        "industry_scope": ["oil and gas", "chemicals", "mining", "heavy industry"],
        "query_seeds": [
            "dynamic permit to work continuous control assurance",
            "digital PTW isolation certificate shift handover",
            "LOTO SIMOPS control of work software industrial safety",
            "permit to work system contractor competence integration",
            "电子作业票 特殊作业 安全生产",
            "作业许可 盲板 抽堵 动火 受限空间 数字化",
            "承包商 能力 作业许可 隔离 交接班",
        ],
    },
    {
        "name": "Industrial transport safety",
        "description": "Телематика, fatigue/distraction, collision avoidance и контроль опасных зон транспорта.",
        "industry_scope": ["oilfield service", "mining", "industrial logistics", "construction"],
        "query_seeds": [
            "heavy equipment collision avoidance mining safety",
            "fatigue detection industrial fleet safety",
            "AI video telematics hazardous industrial transport",
            "worker vehicle proximity detection industrial site",
            "矿卡 防碰撞 系统 安全",
            "疲劳驾驶监测 矿山 车辆",
            "人员车辆 防碰撞 露天矿",
        ],
    },
]


SIGNAL_JUDGE_INSTRUCTIONS = """Ты аналитик технологических сигналов для нефтесервиса.
Оцени пачку evidence не как отдельные новости, а как потенциальный сигнал для радара.

Хороший сигнал:
- переносим в нефтесервис/HSE/бурение/промышленную эксплуатацию;
- описывает новый технологический принцип, промышленное масштабирование или измеримый эффект;
- имеет факты: компания, внедрение, поставщик, объект, цифры, зрелость или внятный why now;
- не является обычным маркетинговым анонсом без признаков применения.

Верни один сигнал или reject. Не добавляй фактов, которых нет во входе."""

SIGNAL_JUDGE_SCHEMA = {
    "name": "technology_signal_judgement",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "title",
            "theme",
            "summary",
            "thesis",
            "transferability",
            "maturity",
            "confidence",
            "score",
            "why_now",
            "why_not_noise",
            "companies",
            "industries",
        ],
        "properties": {
            "title": {"type": "string"},
            "theme": {"type": "string"},
            "summary": {"type": "string"},
            "thesis": {"type": "string"},
            "transferability": {"type": "string"},
            "maturity": {"type": "string", "enum": ["reject", "watch", "shortlist", "proven"]},
            "confidence": {"type": "number"},
            "score": {"type": "number"},
            "why_now": {"type": "string"},
            "why_not_noise": {"type": "string"},
            "companies": {"type": "array", "items": {"type": "string"}},
            "industries": {"type": "array", "items": {"type": "string"}},
        },
    },
}


@dataclass(frozen=True)
class SignalDiscoveryConfig:
    topic: str | None = None
    days: int = 14
    limit: int = 80
    min_score: float = 40
    offline: bool = True
    dry_run: bool = True
    max_signals: int = 10
    web_search: bool = False
    web_only: bool = False
    web_query_limit: int = 8


def seed_default_radar_topics() -> int:
    return repository.seed_signal_radar_topics(DEFAULT_RADAR_TOPICS)


def discover_signals(config: SignalDiscoveryConfig) -> dict[str, Any]:
    topics = _selected_topics(config.topic)
    if not topics:
        topics = [{"name": config.topic or "HSE technology radar", "query_seeds_json": []}]

    all_signals: list[dict[str, Any]] = []
    topic_results = []
    for topic in topics:
        topic_name = str(topic.get("name") or config.topic or "").strip()
        article_rows = []
        if not config.web_only:
            article_rows = repository.list_signal_article_evidence(
                topic=topic_name,
                days=config.days,
                limit=config.limit,
                min_score=config.min_score,
            )
        db_evidence = [_article_to_evidence(row, topic_name) for row in article_rows if _has_industry_context(row)]
        evidence = list(db_evidence)
        web_search = None
        if config.web_search or config.web_only:
            web_search = _search_web_evidence(topic, config)
            evidence.extend(web_search["evidence"])
        clusters = _cluster_evidence(evidence, topic_name)
        judged = []
        for cluster in clusters[: config.max_signals]:
            signal = judge_signal(cluster, topic_name, offline=config.offline)
            if signal["maturity"] == "reject":
                continue
            signal["signal_key"] = _signal_key(topic_name, signal["title"], cluster)
            signal["evidence_count"] = len(cluster)
            signal["evidence"] = cluster
            if not config.dry_run:
                signal_id = repository.upsert_signal(signal)
                signal["id"] = signal_id
                for item in cluster:
                    repository.upsert_signal_evidence(signal_id, item)
            judged.append(signal)
            all_signals.append(signal)
        topic_results.append({
            "topic": topic_name,
            "article_evidence": len(db_evidence),
            "total_evidence": len(evidence),
            "web_search": web_search,
            "clusters": len(clusters),
            "signals": judged,
        })

    all_signals.sort(key=lambda item: (float(item.get("score") or 0), int(item.get("evidence_count") or 0)), reverse=True)
    return {
        "dry_run": config.dry_run,
        "offline": config.offline,
        "web_search": config.web_search or config.web_only,
        "web_only": config.web_only,
        "days": config.days,
        "topics": [str(t.get("name") or "") for t in topics],
        "signals": all_signals[: config.max_signals],
        "topic_results": topic_results,
    }


def judge_signal(evidence: list[dict[str, Any]], topic: str, *, offline: bool = True) -> dict[str, Any]:
    if offline:
        return _offline_signal_judgement(evidence, topic)
    client = make_client(False)
    response: AIResponse = client.complete_json(
        SIGNAL_JUDGE_INSTRUCTIONS,
        _judge_prompt(evidence, topic),
        SIGNAL_JUDGE_SCHEMA,
        max_output_tokens=1800,
    )
    return _normalize_signal_payload(response.data, topic, context=_glossary_context(evidence, topic))


def list_signals(*, maturity: str | None = None, theme: str | None = None, limit: int = 50) -> list[dict]:
    return repository.list_signals(maturity=maturity, theme=theme, limit=limit)


def _selected_topics(topic: str | None) -> list[dict]:
    rows = repository.list_signal_radar_topics(enabled_only=True)
    if not topic:
        return rows or DEFAULT_RADAR_TOPICS
    topic_l = topic.lower()
    selected = [row for row in rows if topic_l in str(row.get("name") or "").lower()]
    if selected:
        return selected
    default_selected = [row for row in DEFAULT_RADAR_TOPICS if topic_l in row["name"].lower()]
    return default_selected or [{"name": topic, "query_seeds_json": []}]


def _article_to_evidence(row: dict[str, Any], topic: str) -> dict[str, Any]:
    title = str(row.get("title_ru") or row.get("title") or "").strip()
    summary = str(row.get("summary") or row.get("raw_text") or "")[:900].strip()
    context = {
        "title": title,
        "raw_text": " ".join([str(row.get("title") or ""), str(row.get("summary") or ""), str(row.get("raw_text") or "")]),
        "language": row.get("language") or "",
    }
    summary_ru = enforce_glossary_text(summary or title, context)
    score = float(row.get("total_score") or 50)
    return {
        "article_id": row.get("article_id"),
        "source_url": row["source_url"],
        "title": title,
        "title_ru": enforce_glossary_text(title, context),
        "publisher": row.get("publisher"),
        "published_at": row.get("published_at"),
        "evidence_type": _evidence_type(title + " " + summary),
        "extracted_fact": summary or title,
        "summary_ru": summary_ru,
        "strength": min(1.0, max(0.1, score / 100)),
        "topic": topic,
        "raw_payload": {
            "tag": row.get("tag_name"),
            "score": score,
            "score_label": row.get("score_label"),
            "relevance_reason": row.get("relevance_reason"),
        },
    }


def _search_web_evidence(topic: dict[str, Any], config: SignalDiscoveryConfig) -> dict[str, Any]:
    from oiltech_digest.source_discovery.agent import generate_search_queries, search_web

    topic_name = str(topic.get("name") or config.topic or "").strip()
    seed_queries = _topic_seed_queries(topic, year=2026)
    generated_queries = generate_search_queries(
        topic_name,
        offline=config.offline,
        limit=config.web_query_limit,
        strategy="broad",
    )
    queries = _dedupe(seed_queries + generated_queries)[: config.web_query_limit]
    search = search_web(queries, limit=config.limit)
    results = search.get("results") or []
    evidence = [
        item
        for item in (_search_result_to_evidence(row, topic_name) for row in results)
        if item and _has_industry_context(item)
    ]
    return {
        "status": search.get("status"),
        "provider": search.get("provider"),
        "reason": search.get("reason"),
        "queries": queries,
        "results": len(results),
        "evidence": evidence,
        "errors": search.get("errors") or [],
    }


def _topic_seed_queries(topic: dict[str, Any], *, year: int) -> list[str]:
    topic_name = str(topic.get("name") or "").strip()
    description = str(topic.get("description") or "").strip()
    raw_seeds = topic.get("query_seeds_json")
    if raw_seeds is None:
        raw_seeds = topic.get("query_seeds") or []
    seeds = [str(item).strip() for item in raw_seeds or [] if str(item).strip()]
    queries = []
    for seed in seeds + [topic_name, description]:
        if not seed:
            continue
        queries.append(f"{year} {seed} news oil gas mining chemicals")
        if _contains_cjk(seed):
            queries.append(f"{year} {seed} 新闻 石油 天然气 石化 矿山")
    return _dedupe(queries)


def _search_result_to_evidence(row: dict[str, Any], topic: str) -> dict[str, Any] | None:
    url = str(row.get("url") or "").strip()
    title = _clean_search_text(str(row.get("title") or ""))
    snippet = _clean_search_text(str(row.get("snippet") or ""))
    if not url or not title:
        return None
    context = {
        "title": title,
        "raw_text": f"{title}\n{snippet}",
        "language": "mixed",
    }
    return {
        "article_id": None,
        "source_url": url,
        "title": title,
        "title_ru": enforce_glossary_text(title, context),
        "publisher": repository.normalize_domain(url) or row.get("provider"),
        "published_at": None,
        "evidence_type": _evidence_type(title + " " + snippet),
        "extracted_fact": snippet or title,
        "summary_ru": enforce_glossary_text(snippet or title, context),
        "strength": 0.72,
        "topic": topic,
        "raw_payload": {
            "query": row.get("query"),
            "provider": row.get("provider"),
            "snippet": snippet,
            "evidence_source": "web_search",
        },
    }


def _has_industry_context(row: dict[str, Any]) -> bool:
    text = " ".join(
        str(row.get(key) or "")
        for key in (
            "title",
            "title_ru",
            "raw_text",
            "summary",
            "extracted_fact",
            "summary_ru",
            "tag_name",
            "tag_name_en",
            "publisher",
        )
    )
    return bool(INDUSTRY_CONTEXT_RE.search(text))


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text or ""))


def _clean_search_text(text: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", text or "")
    cleaned = cleaned.replace("&amp;", "&").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for value in values:
        normalized = re.sub(r"\s+", " ", value).strip()
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _cluster_evidence(evidence: list[dict[str, Any]], topic: str) -> list[list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in evidence:
        key = _cluster_key(item, topic)
        buckets.setdefault(key, []).append(item)
    clusters = sorted(
        buckets.values(),
        key=lambda items: (len(items), sum(float(i.get("strength") or 0) for i in items)),
        reverse=True,
    )
    return clusters


def _cluster_key(evidence: dict[str, Any], topic: str) -> str:
    text = f"{evidence.get('title') or ''} {evidence.get('extracted_fact') or ''}".lower()
    patterns = [
        ("physical-ai-robotics", r"robot|robotic|autonomous|physical ai|drill floor|inspection"),
        ("predictive-hse", r"predict|preventive|condition|corrosion|maintenance|anomaly"),
        ("digital-ptw", r"permit|ptw|control of work|loto|isolation|simops|handover"),
        ("transport-safety", r"fleet|telematics|fatigue|collision|driver|vehicle|proximity"),
        ("computer-vision", r"computer vision|video|camera|ppe|danger zone|unsafe"),
    ]
    for key, pattern in patterns:
        if re.search(pattern, text):
            return key
    words = [w for w in re.findall(r"[a-zа-яё0-9]{4,}", text) if w not in _STOP_WORDS]
    return "-".join(words[:3]) or _slug(topic)


def _offline_signal_judgement(evidence: list[dict[str, Any]], topic: str) -> dict[str, Any]:
    best = max(evidence, key=lambda item: float(item.get("strength") or 0))
    text = " ".join([str(item.get("title") or "") + " " + str(item.get("extracted_fact") or "") for item in evidence])
    score = _offline_signal_score(evidence, text)
    maturity = "reject"
    if score >= 88 and len(evidence) >= 3:
        maturity = "proven"
    elif score >= 82 and len(evidence) >= 2:
        maturity = "shortlist"
    elif score >= 62:
        maturity = "watch"
    companies = _extract_companies(text)
    industries = _industries_for_topic(topic, text)
    context = _glossary_context(evidence, topic)
    return _normalize_signal_payload(
        {
            "title": _signal_title(best, topic),
            "title_ru": best.get("title_ru") or _signal_title(best, topic),
            "theme": topic,
            "summary": _trim(str(best.get("summary_ru") or best.get("extracted_fact") or best.get("title") or ""), 360),
            "thesis": _trim(str(best.get("extracted_fact") or best.get("title") or ""), 420),
            "transferability": _transferability_for_topic(topic),
            "maturity": maturity,
            "confidence": min(0.95, max(0.2, score / 100)),
            "score": score,
            "why_now": f"Найдено evidence за последние циклы: {len(evidence)}; лучший материал: {best.get('publisher') or 'источник не указан'}.",
            "why_not_noise": _why_not_noise(text),
            "companies": companies,
            "industries": industries,
        },
        topic,
        context=context,
    )


def _offline_signal_score(evidence: list[dict[str, Any]], text: str) -> float:
    text_l = text.lower()
    score = 35 + min(20, len(evidence) * 5)
    strong_markers = [
        "deploy", "deployment", "implemented", "supplier", "contract", "partnership",
        "autonomous", "predictive", "hazard", "safety", "robot", "permit", "loto",
        "внедр", "контракт", "поставщик", "автоном", "безопас", "робот", "прогноз",
    ]
    score += min(30, sum(4 for marker in strong_markers if marker in text_l))
    if re.search(r"\b\d+[%x]?\b", text_l):
        score += 8
    if any(float(item.get("strength") or 0) >= 0.75 for item in evidence):
        score += 7
    if re.search(r"marketing|webinar|guide|buyer.?s guide|opinion", text_l):
        score -= 12
    return round(max(0, min(100, score)), 2)


def _normalize_signal_payload(payload: dict[str, Any], topic: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
    maturity = str(payload.get("maturity") or "watch").lower()
    if maturity not in {"reject", "watch", "shortlist", "proven"}:
        maturity = "watch"
    context = context or {"title": topic, "raw_text": " ".join(str(payload.get(key) or "") for key in ("title", "summary", "thesis"))}
    title = _trim(str(payload.get("title") or topic), 220)
    title_ru = _trim(str(payload.get("title_ru") or title), 220)
    summary = _trim(str(payload.get("summary") or payload.get("thesis") or ""), 600)
    return {
        "title": title,
        "title_ru": enforce_glossary_text(title_ru, context),
        "theme": _trim(str(payload.get("theme") or topic), 120),
        "summary": enforce_glossary_text(summary, context),
        "thesis": enforce_glossary_text(_trim(str(payload.get("thesis") or ""), 1200), context),
        "transferability": enforce_glossary_text(_trim(str(payload.get("transferability") or ""), 800), context),
        "maturity": maturity,
        "confidence": float(payload.get("confidence") or 0),
        "score": float(payload.get("score") or 0),
        "why_now": enforce_glossary_text(_trim(str(payload.get("why_now") or ""), 800), context),
        "why_not_noise": enforce_glossary_text(_trim(str(payload.get("why_not_noise") or ""), 800), context),
        "companies": [str(x).strip() for x in payload.get("companies") or [] if str(x).strip()][:10],
        "industries": [str(x).strip() for x in payload.get("industries") or [] if str(x).strip()][:10],
    }


def _judge_prompt(evidence: list[dict[str, Any]], topic: str) -> str:
    rows = []
    for index, item in enumerate(evidence[:8], start=1):
        rows.append(
            "\n".join(
                [
                    f"evidence #{index}",
                    f"title: {item.get('title')}",
                    f"publisher: {item.get('publisher')}",
                    f"url: {item.get('source_url')}",
                    f"type: {item.get('evidence_type')}",
                    f"fact: {item.get('extracted_fact')}",
                    f"summary_ru: {item.get('summary_ru')}",
                ]
            )
        )
    glossary = glossary_prompt_block(_glossary_context(evidence, topic))
    glossary_section = f"\n\n{glossary}" if glossary else ""
    return f"topic: {topic}{glossary_section}\n\n" + "\n\n".join(rows)


def _glossary_context(evidence: list[dict[str, Any]], topic: str) -> dict[str, Any]:
    return {
        "title": topic,
        "raw_text": " ".join(
            str(item.get(key) or "")
            for item in evidence
            for key in ("title", "title_ru", "extracted_fact", "summary_ru")
        ),
        "language": "mixed",
    }


def _signal_key(topic: str, title: str, evidence: list[dict[str, Any]]) -> str:
    seed = topic + "|" + title + "|" + "|".join(sorted(str(item.get("source_url") or "") for item in evidence[:5]))
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _signal_title(evidence: dict[str, Any], topic: str) -> str:
    title = str(evidence.get("title") or topic).strip()
    return title if len(title) <= 160 else title[:157].rstrip() + "..."


def _evidence_type(text: str) -> str:
    text_l = text.lower()
    if re.search(r"deploy|implemented|внедр|запуст", text_l):
        return "deployment"
    if re.search(r"supplier|contract|поставщик|контракт", text_l):
        return "supplier"
    if re.search(r"guide|opinion|podcast|колон", text_l):
        return "analysis"
    return "article"


def _extract_companies(text: str) -> list[str]:
    candidates = re.findall(r"\b[A-Z][A-Za-z0-9&.-]{2,}(?:\s+[A-Z][A-Za-z0-9&.-]{2,}){0,2}\b", text)
    seen = []
    for item in candidates:
        if item.upper() in {"AI", "HSE", "PTW", "LOTO", "SIMOPS", "RSS"}:
            continue
        if item not in seen:
            seen.append(item)
    return seen[:8]


def _industries_for_topic(topic: str, text: str) -> list[str]:
    text_l = (topic + " " + text).lower()
    mapping = [
        ("oil and gas", r"oil|gas|drill|нефт|газ|бур"),
        ("mining", r"mining|mine|карьер|горн"),
        ("chemicals", r"chemical|хими"),
        ("industrial logistics", r"fleet|transport|logistics|vehicle|транспорт"),
        ("metals", r"metal|steel|металл"),
    ]
    found = [name for name, pattern in mapping if re.search(pattern, text_l)]
    return found or ["heavy industry"]


def _transferability_for_topic(topic: str) -> str:
    topic_l = topic.lower()
    if "ptw" in topic_l or "control of work" in topic_l:
        return "Переносимо в буровые, ремонты, SIMOPS и подрядные работы через связку нарядов, изоляций, допуска людей и фактического состояния барьеров."
    if "predictive" in topic_l:
        return "Переносимо в HSE и эксплуатацию как раннее предупреждение опасного состояния оборудования, среды или процесса до инцидента."
    if "robot" in topic_l or "physical" in topic_l:
        return "Переносимо в инспекции, обходы, буровую площадку и опасные операции, где главный эффект - снижение присутствия человека в hazardous zones."
    return "Переносимость требует проверки по нефтесервисному сценарию, но тема относится к промышленной безопасности и операционной эффективности."


def _why_not_noise(text: str) -> str:
    if re.search(r"deploy|implemented|supplier|contract|внедр|контракт|поставщик", text.lower()):
        return "Есть признаки применения или коммерческого допуска, а не только общий маркетинговый тезис."
    return "Сигнал оставлен в watchlist: тема переносима, но нужны подтверждения внедрения или измеримого эффекта."


def _trim(value: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) <= limit else value[: limit - 3].rstrip() + "..."


def _slug(value: str) -> str:
    return "-".join(re.findall(r"[a-z0-9а-яё]+", value.lower()))[:80] or "signal"


_STOP_WORDS = {
    "the", "and", "for", "with", "from", "this", "that", "into", "about",
    "как", "или", "для", "что", "это", "при", "над", "под", "после",
}
