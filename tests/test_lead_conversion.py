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
from app.models import Customer, Lead, Order, OrderStatus
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
            await db.execute(delete(Order).where(Order.customer_id.in_(customer_ids)))
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
        ist = timezone(timedelta(hours=5, minutes=30))
        run_at = (
            datetime.now(timezone.utc)
            .astimezone(ist)
            .replace(hour=10, minute=0, second=0, microsecond=0)
            .astimezone(timezone.utc)
        )
        async with async_session_factory() as db:
            db.add(Customer(phone=TEST_LEAD_PHONE, name="Lead Test"))
            db.add(
                Lead(
                    phone=TEST_LEAD_PHONE,
                    name="Lead Test",
                    stage="INTERESTED",
                    followup_count=0,
                    next_followup_at=run_at - timedelta(minutes=1),
                )
            )
            await db.commit()

        sent_count = await leads_service.run_lead_followups(now=run_at)

        assert sent_count >= 1
        matching = [message for message in sent if message["to"] == TEST_LEAD_PHONE]
        assert len(matching) == 1
        assert "pickup" in matching[0]["text"].lower()

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



async def test_website_utm_fields_are_saved_on_lead(monkeypatch) -> None:
    async def fake_send(db, *, to_phone: str, text: str, **kwargs):
        return "wamid.TEST-lead"

    monkeypatch.setattr(leads_service, "send_message", fake_send)
    try:
        async with async_session_factory() as db:
            customer = Customer(phone=TEST_LEAD_PHONE, name="UTM Test")
            db.add(customer)
            await db.flush()
            await leads_service.note_inquiry(
                db, customer,
                "Hello Kwik Klin\nLead source: website\nutm_source: google"
                "\nutm_medium: organic\nutm_campaign: winter-laundry",
            )
            lead = (
                await db.execute(select(Lead).where(Lead.phone == TEST_LEAD_PHONE))
            ).scalar_one()
            assert lead.source == "google"
            assert lead.source_medium == "organic"
            assert lead.source_campaign == "winter-laundry"
    finally:
        await _cleanup_lead()


async def test_due_lead_followup_respects_marketing_opt_out(monkeypatch) -> None:
    sent = []

    async def fake_send(db, *, to_phone: str, text: str, **kwargs):
        sent.append({"to": to_phone, "text": text})
        return "wamid.TEST-lead"

    async def fake_setting(db, key):
        if key == "marketing_autonomy":
            return "auto"
        return 0

    monkeypatch.setattr(leads_service, "send_message", fake_send)
    monkeypatch.setattr(app_settings, "get", fake_setting)
    try:
        ist = timezone(timedelta(hours=5, minutes=30))
        run_at = datetime.now(timezone.utc).astimezone(ist).replace(
            hour=10, minute=0, second=0, microsecond=0
        ).astimezone(timezone.utc)
        async with async_session_factory() as db:
            customer = Customer(
                phone=TEST_LEAD_PHONE, name="Opted Out", marketing_opt_out=True
            )
            db.add(customer)
            db.add(Lead(
                phone=TEST_LEAD_PHONE, name="Opted Out", stage="INTERESTED",
                followup_count=0, next_followup_at=run_at - timedelta(minutes=1),
            ))
            await db.commit()

        sent_count = await leads_service.run_lead_followups(now=run_at)
        assert sent_count == 0
        assert not [item for item in sent if item["to"] == TEST_LEAD_PHONE]
        async with async_session_factory() as db:
            lead = (
                await db.execute(select(Lead).where(Lead.phone == TEST_LEAD_PHONE))
            ).scalar_one()
            assert lead.stage == "LOST"
            assert lead.next_followup_at is None
    finally:
        await _cleanup_lead()


async def test_utm_campaign_is_preserved_on_first_order_conversion(monkeypatch) -> None:
    try:
        async with async_session_factory() as db:
            customer = Customer(phone=TEST_LEAD_PHONE, name="Attribution Test")
            db.add(customer)
            await db.flush()
            lead = Lead(
                phone=TEST_LEAD_PHONE,
                name="Attribution Test",
                source="google",
                source_medium="organic",
                source_campaign="winter-laundry",
                stage="CONTACTED",
            )
            db.add(lead)
            order = Order(
                order_number=f"KK-ATTR-{uuid.uuid4().hex[:8]}",
                customer_id=customer.id,
                status=OrderStatus.RECEIVED,
                items=[{"type": "shirt", "qty": 1}],
                total_amount=100,
                amount_paid=40,
            )
            db.add(order)
            await db.commit()

            await leads_service.mark_converted(db, TEST_LEAD_PHONE)
            await db.refresh(order)
            await db.refresh(lead)
            assert lead.stage == "CONVERTED"
            assert order.acquisition_source == "google"
            assert order.acquisition_campaign == "winter-laundry"
    finally:
        await _cleanup_lead()


