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

import base64
import json
import time
from contextlib import contextmanager
from contextvars import ContextVar

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


async def _record_usage(
    provider: str, model: str, in_tok: int, out_tok: int, latency_ms: int, ok: bool
) -> None:
    """Persist one call so the dashboard can answer 'kitna use hua'.

    Never raises and never blocks the reply — a usage row is bookkeeping,
    not part of answering the customer.
    """
    try:
        from app.database import async_session_factory
        from app.models import LlmUsage

        async with async_session_factory() as db:
            db.add(
                LlmUsage(
                    provider=provider, model=model, purpose=_purpose.get() or "other",
                    input_tokens=int(in_tok or 0), output_tokens=int(out_tok or 0),
                    latency_ms=latency_ms, ok=ok,
                )
            )
            await db.commit()
    except Exception:
        log.exception("llm_usage_record_failed", model=model)


# What the current call is for — set by callers via track(); read by the
# recorder. A ContextVar keeps it correct under concurrent requests.
_purpose: ContextVar[str] = ContextVar("llm_purpose", default="other")


@contextmanager
def track(purpose: str):
    """Tag every LLM call inside this block, e.g. with track("reply"): ..."""
    token = _purpose.set(purpose[:24])
    try:
        yield
    finally:
        _purpose.reset(token)


async def _generate(
    system: str,
    user_text: str,
    model: str,
    max_tokens: int,
    schema: dict | None = None,
    image: tuple[str, bytes] | None = None,
) -> str:
    """Provider dispatch — one place, so fallback logic stays tiny."""
    if PROVIDER == "gemini":
        return await _gemini_generate(system, user_text, model, max_tokens, schema, image)
    output_config = (
        {"format": {"type": "json_schema", "schema": schema}} if schema is not None else None
    )
    return await _anthropic_generate(
        system, user_text, model, max_tokens, output_config, image
    )


async def _generate_with_fallback(
    system: str,
    user_text: str,
    model: str,
    max_tokens: int,
    schema: dict | None = None,
    image: tuple[str, bytes] | None = None,
) -> str:
    """SMART model down/rate-limited -> one retry on CHEAP before giving up.

    Free tiers throttle the bigger model first; a slightly dumber answer
    beats no answer (ground rule #5).
    """
    try:
        return await _generate(system, user_text, model, max_tokens, schema, image)
    except LLMUnavailable:
        if model == MODEL_CHEAP:
            raise
        log.warning("llm_smart_unavailable_trying_cheap", from_model=model)
        return await _generate(system, user_text, MODEL_CHEAP, max_tokens, schema, image)


