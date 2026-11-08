"""Пробный прогон широкого поиска радара (#138, спека #128): один прогон поиска GPT по всем темам за период, находки
проходят «Отбор», в витрины не попадает ничего. По цифрам владелец решает частоту поиска и включение радара.

- plan/estimate — сухой прогон (по умолчанию): темы, число вызовов и оценка цены до оплаты; ничего не пишет.
- enqueue — запуск: задача прогона радара («поиск только находит», как у любого прогона с 08.11) с меткой пробного
  прогона. Ядро пишет находки отдельно от радара (signal_discovery, trial): метка пути
  SIGNAL_SELECTOR_TRIAL и свой ключ, существующие сигналы прогон не меняет.
- after_search — ядро, когда находки записаны: задачи «Отбора» по ним в тени проверки #134 (вид задачи и воркер те же,
  что в работе; ответы проверяет код ядра, otbor.apply_result), решение — в строке находки таблицы проверки
  (otbor_shadow_store.write_signal), а не в полях сигнала: витрины и шаги конвейера его не видят.
- report — отчёт: материалы, доля уже собранного сбором, полнота по эталону эксперта, доля принятого «Отбором», цена.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from oiltech_digest import config, signal_discovery, signal_reference
from oiltech_digest.db import otbor_shadow_store, otbor_store, repository, search_trial_store

TRIGGER = otbor_shadow_store.SEARCH_TRIAL_TRIGGER
# Оценка цены до оплаты. Вызов поиска — средний по прошлым прогонам радара (ai_processing_runs), без них — замер 18.10
# (gpt-5 low: 74 тыс. токенов входа, 5,1 тыс. выхода, 15,6 поиска на вызов); те прогоны просили до 12 событий на тему,
# поэтому верхняя граница растит выход и поиски по потолку. Вызов «Отбора» — замер 03.11 по 2 649 статьям ленты
# (4 900 токенов входа, 600 выхода) плюс повтор при конфликте шагов.
DEFAULT_SEARCH_CALL = {"input_tokens": 74_000.0, "output_tokens": 5_100.0, "web_search_calls": 15.6}
HISTORY_EVENTS_PER_TOPIC = 12
# Повтор вызова поиска при пустом ответе: 7 прогонов 02.10-18.10 сделали 105 вызовов на 84 темы. С ним формула на тех же
# прогонах даёт их фактическую цену ($4,45 за прогон gpt-5 по 12 темам).
SEARCH_RETRY_SHARE = 0.25
OTBOR_CALL = {"input_tokens": 4_900, "output_tokens": 600}
OTBOR_RETRY_SHARE = 0.1
# Нижняя граница числа находок: половина потолка (технический отсев и уже собранное сбором).
FINDINGS_LOW_SHARE = 0.5
# Сколько примеров принятых находок с русским заголовком страницы показать в отчёте.
RUSSIAN_EXAMPLES = 5
# Границы того, что пробный прогон принимает из командной строки: потолок на тему и уровни рассуждения поиска (у
# gpt-6.1-sol нет minimal и none).
MAX_PER_TOPIC = 100
REASONING_LEVELS = ("low", "medium", "high", "xhigh", "max")


class TrialError(ValueError):
    """Пробный прогон нельзя поставить или посчитать: причина — для человека."""


def priced(model: str) -> bool:
    """У модели своя строка цен (config.OPENAI_MODEL_PRICES): иначе оценка шла бы по ставке по умолчанию."""
    name = (model or "").lower()
    return any(name.startswith(prefix) for prefix in config.OPENAI_MODEL_PRICES)


def plan(*, since: date, until: date, per_topic: int | None = None, model: str | None = None,
         reasoning: str | None = None, otbor_model: str | None = None, otbor_reasoning: str | None = None
         ) -> dict[str, Any]:
    """Что прогон сделает: темы (все, и рынок тоже), период, модели и потолок. Без годных моделей отказ."""
    if until < since:
        raise TrialError(f"период пуст: {since.isoformat()} позже {until.isoformat()}")
    model = (model or config.SIGNAL_RESEARCH_MODEL).strip()
    reasoning = (reasoning or config.SIGNAL_RESEARCH_REASONING).strip()
    otbor_model = (otbor_model or config.OTBOR_MODEL).strip()
    otbor_reasoning = (otbor_reasoning or config.OTBOR_REASONING).strip()
    problem = signal_discovery.research_problem(model, reasoning)
    if problem:
        raise TrialError(problem)
    if reasoning not in REASONING_LEVELS:
        raise TrialError(f"уровень рассуждения поиска {reasoning!r}: нужен один из {', '.join(REASONING_LEVELS)}")
    per_topic = config.SIGNAL_RESEARCH_EVENTS_PER_TOPIC if per_topic is None else int(per_topic)
    if not 1 <= per_topic <= MAX_PER_TOPIC:
        raise TrialError(f"материалов на тему от 1 до {MAX_PER_TOPIC}, получено {per_topic}")
    if not otbor_model:
        raise TrialError("модель «Отбора» не задана: --otbor-model или OTBOR_MODEL в .env ядра")
    for name in (model, otbor_model):
        if not priced(name):
            raise TrialError(f"у модели {name} нет строки в OPENAI_MODEL_PRICES: цену до оплаты не посчитать")
    return {
        "topics": signal_discovery.search_topic_names(),
        # Период поиска — days до until (research_period); ноль код прочёл бы как «по умолчанию», поэтому хотя бы день.
        "since": since.isoformat(), "until": until.isoformat(), "days": max(1, (until - since).days),
        "per_topic": per_topic,
        "model": model, "reasoning": reasoning, "otbor_model": otbor_model, "otbor_reasoning": otbor_reasoning,
    }


def estimate(trial: dict[str, Any]) -> dict[str, Any]:
    """Число вызовов и цена до оплаты (USD), нижняя и верхняя граница."""
    topics = len(trial["topics"])
    call = search_trial_store.search_call_history() or DEFAULT_SEARCH_CALL
    breadth = max(1.0, trial["per_topic"] / HISTORY_EVENTS_PER_TOPIC)
    price_in, price_out = config.price_for_model(trial["model"])

    def search_cost(scale: float) -> float:
        tokens = call["input_tokens"] * price_in + call["output_tokens"] * scale * price_out
        per_call = tokens / 1_000_000 + call["web_search_calls"] * scale * config.SIGNAL_WEB_SEARCH_USD_PER_CALL
        return topics * (1 + SEARCH_RETRY_SHARE) * per_call

    otbor_in, otbor_out = config.price_for_model(trial["otbor_model"])
    otbor_call = (OTBOR_CALL["input_tokens"] * otbor_in + OTBOR_CALL["output_tokens"] * otbor_out) / 1_000_000
    otbor_call *= 1 + OTBOR_RETRY_SHARE
    findings_high = topics * trial["per_topic"]
    findings_low = int(findings_high * FINDINGS_LOW_SHARE)
    search = (search_cost(1.0), search_cost(breadth))
    otbor = (findings_low * otbor_call, findings_high * otbor_call)
    return {
        "search_calls": topics, "search_calls_max": topics * 2, "otbor_calls": (findings_low, findings_high),
        "search_usd": tuple(round(value, 2) for value in search), "otbor_usd": tuple(round(value, 2) for value in otbor),
        "total_usd": (round(search[0] + otbor[0], 2), round(search[1] + otbor[1], 2)),
        "history": call is not DEFAULT_SEARCH_CALL,
    }


def enqueue(trial: dict[str, Any]) -> dict[str, Any]:
    """Один широкий прогон: задача радара на полосе агентов NL, одна попытка (прогон оплачивается целиком). Пока идёт
    другой прогон радара — отказ: два прогона одновременно платили бы за одни и те же находки."""
    from oiltech_digest import network_policy

    if repository.has_recent_background_job(kind="signal_discovery", payload_subset={}, lookback_hours=24 * 30,
                                            statuses=("queued", "running", "finalizing")):
        raise TrialError("идёт другой прогон радара: дождитесь его конца")
    payload = {
        "topic": None, "days": trial["days"], "offline": False, "dry_run": False,
        "research_model": trial["model"], "research_reasoning": trial["reasoning"],
        "research_limit": trial["per_topic"], "research_days": trial["days"], "period_end": trial["until"],
        "trigger": TRIGGER,
        "search_trial": {"otbor_model": trial["otbor_model"], "otbor_reasoning": trial["otbor_reasoning"],
                         "since": trial["since"], "until": trial["until"]},
    }
    route = network_policy.route_ai_processing()
    return repository.create_background_job("signal_discovery", payload, queue_name=route.queue_name,
                                            execution_region=route.execution_region, capability=route.capability,
                                            max_attempts=1)


def after_search(job_id: int, applied: dict[str, Any], trial: dict[str, Any]) -> dict[str, Any]:
    """Ядро, когда находки пробного прогона записаны: задачи «Отбора» в тени проверки #134 (shadow) пачками по
    OTBOR_BATCH, по полосе пересчётов. Решения в витрины не идут."""
    from oiltech_digest import network_policy

    ids = sorted({int(value) for topic in applied.get("topics") or [] for value in topic.get("signal_ids") or []})
    route = network_policy.route_ai_bulk()
    batch = config.OTBOR_BATCH
    jobs = [
        repository.create_background_job(
            otbor_store.KIND,
            {"signal_ids": ids[start:start + batch], "model": trial.get("otbor_model"),
             "reasoning": trial.get("otbor_reasoning"), "trigger": TRIGGER, "shadow": True,
             "search_trial_job_id": int(job_id)},
            queue_name=route.queue_name, execution_region=route.execution_region, capability=route.capability,
        )
        for start in range(0, len(ids), batch)
    ]
    return {"findings": len(ids), "otbor_jobs": [int(job["id"]) for job in jobs]}


# --- Отчёт ---------------------------------------------------------------------------------------


def report(job_id: int, *, reference: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Отчёт владельцу по задаче пробного прогона: материалы по темам, доля уже собранного сбором, решения «Отбора»,
    фактическая цена, полнота по эталону эксперта (строки signal_reference.read_reference) и русские заголовки
    принятых."""
    job = repository.get_background_job(int(job_id))
    payload = (job or {}).get("payload_json") or {}
    if job is None or job.get("kind") != "signal_discovery" or not payload.get("search_trial"):
        raise TrialError(f"задача {job_id} не пробный прогон широкого поиска")
    applied = ((job.get("result_json") or {}).get("applied") or {}) if job.get("status") == "ok" else {}
    new_ids = [value for topic in applied.get("topics") or [] for value in topic.get("signal_ids") or []]
    again_ids = [value for topic in applied.get("topics") or [] for value in topic.get("found_again_ids") or []]
    findings = search_trial_store.findings(new_ids)
    verdicts = {row["signal_id"]: _verdict(row) for row in otbor_shadow_store.signal_rows(new_ids)}
    topics = [_topic_row(topic, verdicts) for topic in applied.get("topics") or []]
    otbor_jobs = search_trial_store.otbor_jobs(int(job_id))
    # Материал — ссылка, прошедшая технические проверки; уже собранная сбором — тоже материал поиска.
    materials = sum(topic["links"] for topic in topics)
    collected = sum(topic["already_collected"] for topic in topics)
    decided = [verdict for verdict in verdicts.values() if verdict and verdict.get("decision")]
    accepted = [verdict for verdict in decided if verdict["decision"] == "accept"]
    costs = search_trial_store.costs([int(job_id), *(int(row["id"]) for row in otbor_jobs)])
    result: dict[str, Any] = {
        "job_id": int(job_id), "status": job.get("status"), "trial": payload["search_trial"],
        "model": payload.get("research_model"), "per_topic": payload.get("research_limit"),
        "topics": topics,
        "materials": materials, "new_findings": len(new_ids), "found_again": len(again_ids),
        "already_collected": collected, "already_collected_share": _share(collected, materials),
        "otbor_jobs": [{"id": int(row["id"]), "status": row["status"], "findings": int(row["findings"] or 0)}
                       for row in otbor_jobs],
        "otbor": {
            "decided": len(decided), "accepted": len(accepted), "accepted_share": _share(len(accepted), len(decided)),
            "failed": sum(1 for verdict in verdicts.values() if verdict and verdict.get("failure")),
            "waiting": len(set(new_ids) - set(verdicts)),
            "disputed": sum(1 for verdict in decided if verdict.get("disputed")),
            "by_category": _count(verdict.get("category") or "без категории" for verdict in accepted),
            "reject_reasons": _count(verdict.get("reject_reason") or "без причины" for verdict in decided
                                     if verdict["decision"] != "accept"),
        },
        "russian_titles": _russian_titles(findings, verdicts),
        "cost_usd": {**costs, "total": round(sum(costs.values()), 4)},
    }
    if reference is not None:
        collected_links = [item for topic in applied.get("topics") or [] for item in topic.get("already_collected") or []]
        result["recall"] = _recall(reference, findings, again_ids, collected_links, payload["search_trial"])
    return result


