"""Customer bill web link (/b/<token>): signed, live amount, UPI app buttons."""

import uuid
from decimal import Decimal

import pytest

from app.config import settings
from app.database import async_session_factory
from app.models import Customer, Order, PaymentMethod
from app.services import app_settings, bill_link, order_service, receipt, tenant_context
from tests.conftest import TEST_CUSTOMER_PHONE


def test_token_roundtrip_tamper_and_expiry() -> None:
    t, o = uuid.uuid4(), uuid.uuid4()
    tok = bill_link.make(t, o, now=1000)
    assert bill_link.parse(tok, now=1001) == (t, o)
    body, sig = tok.split(".")
    other = bill_link.make(t, uuid.uuid4(), now=1000).split(".")[0]
    assert bill_link.parse(f"{other}.{sig}", now=1001) is None      # swapped order
    assert bill_link.parse(tok[:-2] + "AA", now=1001) is None        # broken signature
    assert bill_link.parse(tok, now=1000 + bill_link.TTL_SECONDS + 5) is None
    assert bill_link.parse("garbage") is None


@pytest.fixture
async def order_with_upi(sent):
    home = await tenant_context.get_home_tenant_id()
    async with tenant_context.as_tenant(home):
        async with async_session_factory() as db:
            prev = {k: await app_settings.get(db, k) for k in ("upi_vpa", "upi_payee")}
            await app_settings.set_value(db, "upi_vpa", "kwikklin@testupi")
            await app_settings.set_value(db, "upi_payee", "Kwik Klin")
            o = await order_service.create_order(
                db, customer_phone=TEST_CUSTOMER_PHONE, customer_name="Bill Grahak",
                items=[{"type": "Shirt", "service": "Wash & Iron", "qty": 3, "rate": 40, "amount": 120},
                       {"type": "Saree", "service": "Dry Clean", "qty": 1, "rate": 150, "amount": 150}],
                total_amount=Decimal("270"), created_by="test",
            )
    try:
        yield home, o
    finally:
        async with tenant_context.as_tenant(home):
            async with async_session_factory() as db:
                for k, v in prev.items():
                    await app_settings.set_value(db, k, v or "")


async def test_bill_page_shows_bill_and_upi_apps_with_live_due(client, order_with_upi) -> None:
    home, o = order_with_upi
    tok = bill_link.make(home, o.id)
    r = await client.get(f"/b/{tok}")
    assert r.status_code == 200
    assert r.headers["x-robots-tag"] == "noindex" and "no-store" in r.headers["cache-control"]
    html = r.text
    assert o.order_number in html and "Bill Grahak" in html and "Saree" in html
    assert "••••" + TEST_CUSTOMER_PHONE[-4:] in html and TEST_CUSTOMER_PHONE not in html
    assert "Pay with Google Pay" in html and "Pay with PhonePe" in html and "Pay with Paytm" in html
    assert "package=com.google.android.apps.nbu.paisa.user" in html
    assert "am=270.00" in html and "pa=kwikklin@testupi" in html
    assert "<svg" in html                                          # desktop QR
    assert "{{" not in html

    async with tenant_context.as_tenant(home):
        async with async_session_factory() as db:
            order = await db.get(Order, o.id)
            await order_service.record_payment(db, order, amount=Decimal("100"), method=PaymentMethod.UPI,
                                               recorded_by="test", notify_customer=False)
    html = (await client.get(f"/b/{tok}")).text
    assert "am=170.00" in html                                     # live balance, same link

    async with tenant_context.as_tenant(home):
        async with async_session_factory() as db:
            order = await db.get(Order, o.id)
            await order_service.record_payment(db, order, amount=Decimal("170"), method=PaymentMethod.CASH,
                                               recorded_by="test", notify_customer=False)
    html = (await client.get(f"/b/{tok}")).text
    assert "Paid in full" in html and "Pay with Google Pay" not in html


async def test_bad_or_foreign_token_is_404(client, order_with_upi) -> None:
    home, o = order_with_upi
    assert (await client.get("/b/nonsense")).status_code == 404
    assert (await client.get("/b/nonsense/live")).status_code == 404
    wrong_shop = bill_link.make(uuid.uuid4(), o.id)
    assert (await client.get(f"/b/{wrong_shop}")).status_code == 404
    assert (await client.get(f"/b/{wrong_shop}/live")).status_code == 404


async def test_open_bill_page_learns_about_status_and_payment_changes(client, order_with_upi) -> None:
    """Dukaan status badle ya paisa likhe -> grahak ka khula page khud reload
    kare. Page /live se sirf ek version string poochta hai (koi rakam nahi)."""
    from app.models import OrderStatus

    home, o = order_with_upi
    tok = bill_link.make(home, o.id)
    html = (await client.get(f"/b/{tok}")).text
    assert f'data-live="/b/{tok}/live"' in html and "bill.js" in html
    v0 = (await client.get(f"/b/{tok}/live")).json()["v"]
    assert f'data-v="{v0}"' in html and "270" in v0 and "@" not in v0

    async with tenant_context.as_tenant(home):
        async with async_session_factory() as db:
            order = await db.get(Order, o.id)
            await order_service.update_status(db, order, OrderStatus.IN_WASH, changed_by="test", notify=False)
    v1 = (await client.get(f"/b/{tok}/live")).json()["v"]
    assert v1 != v0 and v1.startswith("IN_WASH|")


async def test_link_goes_into_shared_bill_text(order_with_upi, monkeypatch) -> None:
    home, o = order_with_upi
    monkeypatch.setattr(settings, "SITE_URL", "https://kwikklin.online")
    async with tenant_context.as_tenant(home):
        async with async_session_factory() as db:
            order = await db.get(Order, o.id)
            cust = await db.get(Customer, order.customer_id)
            out = receipt.payload(order, cust, None, await app_settings.all_settings(db), 0, 0)
    assert out["bill_url"].startswith("https://kwikklin.online/b/")
    assert out["bill_url"] in out["text"] and "View bill & pay online" in out["text"]
    assert "kwikklin.online/b/" not in out["print_text"]
    # Sharten WhatsApp mein nahi (bahut lamba ho jaata tha) — kagaz aur web bill par hain
    assert "Terms" not in out["text"] and "Terms & conditions" in out["print_text"]
    assert bill_link.parse(out["bill_url"].rsplit("/", 1)[1]) == (home, o.id)
