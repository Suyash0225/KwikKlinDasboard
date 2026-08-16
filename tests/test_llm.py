"""llm_client + intent tests — all mocked, no real API calls, no key needed."""

from types import SimpleNamespace

import anthropic
import httpx
import pytest

import app.services.intent as intent_module
import app.services.llm_client as llm
from app.services.intent import classify_intent
from app.services.llm_client import LLMError, LLMUnavailable, ask, ask_json


@pytest.fixture(autouse=True)
def _force_anthropic(monkeypatch):
    """These first tests exercise the Claude path regardless of .env;
    Gemini-path tests re-patch PROVIDER themselves."""
    monkeypatch.setattr(llm, "PROVIDER", "anthropic")


def _fake_response(text: str):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        stop_reason="end_turn",
    )


def _status_error(code: int, msg: str = "boom"):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(code, request=req, json={"error": {"message": msg}})
    cls = anthropic.AuthenticationError if code == 401 else anthropic.APIStatusError
    return cls(msg, response=resp, body=None)


def _patch_claude(monkeypatch, create):
    """Claude ka client ab lazy hai — client ki jagah banane wale ko patch
    karo, warna test purane module-level `_client` par tikta hai."""
    class _Stub:
        class messages:
            pass
    _Stub.messages.create = staticmethod(create)
    monkeypatch.setattr(llm, "_anthropic", lambda: _Stub)
    monkeypatch.setattr(llm, "_anthropic_client", None, raising=False)


async def test_ask_returns_text(monkeypatch) -> None:
    async def fake_create(**kw):
        assert kw["model"] == llm.MODEL_CHEAP
        return _fake_response("namaste ji")

    _patch_claude(monkeypatch, fake_create)
    assert await ask(system="s", user_text="hi") == "namaste ji"


async def test_ask_json_parses(monkeypatch) -> None:
    async def fake_create(**kw):
        assert kw["output_config"]["format"]["type"] == "json_schema"
        return _fake_response('{"intent": "GREETING", "language": "hi"}')

    _patch_claude(monkeypatch, fake_create)
    out = await ask_json(system="s", user_text="hi", schema={"type": "object"})
    assert out == {"intent": "GREETING", "language": "hi"}


async def test_ask_json_bad_json_raises_llmerror(monkeypatch) -> None:
    async def fake_create(**kw):
        return _fake_response("not json at all")

    _patch_claude(monkeypatch, fake_create)
    with pytest.raises(LLMError):
        await ask_json(system="s", user_text="hi", schema={"type": "object"})


async def test_network_error_is_unavailable(monkeypatch) -> None:
    async def fake_create(**kw):
        raise anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        )

    _patch_claude(monkeypatch, fake_create)
    with pytest.raises(LLMUnavailable):
        await ask(system="s", user_text="hi")


async def test_5xx_is_unavailable_but_4xx_is_hard_error(monkeypatch) -> None:
    async def fake_500(**kw):
        raise _status_error(500)

    _patch_claude(monkeypatch, fake_500)
    with pytest.raises(LLMUnavailable):
        await ask(system="s", user_text="hi")

    async def fake_400(**kw):
        raise _status_error(400, "bad request")

    _patch_claude(monkeypatch, fake_400)
    with pytest.raises(LLMError):
        await ask(system="s", user_text="hi")


async def test_bad_key_is_unavailable(monkeypatch) -> None:
    """Auth failure must degrade (callers fall back), not crash the webhook."""

    async def fake_create(**kw):
        raise _status_error(401, "invalid x-api-key")

    _patch_claude(monkeypatch, fake_create)
    with pytest.raises(LLMUnavailable):
        await ask(system="s", user_text="hi")


def _gemini_response(status: int, body: dict) -> httpx.Response:
    req = httpx.Request("POST", "https://generativelanguage.googleapis.com/x")
    return httpx.Response(status, request=req, json=body)


def _gemini_ok(text: str) -> dict:
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    }


