"""Regression tests for Gemini failover, hard budget and alert cooldown."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

from app.services import llm_client


@pytest.fixture(autouse=True)
def _disable_circuit_breaker(monkeypatch):
    monkeypatch.setattr(llm_client, "_provider_is_open", lambda _provider: False)
    monkeypatch.setattr(llm_client, "_provider_failed", lambda _provider: None)
    monkeypatch.setattr(llm_client, "_provider_succeeded", lambda _provider: None)
    async def no_usage(*args, **kwargs):
        return None
    monkeypatch.setattr(llm_client, "_record_usage", no_usage)


@asynccontextmanager
async def _unlimited_guard(**kwargs):
    yield


@pytest.mark.asyncio
async def test_timeout_on_smart_model_falls_back_to_flash_lite(monkeypatch):
    monkeypatch.setattr(llm_client, "TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("app.services.quota.ai_quota_guard", _unlimited_guard)
    calls = []

    async def generate(system, text, model, *args, **kwargs):
        calls.append(model)
        if model == llm_client.MODEL_SMART:
            await asyncio.sleep(0.1)
        return "flash-lite reply"

    monkeypatch.setattr(llm_client, "_generate", generate)
    result = await llm_client.ask(system="s", user_text="hi", model=llm_client.MODEL_SMART)
    assert result == "flash-lite reply"
    assert calls == [llm_client.MODEL_SMART, llm_client.MODEL_CHEAP]


@pytest.mark.asyncio
async def test_http_500_failure_falls_back_to_flash_lite(monkeypatch):
    monkeypatch.setattr("app.services.quota.ai_quota_guard", _unlimited_guard)
    calls = []

    async def generate(system, text, model, *args, **kwargs):
        calls.append(model)
        if model == llm_client.MODEL_SMART:
            raise llm_client.LLMUnavailable("Gemini HTTP 500")
        return "flash-lite reply"

    monkeypatch.setattr(llm_client, "_generate", generate)
    result = await llm_client.ask(system="s", user_text="hi", model=llm_client.MODEL_SMART)
    assert result == "flash-lite reply"
    assert calls == [llm_client.MODEL_SMART, llm_client.MODEL_SMART, llm_client.MODEL_CHEAP]


@pytest.mark.asyncio
async def test_monthly_budget_exceeded_on_smart_tries_flash_lite(monkeypatch):
    from app.services import quota

    calls = []

    @asynccontextmanager
    async def guard(**kwargs):
        if kwargs["model"] == llm_client.MODEL_SMART:
            raise quota.QuotaExceeded("AI monthly budget USD", 1.0, 1.0, "test")
        yield

    monkeypatch.setattr(quota, "ai_quota_guard", guard)

    async def generate(system, text, model, *args, **kwargs):
        calls.append(model)
        return "within budget"

    monkeypatch.setattr(llm_client, "_generate", generate)
    result = await llm_client.ask(system="s", user_text="hi", model=llm_client.MODEL_SMART)
    assert result == "within budget"
    assert calls == [llm_client.MODEL_CHEAP]


def test_owner_alert_requires_three_recent_failures_and_one_hour_cooldown():
    from app.services.ai_agent import _failure_alert_due

    now = datetime.now(timezone.utc)
    assert not _failure_alert_due(2, None, now)
    assert _failure_alert_due(3, None, now)
    assert not _failure_alert_due(5, now - timedelta(minutes=59), now)
    assert _failure_alert_due(3, now - timedelta(hours=1), now)


@pytest.mark.asyncio
async def test_flash_lite_budget_exhaustion_stops_without_provider_call(monkeypatch):
    from app.services import quota

    calls = []

    @asynccontextmanager
    async def no_budget(**kwargs):
        calls.append(kwargs["model"])
        raise quota.QuotaExceeded("AI monthly budget USD", 1.0, 1.0, "test")
        yield

    monkeypatch.setattr(quota, "ai_quota_guard", no_budget)

    async def unexpected_generate(*args, **kwargs):
        raise AssertionError("provider must not be called when both budgets are exhausted")

    monkeypatch.setattr(llm_client, "_generate", unexpected_generate)
    with pytest.raises(llm_client.LLMUnavailable, match="AI monthly budget USD"):
        await llm_client.ask(system="s", user_text="hi", model=llm_client.MODEL_SMART)
    assert calls == [llm_client.MODEL_SMART, llm_client.MODEL_CHEAP]


def test_failover_acknowledgement_is_exact_and_not_owner_editable():
    from app.services.messages import EDITABLE_KEYS, get_message

    assert get_message("ai_failover_ack", "hi") == (
        "Aapka message mil gaya hai, thodi der mein jawab dete hain 🙏"
    )
    assert "ai_failover_ack" not in EDITABLE_KEYS