def _topic_row(topic: dict[str, Any], verdicts: dict[int, Any]) -> dict[str, Any]:
    """Тема в отчёте (и база будущей ОС поиска по темам): сколько принёс поиск, сколько отсеяли технические проверки,
    сколько ссылок прошло их (links: и уже собранные сбором — воркер отбросил их до докачки страницы), сколько из них
    в сборе, сколько новых находок после «Дедупа», сколько из них принял «Отбор» и сколько сигналов платформы найдено
    снова."""
    research = topic.get("research") or {}
    decided = [verdicts.get(int(value)) or {} for value in topic.get("signal_ids") or []]
    return {
        "topic": topic.get("topic"), "status": topic.get("web_status"), "error": topic.get("web_error"),
        "found": int(research.get("events") or 0) + int(research.get("dropped") or 0),
        "dropped": int(research.get("dropped") or 0) + int(research.get("unverified") or 0),
        "links": int(research.get("evidence_count") or 0) + int(research.get("already_collected") or 0),
        "already_collected": len(topic.get("already_collected") or []),
        "new": len(topic.get("signal_ids") or []), "found_again": len(topic.get("found_again_ids") or []),
        "accepted": sum(1 for verdict in decided if verdict.get("decision") == "accept"),
        "rejected": sum(1 for verdict in decided if verdict.get("decision") == "reject"),
        "web_search_calls": int(research.get("web_search_calls") or 0),
    }


