"""Outbound retry queue must fail closed on missing or cross-tenant identity."""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import select

from app.database import async_session_factory
from app.models import Customer, OutboundMessage, Tenant
from app.services import tenant_context, whatsapp
from app.services import waha


@pytest.fixture
async def retry_tenant_and_home_customer():
    home_id = await tenant_context.get_home_tenant_id()
    assert home_id is not None
    slug = f"retry-guard-{uuid.uuid4().hex[:10]}"
    phone = f"+9198{uuid.uuid4().int % 10**8:08d}"
    tenant_id = uuid.uuid4()
    queue_ids: list[uuid.UUID] = []

    async with tenant_context.as_system_context():
        async with async_session_factory() as db:
            tenant = Tenant(
                id=tenant_id,
                slug=slug,
                shop_name="Retry Guard Test",
                owner_name="Test",
                plan="growth",
                owner_phone="+919999900987",
                status="active",
                onboarding_done=True,
                wa_token="test-token",
                wa_phone_number_id="test-phone-id",
            )
            db.add(tenant)
            await db.commit()

    async with tenant_context.as_tenant(home_id):
        async with async_session_factory() as db:
            customer = Customer(phone=phone, name="Home Tenant Customer")
            db.add(customer)
            await db.commit()

    try:
        yield {"tenant_id": tenant_id, "home_id": home_id, "phone": phone, "queue_ids": queue_ids}
    finally:
        async with tenant_context.as_system_context():
            async with async_session_factory() as db:
                if queue_ids:
                    rows = (
                        await db.execute(
                            select(OutboundMessage).where(OutboundMessage.id.in_(queue_ids))
                        )
                    ).scalars().all()
                    for row in rows:
                        await db.delete(row)
                await db.commit()
        async with tenant_context.as_tenant(home_id):
            async with async_session_factory() as db:
                row = (
                    await db.execute(select(Customer).where(Customer.phone == phone))
                ).scalar_one_or_none()
                if row is not None:
                    await db.delete(row)
                    await db.commit()
        async with tenant_context.as_system_context():
            async with async_session_factory() as db:
                row = (
                    await db.execute(select(Tenant).where(Tenant.id == tenant_id))
                ).scalar_one_or_none()
                if row is not None:
                    await db.delete(row)
                    await db.commit()


async def _queue_retry(payload: dict, to_phone: str, fixture_data: dict) -> uuid.UUID:
    row_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    # The queue drain only takes the oldest 30 rows. Put these guard rows ahead
    # of leftovers from other tests so the assertions exercise the retry path.
    created_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
    async with tenant_context.as_system_context():
        async with async_session_factory() as db:
            db.add(
                OutboundMessage(
                    id=row_id,
                    to_phone=to_phone,
                    payload=payload,
                    status="queued",
                    attempts=0,
                    next_attempt_at=now,
                    created_at=created_at,
                )
            )
            await db.commit()
    fixture_data["queue_ids"].append(row_id)
    return row_id


async def _queue_row(row_id: uuid.UUID) -> OutboundMessage:
    async with tenant_context.as_system_context():
        async with async_session_factory() as db:
            return (
                await db.execute(
                    select(OutboundMessage).where(OutboundMessage.id == row_id)
                )
            ).scalar_one()


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "must not send", "sent_by": "manager"},
        {"text": "must not send", "sent_by": "manager", "tenant_id": "not-a-uuid"},
    ],
)
async def test_retry_without_valid_tenant_is_dead_lettered_without_send(
    payload, retry_tenant_and_home_customer, monkeypatch
):
    async def forbidden_transport(*args, **kwargs):
        raise AssertionError("transport must not be called for a retry without tenant identity")

    monkeypatch.setattr(waha, "_post", forbidden_transport)
    monkeypatch.setattr(whatsapp, "MAX_OUTBOUND_AGE", timedelta(days=365_000))
    row_id = await _queue_retry(payload, retry_tenant_and_home_customer["phone"], retry_tenant_and_home_customer)

    await whatsapp.drain_outbound_queue()

    row = await _queue_row(row_id)
    assert row.status == "dead"
    assert "tenant" in (row.last_error or "").lower()


async def test_retry_cannot_send_home_customer_message_under_another_tenant(
    retry_tenant_and_home_customer, monkeypatch
):
    async def forbidden_transport(*args, **kwargs):
        raise AssertionError("cross-tenant retry must be rejected before transport")

    monkeypatch.setattr(waha, "_post", forbidden_transport)
    monkeypatch.setattr(whatsapp, "MAX_OUTBOUND_AGE", timedelta(days=365_000))
    data = retry_tenant_and_home_customer
    row_id = await _queue_retry(
        {
            "text": "cross-tenant message must not send",
            "sent_by": "manager",
            "intent": "transactional",
            "tenant_id": str(data["tenant_id"]),
        },
        data["phone"],
        data,
    )

    await whatsapp.drain_outbound_queue()

    row = await _queue_row(row_id)
    assert row.status == "dead"
    assert "recipient" in (row.last_error or "").lower()
