"""LLM deadline tests: timeouts must degrade to the normal fallback path."""

import asyncio

import pytest

import app.services.llm_client as llm_client


async def test_llm_timeout_is_bounded_and_translated(monkeypatch) -> None:
    monkeypatch.setattr(llm_client, "TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(llm_client, "_provider_is_open", lambda provider: False)
    monkeypatch.setattr(llm_client, "_provider_failed", lambda provider: None)
    monkeypatch.setattr(llm_client, "_provider_succeeded", lambda provider: None)

    async def no_quota_issue():
        return None

    monkeypatch.setattr("app.services.quota.check_ai_quota", no_quota_issue)

    async def slow_generate(*args, **kwargs):
        await asyncio.sleep(1)
        return "too late"

    monkeypatch.setattr(llm_client, "_generate", slow_generate)
    with pytest.raises(llm_client.LLMUnavailable, match="timed out"):
        await llm_client.ask(
            system="test",
            user_text="test",
            model=llm_client.MODEL_SMART,
        )