def _parse_json(text: str, model: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        log.error("llm_bad_json", provider=PROVIDER, model=model, text=text[:200])
        raise LLMError(f"model returned invalid JSON: {text[:100]}") from exc


async def ask(
    *,
    system: str,
    user_text: str,
    model: str | None = None,
    max_tokens: int = 500,
) -> str:
    """Plain text completion. Raises LLMUnavailable / LLMError."""
    model = model or MODEL_CHEAP
    return await _generate_with_fallback(system, user_text, model, max_tokens)


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
    text = await _generate_with_fallback(system, user_text, model, max_tokens, schema=schema)
    return _parse_json(text, model)


async def ask_json_image(
    *,
    system: str,
    user_text: str,
    image_bytes: bytes,
    mime_type: str,
    schema: dict,
    model: str | None = None,
    # Vision + thinking models burn output budget on internal reasoning
    # BEFORE emitting JSON — a small cap silently truncates the answer.
    max_tokens: int = 6000,
) -> dict:
    """Like ask_json, but the model also SEES an image (e.g. a bill photo)."""
    model = model or MODEL_SMART  # reading handwriting prefers the better model
    text = await _generate_with_fallback(
        system, user_text, model, max_tokens, schema=schema, image=(mime_type, image_bytes)
    )
    return _parse_json(text, model)


SUPPORTS_AUDIO = PROVIDER == "gemini"

_TRANSCRIBE_SYSTEM = (
    "You transcribe WhatsApp voice notes sent to a laundry shop in Varanasi, "
    "India. Customers speak Hindi, Hinglish or English, often with background "
    "noise.\n"
    "Write ONLY what was said, verbatim, in Latin script (Hinglish) — do not "
    "translate to English, do not answer, do not summarise, do not add "
    "commentary or quotes. Keep numbers, names and addresses exactly as "
    "spoken. If the audio is silent or nothing is intelligible, reply with "
    "the single word: UNCLEAR"
)


async def transcribe_audio(audio_bytes: bytes, mime_type: str) -> str | None:
    """Turn a voice note into text, or None if it can't be understood.

    None means "we genuinely don't know what they said" — the caller must
    fall back to acknowledging the note rather than inventing a message.
    """
    if not SUPPORTS_AUDIO:
        return None
    try:
        with track("voice"):
            out = await _generate(
                _TRANSCRIBE_SYSTEM, "Transcribe this voice note.",
                MODEL_SMART, 400, None, (mime_type, audio_bytes),
            )
    except LLMError:
        log.warning("voice_transcribe_failed", mime=mime_type)
        return None
    text = (out or "").strip().strip('"')
    if not text or text.upper().startswith("UNCLEAR"):
        log.info("voice_transcribe_unclear")
        return None
    log.info("voice_transcribed", chars=len(text))
    return text[:1000]


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
    system: str,
    user_text: str,
    model: str,
    max_tokens: int,
    schema: dict | None,
    image: tuple[str, bytes] | None = None,
) -> str:
    # Floor the budget: Gemini spends output tokens on internal thinking
    # BEFORE emitting the answer — a low cap truncates mid-JSON. 2048 has
    # headroom for the thinking burst; tokens on the free tier cost nothing.
    gen: dict = {"maxOutputTokens": max(max_tokens, 2048)}
    if schema is not None:
        gen["responseMimeType"] = "application/json"
        gen["responseSchema"] = _gemini_schema(schema)
    parts: list[dict] = []
    if image is not None:
        mime_type, blob = image
        parts.append(
            {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(blob).decode()}}
        )
    parts.append({"text": user_text})
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
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
    latency_ms = int((time.monotonic() - started) * 1000)
    log.info(
        "llm_call",
        provider="gemini",
        model=model,
        kind="json" if schema is not None else "text",
        latency_ms=latency_ms,
        input_tokens=usage.get("promptTokenCount"),
        output_tokens=usage.get("candidatesTokenCount"),
    )
    # guarded at the CALL SITE too: the reply must survive even a bug inside
    # the recorder itself, not just a failed DB write
    try:
        await _record_usage(
            "gemini", model, usage.get("promptTokenCount") or 0,
            usage.get("candidatesTokenCount") or 0, latency_ms, True,
        )
    except Exception:
        log.exception("llm_usage_record_crashed", model=model)
    return text


# --------------------------------------------------------------------------
# Anthropic (Claude SDK)
# --------------------------------------------------------------------------


async def _anthropic_generate(
    system: str,
    user_text: str,
    model: str,
    max_tokens: int,
    output_config: dict | None,
    image: tuple[str, bytes] | None = None,
) -> str:
    content: str | list = user_text
    if image is not None:
        mime_type, blob = image
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime_type,
                    "data": base64.b64encode(blob).decode(),
                },
            },
            {"type": "text", "text": user_text},
        ]
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content}],
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
    latency_ms = int((time.monotonic() - started) * 1000)
    log.info(
        "llm_call",
        provider="anthropic",
        model=model,
        kind="json" if output_config is not None else "text",
        latency_ms=latency_ms,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
        stop=resp.stop_reason,
    )
    try:
        await _record_usage(
            "anthropic", model, resp.usage.input_tokens, resp.usage.output_tokens,
            latency_ms, True,
        )
    except Exception:
        log.exception("llm_usage_record_crashed", model=model)
    return text
