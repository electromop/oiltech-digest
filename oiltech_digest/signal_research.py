"""«Режим ChatGPT» радара: находки темы — одним вызовом модели со встроенным поиском OpenAI.

Замечание заказчика 29.09: «ChatGPT по запросу за сентябрь выдал топ-базу из 20 штук», а
радар на поиске Brave и дешёвом судье — слабее. Разница — в сильной модели, которая сама
делает десятки поисковых запросов, отбрасывает SEO-мусор и сравнивает события между собой.
Здесь ровно это: на тему один вызов Responses API с инструментом web_search и строгим JSON.

Модель только находит. Проверяет наш код, как у любой находки радара: страница докачивается
(живая ли ссылка и о том ли она), дальше — наш судья (категория, дата события, смешанные
события, тема, баллы профиля), ревью пачки, дедуп и запись на ядре. Модуль без базы — идёт
на NL-воркере вместе с run_discovery.
"""

from __future__ import annotations

from datetime import date, timedelta
import re
import time
from typing import Any, Callable

import requests

from oiltech_digest import config as app_config
from oiltech_digest.processing.openai_client import AIClientError, output_budget
from oiltech_digest.processing.pipeline import make_client

EVIDENCE_SOURCE = "openai_web_search"

RESEARCH_INSTRUCTIONS = """Ты — аналитик технологического радара нефтесервисной компании
(бурение, заканчивание, ГРП, КРС, добыча, промысловая инфраструктура, HSE, цифровизация).

Найди в интернете главные ТЕХНОЛОГИЧЕСКИЕ события по теме за указанный период — те, что важны
НЕФТЕСЕРВИСУ. Тема задаёт направление, а не отрасль: событие должно быть в нефтегазе или
нефтесервисе (оператор, сервисная компания, промысел, скважина, трубопровод промысла) либо из
другой отрасли, но с очевидным применением на конкретной операции нефтесервиса. Не приноси:
коммунальную и сетевую энергетику, солнечные и ветровые станции для городов и курортов,
грузоперевозки по общим трассам, общие SaaS и ИИ-сервисы, судоходство без связи с промыслом —
даже если тема («Энергетика», «Логистика», «Автоматизация») формально их покрывает. Событие — один
конкретный факт: внедрение, промышленное испытание, пилот, запуск продукта с первым заказчиком,
измеримый технический эффект (KPI), контракт на технологию. Не событие: обзор рынка, прогноз,
отчёт аналитиков, рейтинг, вебинар, каталог услуг, реклама без факта применения.

Правила:
- дата события — внутри периода; событие вне периода не включай;
- ссылка — первоисточник: пресс-релиз или новость компании-участника, отраслевое издание
  (World Oil, JPT, Offshore, Hart Energy, Rigzone, Нефтегаз.ру, «Нефтегазовая вертикаль»,
  сайты «Газпром нефти», «Роснефти», SLB, Halliburton, Baker Hughes и т. п.). Не агрегаторы,
  не пресс-релизы маркетинговых отчётов (researchandmarkets, openpr), не SEO-блоги;
- ссылка — на страницу самого события, а не на раздел сайта или главную;
- одно событие — один пункт; разные события одной компании — разные пункты;
- ищи и на английском, и на русском; российские события важны не меньше мировых;
- лучше меньше, но настоящих: не добавляй событие, ссылку на которое ты не нашёл поиском;
- summary и why_important — по-русски, 1–3 предложения, с фактами (кто, что, где, цифры).
Упорядочь по значимости для нефтесервиса: первое — самое важное."""

RESEARCH_SCHEMA = {
    "name": "radar_topic_research",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["events"],
        "properties": {
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["title", "company", "event_date", "summary", "why_important",
                                 "source_url", "publisher", "source_kind"],
                    "properties": {
                        "title": {"type": "string"},
                        "company": {"type": "string"},
                        "event_date": {"type": "string"},
                        "summary": {"type": "string"},
                        "why_important": {"type": "string"},
                        "source_url": {"type": "string"},
                        "publisher": {"type": "string"},
                        "source_kind": {"type": "string", "enum": ["primary", "trade_press", "secondary"]},
                    },
                },
            },
        },
    },
}

_STRENGTH = {"primary": 0.9, "trade_press": 0.82, "secondary": 0.6}
RESEARCH_ATTEMPTS = 2
RESEARCH_RETRY_PAUSE_SECONDS = 20.0
RETRY_HINT = ("\n\nПредыдущая попытка не нашла ни одного события. Поищи шире: на английском и на русском, "
              "по крупным сервисным компаниям и операторам, по отраслевым изданиям темы.\n")


