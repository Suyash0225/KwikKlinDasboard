"""Outbound policy regression tests; all transport calls are mocked."""
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import select

from app.database import async_session_factory


async def test_outbound_retry_without_tenant_is_dead_lettered_without_send(monkeypatch) -> None:
    """Legacy/malformed retry payloads must never fall back to the home shop."""
    import app.services.whatsapp as whatsapp
    from app.models import OutboundMessage

    async with async_session_factory() as db:
        row = OutboundMessage(
            to_phone="+919999900011",
            payload={"text": "must not send", "sent_by": "bot"},
            status="queued",
            next_attempt_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        db.add(row)
        await db.commit()
        row_id = row.id

    async def unexpected_send(*args, **kwargs):
        raise AssertionError("retry without tenant identity attempted a WhatsApp send")

    monkeypatch.setattr(whatsapp, "send_message", unexpected_send)
    sent = await whatsapp.drain_outbound_queue()
    assert sent == 0

    async with async_session_factory() as db:
        row = await db.get(OutboundMessage, row_id)
        assert row is not None
        assert row.status == "dead"
        assert "tenant_id" in (row.last_error or "")


async def test_outbound_policy_without_tenant_fails_closed() -> None:
    from app.services import tenant_context
    from app.services.outbound_policy import enforce_outbound_policy
    from app.services.whatsapp import SendError

    token = tenant_context.current_tenant_id.set(None)
    try:
        with pytest.raises(SendError, match="tenant context missing"):
            await enforce_outbound_policy(
                None,
                customer=None,
                phone="+919999900011",
                intent="promotional",
            )
    finally:
        tenant_context.current_tenant_id.reset(token)


async def test_repeated_claimed_job_sends_once(monkeypatch) -> None:
    """A simulated restart repeats the same tenant-prefixed claim, not the send."""
    from app.models import Tenant
    from app.services import tenant_context
    from app.services.scheduler import _claim

    async with async_session_factory() as db:
        tenant = (
            await db.execute(select(Tenant).where(Tenant.slug == "kwik-klin"))
        ).scalar_one()
        tenant_id = tenant.id

    sent = []
    key = f"restart-regression:{uuid.uuid4().hex}"
    async with tenant_context.as_tenant(tenant_id):
        for _ in range(2):
            if await _claim(key):
                sent.append("mocked message")
    assert sent == ["mocked message"]
