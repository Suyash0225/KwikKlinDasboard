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


async def test_cash_handover_from_collection_to_manager_count(client, two_shops, sent) -> None:
    """Grahak se paisa -> delivery wale ka hisaab -> manager ki ginti."""
    n1 = await _order_for(two_shops["a"], two_shops["a_del"], CUST_A, delivery=True, total=300)
    n2 = await _order_for(two_shops["a"], two_shops["a_del"], CUST_A, delivery=True, total=200)
    await _login(client, A_DEL_PHONE)
    assert (await client.post(f"/staff/api/orders/{n1}/collect", json={"amount": 300, "method": "cash"})).status_code == 200
    assert (await client.post(f"/staff/api/orders/{n2}/collect", json={"amount": 200, "method": "upi"})).status_code == 200

    c = (await client.get("/staff/api/cash")).json()
    assert c["cash"] == 300 and c["upi"] == 200 and c["count"] == 2
    assert {p["order"] for p in c["payments"]} == {n1, n2}

    # jitna liya usse zyada nahi de sakta; kam de to wajah likhni padti hai
    assert (await client.post("/staff/api/cash/handover", json={"handed_cash": 400})).status_code == 400
    assert (await client.post("/staff/api/cash/handover", json={"handed_cash": 250})).status_code == 400
    sent.clear()
    r = await client.post("/staff/api/cash/handover", json={"handed_cash": 250, "note": "petrol 50"})
    assert r.status_code == 201, r.text
    hid = r.json()["id"]
    assert r.json()["status"] == "PENDING" and r.json()["cash_collected"] == 300

    from app.services import background

    await background.drain()
    assert any("handing over ₹250" in (m.get("text") or "") for m in sent)

    # hisaab de diya — ab haath khaali, dobara wahi paisa nahi ginta
    c2 = (await client.get("/staff/api/cash")).json()
    assert c2["count"] == 0 and c2["cash"] == 0
    assert c2["recent"][0]["id"] == hid
    assert (await client.post("/staff/api/cash/handover", json={"handed_cash": 0})).status_code == 400

    # delivery wala khud pakka nahi kar sakta
    assert (await client.get("/staff/api/cash/handovers")).status_code == 403

    await _login(client, A_MGR_PHONE)
    assert (await client.get("/staff/api/today")).json()["cash_to_confirm"] == 1
    lst = (await client.get("/staff/api/cash/handovers")).json()
    assert [h["id"] for h in lst["pending"]] == [hid]
    sent.clear()
    ok = await client.post(f"/staff/api/cash/handovers/{hid}/confirm", json={"received_cash": 240})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "CONFIRMED" and ok.json()["short"] == 10
    await background.drain()
    assert any("Cash mismatch" in (m.get("text") or "") for m in sent)
    again = await client.post(f"/staff/api/cash/handovers/{hid}/confirm", json={"received_cash": 250})
    assert again.status_code == 409
    assert (await client.get("/staff/api/today")).json()["cash_to_confirm"] == 0


async def test_washerman_has_no_cash_screen(client, two_shops) -> None:
    await _login(client, A_PHONE)
    assert (await client.get("/staff/api/cash")).status_code == 403
    assert (await client.post("/staff/api/cash/handover", json={"handed_cash": 1})).status_code == 403


