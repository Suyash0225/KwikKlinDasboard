"""The only module that talks to the paid Gemini API.

All LLM calls use Google's paid Gemini API. There is no Anthropic or OpenRouter
fallback: if Gemini is unavailable, callers use their existing rule-based
degrade paths.
"""

import asyncio
import base64
import json
import random
import time
from contextlib import contextmanager
from contextvars import ContextVar

import httpx
import structlog
from app.config import settings

log = structlog.get_logger()

PROVIDER = "gemini"
_PROVIDER_MODELS = {
    "gemini": ("gemini-3.1-flash-lite", "gemini-3.8-flash"),
}
MODEL_CHEAP, MODEL_SMART = _PROVIDER_MODELS["gemini"]
_CIRCUIT_FAILURE_THRESHOLD = 3
_CIRCUIT_OPEN_SECONDS = 60.0
_circuit_failures: dict[str, int] = {}
_circuit_open_until: dict[str, float] = {}

def _provider_is_open(provider: str) -> bool:
    until = _circuit_open_until.get(provider, 0.0)
    if until <= time.monotonic():
        if until:
            _circuit_open_until.pop(provider, None)
            _circuit_failures.pop(provider, None)
        return False
    return True

def _provider_failed(provider: str) -> None:
    failures = _circuit_failures.get(provider, 0) + 1
    _circuit_failures[provider] = failures
    if failures >= _CIRCUIT_FAILURE_THRESHOLD:
        _circuit_open_until[provider] = time.monotonic() + _CIRCUIT_OPEN_SECONDS
        log.warning("llm_circuit_open", provider=provider, cooldown_seconds=int(_CIRCUIT_OPEN_SECONDS), failures=failures)

def _provider_succeeded(provider: str) -> None:
    _circuit_failures.pop(provider, None)
    _circuit_open_until.pop(provider, None)

def _provider_models(provider: str) -> tuple[str, str]:
    return _PROVIDER_MODELS[provider]

# A customer is staring at WhatsApp. The SDK's own default is ten MINUTES —
# by then the person has phoned the shop, and our reply arrives as noise.
# Better to give up fast and let the rule-based fallback answer.
TIMEOUT_SECONDS = 75.0
IMAGE_TIMEOUT_SECONDS = 90.0

# Transient failures (429 / 5xx) get retried here with exponential backoff
# and jitter. Jitter matters on the free tier: without it, every message
# that hits the same quota wall retries in lockstep and hits it again.
_MAX_ATTEMPTS = 2
_BACKOFF_BASE = 0.6

async def _with_retry(call, *, provider: str, model: str):
    """Run one provider call, retrying only what is worth retrying.

    Retry: network blips, 408/5xx, and 429 rate limits — a bounded retry
    with jitter can recover transient capacity. Do not retry bad keys or
    application quota exhaustion; the caller's cheaper-model fallback is
    the real escape hatch.
    LLMError (garbage JSON) isn't caught here at all — retrying a confused
    model burns quota to get confused again.
    """
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return await call()
        except LLMAuthError:
            # Invalid key / exhausted app quota is not transient.
            raise
        except LLMRateLimited as exc:
            if attempt == _MAX_ATTEMPTS:
                raise
            delay = _BACKOFF_BASE * (2 ** (attempt - 1)) * (0.5 + random.random())
            log.warning(
                "llm_rate_limited_retry",
                provider=provider, model=model, attempt=attempt,
                error=str(exc)[:200], sleep_ms=int(delay * 1000),
            )
            await asyncio.sleep(delay)
        except LLMUnavailable as exc:
            if attempt == _MAX_ATTEMPTS:
                raise
            delay = _BACKOFF_BASE * (2 ** (attempt - 1)) * (0.5 + random.random())
            log.warning(
                "llm_retry", provider=provider, model=model,
                attempt=attempt, error=str(exc)[:200],
                sleep_ms=int(delay * 1000),
            )
            await asyncio.sleep(delay)


_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class LLMError(Exception):
    """LLM produced something unusable (bad JSON etc.)."""


class LLMUnavailable(LLMError):
    """LLM could not be reached / rate limited / server error / bad key.

    Callers must degrade to rule-based behavior, never crash.
    """


class LLMAuthError(LLMUnavailable):
    """Bad/missing API key, or quota spent. Real, but NOT worth retrying —
    a second identical call fails identically and just costs a second."""


class LLMRateLimited(LLMUnavailable):
    """429. Also not worth retrying on the SAME model — the quota is the
    quota. The right move is the cheap model, which has its own bucket, and
    _generate_with_fallback already goes there. Waiting first just makes
    the customer wait too."""


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
                    customer_id=_customer_id.get(), order_id=_order_id.get(),
                    conversation_id=_conversation_id.get(),
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
_customer_id: ContextVar[object | None] = ContextVar("llm_customer_id", default=None)
_order_id: ContextVar[object | None] = ContextVar("llm_order_id", default=None)
_conversation_id: ContextVar[object | None] = ContextVar("llm_conversation_id", default=None)


