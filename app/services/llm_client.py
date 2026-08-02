"""The ONLY module allowed to import the anthropic SDK (ground rule #9).

Two model tiers (owner's spec):
- CHEAP  (Haiku 4.5): classification, extraction — costs paise per call
- SMART  (Sonnet 5):  composing customer-facing replies

Design rules enforced at this layer:
- Every call logged with model + latency + tokens.
- Transient failures (network, 429, 5xx) raise LLMUnavailable — callers
  MUST catch it and fall back to rule-based behavior (ground rule #5:
  degrade, never go silent).
- ask_json() uses structured outputs (JSON schema) so the model literally
  cannot return malformed data.
"""

import json
import time

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

MODEL_CHEAP = "claude-haiku-4-5"
MODEL_SMART = "claude-sonnet-5"

_client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)


class LLMError(Exception):
    """LLM produced something unusable (bad JSON etc.)."""


class LLMUnavailable(LLMError):
    """LLM could not be reached / rate limited / server error.

    Callers must degrade to rule-based behavior, never crash.
    """


async def ask(
    *,
    system: str,
    user_text: str,
    model: str = MODEL_CHEAP,
    max_tokens: int = 500,
) -> str:
    """Plain text completion. Raises LLMUnavailable / LLMError."""
    started = time.monotonic()
    try:
        resp = await _client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_text}],
        )
    except (APIConnectionError, RateLimitError) as exc:
        log.warning("llm_unavailable", model=model, error=str(exc)[:150])
        raise LLMUnavailable(str(exc)) from exc
    except AuthenticationError as exc:
        log.error("llm_auth_failed", model=model)
        raise LLMUnavailable("invalid ANTHROPIC_API_KEY") from exc
    except APIStatusError as exc:
        if exc.status_code >= 500:
            log.warning("llm_5xx", model=model, status=exc.status_code)
            raise LLMUnavailable(str(exc.message)) from exc
        log.error("llm_rejected", model=model, status=exc.status_code, error=str(exc.message)[:200])
        raise LLMError(str(exc.message)) from exc

    text = "".join(b.text for b in resp.content if b.type == "text")
    log.info(
        "llm_call",
        model=model,
        latency_ms=int((time.monotonic() - started) * 1000),
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
        stop=resp.stop_reason,
    )
    return text


async def ask_json(
    *,
    system: str,
    user_text: str,
    schema: dict,
    model: str = MODEL_CHEAP,
    max_tokens: int = 500,
) -> dict:
    """Completion constrained to a JSON schema (structured outputs).

    Returns the parsed dict. Raises LLMUnavailable / LLMError.
    """
    started = time.monotonic()
    try:
        resp = await _client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_text}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
    except (APIConnectionError, RateLimitError) as exc:
        log.warning("llm_unavailable", model=model, error=str(exc)[:150])
        raise LLMUnavailable(str(exc)) from exc
    except AuthenticationError as exc:
        log.error("llm_auth_failed", model=model)
        raise LLMUnavailable("invalid ANTHROPIC_API_KEY") from exc
    except APIStatusError as exc:
        if exc.status_code >= 500:
            raise LLMUnavailable(str(exc.message)) from exc
        log.error("llm_rejected", model=model, status=exc.status_code, error=str(exc.message)[:200])
        raise LLMError(str(exc.message)) from exc

    text = "".join(b.text for b in resp.content if b.type == "text")
    log.info(
        "llm_call",
        model=model,
        kind="json",
        latency_ms=int((time.monotonic() - started) * 1000),
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
    )
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        log.error("llm_bad_json", model=model, text=text[:200])
        raise LLMError(f"model returned invalid JSON: {text[:100]}") from exc