async def test_counting_at_wash_catches_a_missing_piece(client, two_shops, sent) -> None:
    """Bill par 2 kurta, mile 1 — dhulai shuru hoti hai, par owner ko abhi pata chalta hai."""
    number = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_A)   # Kurta x2
    await _login(client, A_PHONE)
    lines = (await client.get(f"/staff/api/orders/{number}")).json()["lines"]
    sent.clear()
    r = await client.post(
        f"/staff/api/orders/{number}/washing",
        json={"counts": [{"line": lines[0]["line"], "got": 1}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "IN_WASH"
    chk = r.json()["check"]
    assert chk["ok"] is False and chk["expected"] == 2 and chk["got"] == 1
    assert "Count mismatch at wash: Kurta 1 of 2" in await _notes(two_shops["a"], number)

    from app.services import background

    await background.drain()
    assert any("does not match the bill" in (m.get("text") or "") for m in sent)


async def test_counting_at_wash_all_match_and_old_clients_still_work(client, two_shops, sent) -> None:
    n1 = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_A)
    n2 = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_A)
    await _login(client, A_PHONE)
    line = (await client.get(f"/staff/api/orders/{n1}")).json()["lines"][0]["line"]
    ok = await client.post(f"/staff/api/orders/{n1}/washing", json={"counts": [{"line": line, "got": 2}]})
    assert ok.status_code == 200 and ok.json()["check"]["ok"] is True
    assert "all 2 match the bill" in await _notes(two_shops["a"], n1)
    # bina ginti (purana app / bina body) — pehle jaisa
    old = await client.post(f"/staff/api/orders/{n2}/washing")
    assert old.status_code == 200 and "check" not in old.json()


async def test_today_counts_work_due_today(client, two_shops) -> None:
    from datetime import date

    number = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_A)
    async with async_session_factory() as db:
        await db.execute(
            sqltext("UPDATE orders SET expected_delivery = :d WHERE order_number = :n AND tenant_id = :t"),
            {"d": date.today(), "n": number, "t": str(two_shops["a"])},
        )
        await db.commit()
    await _login(client, A_PHONE)
    assert (await client.get("/staff/api/today")).json()["due_today"] >= 1


async def test_manager_sees_stuck_orders_and_each_persons_day(client, two_shops, sent) -> None:
    from datetime import datetime, timedelta, timezone

    stuck = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_A)
    fresh = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_A)
    old = datetime.now(timezone.utc) - timedelta(hours=60)
    async with async_session_factory() as db:
        await db.execute(
            sqltext(
                "UPDATE order_status_history SET changed_at = :at WHERE order_id ="
                " (SELECT id FROM orders WHERE order_number = :n AND tenant_id = :t)"
            ),
            {"at": old, "n": stuck, "t": str(two_shops["a"])},
        )
        await db.commit()

    # washerman ne aaj ek order dhulai mein daala
    await _login(client, A_PHONE)
    assert (await client.post(f"/staff/api/orders/{fresh}/washing")).status_code == 200
    assert (await client.get("/staff/api/stuck")).status_code == 403

    await _login(client, A_MGR_PHONE)
    st = (await client.get("/staff/api/stuck")).json()
    numbers = [o["number"] for o in st["orders"]]
    assert stuck in numbers and fresh not in numbers
    assert next(o for o in st["orders"] if o["number"] == stuck)["hours"] >= 59

    team = (await client.get("/staff/api/team")).json()
    awash = next(r for r in team if r["name"] == "Awash")
    assert awash["today"]["washed"] == 1


async def test_collect_survives_whatsapp_being_down(client, two_shops, sent, monkeypatch) -> None:
    """Paisa commit ho chuka ho aur grahak ki raseed na ja paaye — tab bhi
    200. Pehle send fail par session rollback se order expire hota, log
    line usse padhti aur 500 aata; delivery wala dobara dabata."""
    from app.services import order_service
    from app.services.whatsapp import SendError

    async def down(db, **kwargs):
        db.expire_all()        # asli send_message fail par yahi haalat chhodta hai
        raise SendError("WAHA network error", transient=True)

    number = await _order_for(two_shops["a"], two_shops["a_del"], CUST_A, delivery=True, total=120)
    monkeypatch.setattr(order_service, "send_message", down)
    await _login(client, A_DEL_PHONE)
    r = await client.post(f"/staff/api/orders/{number}/collect", json={"amount": 120, "method": "cash"})
    assert r.status_code == 200, r.text
    assert r.json()["due"] == 0


async def test_deliver_survives_whatsapp_being_down(client, two_shops, sent, monkeypatch) -> None:
    """Delivered commit ho gaya, grahak ka message nahi gaya — tab bhi 200,
    aur delivery ka kaam (task) bhi band hota hai."""
    from app.services import order_service
    from app.services.whatsapp import SendError

    async def down(db, **kwargs):
        db.expire_all()
        raise SendError("WAHA network error", transient=True)

    number = await _order_for(two_shops["a"], two_shops["a_del"], CUST_A, delivery=True)
    await _set_status(two_shops["a"], number, "READY")
    monkeypatch.setattr(order_service, "send_message", down)
    await _login(client, A_DEL_PHONE)
    r = await client.post(f"/staff/api/orders/{number}/deliver", json={"items": None})
    assert r.status_code == 200, r.text
    assert (await client.get(f"/staff/api/orders/{number}")).json()["status"] == "DELIVERED"
