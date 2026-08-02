"""llm_client + intent tests — all mocked, no real API calls, no key needed."""

from types import SimpleNamespace

import anthropic
import httpx
import pytest

import app.services.intent as intent_module
import app.services.llm_client as llm
from app.services.intent import classify_intent
from app.services.llm_client import LLMError, LLMUnavailable, ask, ask_json


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


async def test_ask_returns_text(monkeypatch) -> None:
    async def fake_create(**kw):
        assert kw["model"] == llm.MODEL_CHEAP
        return _fake_response("namaste ji")

    monkeypatch.setattr(llm._client.messages, "create", fake_create)
    assert await ask(system="s", user_text="hi") == "namaste ji"


async def test_ask_json_parses(monkeypatch) -> None:
    async def fake_create(**kw):
        assert kw["output_config"]["format"]["type"] == "json_schema"
        return _fake_response('{"intent": "GREETING", "language": "hi"}')

    monkeypatch.setattr(llm._client.messages, "create", fake_create)
    out = await ask_json(system="s", user_text="hi", schema={"type": "object"})
    assert out == {"intent": "GREETING", "language": "hi"}


async def test_ask_json_bad_json_raises_llmerror(monkeypatch) -> None:
    async def fake_create(**kw):
        return _fake_response("not json at all")

    monkeypatch.setattr(llm._client.messages, "create", fake_create)
    with pytest.raises(LLMError):
        await ask_json(system="s", user_text="hi", schema={"type": "object"})


async def test_network_error_is_unavailable(monkeypatch) -> None:
    async def fake_create(**kw):
        raise anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        )

    monkeypatch.setattr(llm._client.messages, "create", fake_create)
    with pytest.raises(LLMUnavailable):
        await ask(system="s", user_text="hi")


async def test_5xx_is_unavailable_but_4xx_is_hard_error(monkeypatch) -> None:
    async def fake_500(**kw):
        raise _status_error(500)

    monkeypatch.setattr(llm._client.messages, "create", fake_500)
    with pytest.raises(LLMUnavailable):
        await ask(system="s", user_text="hi")

    async def fake_400(**kw):
        raise _status_error(400, "bad request")

    monkeypatch.setattr(llm._client.messages, "create", fake_400)
    with pytest.raises(LLMError):
        await ask(system="s", user_text="hi")


async def test_bad_key_is_unavailable(monkeypatch) -> None:
    """Auth failure must degrade (callers fall back), not crash the webhook."""

    async def fake_create(**kw):
        raise _status_error(401, "invalid x-api-key")

    monkeypatch.setattr(llm._client.messages, "create", fake_create)
    with pytest.raises(LLMUnavailable):
        await ask(system="s", user_text="hi")


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
