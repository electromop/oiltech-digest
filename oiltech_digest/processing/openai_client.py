"""Thin OpenAI Responses API client with structured JSON output.

The project intentionally uses `requests` instead of a heavyweight SDK so the
runtime stays small and token usage is captured from the raw API response.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import requests

from oiltech_digest import config


class AIClientError(RuntimeError):
    """Raised when an AI provider call fails or returns malformed output."""


@dataclass(frozen=True)
class AIResponse:
    data: dict[str, Any]
    model: str
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cost_usd(self) -> float:
        input_rate, output_rate = config.price_for_model(self.model)
        return (
            self.input_tokens * input_rate
            + self.output_tokens * output_rate
        ) / 1_000_000


class OpenAIResponsesClient:
    provider = "openai"

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key if api_key is not None else config.OPENAI_API_KEY
        self.model = model or config.OPENAI_MODEL
        # Радар ставит свой: сильная модель с рассуждением думает дольше OPENAI_TIMEOUT ленты.
        self.timeout = config.OPENAI_TIMEOUT

    def complete_json(self, instructions: str, user_input: str,
                      schema: dict[str, Any], max_output_tokens: int = 900,
                      model: str | None = None, reasoning_effort: str | None = None) -> AIResponse:
        if not self.api_key:
            raise AIClientError("OPENAI_API_KEY is empty")

        used_model = model or self.model
        payload: dict[str, Any] = {
            "model": used_model,
            "instructions": instructions,
            "input": user_input,
            "store": False,
            "max_output_tokens": max_output_tokens,
            "text": {
                "verbosity": "low",
                "format": {
                    "type": "json_schema",
                    "name": schema["name"],
                    "strict": True,
                    "schema": schema["schema"],
                }
            },
        }
        effort = _reasoning_effort(used_model, reasoning_effort or config.OPENAI_REASONING_EFFORT)
        if effort:
            payload["reasoning"] = {"effort": effort}

        response = requests.post(
            f"{config.OPENAI_BASE_URL.rstrip('/')}/responses",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise AIClientError(f"OpenAI API error {response.status_code}: {response.text[:500]}")

        raw = response.json()
        text = _extract_output_text(raw)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AIClientError(f"OpenAI returned non-JSON output: {text[:500]}") from exc

        usage = raw.get("usage") or {}
        return AIResponse(
            data=data,
            model=raw.get("model") or used_model,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        )


    def research_json(self, instructions: str, user_input: str, schema: dict[str, Any], *,
                      model: str, reasoning_effort: str | None, max_output_tokens: int,
                      timeout: float, search_context_size: str = "medium") -> AIResponse:
        """Вызов с встроенным поиском OpenAI (инструмент web_search) и строгим JSON на выходе.

        Отдельный метод, а не параметр complete_json: его подменяют десятки тестов, а лента
        поиском не пользуется. Кроме данных, возвращает адреса, на которые модель сослалась
        (аннотации url_citation), и число поисковых вызовов — по ним видно, что ответ
        опирается на найденное, а не на память модели."""
        if not self.api_key:
            raise AIClientError("OPENAI_API_KEY is empty")
        payload: dict[str, Any] = {
            "model": model,
            "instructions": instructions,
            "input": user_input,
            "store": False,
            "max_output_tokens": max_output_tokens,
            "tools": [{"type": "web_search", "search_context_size": search_context_size}],
            # Адреса, которые поиск реально вернул. Сносок (url_citation) при строгом JSON
            # модель не ставит — прогон 30.09: 0 из 18, — и проверять ссылку было не по чему.
            "include": ["web_search_call.action.sources"],
            "text": {"format": {"type": "json_schema", "name": schema["name"], "strict": True,
                                "schema": schema["schema"]}},
        }
        effort = _reasoning_effort(model, reasoning_effort)
        if effort:
            payload["reasoning"] = {"effort": effort}
        response = requests.post(
            f"{config.OPENAI_BASE_URL.rstrip('/')}/responses",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        if response.status_code >= 400:
            raise AIClientError(f"OpenAI API error {response.status_code}: {response.text[:500]}")
        raw = response.json()
        data = _research_payload(raw)
        cited: list[str] = []
        searches = 0
        for item in raw.get("output") or []:
            if item.get("type") == "web_search_call":
                searches += 1
                for source in (item.get("action") or {}).get("sources") or []:
                    if isinstance(source, dict) and source.get("url"):
                        cited.append(str(source["url"]))
            for content in item.get("content") or []:
                for note in content.get("annotations") or []:
                    if note.get("type") == "url_citation" and note.get("url"):
                        cited.append(str(note["url"]))
        usage = raw.get("usage") or {}
        return AIResponse(
            data={**data, "_cited_urls": cited, "_web_search_calls": searches},
            model=raw.get("model") or model,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        )


def _research_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """JSON ответа с поиском. Модель между поисками пишет несколько сообщений и может дописать
    текст после объекта (сравнительный прогон 30.09: две темы из трёх — «non-JSON output» при
    нормальном списке событий внутри). Берём первый объект из каждой части и из них — тот, где
    больше событий; json.loads целиком этого не умеет."""
    chunks = [
        str(content["text"])
        for item in raw.get("output") or [] if item.get("type") == "message"
        for content in item.get("content") or []
        if content.get("type") in {"output_text", "text"} and content.get("text")
    ]
    if not chunks:
        _extract_output_text(raw)  # поднимет «ответ без текста» с подробностями
    decoder = json.JSONDecoder()
    best: dict[str, Any] | None = None
    for chunk in chunks:
        start = chunk.find("{")
        while start != -1:
            try:
                candidate, _ = decoder.raw_decode(chunk, start)
            except json.JSONDecodeError:
                start = chunk.find("{", start + 1)
                continue
            if isinstance(candidate, dict) and (
                best is None or len(candidate.get("events") or []) > len(best.get("events") or [])
            ):
                best = candidate
            break
    if best is None:
        raise AIClientError(f"OpenAI returned non-JSON output: {' | '.join(chunks)[:500]}")
    return best


# Сколько токенов ответа добавить на рассуждение: лимиты вызовов подбирались под minimal, и на
# medium модель тратила весь лимит на рассуждение — «ответ без текста» (прогон 28.09).
_REASONING_OUTPUT_EXTRA = {"none": 0, "minimal": 0, "low": 2000, "medium": 6000, "high": 14000, "xhigh": 20000}


def output_budget(base: int, reasoning_effort: str | None) -> int:
    """Лимит ответа с запасом на рассуждение; неизвестный уровень — как medium."""
    effort = (reasoning_effort or "").strip().lower()
    return int(base) + _REASONING_OUTPUT_EXTRA.get(effort, _REASONING_OUTPUT_EXTRA["medium"] if effort else 0)


class OfflineAIClient:
    """Deterministic local client for tests and dry development without API keys."""

    provider = "offline"
    model = "offline-deterministic"

    def complete_json(self, instructions: str, user_input: str,
                      schema: dict[str, Any], max_output_tokens: int = 900,
                      model: str | None = None, reasoning_effort: str | None = None) -> AIResponse:
        text = re.sub(r"\s+", " ", user_input).strip()
        approx_input = max(1, len(text) // 4)
        name = schema["name"]
        if name == "article_summary":
            title = _field(user_input, "title") or "Материал"
            body = _field(user_input, "text") or text
            summary = _trim_sentences(body, 2) or title
            data = {"summary": f"{title}: {summary}"[:900]}
        elif name == "article_title_translation":
            title = _field(user_input, "title") or "Материал"
            data = {"title_ru": title[:200]}
        elif name == "article_relevance":
            data = {"relevant": True, "reason": "offline fallback (релевантность не проверяется без API)"}
        elif name == "article_tag":
            data = {"tag_id": 0, "confidence": 0.35, "rationale": "offline fallback"}
        elif name == "article_score":
            data = {
                "incident_without_solution": False,
                "total_score": 50,
                "score_label": "Средняя",
                "explanation": "offline fallback",
                "items": [],
            }
        elif name == "document_chunk":
            # Офлайн-разбор фрагмента: берём ПЕРВОЕ настоящее число из текста и первый
            # якорь фрагмента. Так проверяльщик фактов получает вход, который ДОЛЖЕН
            # подтвердиться, — иначе тест сквозного пути не отличит рабочую сверку
            # от сверки, которая всегда возвращает False.
            anchor = _first_int(user_input, "якоря во фрагменте: с") or 1
            match = re.search(r"\b\d[\d\u00a0 ]*(?:[.,]\d+)?\b", _after_marker(user_input, "текст:"))
            facts = []
            if match:
                facts = [{"value": match.group(0).strip(), "unit": None,
                          "context": "offline fallback", "anchor": anchor}]
            data = {"about": _trim_sentences(_after_marker(user_input, "текст:"), 1)[:400] or "offline fallback",
                    "facts": facts}
        elif name == "document_card":
            data = {
                "passport": {"doc_type": "иное", "publisher": None, "date": None, "language": "ru"},
                "essence": _trim_sentences(text, 2)[:600] or "offline fallback",
                "summary": [line[:200] for line in text.split("[")[1:6]] or ["offline fallback"],
                "claims": [],
            }
        else:
            data = {}
        output = json.dumps(data, ensure_ascii=False)
        return AIResponse(data=data, model=self.model, input_tokens=approx_input, output_tokens=len(output) // 4)



def _after_marker(text: str, marker: str) -> str:
    index = text.find(marker)
    return text[index + len(marker):] if index >= 0 else text


def _first_int(text: str, marker: str) -> int | None:
    tail = _after_marker(text, marker) if marker in text else ""
    match = re.search(r"\d+", tail)
    return int(match.group(0)) if match else None

def _extract_output_text(raw: dict[str, Any]) -> str:
    if raw.get("output_text"):
        return str(raw["output_text"])
    chunks: list[str] = []
    refusals: list[str] = []
    for item in raw.get("output") or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                chunks.append(str(content["text"]))
            if content.get("type") == "refusal" and content.get("refusal"):
                refusals.append(str(content["refusal"]))
    if not chunks:
        details = []
        if raw.get("status"):
            details.append(f"status={raw['status']}")
        if raw.get("incomplete_details"):
            details.append(f"incomplete_details={raw['incomplete_details']}")
        if refusals:
            details.append(f"refusal={' | '.join(refusals)}")
        usage = raw.get("usage")
        if usage:
            details.append(f"usage={usage}")
        suffix = "; ".join(details) if details else "no details"
        raise AIClientError(f"OpenAI response does not contain output text ({suffix})")
    return "\n".join(chunks)


def _reasoning_effort(model: str, configured: str | None) -> str | None:
    """Normalize reasoning effort across GPT-5 generations.

    GPT-5.1 и новее (5.1, 5.2, 5.3, 5.4, 5.5, …) поддерживают `none` и НЕ принимают
    `minimal`; исходный GPT-5 (gpt-5/-mini/-nano) — наоборот: принимает `minimal`,
    но не `none`. Подбираем эффективный дефолт под поколение и переводим
    несовместимые значения, чтобы не ловить 400 при апгрейде модели.
    """
    value = (configured or "").strip().lower()
    model_name = (model or "").lower()
    match = re.match(r"gpt-5\.(\d+)", model_name)
    supports_none = bool(match and int(match.group(1)) >= 1)  # семейство 5.1+
    if not value:
        return "none" if supports_none else "minimal"
    if supports_none and value == "minimal":
        return "none"
    if not supports_none and value == "none" and model_name.startswith("gpt-5"):
        return "minimal"
    return value or None


def _field(text: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}:\s*(.+)$", text, flags=re.MULTILINE | re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _trim_sentences(text: str, max_sentences: int) -> str:
    parts = re.split(r"(?<=[.!?。])\s+", text.strip())
    return " ".join(part for part in parts[:max_sentences] if part)
