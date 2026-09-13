"""UPI pay link — signed token, aur wo public page jo UPI app kholta hai.

Asli baat jo is poore design ki wajah hai: WhatsApp sirf http/https ko
tap-able banata hai. `upi://` seedha message mein daalne par wo plain text
rehta hai. Isliye message mein https jaata hai aur upi:// us page se fire
hota hai.

Token public URL mein rehta hai aur WhatsApp par forward ek tap ka kaam
hai, isliye do cheezein tests mein pakki honi chahiye: usmein grahak ka
kuch na ho, aur amount badal kar apna link na banaya ja sake.
"""

import time
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text as sqltext

from app.database import async_session_factory
from app.services import app_settings, pay_link, tenant_context

VPA = "kwikklin@testbank"
PAYEE = "Kwik Klin Test"


@pytest.fixture
async def shop_upi():
    """Home dukaan ka UPI bhara hua, aur baad mein waisa hi wapas."""
    async with async_session_factory() as db:
        before_vpa = await app_settings.get(db, "upi_vpa")
        before_payee = await app_settings.get(db, "upi_payee")
        before_base = await app_settings.get(db, "public_base_url")
        await app_settings.set_value(db, "upi_vpa", VPA)
        await app_settings.set_value(db, "upi_payee", PAYEE)
        await app_settings.set_value(db, "public_base_url", "https://shop.example")
        await db.commit()
    yield
    async with async_session_factory() as db:
        await app_settings.set_value(db, "upi_vpa", before_vpa or "")
        await app_settings.set_value(db, "upi_payee", before_payee or "")
        await app_settings.set_value(db, "public_base_url", before_base or "")
        await db.commit()


# ------------------------------------------------------------- token --


def test_a_token_round_trips() -> None:
    tid = uuid.uuid4()
    tok = pay_link.make(tid, Decimal("450.00"))

    assert pay_link.parse(tok) == (tid, Decimal("450.00"))


def test_amount_cannot_be_edited_in_the_url() -> None:
    """Bina signature ke koi bhi '₹5 do' wala link bana leta."""
    tid = uuid.uuid4()
    tok = pay_link.make(tid, Decimal("450.00"))
    body, sig = tok.split(".")

    tampered = pay_link.make(tid, Decimal("5.00")).split(".")[0] + "." + sig

    assert pay_link.parse(tampered) is None


def test_a_stale_link_stops_working() -> None:
    tid = uuid.uuid4()
    tok = pay_link.make(tid, Decimal("100.00"), now=time.time() - pay_link.TTL_SECONDS - 60)

    assert pay_link.parse(tok) is None


def test_junk_is_none_not_an_exception() -> None:
    """Ye token public URL se aata hai — kachra aana normal hai."""
    for bad in ["", "x", "a.b", "....", "!!!.???", "a" * 500]:
        assert pay_link.parse(bad) is None


def test_the_token_carries_nothing_about_the_customer() -> None:
    """Link forward ho sakta hai. Usmein sirf dukaan aur amount ho."""
    import base64

    tid = uuid.uuid4()
    body = base64.urlsafe_b64decode(
        pay_link.make(tid, Decimal("450.00")).split(".")[0] + "=="
    ).decode()

    assert body.split(":")[0] == tid.hex
    assert "9999" not in body and "+91" not in body


def test_the_upi_uri_is_what_opens_gpay() -> None:
    uri = pay_link.upi_uri("shop@bank", "My Shop", Decimal("450"))

    assert uri.startswith("upi://pay?")
    assert "pa=shop%40bank" in uri
    assert "am=450.00" in uri and "cu=INR" in uri


# -------------------------------------------------------------- page --


