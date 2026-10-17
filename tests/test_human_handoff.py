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


@pytest.mark.asyncio
async def test_human_handoff_waits_after_manager_inbox_reply(monkeypatch):
    """Dashboard Inbox replies are stored as sent_by=manager and count as human."""
    from app.routers import webhook
    from app.services import app_settings

    now = datetime.now(timezone.utc)
    customer = Customer(phone="+919999999996")
    inbound = Conversation(
        customer_id=customer.id,
        direction=Direction.INBOUND,
        message_text="rate kya hai?",
        wa_message_id="in-manager-1",
        created_at=now,
    )
    manager = Conversation(
        customer_id=customer.id,
        direction=Direction.OUTBOUND,
        message_text="Main rate bata deta hoon",
        wa_message_id="out-manager-1",
        sent_by="manager",
        created_at=now - timedelta(minutes=2),
    )

    class FakeResult:
        def scalar_one_or_none(self):
            return manager

    class FakeDB:
        async def execute(self, query, *_args, **_kwargs):
            # Verify the SQL filter explicitly includes the dashboard's
            # sent_by="manager" value, not only phone-originated "human".
            compiled = query.compile()
            assert "manager" in str(compiled.params)
            return FakeResult()

    async def fake_get(_db, key):
        assert key == "human_handoff_grace_minutes"
        return 15

    monkeypatch.setattr(app_settings, "get", fake_get)
    assert await webhook._human_handoff_waiting(FakeDB(), customer, inbound) is True


@pytest.mark.asyncio
async def test_human_handoff_rechecks_ai_stop_after_model_generation(monkeypatch):
    """A stop switch flipped while the model generates must block the send."""
    from types import SimpleNamespace

    from app.routers import webhook
    from app.services import human_handoff, scheduler

    now = datetime.now(timezone.utc)
    customer = SimpleNamespace(
        id=101,
        phone="+919876543210",
        agent_paused=False,
        opted_out=False,
    )
    inbound = SimpleNamespace(
        id=202,
        wa_message_id="test-human-handoff-stop",
        message_text="Where is my order?",
        created_at=now,
    )
    old_human = SimpleNamespace(created_at=now - timedelta(minutes=20))
    sent = []
    unclaimed = []
    enabled_checks = 0

    async def latest_human(_db, _customer_id):
        return old_human

    async def latest_outbound(_db, _customer_id):
        return None

    async def latest_inbound(_db, _customer_id):
        return inbound

    async def agent_enabled(_db, _customer):
        nonlocal enabled_checks
        enabled_checks += 1
        # Agent is on before generation, then switched off while the model runs.
        return enabled_checks == 1

    async def claim(_key):
        return True

    async def unclaim(key):
        unclaimed.append(key)

    async def build_ai_reply(*_args, **_kwargs):
        return "Your order is being processed."

    async def send_message(*_args, **kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(human_handoff, "_latest_human", latest_human)
    monkeypatch.setattr(human_handoff, "_latest_outbound", latest_outbound)
    monkeypatch.setattr(human_handoff, "_latest_inbound", latest_inbound)
    monkeypatch.setattr(webhook, "_customer_agent_enabled", agent_enabled)
    monkeypatch.setattr(human_handoff, "send_message", send_message)
    monkeypatch.setattr(scheduler, "_claim", claim)
    monkeypatch.setattr(scheduler, "_unclaim", unclaim)
    monkeypatch.setattr("app.services.ai_agent.build_ai_reply", build_ai_reply)

    result = await human_handoff._reply_one(
        db=object(), customer=customer, inbound=inbound, grace_minutes=15
    )

    assert result is False
    assert sent == []
    assert enabled_checks == 2
    assert len(unclaimed) == 1


@pytest.mark.asyncio
async def test_human_reply_during_ai_generation_cancels_pending_send(monkeypatch):
    """A human reply arriving during LLM generation resets the 15-minute pause."""
    from types import SimpleNamespace

    from app.routers import webhook
    from app.services import human_handoff, scheduler

    now = datetime.now(timezone.utc)
    customer = SimpleNamespace(
        id=303,
        phone="+919876543211",
        agent_paused=False,
        opted_out=False,
    )
    inbound = SimpleNamespace(
        id=404,
        wa_message_id="test-human-reply-race",
        message_text="Can you tell me the price?",
        created_at=now - timedelta(minutes=20),
    )
    old_human = SimpleNamespace(created_at=now - timedelta(minutes=20))
    new_human = SimpleNamespace(created_at=now)
    human_checks = 0
    sent = []
    unclaimed = []

    async def latest_human(_db, _customer_id):
        nonlocal human_checks
        human_checks += 1
        # No recent human reply before generation; one arrives while the LLM runs.
        return old_human if human_checks == 1 else new_human

    async def latest_outbound(_db, _customer_id):
        return None

    async def latest_inbound(_db, _customer_id):
        return inbound

    async def agent_enabled(_db, _customer):
        return True

    async def claim(_key):
        return True

    async def unclaim(key):
        unclaimed.append(key)

    async def build_ai_reply(*_args, **_kwargs):
        return "Here is the price."

    async def send_message(*_args, **kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(human_handoff, "_latest_human", latest_human)
    monkeypatch.setattr(human_handoff, "_latest_outbound", latest_outbound)
    monkeypatch.setattr(human_handoff, "_latest_inbound", latest_inbound)
    monkeypatch.setattr(webhook, "_customer_agent_enabled", agent_enabled)
    monkeypatch.setattr(human_handoff, "send_message", send_message)
    monkeypatch.setattr(scheduler, "_claim", claim)
    monkeypatch.setattr(scheduler, "_unclaim", unclaim)
    monkeypatch.setattr("app.services.ai_agent.build_ai_reply", build_ai_reply)

    result = await human_handoff._reply_one(
        db=object(), customer=customer, inbound=inbound, grace_minutes=15
    )

    assert result is False
    assert sent == [], "AI must not send after a human replies during generation"
    assert human_checks == 2
    assert len(unclaimed) == 1



@pytest.mark.asyncio
async def test_repeated_welcome_is_replaced_after_first_welcome():
    from app.routers.webhook import _avoid_repeated_welcome

    previous = [
        type("Message", (), {
            "message_text": "Welcome to Kwik Klin 😊 May I know your name, please?"
        })()
    ]

    class FakeScalars:
        def all(self):
            return previous

    class FakeResult:
        def scalars(self):
            return FakeScalars()

    class FakeDB:
        async def execute(self, *_args, **_kwargs):
            return FakeResult()

    customer = type("Customer", (), {"id": 1, "phone": "+919999999999})()
    result = await _avoid_repeated_welcome(
        FakeDB(), customer,
        "Welcome to Kwik Klin 😊 May I know your name, please?",
    )

    assert "Welcome" not in result
    assert "laundry pickup" in result


@pytest.mark.asyncio
async def test_first_welcome_is_preserved():
    from app.routers.webhook import _avoid_repeated_welcome

    class FakeScalars:
        def all(self):
            return []

    class FakeResult:
        def scalars(self):
            return FakeScalars()

    class FakeDB:
        async def execute(self, *_args, **_kwargs):
            return FakeResult()

    customer = type("Customer", (), {"id": 1, "phone": "+919999999998"})()
    reply = "Welcome to Kwik Klin 😊 May I know your name, please?"
    assert await _avoid_repeated_welcome(FakeDB(), customer, reply) == reply
