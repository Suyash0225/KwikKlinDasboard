"""llm_client + intent tests — all mocked, no real API calls, no key needed."""

import httpx
import pytest

import app.services.llm_client as llm
from app.services.llm_client import LLMError, LLMUnavailable, ask, ask_json


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

    def walk(node):
        if isinstance(node, dict):
            if "enum" in node:
                assert "" not in node["enum"], f"empty enum value in {node}"
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for schema in (_EXTRACT_SCHEMA, _REPLY_SCHEMA):
        walk(schema)


# --- naye pehre: refusal, truncation, caching, thinking -------------------


def _fake_response_full(text: str, stop: str = "end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=5, cache_read_input_tokens=0
        ),
        stop_reason=stop,
    )


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
