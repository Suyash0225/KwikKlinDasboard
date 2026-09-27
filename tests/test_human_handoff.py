from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import Conversation, Customer, Direction


@pytest.mark.asyncio
async def test_human_handoff_waits_15_minutes_after_human_reply(monkeypatch):
    """A human reply pauses AI for 15 minutes from the human reply time."""
    from app.routers import webhook

    now = datetime.now(timezone.utc)
    customer = Customer(phone="+919999999999")
    inbound = Conversation(
        customer_id=customer.id,
        direction=Direction.INBOUND,
        message_text="hello",
        wa_message_id="in-1",
        created_at=now,
    )
    human = Conversation(
        customer_id=customer.id,
        direction=Direction.OUTBOUND,
        message_text="haan ji",
        wa_message_id="out-1",
        sent_by="human",
        created_at=now - timedelta(minutes=2),
    )

    class FakeResult:
        def scalar_one_or_none(self):
            return human

    class FakeDB:
        async def execute(self, *_args, **_kwargs):
            return FakeResult()

    async def fake_get(_db, key):
        assert key == "human_handoff_grace_minutes"
        return 15

    # The helper imports app_settings inside the function, so patch the module
    # object it resolves to.
    from app.services import app_settings
    monkeypatch.setattr(app_settings, "get", fake_get)

    assert await webhook._human_handoff_waiting(FakeDB(), customer, inbound) is True


@pytest.mark.asyncio
async def test_human_handoff_expires_after_grace(monkeypatch):
    from app.routers import webhook

    now = datetime.now(timezone.utc)
    customer = Customer(phone="+919999999998")
    inbound = Conversation(
        customer_id=customer.id,
        direction=Direction.INBOUND,
        message_text="hello",
        wa_message_id="in-2",
        created_at=now - timedelta(minutes=16),
    )
    human = Conversation(
        customer_id=customer.id,
        direction=Direction.OUTBOUND,
        message_text="haan ji",
        wa_message_id="out-2",
        sent_by="human",
        created_at=now - timedelta(minutes=18),
    )

    class FakeResult:
        def scalar_one_or_none(self):
            return human

    class FakeDB:
        async def execute(self, *_args, **_kwargs):
            return FakeResult()

    from app.services import app_settings
    async def fake_get(_db, key):
        return 15
    monkeypatch.setattr(app_settings, "get", fake_get)

    assert await webhook._human_handoff_waiting(FakeDB(), customer, inbound) is False


@pytest.mark.asyncio
async def test_human_handoff_without_human_reply_does_not_pause(monkeypatch):
    from app.routers import webhook

    now = datetime.now(timezone.utc)
    customer = Customer(phone="+919999999997")
    inbound = Conversation(
        customer_id=customer.id,
        direction=Direction.INBOUND,
        message_text="hello",
        wa_message_id="in-3",
        created_at=now,
    )

    class FakeResult:
        def scalar_one_or_none(self):
            return None

    class FakeDB:
        async def execute(self, *_args, **_kwargs):
            return FakeResult()

    assert await webhook._human_handoff_waiting(FakeDB(), customer, inbound) is False
