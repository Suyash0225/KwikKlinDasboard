"""Staff app ke naye auzaar: grahak ghar par nahi, damage/delivery photo,
shaam ka cash hisaab, kapde lete waqt ginti, atke hue order.

Fixtures test_staff_panel se — wahi do dukaanein, wahi log.
"""

import re
from pathlib import Path

from sqlalchemy import text as sqltext

from app.database import async_session_factory
from tests.test_staff_panel import (  # noqa: F401  (two_shops pytest fixture hai)
    A_DEL_PHONE, A_MGR_PHONE, A_PHONE, CUST_A, _login, _order_for, two_shops,
)

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


async def _set_status(tenant_id, number: str, status: str) -> None:
    async with async_session_factory() as db:
        await db.execute(
            sqltext("UPDATE orders SET status = :s WHERE order_number = :n AND tenant_id = :t"),
            {"s": status, "n": number, "t": str(tenant_id)},
        )
        await db.commit()


async def _notes(tenant_id, number: str) -> str:
    async with async_session_factory() as db:
        return (
            await db.execute(
                sqltext("SELECT notes FROM orders WHERE order_number = :n AND tenant_id = :t"),
                {"n": number, "t": str(tenant_id)},
            )
        ).scalar_one() or ""


def test_staff_app_defines_every_function_it_calls_for_messages() -> None:
    """composeMessage pehle sirf app.js mein tha — staff app mein button
    dabta tha aur kuch nahi hota tha."""
    src = Path("app/static/staff.js").read_text(encoding="utf-8")
    assert re.search(r"\basync function composeMessage\(", src)


async def test_not_home_keeps_the_order_and_tells_the_owner(client, two_shops, sent) -> None:
    number = await _order_for(two_shops["a"], two_shops["a_del"], CUST_A, delivery=True)
    await _set_status(two_shops["a"], number, "READY")
    await _login(client, A_DEL_PHONE)
    sent.clear()

    r = await client.post(f"/staff/api/orders/{number}/not-home", json={"note": "darwaza band"})
    assert r.status_code == 200, r.text
    assert r.json()["what"] == "delivery" and r.json()["can_message"] is True
    notes = await _notes(two_shops["a"], number)
    assert "Customer not home" in notes and "darwaza band" in notes

    from app.services import background

    await background.drain()
    assert any("not home" in (c.get("text") or "").lower() for c in sent)

    # status wahi — order list se gayab nahi hota
    detail = (await client.get(f"/staff/api/orders/{number}")).json()
    assert detail["status"] == "READY"

    # grahak ke liye "kab aayein?" message ka text
    m = await client.get(f"/staff/api/orders/{number}/message?kind=not_home")
    assert m.status_code == 200, m.text
    assert number in m.json()["text"]


async def test_not_home_is_only_for_the_road(client, two_shops, sent) -> None:
    number = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_A)
    # washerman ka kaam nahi
    await _login(client, A_PHONE)
    r = await client.post(f"/staff/api/orders/{number}/not-home", json={})
    assert r.status_code == 403
    # dhulai mein pada order "ghar par nahi" nahi ho sakta
    await _login(client, A_MGR_PHONE)
    r = await client.post(f"/staff/api/orders/{number}/not-home", json={})
    assert r.status_code == 409


async def test_damage_and_delivery_photos_are_labelled(client, two_shops, sent) -> None:
    number = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_A)
    await _login(client, A_PHONE)
    sent.clear()
    r = await client.post(
        f"/staff/api/orders/{number}/photo",
        files={"photo": ("k.png", PNG, "image/png")},
        data={"note": "button missing", "kind": "damage"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "damage"
    assert "⚠️ damage photo /admin/media/" in await _notes(two_shops["a"], number)

    from app.services import background

    await background.drain()
    assert any("marked damage" in (c.get("text") or "") for c in sent)

    bad = await client.post(
        f"/staff/api/orders/{number}/photo",
        files={"photo": ("k.png", PNG, "image/png")},
        data={"kind": "selfie"},
    )
    assert bad.status_code == 422
