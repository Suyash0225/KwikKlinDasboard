"""Phase 2 proof: client profile detail, edit with before/after audit,
timeline merge, export/import — aur export ka TENANT-ISOLATION proof.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.models import Customer
from app.models.tenant import Tenant
from app.services import billing, kpis, tenant_context

# /control ka master key. VENDOR_API_KEY set ho to ADMIN_API_KEY wahan
# chalta hi NAHI (orders.vendor_master_key ka jaan-boojh kar rakha gaya
# niyam). Ye test seedha ADMIN_API_KEY bhejte the, isliye purane
# ek-dukaan wale .env par pass hote the aur alag vendor key wale par
# 401. Wahi helper use karo jo server use karta hai — dono soorat mein
# sahi.
from app.routers.orders import vendor_master_key

AUTH = {"X-API-Key": vendor_master_key()}
A_SLUG, B_SLUG = "test-prof-a", "test-prof-b"
A_CUST, B_CUST = "+919999900041", "+919999900042"


async def _purge() -> None:
    tenant_context.current_tenant_id.set(None)
    sub = "(SELECT id FROM tenants WHERE slug LIKE 'test-prof-%')"
    async with async_session_factory() as db:
        await db.execute(
            sqltext("DELETE FROM customers WHERE phone IN (:a, :b)"),
            {"a": A_CUST, "b": B_CUST},
        )
        for q in [
            f"DELETE FROM audit_log WHERE tenant_id IN {sub}",
            f"DELETE FROM billing_events WHERE tenant_id IN {sub}",
            f"DELETE FROM invites WHERE tenant_id IN {sub}",
            f"DELETE FROM invoices WHERE tenant_id IN {sub}",
            f"DELETE FROM login_sessions WHERE user_id IN "
            f"(SELECT id FROM users WHERE tenant_id IN {sub})",
            f"DELETE FROM users WHERE tenant_id IN {sub}",
            "DELETE FROM tenants WHERE slug LIKE 'test-prof-%'",
        ]:
            await db.execute(sqltext(q))
        await db.commit()


@pytest.fixture(autouse=True)
async def _cleanup():
    await _purge()  # pichhle failed run ka kachra bhi saaf
    kpis.invalidate()
    yield
    await _purge()
    kpis.invalidate()


@pytest.fixture
async def two_shops():
    async with async_session_factory() as db:
        a = Tenant(slug=A_SLUG, shop_name="Prof Shop A", owner_name="A Owner",
                   owner_phone="+919999900043", plan="pro", status="active")
        b = Tenant(slug=B_SLUG, shop_name="Prof Shop B", owner_name="B Owner",
                   owner_phone="+919999900044", plan="starter", status="trial",
                   trial_ends_at=datetime.now(timezone.utc) + timedelta(days=5))
        db.add_all([a, b])
        await db.flush()
        db.add(Customer(phone=A_CUST, name="A ka Grahak", tenant_id=a.id))
        db.add(Customer(phone=B_CUST, name="B ka Grahak", tenant_id=b.id))
        await db.commit()
        await db.refresh(a); await db.refresh(b)
        return a, b


async def test_detail_endpoint_shape(client, two_shops) -> None:
    d = (await client.get(f"/control/api/tenants/{A_SLUG}", headers=AUTH)).json()
    assert d["shop_name"] == "Prof Shop A"
    assert d["subscription"]["status"] == "active"
    assert "usage" in d and "invoices" in d and "tags" in d
    assert "wa_token" not in json.dumps(d), "secret leak!"
    assert (
        await client.get("/control/api/tenants/nahi-hai-aisa", headers=AUTH)
    ).status_code == 404


async def test_profile_edit_records_before_after(client, two_shops) -> None:
    r = await client.patch(f"/control/api/tenants/{A_SLUG}", headers=AUTH, json={
        "shop_name": "Prof Shop A2", "city": "Kanpur",
        "tags": ["VIP", " referral ", "vip"],  # dedupe+normalize expect
    })
    assert r.status_code == 200
    d = r.json()
    assert d["shop_name"] == "Prof Shop A2" and d["tags"] == ["referral", "vip"]
    # audit mein before/after
    rows = (
        await client.get(
            f"/control/api/audit?tenant_slug={A_SLUG}&action=tenant_updated", headers=AUTH
        )
    ).json()
    ch = rows[0]["args"]["changes"]
    assert ch["shop_name"] == {"before": "Prof Shop A", "after": "Prof Shop A2"}
    assert ch["city"]["after"] == "Kanpur"


async def test_timeline_merges_sources_sorted(client, two_shops) -> None:
    a, _ = two_shops
    async with async_session_factory() as db:
        t = await db.get(Tenant, a.id)
        billing._billing_event(db, t, "dunning_reminder", note="day 1/30")
        await db.commit()
    await client.patch(f"/control/api/tenants/{A_SLUG}", headers=AUTH,
                       json={"notes": "timeline test"})
    tl = (
        await client.get(f"/control/api/tenants/{A_SLUG}/timeline", headers=AUTH)
    ).json()
    kinds = {x["kind"] for x in tl}
    assert "audit" in kinds and "billing" in kinds
    ats = [x["at"] for x in tl]
    assert ats == sorted(ats, reverse=True), "timeline sorted desc nahi hai"


async def test_export_is_tenant_isolated(client, two_shops) -> None:
    """A ke full backup mein B ka EK BHI byte nahi — isolation proof."""
    r = await client.get(
        f"/control/api/tenants/{A_SLUG}/export?include_business=true", headers=AUTH
    )
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "")
    data = r.json()
    assert data["tenant"]["slug"] == A_SLUG
    phones = [c["phone"] for c in data["business"]["customers"]]
    assert A_CUST in phones
    assert B_CUST not in r.text, "TENANT B KA DATA A KE EXPORT MEIN LEAK!"
    assert "B ka Grahak" not in r.text
    assert "password_hash" not in r.text and "wa_token" not in r.text

    # CSV bhi chalta hai
    r = await client.get(
        f"/control/api/tenants/{A_SLUG}/export?format=csv", headers=AUTH
    )
    assert r.headers["content-type"].startswith("text/csv")
    assert "Prof Shop A" in r.text


async def test_import_recreates_client_with_invites(client, two_shops) -> None:
    exp = (
        await client.get(f"/control/api/tenants/{A_SLUG}/export", headers=AUTH)
    ).json()
    # phone clash hatao (import naya client banata hai)
    exp["tenant"]["owner_phone"] = "+919999900045"
    exp["tenant"]["owner_email"] = "prof-import@test.local"
    exp["users"] = [{"name": "Imported Owner", "email": "prof-import@test.local",
                     "role": "OWNER"}]
    r = await client.post("/control/api/tenants/import", headers=AUTH, json=exp)
    assert r.status_code == 201, r.text
    d = r.json()
    # slug clash tha -> suffix mila
    assert d["tenant"]["slug"].startswith("test-prof-a")
    assert d["tenant"]["slug"] != A_SLUG
    assert d["invites"] and d["invites"][0]["invite_path"].startswith("/invite/")
    # invite accept se login chal jata hai
    token = d["invites"][0]["invite_path"].rsplit("/", 1)[1]
    r = await client.post("/api/invite/accept",
                          json={"token": token, "password": "import-pw-123"})
    assert r.status_code == 200
    client.cookies.delete("kk_session")