def _add_usage(stats: dict[str, Any], response: Any) -> None:
    """Расход — за все попытки: платим и за пустую."""
    data = response.data or {}
    stats["web_search_calls"] += int(data.get("_web_search_calls") or 0)
    stats["input_tokens"] += int(getattr(response, "input_tokens", 0) or 0)
    stats["output_tokens"] += int(getattr(response, "output_tokens", 0) or 0)


def research_topic(
    topic: str,
    *,
    period_end: date | None = None,
    days: int | None = None,
    limit: int | None = None,
    topic_context: str = "",
    client_factory: Callable[[], Any] | None = None,
    heartbeat: Callable[[], None] | None = None,
    feedback: str = "",
) -> dict[str, Any]:
    """События темы за период с первоисточниками. Ошибка модели — пустой итог со статусом,
    а не падение прогона: у темы остаётся поиск Brave, если он включён."""
    end = period_end or date.today()
    start = end - timedelta(days=int(days or app_config.SIGNAL_RESEARCH_DAYS))
    count = int(limit or app_config.SIGNAL_RESEARCH_EVENTS_PER_TOPIC)
    stats: dict[str, Any] = {"status": "ok", "provider": "openai_web_search", "model": app_config.SIGNAL_RESEARCH_MODEL,
                             "period": [start.isoformat(), end.isoformat()], "events": 0, "dropped": [],
                             "feedback_chars": len(feedback.strip()),
                             "web_search_calls": 0, "input_tokens": 0, "output_tokens": 0}
    prompt = (
        f"Тема: {topic}\n"
        f"Период: с {start.isoformat()} по {end.isoformat()}\n"
        f"Сколько событий: до {count}\n"
        + (f"\nЧто входит в тему:\n{topic_context.strip()}\n" if topic_context.strip() else "")
        # Обратная связь заказчика: что одобрено, что отклонено, кому доверяет, что уже есть.
        + (f"\n{feedback.strip()}\n" if feedback.strip() else "")
    )
    client = (client_factory or (lambda: make_client(False)))()
    response = None
    errors: list[str] = []
    # Модель с поиском отвечает по-разному от вызова к вызову: сравнительный прогон 30.09 —
    # на теме автоматизации первая попытка дала события, повтор — пустой список после 15
    # поисков. Пустой ответ и временный сбой — ещё одна попытка с подсказкой искать шире.
    beat = heartbeat or (lambda: None)
    for attempt in range(1, RESEARCH_ATTEMPTS + 1):
        # Признак продвижения перед каждой попыткой: вызов с поиском молчит до
        # SIGNAL_RESEARCH_TIMEOUT_SECONDS, а воркер NL снимает задачу как зависшую после
        # 1200 с без признаков (external_worker._JOB_STALL_SECONDS) — две попытки подряд
        # без beat подходили к порогу вплотную.
        beat()
        attempt_prompt = prompt if attempt == 1 else prompt + RETRY_HINT
        try:
            response = client.research_json(
                RESEARCH_INSTRUCTIONS,
                attempt_prompt,
                RESEARCH_SCHEMA,
                model=app_config.SIGNAL_RESEARCH_MODEL,
                reasoning_effort=app_config.SIGNAL_RESEARCH_REASONING,
                # Ответ — до count событий по ~150 токенов плюс рассуждение между поисками.
                max_output_tokens=output_budget(1500 + 250 * count, app_config.SIGNAL_RESEARCH_REASONING),
                timeout=app_config.SIGNAL_RESEARCH_TIMEOUT_SECONDS,
            )
        except AttributeError as exc:  # клиент без поиска (офлайн) — повтор не поможет
            return {**stats, "status": "error", "error": f"{type(exc).__name__}: {str(exc)[:300]}", "evidence": []}
        except (AIClientError, requests.RequestException) as exc:
            errors.append(f"{type(exc).__name__}: {str(exc)[:300]}")
            response = None
            if attempt < RESEARCH_ATTEMPTS:
                # Короткий обрыв сети (прогон 30.09: DNS на 9 темах подряд) без паузы съедал
                # обе попытки за секунду.
                time.sleep(RESEARCH_RETRY_PAUSE_SECONDS * attempt)
            continue
        _add_usage(stats, response)
        if (response.data or {}).get("events"):
            break
        errors.append("пустой список событий")
    stats["attempts"] = attempt
    if errors:
        stats["retries"] = errors
    if response is None:
        return {**stats, "status": "error", "error": errors[-1] if errors else "no response", "evidence": []}
    data = response.data or {}
    cited = {_url_key(url) for url in data.get("_cited_urls") or []}
    stats["model"] = getattr(response, "model", None) or stats["model"]
    evidence = []
    for event in (data.get("events") or [])[:count]:
        item, reason = _event_to_evidence(event, topic, start=start, end=end, cited=cited)
        if item is None:
            stats["dropped"].append({"title": str(event.get("title") or "")[:120], "reason": reason})
            continue
        evidence.append(item)
    stats["events"] = len(evidence)
    return {**stats, "evidence": evidence}