def _verdict(row: dict[str, Any]) -> dict[str, Any]:
    """Решение «Отбора» по находке из её строки тени: решение, категория, причина отказа, «спорное», сбой."""
    values = row.get("values_json") or {}
    return {"decision": row.get("decision"), "category": values.get("signal_category"),
            "reject_reason": values.get("reject_reason"), "disputed": bool(row.get("disputed")),
            "failure": row.get("failure")}


def _share(part: int, whole: int) -> float:
    return round(part / whole, 3) if whole else 0.0


def _count(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[str(value)] = counts.get(str(value), 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _cyrillic(text: str) -> bool:
    letters = [char for char in text or "" if char.isalpha()]
    return bool(letters) and sum("Ѐ" <= char <= "ӿ" for char in letters) / len(letters) >= 0.5


def _russian_titles(findings: list[dict[str, Any]], verdicts: dict[int, Any]) -> dict[str, Any]:
    """Сколько принятых находок с русским заголовком страницы: он идёт без правил заголовка «Сути и перевода»
    (перевод правит только иноязычный), решение по нему — по этим цифрам."""
    accepted = [row for row in findings if (verdicts.get(row["id"]) or {}).get("decision") == "accept"]
    russian = [row for row in accepted if _cyrillic(str(row.get("title") or ""))]
    return {"accepted": len(accepted), "russian": len(russian), "share": _share(len(russian), len(accepted)),
            "examples": [str(row["title"])[:160] for row in russian[:RUSSIAN_EXAMPLES]]}


def _recall(reference: list[dict[str, Any]], findings: list[dict[str, Any]], again_ids: list[Any],
            collected_links: list[dict[str, Any]], trial: dict[str, Any]) -> dict[str, Any]:
    """Полнота по эталону эксперта (signal_reference.recall): что нашёл поиск (новые находки, сигналы платформы,
    найденные снова, и ссылки, которые уже были в сборе), что есть в сборе за период и что не нашёл никто."""
    search_cards = [{"id": f"signal:{row['id']}", "title": row.get("title"), "urls": row.get("urls") or []}
                    for row in findings]
    search_cards += [{**row, "id": f"signal:{row['id']}"} for row in search_trial_store.platform_cards(again_ids)]
    search_cards += [{"id": f"collected:{number}", "title": item.get("title"), "urls": [item.get("url")]}
                     for number, item in enumerate(collected_links)]
    since = date.fromisoformat(trial["since"]) - timedelta(days=signal_discovery.COLLECTED_MARGIN_DAYS)
    collected_cards = [{**row, "id": f"article:{row['id']}"} for row in search_trial_store.collected_cards(since=since)]
    by_search = signal_reference.recall(reference, search_cards)
    by_collection = signal_reference.recall(reference, collected_cards)
    found_search = {item["title"] for item in by_search["found_items"]}
    found_collection = {item["title"] for item in by_collection["found_items"]}
    titles = [row["title"] for row in reference]
    # База будущей ОС поиска (решение владельца 03.11): по каждой строке эталона и по направлению эксперта — что нашёл
    # поиск, что есть только в сборе, что пропущено.
    items = [{"title": row["title"], "direction": row.get("direction") or "без направления",
              "search": row["title"] in found_search, "collection": row["title"] in found_collection}
             for row in reference]
    by_direction: dict[str, dict[str, int]] = {}
    for item in items:
        counts = by_direction.setdefault(item["direction"], {"total": 0, "search": 0, "either": 0})
        counts["total"] += 1
        counts["search"] += int(item["search"])
        counts["either"] += int(item["search"] or item["collection"])
    return {
        "total": len(titles),
        "search": len(found_search), "collection": len(found_collection),
        "either": len(found_search | found_collection),
        "search_only": sorted(found_search - found_collection),
        "missed": [title for title in titles if title not in found_search | found_collection],
        "search_share": _share(len(found_search), len(titles)),
        "either_share": _share(len(found_search | found_collection), len(titles)),
        "items": items, "by_direction": by_direction,
    }