@contextmanager
def attribution(*, customer_id=None, order_id=None, conversation_id=None):
    """Attach optional business context to every LLM call in this scope.

    Missing context is intentional for background jobs. Never guess an order:
    callers only provide an order when it is unambiguous.
    """
    tc = _customer_id.set(customer_id)
    to = _order_id.set(order_id)
    tv = _conversation_id.set(conversation_id)
    try:
        yield
    finally:
        _customer_id.reset(tc)
        _order_id.reset(to)
        _conversation_id.reset(tv)


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
    provider: str = PROVIDER,
) -> str:
    if provider != "gemini":
        raise LLMUnavailable(f"unsupported LLM provider: {provider}")
    return await _gemini_generate(system, user_text, model, max_tokens, schema, image)


async def _generate_with_fallback(
    system: str,
    user_text: str,
    model: str,
    max_tokens: int,
    schema: dict | None = None,
    image: tuple[str, bytes] | None = None,
) -> str:
    """Use only paid Gemini; retry the same deployment's cheaper tier once."""
    from app.services.quota import QuotaExceeded, check_ai_quota

    try:
        await check_ai_quota()
    except QuotaExceeded as exc:
        raise LLMAuthError(str(exc)) from exc

    if _provider_is_open(PROVIDER):
        raise LLMUnavailable("gemini circuit open")

    cheap, smart = _provider_models(PROVIDER)
    candidates = [model] if model in (cheap, smart) else [smart]
    if candidates[0] != cheap:
        candidates.append(cheap)

    last_exc: LLMUnavailable | None = None
    for candidate in candidates:
        try:
            async with asyncio.timeout(
                IMAGE_TIMEOUT_SECONDS if image is not None else TIMEOUT_SECONDS
            ):
                result = await _with_retry(
                    lambda: _generate(
                        system, user_text, candidate, max_tokens, schema, image, PROVIDER
                    ),
                    provider=PROVIDER,
                    model=candidate,
                )
            _provider_succeeded(PROVIDER)
            return result
        except LLMAuthError as exc:
            last_exc = exc
            log.warning(
                "llm_provider_auth_failed", provider=PROVIDER,
                model=candidate, error=str(exc)[:200],
            )
            break
        except LLMError as exc:
            last_exc = exc
            log.warning(
                "llm_model_error", provider=PROVIDER,
                model=candidate, error=str(exc)[:200],
            )

    _provider_failed(PROVIDER)
    raise last_exc or LLMUnavailable("gemini unavailable")


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


SUPPORTS_AUDIO = True

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
            # Baaki har jagah ki tarah yahan bhi fallback chahiye. Free tier
            # SMART model ko pehle throttle karta hai (429); ye seedha
            # _generate par tha, isliye us waqt transcript None aa jaata —
            # aur voice note bina shabdon ke pada rehta: na Inbox mein
            # kuch padhne ko, na staff ko koi jawab. Chhota model thodi
            # kam sundar transcript deta hai, par kuch na hone se behtar.
            out = await _generate_with_fallback(
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
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        return await client.post(
            f"{_GEMINI_BASE}/{model}:generateContent",
            headers={"x-goog-api-key": settings.GEMINI_API_KEY},
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
        raise LLMAuthError("invalid GEMINI_API_KEY")
    if resp.status_code == 400 and "API_KEY_INVALID" in resp.text:
        log.error("llm_auth_failed", provider="gemini", model=model, status=400)
        raise LLMAuthError("invalid GEMINI_API_KEY")
    if resp.status_code in (404, 408):
        raise LLMUnavailable(f"gemini HTTP {resp.status_code}")
    if resp.status_code == 429:
        retry_after = resp.headers.get("retry-after")
        detail = resp.text[:300]
        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            await _record_usage("gemini", model, 0, 0, latency_ms, False)
        except Exception:
            log.exception("llm_usage_record_crashed", model=model)
        log.warning(
            "llm_rate_limited",
            provider="gemini", model=model, status=429,
            retry_after=retry_after, error=detail,
        )
        raise LLMRateLimited(f"gemini HTTP 429: {detail}")
    if resp.status_code >= 500:
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

    # Kata hua JSON poora bekaar hai — naam se pakdo, warna upar sirf ek
    # confusing "bad JSON" dikhta hai jiski asli wajah token budget thi.
    finish = str((data.get("candidates") or [{}])[0].get("finishReason") or "")
    if finish == "MAX_TOKENS" and schema is not None:
        log.warning("llm_truncated", provider="gemini", model=model)
        raise LLMError("gemini output truncated at maxOutputTokens before JSON completed")

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


