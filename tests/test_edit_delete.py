"""Owner-facing edit + delete for bills and customers.

The risky part is delete: a half-deleted bill leaves orphan payment rows
that keep showing up in reports, and deleting a customer must not silently
take their order history without saying so.
"""

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.database import async_session_factory
from app.models import (
    Conversation,
    Customer,
    Direction,
    Order,
    OrderStatusHistory,
    Payment,
    PaymentMethod,
)
from app.services import order_service
from tests.conftest import TEST_CUSTOMER_PHONE, purge_phones

H = {"X-API-Key": settings.ADMIN_API_KEY}


@pytest.fixture
async def bill(sent):
    """A real bill with a payment and status history."""
    async with async_session_factory() as db:
        order = await order_service.create_order(
            db, customer_phone=TEST_CUSTOMER_PHONE, customer_name="Editwala",
            items=[{"type": "shirt", "qty": 2, "service": "wash"}],
            total_amount=Decimal("400"), created_by="test",
        )
        await order_service.record_payment(
            db, order, amount=Decimal("100"), method=PaymentMethod.CASH
        )
        num, oid = order.order_number, order.id
    yield num, oid
    await purge_phones(TEST_CUSTOMER_PHONE)


# --- bill edit ---

async def test_edit_bill_items_total_date(client, bill) -> None:
    num, _ = bill
    r = await client.put(f"/orders/{num}", headers=H, json={
        "items": [{"qty": 5, "type": "pant"}],
        "total_amount": 750, "expected_delivery": "2026-09-01",
        "notes": "collar pe daag",
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert float(d["total_amount"]) == 750.0
    assert d["items"] == [{"qty": 5, "type": "pant"}]
    assert d["expected_delivery"] == "2026-09-01"
    assert d["notes"] == "collar pe daag"


async def test_edit_recalculates_payment_status(client, bill) -> None:
    """₹100 was paid on a ₹400 bill (PARTIAL). Drop the bill to ₹100 and it
    must become PAID by itself — the owner should never have to fix that."""
    num, _ = bill
    r = await client.put(f"/orders/{num}", headers=H, json={"total_amount": 100})
    assert r.status_code == 200
    assert r.json()["payment_status"] == "PAID"


async def test_edit_unknown_bill_404(client) -> None:
    r = await client.put("/orders/KK-00000000-99", headers=H, json={"total_amount": 10})
    assert r.status_code == 404


# --- bill delete ---

async def test_delete_bill_removes_payments_and_history(client, bill) -> None:
    num, oid = bill
    r = await client.delete(f"/orders/{num}", headers=H)
    assert r.status_code == 200

    async with async_session_factory() as db:
        assert (await db.execute(
            select(func.count()).select_from(Order).where(Order.id == oid)
        )).scalar_one() == 0
        assert (await db.execute(
            select(func.count()).select_from(Payment).where(Payment.order_id == oid)
        )).scalar_one() == 0, "orphan payments would still show in reports"
        assert (await db.execute(
            select(func.count()).select_from(OrderStatusHistory)
            .where(OrderStatusHistory.order_id == oid)
        )).scalar_one() == 0

    assert (await client.get(f"/orders/{num}", headers=H)).status_code == 404


async def test_delete_bill_is_audited(client, bill) -> None:
    num, _ = bill
    await client.delete(f"/orders/{num}?deleted_by=qa", headers=H)
    r = await client.get("/admin/api/activity?limit=20", headers=H)
    actions = [a["action"] for a in r.json()]
    assert "order_deleted" in actions, "destructive actions must leave a trail"


# --- customer edit ---

async def test_edit_customer_name_and_address(client, bill) -> None:
    r = await client.put(
        f"/admin/api/customers/{TEST_CUSTOMER_PHONE}", headers=H,
        json={"name": "Naya Naam", "address": "Sigra, Varanasi"},
    )
    assert r.status_code == 200
    async with async_session_factory() as db:
        c = (await db.execute(
            select(Customer).where(Customer.phone == TEST_CUSTOMER_PHONE)
        )).scalar_one()
        assert c.name == "Naya Naam"
        assert c.address == "Sigra, Varanasi"


async def test_edit_customer_phone_clash_rejected(client, bill) -> None:
    other = "+919999900095"
    async with async_session_factory() as db:
        db.add(Customer(phone=other, name="Doosra"))
        await db.commit()
    try:
        r = await client.put(
            f"/admin/api/customers/{TEST_CUSTOMER_PHONE}", headers=H,
            json={"phone": other.replace("+91", "")},
        )
        assert r.status_code == 409
    finally:
        await purge_phones(other)


async def test_edit_unknown_customer_404(client) -> None:
    r = await client.put(
        "/admin/api/customers/+919999900099", headers=H, json={"name": "X"}
    )
    assert r.status_code == 404


# --- customer delete ---

async def test_delete_customer_with_bills_blocked_without_force(client, bill) -> None:
    r = await client.delete(f"/admin/api/customers/{TEST_CUSTOMER_PHONE}", headers=H)
    assert r.status_code == 409
    assert "bill" in r.json()["detail"]
    async with async_session_factory() as db:
        assert (await db.execute(
            select(func.count()).select_from(Customer)
            .where(Customer.phone == TEST_CUSTOMER_PHONE)
        )).scalar_one() == 1, "nothing may be deleted on the refusal path"


async def test_delete_customer_force_cascades(client, bill) -> None:
    num, oid = bill
    async with async_session_factory() as db:
        cust = (await db.execute(
            select(Customer).where(Customer.phone == TEST_CUSTOMER_PHONE)
        )).scalar_one()
        db.add(Conversation(
            customer_id=cust.id, direction=Direction.INBOUND,
            message_text="kab tak ready hoga", wa_message_id="wamid.TESTdel1",
        ))
        await db.commit()
        cid = cust.id

    r = await client.delete(
        f"/admin/api/customers/{TEST_CUSTOMER_PHONE}?force=true", headers=H
    )
    assert r.status_code == 200
    assert r.json()["orders_deleted"] == 1

    async with async_session_factory() as db:
        for stmt, what in (
            (select(func.count()).select_from(Customer).where(Customer.id == cid), "customer"),
            (select(func.count()).select_from(Order).where(Order.customer_id == cid), "orders"),
            (select(func.count()).select_from(Payment).where(Payment.order_id == oid), "payments"),
            (select(func.count()).select_from(Conversation)
             .where(Conversation.customer_id == cid), "messages"),
        ):
            assert (await db.execute(stmt)).scalar_one() == 0, f"{what} left behind"


async def test_delete_customer_without_bills_needs_no_force(client) -> None:
    phone = "+919999900096"
    async with async_session_factory() as db:
        db.add(Customer(phone=phone, name="Khali"))
        await db.commit()
    try:
        r = await client.delete(f"/admin/api/customers/{phone}", headers=H)
        assert r.status_code == 200
    finally:
        await purge_phones(phone)
