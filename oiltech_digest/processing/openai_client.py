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
        return (
            self.input_tokens * config.OPENAI_INPUT_USD_PER_MTOK
            + self.output_tokens * config.OPENAI_OUTPUT_USD_PER_MTOK
        ) / 1_000_000


class OpenAIResponsesClient:
    provider = "openai"

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key if api_key is not None else config.OPENAI_API_KEY
        self.model = model or config.OPENAI_MODEL

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
            timeout=config.OPENAI_TIMEOUT,
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
                "total_score": 50,
                "score_label": "Средняя",
                "explanation": "offline fallback",
                "items": [],
            }
        else:
            data = {}
        output = json.dumps(data, ensure_ascii=False)
        return AIResponse(data=data, model=self.model, input_tokens=approx_input, output_tokens=len(output) // 4)


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
