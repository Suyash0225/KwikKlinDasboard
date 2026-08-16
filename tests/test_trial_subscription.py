"""Trial & subscription lifecycle proof.

1. Signup: trial_ends_at = aaj + 7 din, status = 'trial'.
2. /api/me: banner ke liye subscription fields (days_left waghera).
3. Sweep: trial expiry -> past_due (read-only), 30-din grace -> locked;
   DATA KABHI DELETE NAHI HOTA (row counts same rehte hain).
4. Gates: past_due home = writes 402/reads 200; locked home = sab 402,
   lekin /api/me chalta hai taaki user apni haalat dekh sake.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sqltext

from app.config import settings
from app.database import async_session_factory
from app.models.tenant import (
    GRACE_DAYS,
    TENANT_ACTIVE,
    TENANT_LOCKED,
    TENANT_PAST_DUE,
    TENANT_TRIAL,
    Tenant,
    User,
)
from app.services import auth, billing, plans, tenant_context

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}
SLUGS = ("test-sweep-t1", "test-sweep-t2", "test-sweep-t3", "test-sweep-t4")
SIGNUP_PHONE = "+919999900071"
NOW = lambda: datetime.now(timezone.utc)


async def _purge_test_tenants() -> None:
    tenant_context.current_tenant_id.set(None)
    async with async_session_factory() as db:
        await db.execute(
            sqltext(
                "DELETE FROM billing_events WHERE tenant_id IN "
                "(SELECT id FROM tenants WHERE slug = ANY(:s) OR owner_phone = :p)"
            ),
            {"s": list(SLUGS), "p": SIGNUP_PHONE},
        )
        await db.execute(
            sqltext(
                "DELETE FROM invites WHERE tenant_id IN "
                "(SELECT id FROM tenants WHERE slug = ANY(:s) OR owner_phone = :p)"
            ),
            {"s": list(SLUGS), "p": SIGNUP_PHONE},
        )
        await db.execute(
            sqltext("DELETE FROM sent_events WHERE event_key LIKE 'dunning:test-sweep-%'")
        )
        await db.execute(
            sqltext(
                "DELETE FROM login_sessions WHERE user_id IN (SELECT id FROM users "
                "WHERE tenant_id IN (SELECT id FROM tenants WHERE slug = ANY(:s) "
                "OR owner_phone = :p))"
            ),
            {"s": list(SLUGS), "p": SIGNUP_PHONE},
        )
        await db.execute(
            sqltext(
                "DELETE FROM users WHERE tenant_id IN "
                "(SELECT id FROM tenants WHERE slug = ANY(:s) OR owner_phone = :p)"
            ),
            {"s": list(SLUGS), "p": SIGNUP_PHONE},
        )
        await db.execute(
            sqltext("DELETE FROM tenants WHERE slug = ANY(:s) OR owner_phone = :p"),
            {"s": list(SLUGS), "p": SIGNUP_PHONE},
        )
        await db.commit()


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    await _purge_test_tenants()


def _tenant(slug: str, status: str, **kw) -> Tenant:
    return Tenant(
        slug=slug, shop_name=f"Sweep {slug}", owner_name="T", plan="starter",
        owner_phone=f"+9199999001{slug[-1]}0", status=status, **kw,
    )


async def test_signup_sets_7_day_trial(client) -> None:
    assert plans.TRIAL_DAYS == 7
    r = await client.post("/api/signup", json={
        "shop_name": "Trial Test Shop", "owner_name": "Trial Owner",
        "email": "trial-test@test.local", "phone": SIGNUP_PHONE,
        "password": "trial-pw-123", "city": "Varanasi",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    ends = datetime.fromisoformat(body["trial_ends_at"])
    days = (ends - NOW()).total_seconds() / 86400
    assert 6.9 < days <= 7.01, f"trial is {days} days, expected 7"

    # /api/me: banner fields
    r = await client.post("/api/login", json={
        "email": "trial-test@test.local", "password": "trial-pw-123",
    })
    assert r.status_code == 200
    me = await client.get("/api/me")
    sub = me.json()["subscription"]
    assert sub["status"] == TENANT_TRIAL
    assert sub["days_left"] == 7
    assert sub["read_only"] is False and sub["locked"] is False
    client.cookies.delete("kk_session")


async def test_sweep_transitions_and_data_safety(monkeypatch) -> None:
    # dunning reminders WhatsApp bhejte hain — tests mein kabhi real nahi
    from app.services import whatsapp as _wa

    async def _no_send(db, **kw):
        return "wamid.FAKE"

    monkeypatch.setattr(_wa, "send_message", _no_send)
    now = NOW()
    async with async_session_factory() as db:
        db.add(_tenant(SLUGS[0], TENANT_TRIAL, trial_ends_at=now - timedelta(days=1)))
        db.add(_tenant(SLUGS[1], TENANT_PAST_DUE,
                       trial_ends_at=now - timedelta(days=GRACE_DAYS + 1)))
        db.add(_tenant(SLUGS[2], TENANT_TRIAL, trial_ends_at=now + timedelta(days=3)))
        db.add(_tenant(SLUGS[3], TENANT_ACTIVE,
                       current_period_end=now - timedelta(days=2)))
        await db.commit()
        before = (await db.execute(sqltext("SELECT count(*) FROM tenants"))).scalar_one()

    async with async_session_factory() as db:
        moved = await billing.run_subscription_sweep(db)
    assert moved["past_due"] == 2 and moved["locked"] == 1

    async with async_session_factory() as db:
        rows = dict((
            await db.execute(
                sqltext("SELECT slug, status FROM tenants WHERE slug = ANY(:s)"),
                {"s": list(SLUGS)},
            )
        ).all())
        after = (await db.execute(sqltext("SELECT count(*) FROM tenants"))).scalar_one()

    assert rows[SLUGS[0]] == TENANT_PAST_DUE      # trial expired -> read-only
    assert rows[SLUGS[1]] == TENANT_LOCKED        # grace over -> locked
    assert rows[SLUGS[2]] == TENANT_TRIAL         # abhi trial mein hai — untouched
    assert rows[SLUGS[3]] == TENANT_PAST_DUE      # subscription lapsed
    assert after == before, "sweep ne data delete kiya — kabhi nahi hona chahiye!"


async def _set_home_status(status: str) -> None:
    async with async_session_factory() as db:
        await db.execute(
            sqltext("UPDATE tenants SET status = :st WHERE slug = 'kwik-klin'"),
            {"st": status},
        )
        await db.commit()


async def test_past_due_home_is_read_only(client) -> None:
    """Trial khatam = reads chalte hain, writes 402 — API key se bhi."""
    await _set_home_status(TENANT_PAST_DUE)
    try:
        assert (await client.get("/orders", headers=AUTH)).status_code == 200
        assert (await client.get("/admin/api/dashboard", headers=AUTH)).status_code == 200
        r = await client.post("/orders", headers=AUTH, json={
            "customer_phone": "+919999900072", "items": [{"type": "shirt", "qty": 1}],
        })
        assert r.status_code == 402, f"write in read-only got {r.status_code}"
        assert "read-only" in r.json()["detail"]
    finally:
        await _set_home_status(TENANT_ACTIVE)


async def test_locked_home_blocks_dashboard_but_not_me(client) -> None:
    """Locked = dashboard poora band (reads bhi), /api/me zinda (renew CTA)."""
    hid = await tenant_context.get_home_tenant_id()
    async with async_session_factory() as db:
        u = User(tenant_id=hid, name="LockTest", email="lock-test@test.local",
                 password_hash=auth.hash_password("lock-pw-123"), role="OWNER")
        db.add(u)
        await db.commit()
        token = await auth.start_session(db, u, ip="127.0.0.1", user_agent="pytest")
    await _set_home_status(TENANT_LOCKED)
    client.cookies.set("kk_session", token)
    try:
        assert (await client.get("/orders", headers=AUTH)).status_code == 402
        assert (await client.get("/admin/api/dashboard")).status_code == 402
        r = await client.get("/api/me")
        assert r.status_code == 200
        assert r.json()["subscription"]["locked"] is True
    finally:
        client.cookies.delete("kk_session")
        await _set_home_status(TENANT_ACTIVE)
        async with async_session_factory() as db:
            await db.execute(
                sqltext(
                    "DELETE FROM login_sessions WHERE user_id IN "
                    "(SELECT id FROM users WHERE email = 'lock-test@test.local')"
                )
            )
            await db.execute(
                sqltext("DELETE FROM users WHERE email = 'lock-test@test.local'")
            )
            await db.commit()