def _event_to_evidence(
    event: dict[str, Any],
    topic: str,
    *,
    start: date,
    end: date,
    cited: set[str],
) -> tuple[dict[str, Any] | None, str]:
    url = str(event.get("source_url") or "").strip()
    title = re.sub(r"\s+", " ", str(event.get("title") or "")).strip()
    if not re.match(r"https?://[^/\s]+\.[^/\s]+", url) or not title:
        return None, "нет ссылки или заголовка"
    url = _strip_tracking(url)
    event_date = _parse_date(event.get("event_date"))
    # Дата вне периода — модель нарушила условие; без даты оставляем: судья найдёт её сам.
    if event_date and not (start - timedelta(days=3) <= event_date <= end + timedelta(days=1)):
        return None, f"дата события {event_date.isoformat()} вне периода"
    summary = str(event.get("summary") or "").strip()
    why = str(event.get("why_important") or "").strip()
    kind = str(event.get("source_kind") or "secondary")
    return {
        "article_id": None,
        "source_url": url,
        "title": title,
        "title_ru": title,
        "publisher": str(event.get("publisher") or "").strip() or _domain(url),
        "published_at": event_date.isoformat() if event_date else None,
        "evidence_type": "news",
        "extracted_fact": " ".join(part for part in (summary, why) if part),
        "summary_ru": summary,
        "strength": _STRENGTH.get(kind, 0.6),
        "topic": topic,
        "raw_payload": {
            "evidence_source": EVIDENCE_SOURCE,
            "company": str(event.get("company") or "").strip(),
            "source_kind": kind,
            "why_important": why,
            "research_summary": summary,
            # Сослалась ли модель на этот адрес в найденном — признак, что он из поиска, а
            # не из памяти модели. Вместе с докачкой страницы решает, верить ли ссылке.
            "cited_by_search": _url_key(url) in cited,
        },
    }, ""


def verify_research_evidence(evidence: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """После докачки страниц: оставить подтверждённые находки.

    Подтверждена, если страница открылась (full_text_fetched) или модель сослалась на адрес в
    найденном (cited_by_search: сайт мог закрыться от бота). Ни то ни другое — вероятно,
    выдуманная ссылка: такой находке судья поверил бы на слово. Сводку модели возвращаем в
    summary_ru — докачка заменила её текстом страницы, а судье нужны обе."""
    kept, dropped = [], []
    for item in evidence:
        payload = item.get("raw_payload") or {}
        if payload.get("evidence_source") != EVIDENCE_SOURCE:
            kept.append(item)
            continue
        if payload.get("full_text_fetched") or payload.get("cited_by_search"):
            kept.append({**item, "summary_ru": payload.get("research_summary") or item.get("summary_ru")})
        else:
            dropped.append({"title": str(item.get("title") or "")[:120], "url": item.get("source_url"),
                            "reason": "страница не открылась и адрес не из найденного"})
    return kept, dropped


def is_research_evidence(item: dict[str, Any]) -> bool:
    return (item.get("raw_payload") or {}).get("evidence_source") == EVIDENCE_SOURCE


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _strip_tracking(url: str) -> str:
    # Поиск OpenAI дописывает utm_source=openai — в ключе ссылки ему не место.
    url = re.sub(r"([?&])utm_[a-z]+=[^&#]*", r"\1", url)
    return re.sub(r"[?&]+(#|$)", r"\1", url).rstrip("?&")


def _url_key(url: str) -> str:
    value = re.sub(r"^https?://", "", _strip_tracking(str(url or "")).strip().lower())
    return value.split("#", 1)[0].rstrip("/").removeprefix("www.")


def _domain(url: str) -> str:
    return _url_key(url).split("/", 1)[0]