def test_website_source_marker_is_detected_without_personal_data() -> None:
    """Website attribution marker survives WhatsApp prefill into the lead source."""
    from app.services.leads import _source_from_message

    assert _source_from_message("Hello Kwik Klin\nLead source: website") == "website"
    assert _source_from_message("Hello Kwik Klin\nLead source: website\nutm_source: google") == "google"
    assert _source_from_message("Hello Kwik Klin\nLead source: website\nutm_source: Instagram Ads") == "instagramads"
    assert _source_from_message("Hello Kwik Klin, I have a question.") == "whatsapp"
    assert _source_from_message("GOOGLE pickup request") == "google"



TEST_MANUAL_LEAD_PHONE = f"+91998{uuid.uuid4().int % 10_000_000:07d}"


async def _cleanup_manual_lead() -> None:
    async with async_session_factory() as db:
        await db.execute(delete(Lead).where(Lead.phone == TEST_MANUAL_LEAD_PHONE))
        await db.execute(delete(Customer).where(Customer.phone == TEST_MANUAL_LEAD_PHONE))
        await db.commit()


async def test_manual_phone_call_lead_saves_without_sending(client) -> None:
    """Saving a call lead must not silently send or schedule a follow-up."""
    from app.config import settings

    try:
        response = await client.post(
            "/admin/api/leads/manual",
            headers={"X-API-Key": settings.ADMIN_API_KEY},
            json={
                "name": "Phone Call Lead",
                "phone": TEST_MANUAL_LEAD_PHONE,
                "need": "Dry cleaning",
                "call_notes": "Asked about price and pickup",
                "send_whatsapp": False,
                "whatsapp_consent_confirmed": False,
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["whatsapp_status"] == "not_sent"
        async with async_session_factory() as db:
            lead = (
                await db.execute(
                    select(Lead).where(Lead.phone == TEST_MANUAL_LEAD_PHONE)
                )
            ).scalar_one()
            customer = (
                await db.execute(
                    select(Customer).where(Customer.phone == TEST_MANUAL_LEAD_PHONE)
                )
            ).scalar_one()
            assert customer.name == "Phone Call Lead"
            assert lead.source == "phone_call"
            assert lead.items_text == "Dry cleaning"
            assert lead.stage == "NEW"
            assert lead.next_followup_at is None
            assert "Asked about price and pickup" in (lead.notes or "")
    finally:
        await _cleanup_manual_lead()


async def test_manual_phone_call_lead_requires_consent_to_send(client) -> None:
    from app.config import settings

    response = await client.post(
        "/admin/api/leads/manual",
        headers={"X-API-Key": settings.ADMIN_API_KEY},
        json={
            "name": "No Consent Lead",
            "phone": TEST_MANUAL_LEAD_PHONE,
            "need": "Laundry",
            "send_whatsapp": True,
            "whatsapp_consent_confirmed": False,
            "message": "Hello",
        },
    )
    assert response.status_code == 400
    assert "agreed to WhatsApp" in response.json()["detail"]


async def test_manual_phone_call_lead_can_send_approved_whatsapp(client, monkeypatch) -> None:
    from app.config import settings
    from app.services import whatsapp

    sent = []

    async def fake_send(db, *, to_phone, text, **kwargs):
        sent.append({"to": to_phone, "text": text, "kwargs": kwargs})
        return "wamid.TEST-manual-lead"

    monkeypatch.setattr(whatsapp, "send_message", fake_send)
    try:
        response = await client.post(
            "/admin/api/leads/manual",
            headers={"X-API-Key": settings.ADMIN_API_KEY},
            json={
                "name": "Manual Lead",
                "phone": TEST_MANUAL_LEAD_PHONE,
                "need": "Bulk laundry",
                "call_notes": "Asked for a quote",
                "whatsapp_consent_confirmed": True,
                "send_whatsapp": True,
                "message": "Namaste ji, here are the details you requested.",
            },
        )
        assert response.status_code == 201, response.text
        result = response.json()
        assert result["whatsapp_status"] == "sent"
        assert len(sent) == 1
        assert sent[0]["to"] == TEST_MANUAL_LEAD_PHONE
        assert sent[0]["kwargs"]["intent"] == "promotional"
        async with async_session_factory() as db:
            lead = (
                await db.execute(
                    select(Lead).where(Lead.phone == TEST_MANUAL_LEAD_PHONE)
                )
            ).scalar_one()
            assert lead.stage == "CONTACTED"
            assert lead.next_followup_at is not None
            assert "consent confirmed by owner" in (lead.notes or "")
    finally:
        await _cleanup_manual_lead()