async def test_the_page_hands_off_to_the_upi_app(client, shop_upi) -> None:
    tid = await tenant_context.get_home_tenant_id()
    tok = pay_link.make(tid, Decimal("450.00"))

    r = await client.get(f"/pay/{tok}")

    assert r.status_code == 200
    body = r.text
    # upi:// teen jagah: auto-redirect, button, aur inke bina page bekaar hai
    assert "upi://pay?" in body
    assert "pa=kwikklin%40testbank" in body
    assert "am=450.00" in body
    # VPA saamne bhi — link na chale to grahak khud bhej sake
    assert VPA in body


async def test_a_tampered_link_gets_a_flat_404(client, shop_upi) -> None:
    tid = await tenant_context.get_home_tenant_id()
    good = pay_link.make(tid, Decimal("450.00"))
    tampered = pay_link.make(tid, Decimal("1.00")).split(".")[0] + "." + good.split(".")[1]

    r = await client.get(f"/pay/{tampered}")

    assert r.status_code == 404
    # Kyun galat hai, ye batana sirf chhedne wale ke kaam aata hai
    assert "signature" not in r.text.lower() and "expired" not in r.text.lower()


async def test_no_vpa_means_404_not_an_empty_page(client) -> None:
    """VPA hataye jaane ke baad purane link zinda reh jaate hain."""
    async with async_session_factory() as db:
        before = await app_settings.get(db, "upi_vpa")
        await app_settings.set_value(db, "upi_vpa", "")
        await db.commit()
    tid = await tenant_context.get_home_tenant_id()
    try:
        r = await client.get(f"/pay/{pay_link.make(tid, Decimal('450.00'))}")
    finally:
        async with async_session_factory() as db:
            await app_settings.set_value(db, "upi_vpa", before or "")
            await db.commit()

    assert r.status_code == 404


async def test_the_landing_page_still_answers(client) -> None:
    """/ and /join are stacked decorators on ONE function.

    Adding /pay landed between them, so `/` bound to pay_page, which wants a
    token and answered 422 to every visitor. Nothing caught it: the suite
    never asked for the front page. Now it does.
    """
    for path in ("/", "/join"):
        r = await client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        assert "text/html" in r.headers["content-type"]


async def test_the_page_needs_no_login(client, shop_upi) -> None:
    """Grahak hamara user nahi hai — koi cookie, koi API key nahi."""
    tid = await tenant_context.get_home_tenant_id()
    client.cookies.clear()

    r = await client.get(f"/pay/{pay_link.make(tid, Decimal('450.00'))}")

    assert r.status_code == 200


# ---------------------------------------------------------- reminder --


async def test_the_reminder_carries_a_tappable_https_link(client, shop_upi) -> None:
    """Wahi baat jo is design ki jad hai: message mein https, upi:// nahi."""
    from app.config import settings

    phone = "+919999900011"
    async with async_session_factory() as db:
        await db.execute(sqltext("DELETE FROM customers WHERE phone = :p"), {"p": phone})
        await db.commit()
    r = await client.post(
        "/orders", headers={"X-API-Key": settings.ADMIN_API_KEY},
        json={"customer_phone": phone, "items": [{"type": "shirt", "qty": 2}]},
    )
    assert r.status_code == 201
    # Reminder sirf baaki paise par jaata hai. Pricing yahan test nahi ho
    # rahi — due seedha likh do, taaki ye test rate card ke bharose na ho.
    async with async_session_factory() as db:
        await db.execute(
            sqltext(
                "UPDATE orders SET total_amount = 450, amount_paid = 0 "
                "WHERE customer_id IN (SELECT id FROM customers WHERE phone = :p)"
            ), {"p": phone},
        )
        await db.commit()

    rem = await client.post(
        "/admin/api/customers/reminder",
        headers={"X-API-Key": settings.ADMIN_API_KEY}, json={"phone": phone},
    )

    assert rem.status_code == 200
    text = rem.json()["text"]
    assert "https://shop.example/pay/" in text
    assert "upi://" not in text, "WhatsApp isko link nahi banata"
    assert VPA in text, "link fail ho to VPA hi aakhri sahara hai"