async def test_gemini_ask_json_and_schema_stripping(monkeypatch) -> None:
    monkeypatch.setattr(llm, "PROVIDER", "gemini")
    seen: dict = {}

    async def fake_post(model, payload):
        seen.update(payload)
        return _gemini_response(200, _gemini_ok('{"intent": "GREETING", "language": "hi"}'))

    monkeypatch.setattr(llm, "_gemini_post", fake_post)
    out = await ask_json(
        system="s",
        user_text="hi",
        schema={
            "type": "object",
            "properties": {"intent": {"type": "string"}},
            "additionalProperties": False,
        },
    )
    assert out["intent"] == "GREETING"
    # Gemini rejects additionalProperties — must be stripped from the payload
    assert "additionalProperties" not in str(seen["generationConfig"]["responseSchema"])
    assert seen["generationConfig"]["responseMimeType"] == "application/json"


async def test_gemini_errors_map_correctly(monkeypatch) -> None:
    monkeypatch.setattr(llm, "PROVIDER", "gemini")

    async def post_403(model, payload):
        return _gemini_response(403, {"error": {"message": "bad key"}})

    monkeypatch.setattr(llm, "_gemini_post", post_403)
    with pytest.raises(LLMUnavailable):
        await ask(system="s", user_text="hi")

    async def post_429(model, payload):
        return _gemini_response(429, {"error": {"message": "quota"}})

    monkeypatch.setattr(llm, "_gemini_post", post_429)
    with pytest.raises(LLMUnavailable):
        await ask(system="s", user_text="hi")

    async def post_400(model, payload):
        return _gemini_response(400, {"error": {"message": "bad request"}})

    monkeypatch.setattr(llm, "_gemini_post", post_400)
    with pytest.raises(LLMError):
        await ask(system="s", user_text="hi")

    async def post_network(model, payload):
        raise httpx.ConnectError("no internet")

    monkeypatch.setattr(llm, "_gemini_post", post_network)
    with pytest.raises(LLMUnavailable):
        await ask(system="s", user_text="hi")


async def test_smart_rate_limited_falls_back_to_cheap(monkeypatch) -> None:
    """429 on the SMART model must retry once on CHEAP, not fail."""
    monkeypatch.setattr(llm, "PROVIDER", "gemini")
    models_called: list[str] = []

    async def fake_post(model, payload):
        models_called.append(model)
        if model == llm.MODEL_SMART:
            return _gemini_response(429, {"error": {"message": "quota"}})
        return _gemini_response(200, _gemini_ok('{"reply": "sasta jawaab"}'))

    monkeypatch.setattr(llm, "_gemini_post", fake_post)
    out = await ask_json(
        system="s", user_text="hi", schema={"type": "object"}, model=llm.MODEL_SMART
    )
    assert out == {"reply": "sasta jawaab"}
    assert models_called == [llm.MODEL_SMART, llm.MODEL_CHEAP]


async def test_gemini_safety_block_is_hard_error(monkeypatch) -> None:
    monkeypatch.setattr(llm, "PROVIDER", "gemini")

    async def post_empty(model, payload):
        return _gemini_response(200, {"candidates": []})

    monkeypatch.setattr(llm, "_gemini_post", post_empty)
    with pytest.raises(LLMError):
        await ask(system="s", user_text="hi")


def test_no_empty_enum_values_in_any_schema() -> None:
    """Gemini's responseSchema 400s on '' inside an enum — guard every schema."""
    from app.services.ai_agent import _REPLY_SCHEMA
    from app.services.bill_agent import _EXTRACT_SCHEMA
    from app.services.intent import _SCHEMA as intent_schema

    def walk(node):
        if isinstance(node, dict):
            if "enum" in node:
                assert "" not in node["enum"], f"empty enum value in {node}"
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for schema in (_EXTRACT_SCHEMA, intent_schema, _REPLY_SCHEMA):
        walk(schema)


async def test_classify_intent_happy(monkeypatch) -> None:
    async def fake_ask_json(**kw):
        return {"intent": "ORDER_STATUS", "language": "hi"}

    monkeypatch.setattr(intent_module.llm_client, "ask_json", fake_ask_json)
    out = await classify_intent("bhaiya kapde kab milenge")
    assert out == {"intent": "ORDER_STATUS", "language": "hi"}


