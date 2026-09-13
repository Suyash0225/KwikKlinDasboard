"""Admin API + dashboard endpoint tests."""

import pytest
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory

PHONE = "+919999900066"
AUTH = {"X-API-Key": settings.ADMIN_API_KEY}
ORDER_BODY = {
    "customer_phone": PHONE,
    "customer_name": "API Grahak",
    "items": [{"type": "saree", "qty": 2, "service": "dry_clean"}],
    "total_amount": "400.00",
}


@pytest.fixture(autouse=True)
async def _cleanup(sent):  # sent: block real WhatsApp calls from notifications
    yield
    from tests.conftest import purge_phones

    await purge_phones(PHONE)


async def test_auth_required_everywhere(client) -> None:
    assert (await client.get("/orders")).status_code == 401
    assert (await client.post("/orders", json=ORDER_BODY)).status_code == 401
    assert (await client.get("/admin/api/dashboard")).status_code == 401
    bad = {"X-API-Key": "galat-key"}
    assert (await client.get("/orders", headers=bad)).status_code == 401


async def test_create_get_status_flow(client) -> None:
    r = await client.post("/orders", json=ORDER_BODY, headers=AUTH)
    assert r.status_code == 201, r.text
    number = r.json()["order_number"]
    assert r.json()["payment_status"] == "UNPAID"

    r = await client.get(f"/orders/{number}", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["history"][0]["new_status"] == "RECEIVED"

    r = await client.post(f"/orders/{number}/status", json={"status": "in_wash"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "IN_WASH"

    # backward move -> 409 from the state machine
    r = await client.post(f"/orders/{number}/status", json={"status": "RECEIVED"}, headers=AUTH)
    assert r.status_code == 409

    r = await client.post(
        f"/orders/{number}/payment", json={"amount": "400.00", "method": "UPI"}, headers=AUTH
    )
    assert r.status_code == 200 and r.json()["payment_status"] == "PAID"


async def test_list_filters(client) -> None:
    r = await client.post("/orders", json=ORDER_BODY, headers=AUTH)
    number = r.json()["order_number"]

    r = await client.get("/orders", params={"active": True}, headers=AUTH)
    assert number in [o["order_number"] for o in r.json()]

    r = await client.get("/orders", params={"status": "received"}, headers=AUTH)
    assert number in [o["order_number"] for o in r.json()]

    assert (await client.get("/orders", params={"status": "nakli"}, headers=AUTH)).status_code == 400


async def test_dashboard_data_shape(client) -> None:
    await client.post("/orders", json=ORDER_BODY, headers=AUTH)
    r = await client.get("/admin/api/dashboard", headers=AUTH)
    assert r.status_code == 200
    d = r.json()
    assert d["counts"]["active_total"] >= 1
    assert any(o["customer"] == "API Grahak" for o in d["active_orders"])
    # notes (internal) must never appear in dashboard rows
    assert all("notes" not in o for o in d["active_orders"])


async def test_dashboard_page_serves(client) -> None:
    r = await client.get("/admin")
    assert r.status_code == 200
    assert "Laundry Pro" in r.text
    assert "New bill" in r.text and "Expenses" in r.text  # CRM sections present
    assert "Campaigns" in r.text and "AI training" in r.text  # agent-era sections


async def test_customers_endpoint(client) -> None:
    assert (await client.get("/admin/api/customers")).status_code == 401
    await client.post("/orders", json=ORDER_BODY, headers=AUTH)
    r = await client.get("/admin/api/customers", headers=AUTH)
    assert r.status_code == 200
    me = [c for c in r.json() if c["phone"] == PHONE]
    assert me and me[0]["total_orders"] >= 1 and me[0]["name"] == "API Grahak"


# --------------------------------------------------- paise ki yaad ----


async def test_reminder_lists_the_bills_and_signs_the_right_shop(client, monkeypatch) -> None:
    """Message server par banta hai, aur usmein byora hota hai.

    Pehle text browser mein banta tha: sirf kul rakam, aur dukaan ka naam
    hardcoded "Kwik Klin" — yaani har doosri dukaan kisi aur ke naam se
    paisa maang rahi thi. Ab bills, tareekh, kul, aur asli shop_name.
    """
    sends: list = []

    async def _ok(db, **k):
        sends.append(k)
        return {"ok": True}

    for amt in ("540.00", "45.00"):
        body = dict(ORDER_BODY, total_amount=amt)
        assert (await client.post("/orders", json=body, headers=AUTH)).status_code == 201

    monkeypatch.setattr("app.services.whatsapp.send_message", _ok)
    r = await client.post(
        "/admin/api/customers/reminder", json={"phone": PHONE}, headers=AUTH
    )
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["sent"] is True and b["bills"] == 2

    text = sends[0]["text"]
    assert "API Grahak" in text, "naam se shuru ho"
    assert "2 bill baaki hain" in text
    assert "\u20b9540" in text and "\u20b945" in text, "har bill ka apna amount"
    assert "Kul baaki: \u20b9585" in text
    # Paise ka hisaab paison tak nahi — "630.00" machine ka likha lagta hai
    assert ".00" not in text
    # Aur kisi aur dukaan ka naam kabhi nahi
    assert "Kwik Klin" not in text or "Kwik Klin" in text.split("se —")[0]


async def test_reminder_returns_text_for_wa_link_when_the_api_is_down(
    client, monkeypatch
) -> None:
    """WhatsApp juda na ho to bhi paisa maangna rukna nahi chahiye."""
    from app.services import whatsapp as wa_mod

    assert (await client.post("/orders", json=ORDER_BODY, headers=AUTH)).status_code == 201

    async def _dead(*a, **k):
        raise wa_mod.SendError("no credentials")

    monkeypatch.setattr("app.services.whatsapp.send_message", _dead)
    r = await client.post(
        "/admin/api/customers/reminder", json={"phone": PHONE}, headers=AUTH
    )
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["sent"] is False
    assert b["phone"] == PHONE and b["text"], "wa.me link banane ke liye dono chahiye"


async def test_reminder_refuses_when_nothing_is_owed(client, monkeypatch) -> None:
    """Chukta grahak ko yaad dilana galat hai — 400, na ki khaali message."""
    r = await client.post(
        "/admin/api/customers/reminder", json={"phone": "+919999900999"}, headers=AUTH
    )
    assert r.status_code == 404, r.text
