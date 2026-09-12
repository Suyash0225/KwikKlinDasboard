"""Staff panel ke teen pehre: TENANT, ROLE, PLAN.

Ye teenon server par hone chahiye. UI mein chhupana kaafi nahi — koi bhi
curl se wahi call maar sakta hai. Isliye har test seedha API par hai.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text as sqltext

from app.database import async_session_factory
from app.models import Staff, StaffRole, Task
from app.models.tenant import Tenant
from app.services import staff_auth
from app.services.order_service import create_order
from tests.conftest import purge_phones

A_PHONE = "+919999900201"     # dukaan A ka washerman
A_MGR_PHONE = "+919999900202"  # dukaan A ka manager
B_PHONE = "+919999900203"     # dukaan B ka washerman — WAHI number chalta hai
CUST_A = "+919999900204"
CUST_B = "+919999900205"
PW = "khulja-sim-sim"


async def _tenant(slug: str, plan: str) -> uuid.UUID:
    async with async_session_factory() as db:
        row = (
            await db.execute(sqltext("SELECT id FROM tenants WHERE slug = :s"), {"s": slug})
        ).scalar_one_or_none()
        if row is not None:
            await db.execute(
                sqltext("UPDATE tenants SET plan = :p, status = 'active' WHERE id = :i"),
                {"p": plan, "i": str(row)},
            )
            await db.commit()
            return row
        tid = uuid.uuid4()
        await db.execute(
            sqltext(
                "INSERT INTO tenants (id, slug, shop_name, owner_name, owner_phone, plan, status)"
                " VALUES (:i, :s, :n, 'Owner', '+910000000000', :p, 'active')"
            ),
            {"i": str(tid), "s": slug, "n": slug.title(), "p": plan},
        )
        await db.commit()
        return tid


async def _purge_staff(*phones: str) -> None:
    """Pichhle adhoore run ka kachra — warna (tenant, phone) unique tootta."""
    async with async_session_factory() as db:
        for phone in phones:
            await db.execute(
                sqltext(
                    "DELETE FROM staff_sessions WHERE staff_id IN"
                    " (SELECT id FROM staff WHERE phone = :p)"
                ),
                {"p": phone},
            )
            await db.execute(
                sqltext(
                    "UPDATE tasks SET assigned_staff_id = NULL WHERE assigned_staff_id IN"
                    " (SELECT id FROM staff WHERE phone = :p)"
                ),
                {"p": phone},
            )
            # order bhi staff par ishara karte hain — pehle unhe chhodo,
            # warna delete FK par atak jata hai
            for col in ("assigned_washer_id", "assigned_delivery_id"):
                await db.execute(
                    sqltext(
                        f"UPDATE orders SET {col} = NULL WHERE {col} IN"
                        " (SELECT id FROM staff WHERE phone = :p)"
                    ),
                    {"p": phone},
                )
            await db.execute(sqltext("DELETE FROM staff WHERE phone = :p"), {"p": phone})
        await db.commit()


async def _staff(tenant_id, phone: str, name: str, role: StaffRole) -> uuid.UUID:
    async with async_session_factory() as db:
        sid = uuid.uuid4()
        await db.execute(
            sqltext(
                "INSERT INTO staff (id, tenant_id, phone, name, role, is_active,"
                " must_change_password) VALUES (:i, :t, :p, :n, :r, true, false)"
            ),
            {"i": str(sid), "t": str(tenant_id), "p": phone, "n": name, "r": role.name},
        )
        await db.commit()
        st = await db.get(Staff, sid)
        await staff_auth.set_password(db, st, PW, temp=False)
        return sid


@pytest.fixture
async def two_shops():
    """Do alag laundry, dono active — A Premium par, B Basic par."""
    await _purge_staff(A_PHONE, A_MGR_PHONE, B_PHONE)
    a = await _tenant("panel-a", "pro")
    b = await _tenant("panel-b", "starter")
    ids = {
        "a": a, "b": b,
        "a_wash": await _staff(a, A_PHONE, "Awash", StaffRole.WASHER),
        "a_mgr": await _staff(a, A_MGR_PHONE, "Amgr", StaffRole.MANAGER),
        "b_wash": await _staff(b, B_PHONE, "Bwash", StaffRole.WASHER),
    }
    yield ids
    async with async_session_factory() as db:
        for phone in (A_PHONE, A_MGR_PHONE, B_PHONE):
            await db.execute(
                sqltext(
                    "DELETE FROM staff_sessions WHERE staff_id IN"
                    " (SELECT id FROM staff WHERE phone = :p)"
                ),
                {"p": phone},
            )
            await db.execute(
                sqltext(
                    "UPDATE tasks SET assigned_staff_id = NULL WHERE assigned_staff_id IN"
                    " (SELECT id FROM staff WHERE phone = :p)"
                ),
                {"p": phone},
            )
            # order bhi staff par ishara karte hain — pehle unhe chhodo,
            # warna delete FK par atak jata hai
            for col in ("assigned_washer_id", "assigned_delivery_id"):
                await db.execute(
                    sqltext(
                        f"UPDATE orders SET {col} = NULL WHERE {col} IN"
                        " (SELECT id FROM staff WHERE phone = :p)"
                    ),
                    {"p": phone},
                )
            await db.execute(sqltext("DELETE FROM staff WHERE phone = :p"), {"p": phone})
        await db.commit()
    await purge_phones(CUST_A, CUST_B)
    async with async_session_factory() as db:
        await db.execute(sqltext("DELETE FROM tenants WHERE slug IN ('panel-a','panel-b')"))
        await db.commit()


async def _login(client, phone: str) -> None:
    r = await client.post("/staff/api/login", json={"phone": phone, "password": PW})
    assert r.status_code == 200, r.text


async def _task_for(tenant_id, staff_id, cust_phone: str, title: str) -> str:
    """Us dukaan ka ek order + uspar ek kaam."""
    from app.services import tenant_context

    token = tenant_context.current_tenant_id.set(tenant_id)
    try:
        async with async_session_factory() as db:
            order = await create_order(
                db, customer_phone=cust_phone, customer_name="Grahak",
                items=[{"type": "Kurta", "qty": 1}], created_by="test",
            )
            st = await db.get(Staff, staff_id)
            from app.services import tasks as task_service

            t = await task_service.create_task(
                db, title=title, staff=st, order=order, notify=False,
            )
            return t.code
    finally:
        tenant_context.current_tenant_id.reset(token)


# --- 1. TENANT -------------------------------------------------------------


async def test_one_shop_can_never_see_anothers_work(client, two_shops, sent) -> None:
    """Sabse zaroori test: A ka aadmi B ka kaam na dekhe, na chhue."""
    b_code = await _task_for(two_shops["b"], two_shops["b_wash"], CUST_B, "B ka kaam")

    await _login(client, A_MGR_PHONE)          # A ka MANAGER — sabse zyada taakat
    listed = (await client.get("/staff/api/tasks?tab=pending")).json()
    assert all(t["code"] != b_code for t in listed["tasks"]), "B ka kaam A ko dikh gaya"

    # seedha code se maangne par bhi nahi
    r = await client.post(f"/staff/api/tasks/{b_code}/done", json={"note": ""})
    assert r.status_code in (403, 404), r.text
    async with async_session_factory() as db:
        t = (
            await db.execute(
                sqltext("SELECT status FROM tasks WHERE code = :c AND tenant_id = :t"),
                {"c": b_code, "t": str(two_shops["b"])},
            )
        ).scalar_one()
        assert t == "OPEN", "doosri dukaan ka kaam band ho gaya"


async def test_same_phone_can_work_at_two_shops(client, two_shops) -> None:
    """Ek hi delivery boy do dukaan mein ho sakta hai — phone ab per-shop
    unique hai. Pehle poore DB mein ek hi baar chal sakta tha."""
    async with async_session_factory() as db:
        sid = uuid.uuid4()
        await db.execute(
            sqltext(
                "INSERT INTO staff (id, tenant_id, phone, name, role, is_active,"
                " must_change_password) VALUES (:i, :t, :p, 'Dono jagah', 'DELIVERY', true, false)"
            ),
            {"i": str(sid), "t": str(two_shops["a"]), "p": B_PHONE},
        )
        await db.commit()
        rows = (
            await db.execute(sqltext("SELECT count(*) FROM staff WHERE phone = :p"), {"p": B_PHONE})
        ).scalar_one()
    assert rows == 2, "ek number do dukaan mein hona chahiye"
    async with async_session_factory() as db:
        await db.execute(sqltext("DELETE FROM staff WHERE id = :i"), {"i": str(sid)})
        await db.commit()


# --- 2. ROLE ---------------------------------------------------------------


async def test_a_worker_sees_only_his_own_work(client, two_shops, sent) -> None:
    mine = await _task_for(two_shops["a"], two_shops["a_wash"], CUST_A, "Mera kaam")
    other = await _task_for(two_shops["a"], two_shops["a_mgr"], CUST_A, "Kisi aur ka")

    await _login(client, A_PHONE)              # washerman
    codes = [t["code"] for t in (await client.get("/staff/api/tasks?tab=mine")).json()["tasks"]]
    assert mine in codes and other not in codes

    # doosre ka kaam URL se bhi nahi
    r = await client.post(f"/staff/api/tasks/{other}/done", json={"note": ""})
    assert r.status_code == 403


async def test_manager_sees_the_whole_shop_but_only_his_shop(client, two_shops, sent) -> None:
    a1 = await _task_for(two_shops["a"], two_shops["a_wash"], CUST_A, "Washer ka")
    await _login(client, A_MGR_PHONE)
    body = (await client.get("/staff/api/tasks?tab=pending")).json()
    assert body["is_manager"] is True
    assert a1 in [t["code"] for t in body["tasks"]]


async def test_worker_cannot_reach_manager_only_endpoints(client, two_shops, sent) -> None:
    await _login(client, A_PHONE)
    assert (await client.get("/staff/api/team")).status_code == 403


# --- 3. PLAN ---------------------------------------------------------------


async def test_basic_plan_has_no_roles_no_cod_no_cancel_flow(client, two_shops, sent) -> None:
    """Basic mein sab ek jaise "staff" hain — na manager ki taakat, na COD,
    na cancel-approval. Data rehta hai, sirf suvidha band."""
    code = await _task_for(two_shops["b"], two_shops["b_wash"], CUST_B, "B ka kaam")
    await _login(client, B_PHONE)

    me = (await client.get("/staff/api/me")).json()
    assert me["plan"] == "Basic"
    assert "staff_panel" in me["features"]
    assert "cod_collection" not in me["features"] and "staff_roles" not in me["features"]
    assert me["is_manager"] is False

    r = await client.post(f"/staff/api/tasks/{code}/cancel-request", json={"reason": "customer ne mana kiya"})
    assert r.status_code == 402 and "Premium" in r.json()["detail"]

    r2 = await client.post("/staff/api/orders/KK-NOPE/collect", json={"amount": 10, "method": "cash"})
    assert r2.status_code == 402, "Basic mein COD nahi milna chahiye"


async def test_premium_gets_roles_cancel_flow_and_cod(client, two_shops, sent) -> None:
    code = await _task_for(two_shops["a"], two_shops["a_wash"], CUST_A, "Premium ka kaam")
    await _login(client, A_PHONE)
    me = (await client.get("/staff/api/me")).json()
    assert me["plan"] == "Premium"
    for f in ("staff_roles", "cancel_approval", "cod_collection", "order_timeline"):
        assert f in me["features"]

    r = await client.post(
        f"/staff/api/tasks/{code}/cancel-request", json={"reason": "customer ne mana kiya"}
    )
    assert r.status_code == 200, r.text
    async with async_session_factory() as db:
        row = (
            await db.execute(
                sqltext(
                    "SELECT cancel_reason, status FROM tasks WHERE code = :c AND tenant_id = :t"
                ),
                {"c": code, "t": str(two_shops["a"])},
            )
        ).first()
    assert row[0] == "customer ne mana kiya"
    assert row[1] == "OPEN", "cancel maangne se kaam band nahi hota — manager tay karega"


async def test_manager_approves_the_cancel(client, two_shops, sent) -> None:
    code = await _task_for(two_shops["a"], two_shops["a_wash"], CUST_A, "Cancel hone wala")
    await _login(client, A_PHONE)
    await client.post(f"/staff/api/tasks/{code}/cancel-request", json={"reason": "kapde nahi mile"})
    await client.post("/staff/api/logout")

    await _login(client, A_MGR_PHONE)
    r = await client.post(f"/staff/api/tasks/{code}/cancel-decide", json={"approve": True})
    assert r.status_code == 200 and r.json()["result"] == "approved"
    async with async_session_factory() as db:
        st = (
            await db.execute(
                sqltext("SELECT status FROM tasks WHERE code = :c AND tenant_id = :t"),
                {"c": code, "t": str(two_shops["a"])},
            )
        ).scalar_one()
    assert st == "CANCELLED"


async def test_cod_collection_updates_the_ledger(client, two_shops, sent) -> None:
    from decimal import Decimal

    from app.services import tenant_context

    token = tenant_context.current_tenant_id.set(two_shops["a"])
    try:
        async with async_session_factory() as db:
            order = await create_order(
                db, customer_phone=CUST_A, customer_name="COD Grahak",
                items=[{"type": "Kurta", "qty": 2}], total_amount=Decimal("300"),
                created_by="test",
            )
            number = order.order_number
            st = await db.get(Staff, two_shops["a_wash"])
            from app.services import tasks as task_service

            await task_service.create_task(
                db, title="deliver karo", staff=st, order=order, notify=False
            )
    finally:
        tenant_context.current_tenant_id.reset(token)

    await _login(client, A_PHONE)
    r = await client.post(
        f"/staff/api/orders/{number}/collect", json={"amount": 300, "method": "cash"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["due"] == 0
    async with async_session_factory() as db:
        paid = (
            await db.execute(
                sqltext(
                    "SELECT amount_paid FROM orders WHERE order_number = :n AND tenant_id = :t"
                ),
                {"n": number, "t": str(two_shops["a"])},
            )
        ).scalar_one()
    assert float(paid) == 300.0

    # zyada paisa nahi liya ja sakta
    r2 = await client.post(
        f"/staff/api/orders/{number}/collect", json={"amount": 50, "method": "cash"}
    )
    assert r2.status_code == 400


# --- auth ke apne niyam ----------------------------------------------------


async def test_no_login_no_data(client, two_shops) -> None:
    assert (await client.get("/staff/api/tasks")).status_code == 401
    assert (await client.get("/staff/api/me")).status_code == 401


async def test_wrong_password_says_nothing_useful(client, two_shops) -> None:
    r = await client.post("/staff/api/login", json={"phone": A_PHONE, "password": "galat"})
    assert r.status_code == 401
    detail = r.json()["detail"].lower()
    # Ek hi jawab dono galtiyon par: "number galat" aur "password galat"
    # alag-alag batana bhi ek leak hai — usse pata chal jata hai kaunsa
    # number is dukaan mein hai.
    assert detail == "wrong number or password"


async def test_password_is_never_stored_in_plain(client, two_shops) -> None:
    async with async_session_factory() as db:
        h = (
            await db.execute(
                sqltext("SELECT password_hash FROM staff WHERE phone = :p"), {"p": A_PHONE}
            )
        ).scalar_one()
    assert h and PW not in h and h.startswith("scrypt")


async def test_staff_can_change_only_his_own_password(client, two_shops) -> None:
    await _login(client, A_PHONE)
    bad = await client.post(
        "/staff/api/password", json={"old_password": "galat", "new_password": "naya-pass-123"}
    )
    assert bad.status_code == 403
    ok = await client.post(
        "/staff/api/password", json={"old_password": PW, "new_password": "naya-pass-123"}
    )
    assert ok.status_code == 200
    # naya password chalta hai, purana nahi
    await client.post("/staff/api/logout")
    assert (await client.post(
        "/staff/api/login", json={"phone": A_PHONE, "password": PW})).status_code == 401
    assert (await client.post(
        "/staff/api/login", json={"phone": A_PHONE, "password": "naya-pass-123"})).status_code == 200


# --- owner staff ko login deta hai (staff khud nahi bana sakta) -------------


async def test_owner_grants_panel_login_and_staff_can_use_it(client, two_shops) -> None:
    """Poora rasta: owner button dabata hai -> temp password -> staff login."""
    from app.config import settings

    H = {"X-API-Key": settings.ADMIN_API_KEY}
    async with async_session_factory() as db:
        sid = uuid.uuid4()
        await db.execute(
            sqltext(
                "INSERT INTO staff (id, tenant_id, phone, name, role, is_active,"
                " must_change_password) VALUES (:i, :t, :p, 'Naya Ladka', 'WASHER', true, true)"
            ),
            {"i": str(sid), "t": str(await _home_tenant_id()), "p": "+919999900209"},
        )
        await db.commit()
    try:
        r = await client.post(f"/admin/api/staff/{sid}/access", headers=H, json={"role": "DELIVERY"})
        assert r.status_code == 200, r.text
        pw = r.json()["temp_password"]
        assert r.json()["role"] == "DELIVERY"

        # password DB mein plain nahi jata
        async with async_session_factory() as db:
            h = (
                await db.execute(
                    sqltext("SELECT password_hash FROM staff WHERE id = :i"), {"i": str(sid)}
                )
            ).scalar_one()
        assert pw not in h

        # aur usi se panel khulta hai
        lg = await client.post(
            "/staff/api/login", json={"phone": "+919999900209", "password": pw}
        )
        assert lg.status_code == 200 and lg.json()["must_change_password"] is True

        # access hatate hi login band
        await client.post("/staff/api/logout")
        rv = await client.post(f"/admin/api/staff/{sid}/access/revoke", headers=H)
        assert rv.status_code == 200
        again = await client.post(
            "/staff/api/login", json={"phone": "+919999900209", "password": pw}
        )
        assert again.status_code == 401
    finally:
        async with async_session_factory() as db:
            await db.execute(
                sqltext("DELETE FROM staff_sessions WHERE staff_id = :i"), {"i": str(sid)}
            )
            await db.execute(sqltext("DELETE FROM staff WHERE id = :i"), {"i": str(sid)})
            await db.commit()


async def _home_tenant_id():
    from app.services import tenant_context

    return await tenant_context.get_home_tenant_id()


# --- creds WhatsApp par bhejna ---------------------------------------------


async def _make_staff(phone: str, *, fresh_window: bool = True):
    """Ek staff row banao. fresh_window: 24h ka darwaza khula rakho."""
    sid = uuid.uuid4()
    async with async_session_factory() as db:
        await db.execute(
            sqltext(
                "INSERT INTO staff (id, tenant_id, phone, name, role, is_active,"
                " must_change_password, last_message_at) VALUES"
                " (:i, :t, :p, 'Share Test', 'WASHER', true, true, :lm)"
            ),
            {
                "i": str(sid),
                "t": str(await _home_tenant_id()),
                "p": phone,
                "lm": datetime.now(timezone.utc) if fresh_window else None,
            },
        )
        await db.commit()
    return sid


async def _drop_staff(sid) -> None:
    async with async_session_factory() as db:
        await db.execute(
            sqltext("DELETE FROM conversations WHERE staff_id = :i"), {"i": str(sid)}
        )
        await db.execute(
            sqltext("DELETE FROM staff_sessions WHERE staff_id = :i"), {"i": str(sid)}
        )
        await db.execute(sqltext("DELETE FROM staff WHERE id = :i"), {"i": str(sid)})
        await db.commit()


async def test_share_sends_the_login_and_never_logs_the_password(client, two_shops) -> None:
    """Owner "Send on WhatsApp" dabaye -> creds staff ke phone par.

    Aur sabse zaroori: password HAMARE apne message log mein nahi likha
    jata. WhatsApp use le jaye, theek — par Inbox, backup aur export mein
    wo plain text kabhi nahi baithna chahiye.
    """
    from app.config import settings

    H = {"X-API-Key": settings.ADMIN_API_KEY}
    phone = "+919999900211"
    sid = await _make_staff(phone)
    try:
        pw = (await client.post(
            f"/admin/api/staff/{sid}/access", headers=H, json={"role": "WASHER"}
        )).json()["temp_password"]

        r = await client.post(
            f"/admin/api/staff/{sid}/access/share", headers=H, json={"password": pw}
        )
        assert r.status_code == 200, r.text
        assert r.json()["to"] == phone

        async with async_session_factory() as db:
            rows = (
                await db.execute(
                    sqltext(
                        "SELECT message_text FROM conversations WHERE staff_id = :i"
                        " AND direction = 'OUTBOUND'"
                    ),
                    {"i": str(sid)},
                )
            ).scalars().all()
        assert rows, "message log mein kuch to jana chahiye"
        assert all(pw not in (t or "") for t in rows), "password log mein nahi jana chahiye"
        assert any("password hidden" in (t or "") for t in rows)
    finally:
        await _drop_staff(sid)


async def test_share_refuses_a_password_that_is_not_the_current_one(client, two_shops) -> None:
    """Endpoint "staff ko kuch bhi bhej do" na ban jaye — password hash se
    milta hai, warna 409. Purani modal khuli reh gayi ho to bhi yahi."""
    from app.config import settings

    H = {"X-API-Key": settings.ADMIN_API_KEY}
    sid = await _make_staff("+919999900212")
    try:
        await client.post(f"/admin/api/staff/{sid}/access", headers=H, json={"role": "WASHER"})
        r = await client.post(
            f"/admin/api/staff/{sid}/access/share", headers=H, json={"password": "kuch-bhi-123"}
        )
        assert r.status_code == 409
    finally:
        await _drop_staff(sid)


async def test_share_refuses_once_the_staff_has_set_their_own_password(client, two_shops) -> None:
    """Temp password hi share hota hai. Staff ne apna rakh liya, to hum wo
    kabhi nahi bhejte — wo unka hai, owner ka nahi."""
    from app.config import settings

    H = {"X-API-Key": settings.ADMIN_API_KEY}
    phone = "+919999900213"
    sid = await _make_staff(phone)
    try:
        pw = (await client.post(
            f"/admin/api/staff/{sid}/access", headers=H, json={"role": "WASHER"}
        )).json()["temp_password"]
        await client.post("/staff/api/login", json={"phone": phone, "password": pw})
        await client.post(
            "/staff/api/password", json={"old_password": pw, "new_password": "mera-apna-999"}
        )
        await client.post("/staff/api/logout")

        r = await client.post(
            f"/admin/api/staff/{sid}/access/share", headers=H, json={"password": "mera-apna-999"}
        )
        assert r.status_code == 409
    finally:
        await _drop_staff(sid)


# --- phone se bill (Ravi bhi, Ajit bhi) ------------------------------------


async def test_staff_can_bill_from_the_panel_at_rate_card_prices(client, two_shops, sent) -> None:
    """Counter par khada aadmi apne phone se bill banaye — daam rate card se.

    Bhejne wala rate bhej hi nahi sakta (field hi nahi hai), isliye WhatsApp
    wala bill aur panel wala bill hamesha ek hi hisaab dete hain.
    """
    from decimal import Decimal

    from app.models import Rate
    from app.services import tenant_context

    token = tenant_context.current_tenant_id.set(two_shops["a"])
    try:
        async with async_session_factory() as db:
            db.add(Rate(service="PanelSvc", garment="Kurta", unit="pc", rate=Decimal("40")))
            await db.commit()
    finally:
        tenant_context.current_tenant_id.reset(token)

    await _login(client, A_PHONE)
    try:
        rates = (await client.get("/staff/api/rates")).json()
        assert any(r["service"] == "PanelSvc" for r in rates)

        r = await client.post("/staff/api/bills", json={
            "customer_phone": CUST_A, "customer_name": "Panel Grahak",
            "items": [{"service": "PanelSvc", "garment": "Kurta", "qty": 3}],
            "advance": 20,
        })
        assert r.status_code == 201, r.text
        assert r.json()["total"] == 120.0 and r.json()["due"] == 100.0

        # rate card mein na ho to bill banta hi nahi — andaze se daam nahi
        bad = await client.post("/staff/api/bills", json={
            "customer_phone": CUST_A,
            "items": [{"service": "PanelSvc", "garment": "Spacesuit", "qty": 1}],
        })
        assert bad.status_code == 400 and "rate card" in bad.json()["detail"]
    finally:
        async with async_session_factory() as db:
            await db.execute(sqltext("DELETE FROM rate_card WHERE service = 'PanelSvc'"))
            await db.commit()


async def test_the_panel_installs_as_an_app(client) -> None:
    """Manifest + service worker + icon — bina inke 'Add to home screen'
    nahi aata aur staff har baar browser kholta rehta."""
    m = await client.get("/staff/manifest.webmanifest")
    assert m.status_code == 200 and m.json()["start_url"] == "/staff"
    assert m.json()["display"] == "standalone"
    sw = await client.get("/staff/sw.js")
    assert sw.status_code == 200
    # kaam ka data cache nahi hota — purana order dikhana khatarnak hai
    assert "/staff/api" in sw.text and "return" in sw.text
    assert (await client.get("/staff/icon.svg")).status_code == 200


async def test_login_screen_hides_once_you_are_in(client) -> None:
    """Tablet par login ka card logged-in app ke upar chipka reh gaya tha.

    Wajah CSS thi: `.login{display:grid}` browser ki apni
    `[hidden]{display:none}` ko haraa deti hai. Ye test us ek line ko
    pakad kar rakhta hai — bina iske do screen ek saath dikhti hain.
    """
    css = (await client.get("/admin/static/staff.css")).text
    assert "[hidden] { display: none !important; }" in css, \
        "[hidden] ka override hata to login screen wapas chipak jayegi"


async def test_giving_the_owner_a_login_never_demotes_them(client, two_shops) -> None:
    """Owner ko panel login dena unse maalik ka darja nahi chheenta.

    Asli ghatna: owner ne apne hi row par "Panel login" dabaya. Us modal ke
    dropdown mein ADMIN hai hi nahi (owner koi role nahi, wo maalik hai),
    isliye jo bhi chuna gaya wahi lag gaya — aur owner chup-chaap MANAGER
    ban gaye. Uske saath hi team.is_admin_phone() ne unhe pehchanna band
    kar diya: order aur payment ki khabar unke apne WhatsApp par aani band.

    Login dena aur darja badalna do alag baatein hain. Ye endpoint sirf
    pehla kaam karta hai.
    """
    from app.config import settings

    H = {"X-API-Key": settings.ADMIN_API_KEY}
    sid = uuid.uuid4()
    async with async_session_factory() as db:
        await db.execute(
            sqltext(
                "INSERT INTO staff (id, tenant_id, phone, name, role, is_active)"
                " VALUES (:i, :t, :p, 'Malik', 'ADMIN', true)"
            ),
            {"i": str(sid), "t": str(await _home_tenant_id()), "p": "+919999900214"},
        )
        await db.commit()
    try:
        r = await client.post(
            f"/admin/api/staff/{sid}/access", headers=H, json={"role": "MANAGER"}
        )
        assert r.status_code == 200, r.text
        assert r.json()["role"] == "ADMIN", "owner ka darja waisa ka waisa"

        async with async_session_factory() as db:
            role = (
                await db.execute(
                    sqltext("SELECT role FROM staff WHERE id = :i"), {"i": str(sid)}
                )
            ).scalar_one()
        assert role == "ADMIN"
    finally:
        await _drop_staff(sid)


# --- naam se purana customer dhoondhna --------------------------------------


async def test_customer_search_finds_by_name_and_never_leaks_the_number(
    client, two_shops, sent
) -> None:
    """Naam ka tukda likho -> purana customer mile, par number MASKED.

    Panel ka poora usool yahi hai: ek staff ke phone se poori customer list
    nikal jaana sabse aasan leak hai. Isliye sujhaav mein sirf naam aur
    dhaka hua number jaata hai — aur chunne ke liye ek `ref`.
    """
    await _task_for(two_shops["a"], two_shops["a_wash"], CUST_A, "Kaam")
    await _login(client, A_PHONE)

    r = await client.get("/staff/api/customers/search?q=Grah")
    assert r.status_code == 200, r.text
    hits = r.json()
    assert hits, "naam se customer milna chahiye"
    one = hits[0]
    assert one["name"] == "Grahak"
    assert one["ref"], "chunne ke liye ref chahiye"
    assert CUST_A not in str(one), "poora number kabhi nahi jaana chahiye"
    assert one["phone_masked"] != CUST_A

    # ek akshar par kuch nahi — poori dukaan lautana bekaar hai
    assert (await client.get("/staff/api/customers/search?q=G")).json() == []


async def test_search_cannot_reach_into_another_shop(client, two_shops, sent) -> None:
    """A ka staff B ke customer ko na dhoondh paye, na uspar bill bana paye."""
    await _task_for(two_shops["b"], two_shops["b_wash"], CUST_B, "B ka kaam")
    b_ref = None
    async with async_session_factory() as db:
        b_ref = (
            await db.execute(
                sqltext("SELECT id FROM customers WHERE phone = :p AND tenant_id = :t"),
                {"p": CUST_B, "t": str(two_shops["b"])},
            )
        ).scalar_one()

    await _login(client, A_PHONE)              # dukaan A ka aadmi
    assert (await client.get("/staff/api/customers/search?q=Grah")).json() == [] or all(
        h["ref"] != str(b_ref)
        for h in (await client.get("/staff/api/customers/search?q=Grah")).json()
    )
    # ref haath lag bhi jaye to bill nahi banta — lookup tenant-scoped hai
    bad = await client.post(
        "/staff/api/bills",
        json={
            "customer_ref": str(b_ref),
            "items": [{"service": "Wash", "garment": "Kurta", "qty": 1}],
        },
    )
    assert bad.status_code == 404, bad.text


async def test_bill_from_a_picked_customer_needs_no_typed_number(
    client, two_shops, sent
) -> None:
    """Suggestion se chuna -> number DB se aata hai, staff likhta hi nahi.

    Yahi wo galti khatam karta hai jahan wahi grahak ek digit badal jaane
    se dobara ban jaata tha aur uska purana hisaab kahin aur pada rehta tha.
    """
    from decimal import Decimal

    from app.models import Rate
    from app.services import tenant_context

    await _task_for(two_shops["a"], two_shops["a_wash"], CUST_A, "Kaam")
    token = tenant_context.current_tenant_id.set(two_shops["a"])
    try:
        async with async_session_factory() as db:
            db.add(Rate(service="PickSvc", garment="Kurta", unit="pc", rate=Decimal("40")))
            await db.commit()
    finally:
        tenant_context.current_tenant_id.reset(token)

    try:
        await _login(client, A_PHONE)
        ref = (await client.get("/staff/api/customers/search?q=Grah")).json()[0]["ref"]

        r = await client.post(
            "/staff/api/bills",
            json={
                "customer_ref": ref,
                "customer_phone": "",          # kuch likha hi nahi gaya
                "items": [{"service": "PickSvc", "garment": "Kurta", "qty": 2}],
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["total"] == 80

        # aur wo bill USI purane customer par baitha, naya customer nahi bana
        async with async_session_factory() as db:
            n = (
                await db.execute(
                    sqltext("SELECT count(*) FROM customers WHERE phone = :p"), {"p": CUST_A}
                )
            ).scalar_one()
        assert n == 1, "naya duplicate customer nahi banna chahiye"
    finally:
        # rate_card tenant par ishara karta hai — chhoda to teardown FK par atkega
        async with async_session_factory() as db:
            await db.execute(sqltext("DELETE FROM rate_card WHERE service = 'PickSvc'"))
            await db.commit()


async def test_a_bill_still_needs_a_number_when_nobody_was_picked(
    client, two_shops, sent
) -> None:
    """Naya customer ho to number lazmi — chup-chaap bina number bill nahi."""
    await _task_for(two_shops["a"], two_shops["a_wash"], CUST_A, "Kaam")
    await _login(client, A_PHONE)
    r = await client.post(
        "/staff/api/bills",
        json={"items": [{"service": "Wash", "garment": "Kurta", "qty": 1}]},
    )
    assert r.status_code == 400
    assert "number" in r.json()["detail"].lower()


async def test_action_buttons_wrap_instead_of_cutting_their_text(client) -> None:
    """Phone par button ka text pill ke bahar nikal aata tha.

    Wajah: `flex: 1` yani flex-basis 0, aur `.btn` ka min-width sirf 44px —
    to char button (Close / Ask again / Cancel task / Mark done) 360px ke
    phone par 44px tak sikud jate the, jabki text `nowrap` tha. Ilaaj:
    `flex-basis: auto` + `min-width: auto`, taaki button apne text se chhota
    ho hi na sake aur jagah kam padne par agli line par chala jaye.

    Ye test un jagahon ko pakadta hai jahan sabse zyada button ek saath
    aate hain — task ka detail sheet, task card, aur staff panel.
    """
    import re as _re

    admin_css = (await client.get("/admin/static/app.css")).text
    staff_css = (await client.get("/admin/static/staff.css")).text

    for css, sel in (
        (admin_css, ".modal .btnrow .btn"),
        (admin_css, ".taskcard .tc-acts .btn"),
        (staff_css, ".btnrow .btn"),
        (staff_css, ".acts .btn"),
    ):
        i = css.find(sel)
        assert i != -1, f"{sel} gayab ho gaya"
        block = css[i : css.find("}", i)]
        # Shart INTENT hai, ek khaas value nahi: flex-basis `auto` ho (0
        # nahi) aur min-width `auto`. `1 1 auto` aur `0 1 auto` dono theek
        # hain — dono mein button apne text se chhota nahi ho sakta; farak
        # sirf itna ki wo bachi jagah bharta hai ya nahi, jo design ka
        # faisla hai, safety ka nahi. Pehle test sirf "1 1 auto" maanta
        # tha, to layout badalte hi jhootha laal ho jaata tha.
        flex = _re.search(r"flex:\s*(\d+)\s+(\d+)\s+(\w+)", block)
        assert flex is not None, f"{sel}: flex shorthand hi nahi mila"
        assert flex.group(3) == "auto", \
            f"{sel}: flex-basis {flex.group(3)} hai — 0 hote hi text button ke bahar niklega"
        assert "min-width: auto" in block or "min-width:auto" in block, \
            f"{sel}: min-width auto hataya to button apne text se chhota ho jayega"

    # jahan button toot sakein, wahan wrap zaroori hai.
    # Selector regex se dhoonda jaata hai — pehle exact ".btnrow{" khoja
    # jaata tha, to CSS mein ek space daalte hi test jhootha pass ho jaata.
    for css, sel in ((admin_css, r"\.modal \.btnrow"), (staff_css, r"\.btnrow")):
        m = _re.search(sel + r"\s*\{([^}]*)\}", css)
        assert m is not None, f"{sel} gayab ho gaya"
        assert "wrap" in m.group(1), \
            f"{sel} par flex-wrap chahiye, warna line toot nahi paegi"


async def test_every_password_box_has_an_eye(client) -> None:
    """Password type karte waqt dikhna zaroori hai — warna staff phone par
    galat password bhar kar baar-baar atakta hai."""
    html = (await client.get("/staff")).text
    js = (await client.get("/admin/static/staff.js")).text
    assert 'id="lg-eye"' in html, "login par aankh"
    assert 'data-eye="p-old"' in js and 'data-eye="p-new"' in js, \
        "password badalne wale dono box par aankh"


# --- rozmarra ke chaar kaam ------------------------------------------------


async def _order_for(tenant_id, staff_id, cust_phone, *, delivery=False, total=0):
    """Us dukaan ka ek order, is aadmi ke naam."""
    from decimal import Decimal

    from app.models import Order
    from app.services import tenant_context

    token = tenant_context.current_tenant_id.set(tenant_id)
    try:
        async with async_session_factory() as db:
            order = await create_order(
                db, customer_phone=cust_phone, customer_name="Route Grahak",
                items=[{"type": "Kurta", "qty": 2}],
                total_amount=Decimal(str(total)) if total else None,
                created_by="test",
            )
            row = (
                await db.execute(
                    sqltext(
                        "UPDATE orders SET assigned_delivery_id = :d, assigned_washer_id = :w"
                        " WHERE order_number = :n"
                    ),
                    {
                        "d": str(staff_id) if delivery else None,
                        "w": None if delivery else str(staff_id),
                        "n": order.order_number,
                    },
                )
            )
            await db.commit()
            return order.order_number
    finally:
        tenant_context.current_tenant_id.reset(token)


async def test_call_gives_the_real_number_only_for_your_own_order(
    client, two_shops, sent
) -> None:
    """List mein number masked rehta hai — poora tabhi jab kaam ke liye
    maanga jaye, aur har baar audit hota hai. Warna kisi bhi phone se
    poori customer list nikaal lena sabse aasan leak hai."""
    mine = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_A)
    theirs = await _order_for(two_shops["b"], two_shops["b_wash"], CUST_B)

    await _login(client, A_PHONE)
    r = await client.get(f"/staff/api/orders/{mine}/call")
    assert r.status_code == 200 and r.json()["phone"] == CUST_A

    # Order number ab per-dukaan hai, to dono shops mein "KK-...-01" ho
    # sakta hai. Isolation ka asli sabooot yahi hai: wahi number maangne
    # par bhi APNI hi dukaan ka order milta hai, doosri ka grahak kabhi
    # nahi.
    same = await client.get(f"/staff/api/orders/{theirs}/call")
    assert same.status_code in (403, 404) or same.json()["phone"] != CUST_B

    async with async_session_factory() as db:
        n = (
            await db.execute(
                sqltext(
                    "SELECT count(*) FROM audit_log WHERE action = 'customer_number_viewed'"
                    " AND at > now() - interval '2 minutes'"
                )
            )
        ).scalar_one()
    assert n >= 1, "number dekhna hamesha audit hona chahiye"


async def test_photo_attaches_to_the_order_and_tells_the_owner(
    client, two_shops, sent
) -> None:
    """Kapde ki halat ka saboot — baad ki 'aisa to nahi tha' wali baat ka jawab."""
    number = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_A)
    await _login(client, A_PHONE)
    sent.clear()

    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
        "00000049454e44ae426082"
    )
    r = await client.post(
        f"/staff/api/orders/{number}/photo",
        files={"photo": ("kapda.png", png, "image/png")},
        data={"note": "collar par daag tha"},
    )
    assert r.status_code == 201, r.text
    async with async_session_factory() as db:
        notes = (
            await db.execute(
                sqltext("SELECT notes FROM orders WHERE order_number = :n AND tenant_id = :t"),
                {"n": number, "t": str(two_shops["a"])},
            )
        ).scalar_one()
    assert "/admin/media/job-" in notes and "collar par daag" in notes

    # Owner ki khabar ab REQUEST KE BAAD jaati hai. Pehle wo inline thi, yani
    # staff ka phone WhatsApp ke jawab ka intezaar karta tha — wahi "app atak
    # gayi" wali dikkat thi. Photo mehfooz ho chuki hai (upar assert hua),
    # khabar ek pal baad. Test us pal ka intezaar karta hai.
    from app.services import background

    await background.drain()
    assert any("photo" in (c.get("text") or "").lower() for c in sent), "owner ko khabar jaye"

    # sirf photo — koi bhi file nahi
    bad = await client.post(
        f"/staff/api/orders/{number}/photo",
        files={"photo": ("virus.exe", b"MZ", "application/octet-stream")},
    )
    assert bad.status_code == 400


async def test_today_shows_his_own_day(client, two_shops, sent) -> None:
    """Din ki pehli nazar — kitna bacha, kitna nipta."""
    code = await _task_for(two_shops["a"], two_shops["a_wash"], CUST_A, "Aaj ka kaam")
    await _login(client, A_PHONE)

    t = (await client.get("/staff/api/today")).json()
    assert t["pending"] >= 1 and t["done_today"] == 0

    await client.post(f"/staff/api/tasks/{code}/done", json={"note": ""})
    t2 = (await client.get("/staff/api/today")).json()
    assert t2["done_today"] == 1 and t2["pending"] == t["pending"] - 1


async def test_route_is_role_aware_and_shop_scoped(client, two_shops, sent) -> None:
    """Delivery wale ko jahan jaana hai wahi, washerman ko uski kataar —
    aur doosri dukaan ka koi stop kabhi nahi."""
    from app.models import Staff, StaffRole

    async with async_session_factory() as db:
        sid = uuid.uuid4()
        await db.execute(
            sqltext(
                "INSERT INTO staff (id, tenant_id, phone, name, role, is_active,"
                " must_change_password, password_hash) SELECT :i, :t, :p, 'Aroute',"
                " 'DELIVERY', true, false, password_hash FROM staff WHERE id = :src"
            ),
            {"i": str(sid), "t": str(two_shops["a"]), "p": "+919999900207",
             "src": str(two_shops["a_wash"])},
        )
        await db.commit()
    try:
        wash_no = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_A)
        await _order_for(two_shops["b"], two_shops["b_wash"], CUST_B)

        await _login(client, A_PHONE)          # washerman
        r = (await client.get("/staff/api/route")).json()
        assert r["kind"] == "wash"
        assert wash_no in [s["number"] for s in r["stops"]]
        await client.post("/staff/api/logout")

        await _login(client, "+919999900207")  # delivery
        r2 = (await client.get("/staff/api/route")).json()
        assert r2["kind"] == "delivery"
        # dhulai wala order delivery ki list mein nahi aata
        assert wash_no not in [s["number"] for s in r2["stops"]]
    finally:
        async with async_session_factory() as db:
            await db.execute(
                sqltext("DELETE FROM staff_sessions WHERE staff_id = :i"), {"i": str(sid)}
            )
            await db.execute(
                sqltext(
                    "UPDATE orders SET assigned_delivery_id = NULL WHERE assigned_delivery_id = :i"
                ),
                {"i": str(sid)},
            )
            await db.execute(sqltext("DELETE FROM staff WHERE id = :i"), {"i": str(sid)})
            await db.commit()


async def test_panel_bill_is_a_whole_bill_not_half_of_one(client, two_shops, sent) -> None:
    """Panel ka bill dashboard ke bill jitna POORA ho.

    Pehle panel se bana bill "aadha" tha: na delivery date (customer se
    "kab milega" ka koi jawab nahi), na washerman ko work order (kaam ki
    khabar kisi ko nahi — order chupchaap pada rehta). Ab dono wahi hain
    jo dashboard ke New Bill par hain.
    """
    from decimal import Decimal

    from app.models import Rate
    from app.services import app_settings, tenant_context

    token = tenant_context.current_tenant_id.set(two_shops["a"])
    try:
        async with async_session_factory() as db:
            await db.execute(sqltext("DELETE FROM rate_card WHERE service = 'WholeSvc'"))
            await db.commit()
            db.add(Rate(service="WholeSvc", garment="Shirt", unit="pc", rate=Decimal("25")))
            await db.commit()
            # dukaan ka default washerman — work order isi ko jayega
            await app_settings.set_value(db, "default_washer_phone", A_PHONE)
    finally:
        tenant_context.current_tenant_id.reset(token)

    await _login(client, A_MGR_PHONE)
    try:
        r = await client.post("/staff/api/bills", json={
            "customer_phone": CUST_A, "customer_name": "Poora Grahak",
            "items": [{"service": "WholeSvc", "garment": "Shirt", "qty": 2}],
        })
        assert r.status_code == 201, r.text
        number = r.json()["order_number"]

        # 1. Delivery date turnaround se bhari hui hai
        async with async_session_factory() as db:
            row = (
                await db.execute(
                    sqltext(
                        "SELECT expected_delivery FROM orders"
                        " WHERE order_number = :n AND tenant_id = :t"
                    ),
                    {"n": number, "t": str(two_shops["a"])},
                )
            ).scalar_one()
        assert row is not None, "panel ke bill par delivery date khaali thi"

        # 2. Washerman ko WhatsApp par work order gaya
        work_orders = [m for m in sent if m["to"] == A_PHONE and number in (m.get("text") or "")]
        assert work_orders, f"washerman ko work order nahi gaya: {sent}"
    finally:
        async with async_session_factory() as db:
            await db.execute(sqltext("DELETE FROM rate_card WHERE service = 'WholeSvc'"))
            await db.execute(
                sqltext(
                    "DELETE FROM settings_kv WHERE key = 'default_washer_phone'"
                    " AND tenant_id = :t"
                ),
                {"t": str(two_shops["a"])},
            )
            await db.commit()


async def test_receipt_gives_bill_text_only_for_your_own_order(
    client, two_shops, sent
) -> None:
    """Delivery wala apne phone se bill WhatsApp kar sake — text + poora
    number, par sirf apne order ka, aur /call ki tarah audit ke saath."""
    mine = await _order_for(two_shops["a"], two_shops["a_wash"], CUST_A)
    theirs = await _order_for(two_shops["b"], two_shops["b_wash"], CUST_B)

    await _login(client, A_PHONE)
    r = await client.get(f"/staff/api/orders/{mine}/receipt")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["phone"] == CUST_A
    assert f"Bill: {mine}" in body["text"]
    assert "Total:" in body["text"] and "Due:" in body["text"]

    same = await client.get(f"/staff/api/orders/{theirs}/receipt")
    assert same.status_code in (403, 404) or same.json()["phone"] != CUST_B

    async with async_session_factory() as db:
        n = (
            await db.execute(
                sqltext(
                    "SELECT count(*) FROM audit_log WHERE action = 'customer_number_viewed'"
                    " AND result = 'share_bill' AND at > now() - interval '2 minutes'"
                )
            )
        ).scalar_one()
    assert n >= 1, "bill share bhi number dikhata hai — audit zaroori"


async def test_a_bill_i_made_shows_up_and_i_can_share_it(client, two_shops, sent) -> None:
    """Bill banane ke baad wo kahin dikhta hi nahi tha (task washer ko jaata
    hai, Route sirf pickup/delivery). Ab: apne bill /bills mein, aur unpar
    receipt/share chalta hai bina assign hue. Doosre ka bill nahi dikhta."""
    from decimal import Decimal

    from app.models import Rate
    from app.services import tenant_context

    token = tenant_context.current_tenant_id.set(two_shops["a"])
    try:
        async with async_session_factory() as db:
            db.add(Rate(service="MySvc", garment="Saree", unit="pc", rate=Decimal("50")))
            await db.commit()
    finally:
        tenant_context.current_tenant_id.reset(token)

    # kisi aur (manager) ka bill — washer ko nahi dikhna chahiye
    someone_elses = await _order_for(two_shops["a"], two_shops["a_mgr"], CUST_A)

    await _login(client, A_PHONE)
    try:
        r = await client.post("/staff/api/bills", json={
            "customer_phone": CUST_A, "customer_name": "Saree Wali",
            "items": [{"service": "MySvc", "garment": "Saree", "qty": 2}],
        })
        assert r.status_code == 201, r.text
        mine = r.json()["order_number"]

        bills = (await client.get("/staff/api/bills")).json()
        numbers = [b["number"] for b in bills]
        assert mine in numbers, "apna banaya bill dikhna chahiye"
        assert someone_elses not in numbers, "doosre ka bill washer ko nahi"
        b = next(x for x in bills if x["number"] == mine)
        assert b["total"] == 100.0 and b["due"] == 100.0

        # assign nahi hua, phir bhi mera hai — receipt milna chahiye
        rc = await client.get(f"/staff/api/orders/{mine}/receipt")
        assert rc.status_code == 200, rc.text
        assert rc.json()["phone"] == CUST_A

        # manager ko dukaan ke sab bill
        await _login(client, A_MGR_PHONE)
        all_bills = [x["number"] for x in (await client.get("/staff/api/bills")).json()]
        assert mine in all_bills and someone_elses in all_bills
    finally:
        async with async_session_factory() as db:
            await db.execute(sqltext("DELETE FROM rate_card WHERE service = 'MySvc'"))
            await db.commit()