async def test_classify_intent_none_when_llm_down(monkeypatch) -> None:
    async def fake_ask_json(**kw):
        raise LLMUnavailable("down")

    monkeypatch.setattr(intent_module.llm_client, "ask_json", fake_ask_json)
    assert await classify_intent("hi") is None


# --- naye pehre: refusal, truncation, caching, thinking -------------------


def _fake_response_full(text: str, stop: str = "end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=5, cache_read_input_tokens=0
        ),
        stop_reason=stop,
    )


async def test_claude_refusal_is_hard_error_not_empty_reply(monkeypatch) -> None:
    """Naye Claude models 200 ke saath stop_reason='refusal' lauta sakte hain.

    Pehle wo khaali text ban kar aage badhta — JSON path par "bad JSON",
    text path par customer ko khaali message. Ab saaf LLMError: caller ka
    degrade path (rule-based reply) chalta hai.
    """

    async def fake_create(**kw):
        return SimpleNamespace(
            content=[],
            usage=SimpleNamespace(input_tokens=10, output_tokens=0),
            stop_reason="refusal",
        )

    _patch_claude(monkeypatch, fake_create)
    with pytest.raises(LLMError):
        await ask(system="s", user_text="hi")


async def test_claude_truncated_json_is_named_not_confusing(monkeypatch) -> None:
    """max_tokens par kata JSON = LLMError jiska naam truncation hai.

    Sonnet 5 par thinking default-on hai aur max_tokens thinking+jawab dono
    ka dhakkan — bina is check ke log mein sirf "bad JSON" dikhta aur koi
    kabhi na samajhta ki asli wajah token budget thi.
    """

    async def fake_create(**kw):
        return _fake_response_full('{"reply": "aadha kata hu', stop="max_tokens")

    _patch_claude(monkeypatch, fake_create)
    with pytest.raises(LLMError):
        await ask_json(system="s", user_text="hi", schema={"type": "object"})


async def test_claude_request_shape_caching_floor_thinking(monkeypatch) -> None:
    """Ek hi call mein teen pehre:

    1. system cache-friendly block hai (cache_control) — repeat calls sasti.
    2. max_tokens ka floor 1024 — thinking wale model par 400 ka cap JSON
       ko beech mein kaat deta tha.
    3. Sonnet 5 par thinking saaf-saaf band — WhatsApp ka jawab 25s ke
       andar chahiye, thinking wahan sirf latency hai.
    """
    seen: dict = {}

    async def fake_create(**kw):
        seen.update(kw)
        return _fake_response_full('{"ok": true}')

    _patch_claude(monkeypatch, fake_create)
    await ask_json(
        system="stable system prompt", user_text="hi",
        schema={"type": "object"}, model="claude-sonnet-5", max_tokens=400,
    )
    assert seen["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert seen["system"][0]["text"] == "stable system prompt"
    assert seen["max_tokens"] >= 1024
    assert seen["thinking"] == {"type": "disabled"}


async def test_claude_haiku_gets_no_thinking_param(monkeypatch) -> None:
    """Haiku 4.5 purana scheme use karta hai — naya param bhejna 400 ka
    khatra hai, aur wahan thinking waise bhi band hai."""
    seen: dict = {}

    async def fake_create(**kw):
        seen.update(kw)
        return _fake_response_full("theek hai")

    _patch_claude(monkeypatch, fake_create)
    await ask(system="s", user_text="hi", model="claude-haiku-4-5")
    assert "thinking" not in seen


async def test_gemini_truncated_json_is_hard_error(monkeypatch) -> None:
    monkeypatch.setattr(llm, "PROVIDER", "gemini")

    async def fake_post(model, payload):
        return _gemini_response(200, {
            "candidates": [{
                "content": {"parts": [{"text": '{"reply": "aadha'}]},
                "finishReason": "MAX_TOKENS",
            }],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
        })

    monkeypatch.setattr(llm, "_gemini_post", fake_post)
    with pytest.raises(LLMError):
        await ask_json(system="s", user_text="hi", schema={"type": "object"})
