"""AI lead-conversion funnel tests.

These tests never call real WhatsApp. They verify:
- enquiry creates a timed conversion follow-up
- a customer reply keeps the lead alive instead of cancelling follow-up
- due follow-up sends a conversion-oriented message
"""

from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import delete, select

from app.database import async_session_factory
from app.models import Customer, Lead
from app.services import app_settings, leads as leads_service


TEST_LEAD_PHONE = f"+919999{uuid.uuid4().int % 1_000_000:06d}"


async def _cleanup_lead() -> None:
    async with async_session_factory() as db:
        customer_ids = (
            await db.execute(
                select(Customer.id).where(Customer.phone == TEST_LEAD_PHONE)
            )
        ).scalars().all()
        await db.execute(delete(Lead).where(Lead.phone == TEST_LEAD_PHONE))
        if customer_ids:
            await db.execute(
                delete(Customer).where(Customer.id.in_(customer_ids))
            )
        await db.commit()


async def test_inquiry_creates_conversion_followup(monkeypatch) -> None:
    sent = []

    async def fake_send(db, *, to_phone: str, text: str, **kwargs):
        sent.append({"to": to_phone, "text": text})
        return "wamid.TEST-lead"

    monkeypatch.setattr(leads_service, "send_message", fake_send)

    try:
        async with async_session_factory() as db:
            customer = Customer(phone=TEST_LEAD_PHONE, name="Lead Test")
            db.add(customer)
            await db.flush()

            await leads_service.note_inquiry(
                db, customer, "shirt wash ka rate kya hai?"
            )

            lead = (
                await db.execute(
                    select(Lead).where(Lead.phone == TEST_LEAD_PHONE)
                )
            ).scalar_one()

            assert lead.stage == "CONTACTED"
            assert lead.next_followup_at is not None
            assert lead.next_followup_at <= (
                datetime.now(timezone.utc) + timedelta(hours=2, minutes=1)
            )
            assert lead.next_followup_at >= (
                datetime.now(timezone.utc) + timedelta(hours=1, minutes=59)
            )
    finally:
        await _cleanup_lead()


async def test_customer_reply_keeps_followup_alive(monkeypatch) -> None:
    async def fake_send(db, *, to_phone: str, text: str, **kwargs):
        return "wamid.TEST-lead"

    monkeypatch.setattr(leads_service, "send_message", fake_send)

    try:
        async with async_session_factory() as db:
            customer = Customer(phone=TEST_LEAD_PHONE, name="Lead Test")
            db.add(customer)
            await db.flush()
            lead = Lead(
                phone=TEST_LEAD_PHONE,
                name="Lead Test",
                stage="CONTACTED",
                followup_count=0,
                next_followup_at=datetime.now(timezone.utc),
            )
            db.add(lead)
            await db.commit()

            await leads_service.note_inquiry(
                db, customer, "haan price batao"
            )

            lead = (
                await db.execute(
                    select(Lead).where(Lead.phone == TEST_LEAD_PHONE)
                )
            ).scalar_one()
            assert lead.stage == "INTERESTED"
            assert lead.next_followup_at is not None
            assert lead.next_followup_at > datetime.now(timezone.utc)
    finally:
        await _cleanup_lead()


async def test_due_interested_lead_gets_conversion_nudge(monkeypatch) -> None:
    sent = []

    async def fake_send(db, *, to_phone: str, text: str, **kwargs):
        sent.append({"to": to_phone, "text": text})
        return "wamid.TEST-lead"

    async def fake_setting(db, key):
        if key == "marketing_autonomy":
            return "auto"
        return "0"

    monkeypatch.setattr(leads_service, "send_message", fake_send)
    monkeypatch.setattr(app_settings, "get", fake_setting)

    try:
        async with async_session_factory() as db:
            db.add(
                Lead(
                    phone=TEST_LEAD_PHONE,
                    name="Lead Test",
                    stage="INTERESTED",
                    followup_count=0,
                    next_followup_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                )
            )
            await db.commit()

        sent_count = await leads_service.run_lead_followups(
            now=datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        )

        assert sent_count == 1
        assert len(sent) == 1
        assert sent[0]["to"] == TEST_LEAD_PHONE
        assert "pickup" in sent[0]["text"].lower()

        async with async_session_factory() as db:
            lead = (
                await db.execute(
                    select(Lead).where(Lead.phone == TEST_LEAD_PHONE)
                )
            ).scalar_one()
            assert lead.followup_count == 1
            assert lead.stage == "INTERESTED"
            assert lead.next_followup_at is not None
    finally:
        await _cleanup_lead()
