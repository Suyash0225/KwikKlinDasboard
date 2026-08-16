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

import asyncio
import base64
import json
import random
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

# A customer is staring at WhatsApp. The SDK's own default is ten MINUTES —
# by then the person has phoned the shop, and our reply arrives as noise.
# Better to give up fast and let the rule-based fallback answer.
TIMEOUT_SECONDS = 25.0

# Transient failures (429 / 5xx) get retried here with exponential backoff
# and jitter. Jitter matters on the free tier: without it, every message
# that hits the same quota wall retries in lockstep and hits it again.
_MAX_ATTEMPTS = 2
_BACKOFF_BASE = 0.6

_anthropic_client: AsyncAnthropic | None = None


def _anthropic() -> AsyncAnthropic:
    """Built on first use — a gemini deployment never needs a Claude key."""
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = AsyncAnthropic(
            api_key=settings.ANTHROPIC_API_KEY,
            timeout=TIMEOUT_SECONDS,
            max_retries=0,  # retries are ours (_with_retry), so the budget is one place
        )
    return _anthropic_client


async def _with_retry(call, *, provider: str, model: str):
    """Run one provider call, retrying only what is worth retrying.

    Retry: network blips and 5xx — a second attempt genuinely often works.
    Don't: bad key, spent quota, 429. Those fail identically the second
    time; the caller's cheaper-model fallback is the real escape hatch.
    LLMError (garbage JSON) isn't caught here at all — retrying a confused
    model burns quota to get confused again.
    """
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return await call()
        except (LLMAuthError, LLMRateLimited):
            raise
        except LLMUnavailable:
            if attempt == _MAX_ATTEMPTS:
                raise
            delay = _BACKOFF_BASE * (2 ** (attempt - 1)) * (0.5 + random.random())
            log.info(
                "llm_retry", provider=provider, model=model,
                attempt=attempt, sleep_ms=int(delay * 1000),
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
    # Plan ki AI quota — network call se PEHLE. Exceeded -> LLMUnavailable,
    # jiska degrade path (escalate/owner ko batao) pehle se tested hai.
    from app.services.quota import QuotaExceeded, check_ai_quota

    try:
        await check_ai_quota()
    except QuotaExceeded as exc:
        raise LLMAuthError(str(exc)) from exc

    def _attempt(m: str):
        return lambda: _generate(system, user_text, m, max_tokens, schema, image)

    try:
        return await _with_retry(_attempt(model), provider=PROVIDER, model=model)
    except LLMUnavailable:
        if model == MODEL_CHEAP:
            raise
        log.warning("llm_smart_unavailable_trying_cheap", from_model=model)
        return await _with_retry(
            _attempt(MODEL_CHEAP), provider=PROVIDER, model=MODEL_CHEAP
        )


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
        raise LLMAuthError("invalid GEMINI_API_KEY")
    if resp.status_code == 429:
        log.warning("llm_unavailable", provider="gemini", model=model, status=429)
        raise LLMRateLimited("gemini HTTP 429")
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
        # Floor the budget, Gemini ki tarah yahan bhi: naye Claude models
        # par max_tokens thinking + jawab DONO ka dhakkan hai. 400 ka cap
        # JSON ko beech mein kaat deta ("llm_bad_json" jo asal mein
        # truncation hai). Extra tokens ka daam sirf tab lagta hai jab wo
        # sach mein likhe jayen.
        "max_tokens": max(max_tokens, 1024),
        # System prompt cache-friendly block ke roop mein: prefix same
        # rahe to agli call ~90% sasti. Chhote prompt par ye chupchaap
        # no-op hai (minimum se neeche kuch cache nahi hota) — nuksaan
        # kabhi nahi, owner ka knowledge badhne par fayda apne aap.
        "system": [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ],
        "messages": [{"role": "user", "content": content}],
    }
    # WhatsApp par khada customer 25 second se zyada nahi rukta. Sonnet 5
    # par thinking DEFAULT se chalu hai — chhote front-desk jawab ke liye
    # wo sirf latency aur tokens hai. Isliye band. (Haiku 4.5 par param
    # bheja hi nahi jata — wahan thinking waise bhi band hai, aur purane
    # model naya param maante nahi.)
    if model.startswith("claude-sonnet-5"):
        kwargs["thinking"] = {"type": "disabled"}
    if output_config is not None:
        kwargs["output_config"] = output_config

    started = time.monotonic()
    try:
        resp = await _anthropic().messages.create(**kwargs)
    except RateLimitError as exc:
        log.warning("llm_unavailable", provider="anthropic", model=model, status=429)
        raise LLMRateLimited(str(exc)) from exc
    except APIConnectionError as exc:
        log.warning("llm_unavailable", provider="anthropic", model=model, error=str(exc)[:150])
        raise LLMUnavailable(str(exc)) from exc
    except AuthenticationError as exc:
        log.error("llm_auth_failed", provider="anthropic", model=model)
        raise LLMAuthError("invalid ANTHROPIC_API_KEY") from exc
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
    # stop_reason content se PEHLE dekho — naye Claude models par safety
    # classifier 200 ke saath "refusal" lauta sakta hai (content khaali),
    # aur max_tokens par JSON aadha kata hota hai. Dono ko naam se pakdo,
    # warna upar sirf ek confusing "bad JSON" dikhta hai. Tokens phir bhi
    # lage hain, isliye hisaab (usage row) dono haal mein likha jata hai.
    failed_reason = None
    if resp.stop_reason == "refusal":
        failed_reason = "model refused the request (safety)"
        log.warning("llm_refused", provider="anthropic", model=model)
    elif resp.stop_reason == "max_tokens" and output_config is not None:
        # JSON beech mein kata = poora bekaar. Saaf error, taaki caller
        # ka degrade path chale aur log mein asli wajah dikhe.
        failed_reason = "output truncated at max_tokens before JSON completed"
        log.warning("llm_truncated", provider="anthropic", model=model)
    log.info(
        "llm_call",
        provider="anthropic",
        model=model,
        kind="json" if output_config is not None else "text",
        latency_ms=latency_ms,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
        cache_read=getattr(resp.usage, "cache_read_input_tokens", None),
        stop=resp.stop_reason,
    )
    try:
        await _record_usage(
            "anthropic", model, resp.usage.input_tokens, resp.usage.output_tokens,
            latency_ms, failed_reason is None,
        )
    except Exception:
        log.exception("llm_usage_record_crashed", model=model)
    if failed_reason is not None:
        raise LLMError(failed_reason)
    return text
