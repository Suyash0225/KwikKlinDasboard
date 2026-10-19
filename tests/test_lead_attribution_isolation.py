"""Tenant isolation regression for duplicate phone numbers and lead attribution.

A phone number may enquire at more than one laundry. Converting the lead for
one tenant must never convert or copy attribution into another tenant's rows.
No live messaging is performed by this test.
"""
import uuid

import pytest
from sqlalchemy import delete, select

from app.database import async_session_factory
from app.models import Customer, Lead, Order, OrderStatus, Tenant
from app.services import leads as leads_service, tenant_context


@pytest.mark.requires_independent_sessions
async def test_duplicate_phone_attribution_is_scoped_to_current_tenant() -> None:
    home_id = await tenant_context.get_home_tenant_id()
    assert home_id is not None
    suffix = uuid.uuid4().hex[:10]
    phone = f"+9198{uuid.uuid4().int % 10**8:08d}"
    slug = f"lead-attribution-{suffix}"
    tenant_b_id = None
    customer_a_id = customer_b_id = None
    lead_a_id = lead_b_id = None
    order_a_id = order_b_id = None

    try:
        async with async_session_factory() as db:
            async with tenant_context.system_context(db):
                tenant_b = Tenant(
                    slug=slug,
                    shop_name="Lead Attribution Isolation Test",
                    owner_name="Test Owner",
                    owner_phone=f"+9197{uuid.uuid4().int % 10**8:08d}",
                    plan="starter",
                    status="active",
                )
                db.add(tenant_b)
                await db.flush()
                tenant_b_id = tenant_b.id

                customer_a = Customer(
                    phone=phone, name="Tenant A Customer", tenant_id=home_id
                )
                customer_b = Customer(
                    phone=phone, name="Tenant B Customer", tenant_id=tenant_b_id
                )
                db.add_all([customer_a, customer_b])
                await db.flush()
                customer_a_id, customer_b_id = customer_a.id, customer_b.id

                lead_a = Lead(
                    phone=phone, name="Tenant A Lead", source="google",
                    source_campaign="tenant-a-campaign", stage="CONTACTED",
                    tenant_id=home_id,
                )
                lead_b = Lead(
                    phone=phone, name="Tenant B Lead", source="website",
                    source_campaign="tenant-b-campaign", stage="CONTACTED",
                    tenant_id=tenant_b_id,
                )
                db.add_all([lead_a, lead_b])
                await db.flush()
                lead_a_id, lead_b_id = lead_a.id, lead_b.id

                order_a = Order(
                    order_number=f"KK-A-{suffix}", customer_id=customer_a_id,
                    status=OrderStatus.RECEIVED, items=[{"type": "shirt", "qty": 1}],
                    total_amount=100, amount_paid=0, tenant_id=home_id,
                )
                order_b = Order(
                    order_number=f"KK-B-{suffix}", customer_id=customer_b_id,
                    status=OrderStatus.RECEIVED, items=[{"type": "shirt", "qty": 1}],
                    total_amount=100, amount_paid=0, tenant_id=tenant_b_id,
                )
                db.add_all([order_a, order_b])
                await db.flush()
                order_a_id, order_b_id = order_a.id, order_b.id
                await db.commit()

        async with tenant_context.as_tenant(tenant_b_id):
            async with async_session_factory() as db:
                await leads_service.mark_converted(db, phone)

        async with async_session_factory() as db:
            async with tenant_context.system_context(db):
                lead_a = await db.get(Lead, lead_a_id)
                lead_b = await db.get(Lead, lead_b_id)
                order_a = await db.get(Order, order_a_id)
                order_b = await db.get(Order, order_b_id)

                assert lead_a is not None and lead_a.stage == "CONTACTED"
                assert lead_b is not None and lead_b.stage == "CONVERTED"
                assert order_a is not None and order_a.acquisition_source is None
                assert order_a.acquisition_campaign is None
                assert order_b is not None and order_b.acquisition_source == "website"
                assert order_b.acquisition_campaign == "tenant-b-campaign"
    finally:
        async with async_session_factory() as db:
            async with tenant_context.system_context(db):
                if order_a_id or order_b_id:
                    await db.execute(delete(Order).where(Order.id.in_([x for x in (order_a_id, order_b_id) if x])))
                if lead_a_id or lead_b_id:
                    await db.execute(delete(Lead).where(Lead.id.in_([x for x in (lead_a_id, lead_b_id) if x])))
                if customer_a_id or customer_b_id:
                    await db.execute(delete(Customer).where(Customer.id.in_([x for x in (customer_a_id, customer_b_id) if x])))
                if tenant_b_id:
                    await db.execute(delete(Tenant).where(Tenant.id == tenant_b_id))
                await db.commit()
