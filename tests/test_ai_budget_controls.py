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
