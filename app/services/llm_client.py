"""The ONLY module that talks to an LLM (ground rule #9).

Two providers, chosen by settings.LLM_PROVIDER — the rest of the app never
knows which one is live:
- gemini:    Google AI Studio REST API via httpx (free tier; AIza... key)
- anthropic: Claude via the anthropic SDK (sk-ant-... key)

Two model tiers either way:
- CHEAP: classification, extraction — costs paise (or nothing) per call
- SMART: composing customer-facing replies

Design rules enforced at this layer:
- Every call logged with provider + model + latency + tokens.
- Transient failures (network, 429, 5xx, bad key) raise LLMUnavailable —
  callers MUST catch it and fall back to rule-based behavior (ground rule
  #5: degrade, never go silent).
- ask_json() uses structured outputs (JSON schema) on both providers so
  the model cannot return malformed data.
"""

import json
import time

import httpx
import structlog
from anthropic import (
    APIConnectionError,
    APIStatusError,
    AsyncAnthropic,
    AuthenticationError,
    RateLimitError,
)

from app.config import settings

log = structlog.get_logger()

PROVIDER = settings.LLM_PROVIDER

if PROVIDER == "gemini":
    MODEL_CHEAP = "gemini-3.5-flash-lite"
    MODEL_SMART = "gemini-3.5-flash"
else:
    MODEL_CHEAP = "claude-haiku-4-5"
    MODEL_SMART = "claude-sonnet-5"

_client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class LLMError(Exception):
    """LLM produced something unusable (bad JSON etc.)."""


class LLMUnavailable(LLMError):
    """LLM could not be reached / rate limited / server error / bad key.

    Callers must degrade to rule-based behavior, never crash.
    """


async def ask(
    *,
    system: str,
    user_text: str,
    model: str | None = None,
    max_tokens: int = 500,
) -> str:
    """Plain text completion. Raises LLMUnavailable / LLMError."""
    model = model or MODEL_CHEAP
    if PROVIDER == "gemini":
        return await _gemini_generate(system, user_text, model, max_tokens, schema=None)
    return await _anthropic_generate(system, user_text, model, max_tokens, output_config=None)


async def ask_json(
    *,
    system: str,
    user_text: str,
    schema: dict,
    model: str | None = None,
    max_tokens: int = 500,
) -> dict:
    """Completion constrained to a JSON schema (structured outputs).

    Returns the parsed dict. Raises LLMUnavailable / LLMError.
    """
    model = model or MODEL_CHEAP
    if PROVIDER == "gemini":
        text = await _gemini_generate(system, user_text, model, max_tokens, schema=schema)
    else:
        text = await _anthropic_generate(
            system,
            user_text,
            model,
            max_tokens,
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        log.error("llm_bad_json", provider=PROVIDER, model=model, text=text[:200])
        raise LLMError(f"model returned invalid JSON: {text[:100]}") from exc


# --------------------------------------------------------------------------
# Gemini (REST via httpx — no SDK dependency)
# --------------------------------------------------------------------------


async def _gemini_post(model: str, payload: dict) -> httpx.Response:
    """One HTTP call, isolated so tests can fake it."""
    async with httpx.AsyncClient(timeout=60) as client:
        return await client.post(
            f"{_GEMINI_BASE}/{model}:generateContent",
            params={"key": settings.GEMINI_API_KEY},
            json=payload,
        )


def _gemini_schema(schema: dict) -> dict:
    """Gemini's responseSchema rejects additionalProperties — strip it."""
    if isinstance(schema, dict):
        return {
            k: _gemini_schema(v)
            for k, v in schema.items()
            if k != "additionalProperties"
        }
    if isinstance(schema, list):
        return [_gemini_schema(v) for v in schema]
    return schema


async def _gemini_generate(
    system: str, user_text: str, model: str, max_tokens: int, schema: dict | None
) -> str:
    # Floor the budget: Gemini spends output tokens on internal thinking,
    # and a truncated JSON answer is worse than a slightly pricier call.
    gen: dict = {"maxOutputTokens": max(max_tokens, 512)}
    if schema is not None:
        gen["responseMimeType"] = "application/json"
        gen["responseSchema"] = _gemini_schema(schema)
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": gen,
    }

    started = time.monotonic()
    try:
        resp = await _gemini_post(model, payload)
    except httpx.HTTPError as exc:
        log.warning("llm_unavailable", provider="gemini", model=model, error=str(exc)[:150])
        raise LLMUnavailable(str(exc)) from exc

    if resp.status_code in (401, 403):
        log.error("llm_auth_failed", provider="gemini", model=model)
        raise LLMUnavailable("invalid GEMINI_API_KEY")
    if resp.status_code == 429 or resp.status_code >= 500:
        log.warning("llm_unavailable", provider="gemini", model=model, status=resp.status_code)
        raise LLMUnavailable(f"gemini HTTP {resp.status_code}")
    if resp.status_code != 200:
        log.error(
            "llm_rejected", provider="gemini", model=model,
            status=resp.status_code, error=resp.text[:200],
        )
        raise LLMError(f"gemini HTTP {resp.status_code}: {resp.text[:150]}")

    data = resp.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError) as exc:
        # No candidates usually means a safety block — unusable, not down.
        log.error("llm_empty_response", provider="gemini", model=model, body=str(data)[:200])
        raise LLMError(f"gemini returned no text: {str(data)[:100]}") from exc

    usage = data.get("usageMetadata", {})
    log.info(
        "llm_call",
        provider="gemini",
        model=model,
        kind="json" if schema is not None else "text",
        latency_ms=int((time.monotonic() - started) * 1000),
        input_tokens=usage.get("promptTokenCount"),
        output_tokens=usage.get("candidatesTokenCount"),
    )
    return text


# --------------------------------------------------------------------------
# Anthropic (Claude SDK)
# --------------------------------------------------------------------------


async def _anthropic_generate(
    system: str, user_text: str, model: str, max_tokens: int, output_config: dict | None
) -> str:
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user_text}],
    }
    if output_config is not None:
        kwargs["output_config"] = output_config

    started = time.monotonic()
    try:
        resp = await _client.messages.create(**kwargs)
    except (APIConnectionError, RateLimitError) as exc:
        log.warning("llm_unavailable", provider="anthropic", model=model, error=str(exc)[:150])
        raise LLMUnavailable(str(exc)) from exc
    except AuthenticationError as exc:
        log.error("llm_auth_failed", provider="anthropic", model=model)
        raise LLMUnavailable("invalid ANTHROPIC_API_KEY") from exc
    except APIStatusError as exc:
        if exc.status_code >= 500:
            log.warning("llm_5xx", provider="anthropic", model=model, status=exc.status_code)
            raise LLMUnavailable(str(exc.message)) from exc
        log.error(
            "llm_rejected", provider="anthropic", model=model,
            status=exc.status_code, error=str(exc.message)[:200],
        )
        raise LLMError(str(exc.message)) from exc

    text = "".join(b.text for b in resp.content if b.type == "text")
    log.info(
        "llm_call",
        provider="anthropic",
        model=model,
        kind="json" if output_config is not None else "text",
        latency_ms=int((time.monotonic() - started) * 1000),
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
        stop=resp.stop_reason,
    )
    return text
