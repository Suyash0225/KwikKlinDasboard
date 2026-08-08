"""Live-conversation follow-ups: nudge the quiet, never pester the silent.

The owner wants the 24h window kept open and orders won from chatter. The
risk on the other side is a banned WhatsApp number, so these tests pin the
limits: the cap, the gap, the window, and every "leave them alone" case.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text as sqltext

from app.database import async_session_factory
from app.models import Conversation, Customer, Direction
from app.services import app_settings, engage
from tests.conftest import TEST_CUSTOMER_PHONE, purge_phones

HOURS_AGO = lambda h: datetime.now(timezone.utc) - timedelta(hours=h)  # noqa: E731


@pytest.fixture(autouse=True)
async def _clean():
    await purge_phones(TEST_CUSTOMER_PHONE)
    yield
    await purge_phones(TEST_CUSTOMER_PHONE)


@pytest.fixture(autouse=True)
def _no_quiet_hours(monkeypatch):
    """These tests are about the rules, not about what time it is."""
    monkeypatch.setattr(engage, "_in_quiet_hours", lambda now_ist: False)


async def _talking_customer(hours_ago: float, *, name="Chup Grahak") -> Customer:
    """A customer who wrote `hours_ago` and never heard back since."""
    async with async_session_factory() as s:
        cust = Customer(phone=TEST_CUSTOMER_PHONE, name=name, last_message_at=HOURS_AGO(hours_ago))
        s.add(cust)
        await s.flush()
        s.add(
            Conversation(
                customer_id=cust.id, direction=Direction.INBOUND,
                message_text="bhaiya blanket dhulwana tha, kitna lagega?",
                wa_message_id="wamid.TESTeng-in", created_at=HOURS_AGO(hours_ago),
            )
        )
        await s.commit()
        return cust


async def _nudges() -> list[Conversation]:
    async with async_session_factory() as s:
        return list(
            (
                await s.execute(
                    select(Conversation)
                    .join(Customer, Customer.id == Conversation.customer_id)
                    .where(Customer.phone == TEST_CUSTOMER_PHONE,
                           Conversation.sent_by == engage.SENT_BY)
                    .order_by(Conversation.created_at)
                )
            ).scalars().all()
        )


async def test_quiet_conversation_gets_one_warm_nudge(sent) -> None:
    await _talking_customer(7)
    await engage.run_conversation_followups()
    rows = await _nudges()
    assert len(rows) == 1 and rows[0].message_text.strip()


async def test_too_soon_is_left_alone(sent) -> None:
    """Silence of two hours is a person having lunch, not a lost customer."""
    await _talking_customer(2)
    await engage.run_conversation_followups()
    assert await _nudges() == []


async def test_a_closed_window_is_never_chased(sent) -> None:
    """Past 24h only a template could go out — and a marketing blast to
    someone who went quiet is how a WhatsApp number gets banned."""
    await _talking_customer(30)
    await engage.run_conversation_followups()
    assert await _nudges() == []


async def test_the_cap_holds(sent) -> None:
    """Two nudges per conversation, then stop until THEY say something."""
    await _talking_customer(7)
    await engage.run_conversation_followups()
    assert len(await _nudges()) == 1

    # pretend both the first nudge and enough time have passed
    async with async_session_factory() as s:
        await s.execute(
            sqltext(
                "UPDATE conversations SET created_at = now() - interval '7 hours' "
                "WHERE sent_by = :sb AND customer_id IN "
                "(SELECT id FROM customers WHERE phone = :p)"
            ),
            {"sb": engage.SENT_BY, "p": TEST_CUSTOMER_PHONE},
        )
        await s.commit()
    await engage.run_conversation_followups()
    assert len(await _nudges()) == 2, "doosra nudge chalta hai"

    async with async_session_factory() as s:
        await s.execute(
            sqltext(
                "UPDATE conversations SET created_at = now() - interval '7 hours' "
                "WHERE sent_by = :sb AND customer_id IN "
                "(SELECT id FROM customers WHERE phone = :p)"
            ),
            {"sb": engage.SENT_BY, "p": TEST_CUSTOMER_PHONE},
        )
        await s.commit()
    await engage.run_conversation_followups()
    assert len(await _nudges()) == 2, "teesra nahi jana chahiye"


async def test_an_order_ends_the_chase(sent) -> None:
    """They already ordered — there is nothing left to sell them today."""
    from app.services.order_service import create_order

    await _talking_customer(7)
    async with async_session_factory() as db:
        await create_order(
            db, customer_phone=TEST_CUSTOMER_PHONE, items=[{"type": "Blanket", "qty": 1}],
            created_by="agent",
        )
    await engage.run_conversation_followups()
    assert await _nudges() == []


async def test_opted_out_is_untouchable(sent) -> None:
    await _talking_customer(7)
    async with async_session_factory() as s:
        cust = (
            await s.execute(select(Customer).where(Customer.phone == TEST_CUSTOMER_PHONE))
        ).scalar_one()
        cust.opted_out = True
        await s.commit()
    await engage.run_conversation_followups()
    assert await _nudges() == []


async def test_owner_takeover_silences_the_agent(sent) -> None:
    """He is answering this thread himself — the bot must not talk over him."""
    await _talking_customer(7)
    async with async_session_factory() as s:
        cust = (
            await s.execute(select(Customer).where(Customer.phone == TEST_CUSTOMER_PHONE))
        ).scalar_one()
        cust.agent_paused = True
        await s.commit()
    await engage.run_conversation_followups()
    assert await _nudges() == []


async def test_switch_off_means_off(sent) -> None:
    await _talking_customer(7)
    async with async_session_factory() as db:
        await app_settings.set_value(db, "engage_followups_enabled", False)
    try:
        await engage.run_conversation_followups()
        assert await _nudges() == []
    finally:
        async with async_session_factory() as db:
            await app_settings.set_value(db, "engage_followups_enabled", True)
