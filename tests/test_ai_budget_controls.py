"""Regression tests for the owner-configured daily AI request cap.

These tests use a uniquely named usage row and never call the real Gemini API.
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from app.database import async_session_factory
from app.models import LlmUsage
from app.services import app_settings, quota, tenant_context
from app.services.quota import QuotaExceeded


@pytest.mark.asyncio
async def test_daily_ai_cap_is_enforced_before_provider_call():
    model = "test-daily-cap-control"
    async with async_session_factory() as db:
        prior = await app_settings.get(db, "llm_daily_request_cap")
        await app_settings.set_value(db, "llm_daily_request_cap", 1)
        db.add(
            LlmUsage(
                tenant_id=tenant_context.cached_home_tenant_id(),
                provider="test",
                model=model,
                purpose="test",
                input_tokens=1,
                output_tokens=1,
                latency_ms=1,
                ok=True,
                at=datetime.now(timezone.utc),
            )
        )
        await db.commit()

    try:
        with pytest.raises(QuotaExceeded, match="AI daily requests"):
            await quota.check_ai_quota(model="gemini-3.1-flash-lite", input_text="hello")
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(LlmUsage).where(LlmUsage.model == model))
            await app_settings.set_value(db, "llm_daily_request_cap", prior or 0)
            await db.commit()


@pytest.mark.asyncio
async def test_daily_ai_cap_zero_means_unlimited():
    async with async_session_factory() as db:
        prior = await app_settings.get(db, "llm_daily_request_cap")
        await app_settings.set_value(db, "llm_daily_request_cap", 0)
    try:
        # Existing plan-level quota checks still apply, but a zero daily cap
        # itself must not block a request.
        result = await quota.check_ai_quota(model="gemini-3.1-flash-lite", input_text="hello")
        assert result is None
    finally:
        async with async_session_factory() as db:
            await app_settings.set_value(db, "llm_daily_request_cap", prior or 0)
            await db.commit()


@pytest.mark.asyncio
async def test_provider_503_uses_existing_degrade_error(monkeypatch):
    import app.services.llm_client as llm

    class FakeResponse:
        status_code = 503
        text = "provider overloaded"
        headers = {}

    async def fake_post(model, payload):
        return FakeResponse()

    monkeypatch.setattr(llm, "_gemini_post", fake_post)
    with pytest.raises(llm.LLMUnavailable, match="503"):
        await llm._gemini_generate("system", "hello", "test-503", 100, None, None)


@pytest.mark.asyncio
async def test_provider_429_is_not_retried_and_is_metered(monkeypatch):
    import app.services.llm_client as llm

    class FakeResponse:
        status_code = 429
        text = "quota exceeded"
        headers = {"retry-after": "1"}

    async def fake_post(model, payload):
        return FakeResponse()

    monkeypatch.setattr(llm, "_gemini_post", fake_post)
    try:
        with pytest.raises(llm.LLMRateLimited, match="429"):
            await llm._gemini_generate("system", "hello", "test-429", 100, None, None)
        async with async_session_factory() as db:
            row = (
                await db.execute(
                    __import__("sqlalchemy").select(LlmUsage).where(LlmUsage.model == "test-429")
                )
            ).scalar_one()
            assert row.ok is False
            assert row.input_tokens == 0 and row.output_tokens == 0
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(LlmUsage).where(LlmUsage.model == "test-429"))
            await db.commit()
