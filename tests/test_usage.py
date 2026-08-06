"""AI usage + cost tracking.

Before this, token counts only reached stdout — there was no way to answer
"kitna use hua, kitne me khatam hoga".
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

import app.services.llm_client as llm
from app.config import settings
from app.database import async_session_factory
from app.models import LlmUsage
from app.services import app_settings

H = {"X-API-Key": settings.ADMIN_API_KEY}


@pytest.fixture(autouse=True)
async def _clean_usage():
    yield
    async with async_session_factory() as s:
        await s.execute(delete(LlmUsage).where(LlmUsage.model.like("test-%")))
        await s.commit()


async def _add(model="test-model", tin=1000, tout=500, purpose="reply", when=None):
    async with async_session_factory() as s:
        row = LlmUsage(
            provider="test", model=model, purpose=purpose,
            input_tokens=tin, output_tokens=tout, latency_ms=120,
        )
        if when is not None:
            row.at = when
        s.add(row)
        await s.commit()


async def test_gemini_call_is_recorded(monkeypatch) -> None:
    """The real client must write a usage row — not just log tokens."""
    class FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {
                "candidates": [{"content": {"parts": [{"text": "hi"}]}}],
                "usageMetadata": {"promptTokenCount": 321, "candidatesTokenCount": 47},
            }

    async def fake_post(model, payload):
        return FakeResp()

    monkeypatch.setattr(llm, "_gemini_post", fake_post)
    monkeypatch.setattr(llm, "PROVIDER", "gemini")

    async with async_session_factory() as s:
        before = len((await s.execute(select(LlmUsage))).scalars().all())

    with llm.track("reply"):
        await llm._gemini_generate("sys", "hello", "test-gemini", 100, None, None)

    async with async_session_factory() as s:
        rows = (
            await s.execute(select(LlmUsage).where(LlmUsage.model == "test-gemini"))
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].input_tokens == 321 and rows[0].output_tokens == 47
    assert rows[0].purpose == "reply", "the caller's tag must land on the row"


async def test_recording_never_breaks_the_reply(monkeypatch) -> None:
    """A bookkeeping failure must not cost the customer their answer."""
    async def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(llm, "_record_usage", boom)

    class FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {"candidates": [{"content": {"parts": [{"text": "still works"}]}}]}

    monkeypatch.setattr(llm, "_gemini_post", lambda m, p: _ok(FakeResp()))
    with pytest.raises(RuntimeError):
        # sanity: our stub really does raise when called directly
        await boom()

    out = await llm._gemini_generate("sys", "hi", "test-gemini2", 100, None, None)
    assert out == "still works"


async def _ok(v):
    return v


async def test_usage_api_totals_and_cost(client) -> None:
    async with async_session_factory() as db:
        rates = dict(await app_settings.get(db, "llm_rates") or {})
        rates["test-paid"] = {"in": 5.0, "out": 25.0}
        await app_settings.set_value(db, "llm_rates", rates)

    # 1M in + 1M out at $5/$25 = $30
    await _add(model="test-paid", tin=1_000_000, tout=1_000_000, purpose="reply")
    await _add(model="test-free", tin=5000, tout=1000, purpose="intent")

    r = await client.get("/admin/api/usage", headers=H)
    assert r.status_code == 200
    u = r.json()

    paid = next(m for m in u["month"]["by_model"] if m["model"] == "test-paid")
    assert paid["cost_usd"] == pytest.approx(30.0)
    assert paid["priced"] is True

    free = next(m for m in u["month"]["by_model"] if m["model"] == "test-free")
    assert free["cost_usd"] == 0 and free["priced"] is False

    assert u["today"]["calls"] >= 2
    assert u["month"]["cost_usd"] >= 30.0
    assert u["all_free"] is False


async def test_cost_is_split_by_purpose_not_flattened(client) -> None:
    """Purpose rows must price per model — grouping the model away first
    would report every purpose as free."""
    async with async_session_factory() as db:
        rates = dict(await app_settings.get(db, "llm_rates") or {})
        rates["test-paid2"] = {"in": 10.0, "out": 10.0}
        await app_settings.set_value(db, "llm_rates", rates)

    await _add(model="test-paid2", tin=1_000_000, tout=0, purpose="marketing")

    u = (await client.get("/admin/api/usage", headers=H)).json()
    row = next(p for p in u["by_purpose"] if p["purpose"] == "marketing")
    assert row["cost_usd"] == pytest.approx(10.0)


async def test_projection_and_cap(client) -> None:
    async with async_session_factory() as db:
        await app_settings.set_value(db, "llm_daily_request_cap", 50)
    try:
        await _add(model="test-model")
        u = (await client.get("/admin/api/usage", headers=H)).json()
        assert u["daily_request_cap"] == 50
        # never negative — a busy day past the cap reads "0 left", not "-3"
        assert u["calls_left_today"] == max(50 - u["today"]["calls"], 0)
        assert u["projected_month_usd"] >= 0
    finally:
        async with async_session_factory() as db:
            await app_settings.set_value(db, "llm_daily_request_cap", 0)


async def test_series_covers_past_days(client) -> None:
    await _add(model="test-model", when=datetime.now(timezone.utc) - timedelta(days=3))
    await _add(model="test-model")
    u = (await client.get("/admin/api/usage?days=30", headers=H)).json()
    assert len(u["series"]) >= 2, "each day with calls should appear in the chart"
    assert all("date" in d and "calls" in d for d in u["series"])
