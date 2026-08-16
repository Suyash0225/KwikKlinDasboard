"""Recurring subscription payments (Razorpay) ka proof.

1. Keys nahi (test .env) -> checkout gracefully "disabled", trial chalta hai.
2. create_subscription: Razorpay Plan auto-create + settings_kv cache,
   subscription with setup-fee addon, tenant par rzp_subscription_id.
   (Razorpay HTTP mocked — tests kabhi asli API nahi chhoote.)
3. Webhook subscription.charged -> tenant ACTIVE, period Razorpay ke
   current_start/current_end se, INVOICE row banta hai; replay = duplicate,
   dusra event same payment ka = ek hi invoice.
4. payment.failed -> past_due (read-only). Grace ke baad sweep -> locked
   (wo test_account mein covered hai).
5. GET /api/billing/invoices sirf apne tenant ki receipts deta hai.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text as sqltext

from app.database import async_session_factory
from app.models import Invoice
from app.models.tenant import TENANT_ACTIVE, TENANT_PAST_DUE, Tenant, User
from app.services import auth, billing, tenant_context

PHONE = "+919999900061"
EMAIL = "subpay@test.local"
SLUG_LIKE = "subpay"


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    tenant_context.current_tenant_id.set(None)
    async with async_session_factory() as db:
        await db.execute(
            sqltext(
                "DELETE FROM invoices WHERE tenant_id IN "
                "(SELECT id FROM tenants WHERE owner_phone = :p)"
            ), {"p": PHONE},
        )
        await db.execute(
            sqltext(
                "DELETE FROM billing_events WHERE tenant_id IN "
                "(SELECT id FROM tenants WHERE owner_phone = :p) "
                "OR event_id LIKE 'evt_subtest%'"
            ), {"p": PHONE},
        )
        await db.execute(
            sqltext(
                "DELETE FROM login_sessions WHERE user_id IN (SELECT id FROM users "
                "WHERE tenant_id IN (SELECT id FROM tenants WHERE owner_phone = :p))"
            ), {"p": PHONE},
        )
        await db.execute(
            sqltext(
                "DELETE FROM users WHERE tenant_id IN "
                "(SELECT id FROM tenants WHERE owner_phone = :p)"
            ), {"p": PHONE},
        )
        await db.execute(
            sqltext("DELETE FROM tenants WHERE owner_phone = :p"), {"p": PHONE}
        )
        await db.execute(
            sqltext("DELETE FROM settings_kv WHERE key = 'rzp_plan_ids'")
        )
        await db.commit()


async def _make_tenant() -> Tenant:
    async with async_session_factory() as db:
        t = Tenant(
            slug="test-subpay", shop_name="SubPay Test", owner_name="SP",
            owner_phone=PHONE, plan="starter", status="trial",
            trial_ends_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return t


class _FakeResp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, json.dumps(data)

    def json(self):
        return self._d


class _FakeClient:
    """httpx.AsyncClient ka double — Razorpay API kabhi asli nahi lagti."""

    calls: list = []

    def __init__(self, *a, **k): ...
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, json=None, auth=None):
        _FakeClient.calls.append((url, json))
        if url.endswith("/plans"):
            return _FakeResp({"id": "plan_TEST123"})
        if url.endswith("/subscriptions"):
            return _FakeResp({"id": "sub_TEST456", "status": "created"})
        return _FakeResp({}, 500)


@pytest.fixture
def rzp_test_mode(monkeypatch):
    """Test-mode keys (env-style) + mocked HTTP."""
    from app.config import settings

    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "rzp_test_FAKEKEY")
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", "test_secret")
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(billing.httpx, "AsyncClient", _FakeClient)
    _FakeClient.calls = []


async def test_checkout_without_keys_is_disabled_not_broken() -> None:
    t = await _make_tenant()
    async with async_session_factory() as db:
        t = await db.get(Tenant, t.id)
        out = await billing.create_subscription(db, t, "pro", annual=False)
    assert out.get("disabled") is True and "breakup" in out
    async with async_session_factory() as db:
        assert (await db.get(Tenant, t.id)).status == "trial"


async def test_create_subscription_with_plan_cache_and_addon(rzp_test_mode) -> None:
    t = await _make_tenant()
    async with async_session_factory() as db:
        t = await db.get(Tenant, t.id)
        out = await billing.create_subscription(db, t, "pro", annual=False)
    assert out["subscription_id"] == "sub_TEST456"
    assert out["key_id"] == "rzp_test_FAKEKEY"

    plan_calls = [c for c in _FakeClient.calls if c[0].endswith("/plans")]
    sub_calls = [c for c in _FakeClient.calls if c[0].endswith("/subscriptions")]
    assert len(plan_calls) == 1 and len(sub_calls) == 1
    # setup fee due tha -> addon laga
    assert "addons" in sub_calls[0][1]
    assert sub_calls[0][1]["notes"]["tenant_slug"] == "test-subpay"

    # tenant par subscription id save hui
    async with async_session_factory() as db:
        assert (await db.get(Tenant, t.id)).rzp_subscription_id == "sub_TEST456"

    # dobara checkout: plan CACHE se aata hai (naya /plans call nahi)
    _FakeClient.calls = []
    async with async_session_factory() as db:
        t2 = await db.get(Tenant, t.id)
        await billing.create_subscription(db, t2, "pro", annual=False)
    assert not [c for c in _FakeClient.calls if c[0].endswith("/plans")]


def _charged_event(eid: str, *, slug: str, payment_id: str, start: int, end: int) -> dict:
    return {
        "event": "subscription.charged",
        "id": eid,
        "payload": {
            "subscription": {"entity": {
                "id": "sub_TEST456",
                "notes": {"tenant_slug": slug, "plan": "pro", "cycle": "monthly"},
                "current_start": start, "current_end": end,
            }},
            "payment": {"entity": {
                "id": payment_id, "amount": 235882, "currency": "INR",
                "subscription_id": "sub_TEST456",
            }},
        },
    }


async def test_subscription_charged_activates_and_stores_invoice() -> None:
    t = await _make_tenant()
    now = int(datetime.now(timezone.utc).timestamp())
    ev = _charged_event(
        "evt_subtest_001", slug="test-subpay", payment_id="pay_SUBTEST1",
        start=now, end=now + 31 * 86400,
    )
    async with async_session_factory() as db:
        assert await billing.handle_event(db, ev) == "activated"
        # Razorpay retry — dobara activate/invoice NAHI
        assert await billing.handle_event(db, ev) == "duplicate"

    async with async_session_factory() as db:
        t2 = await db.get(Tenant, t.id)
        assert t2.status == TENANT_ACTIVE
        assert t2.plan == "pro" and t2.setup_fee_paid is True
        assert t2.rzp_subscription_id == "sub_TEST456"
        # period Razorpay ke current_end se aaya
        assert abs((t2.current_period_end - datetime.now(timezone.utc)).days - 31) <= 1

        invs = (
            await db.execute(select(Invoice).where(Invoice.tenant_id == t.id))
        ).scalars().all()
        assert len(invs) == 1
        inv = invs[0]
        assert inv.rzp_payment_id == "pay_SUBTEST1"
        assert inv.amount_paise == 235882 and inv.cycle == "monthly"

    # same payment ka doosra event (payment.captured) -> invoice phir bhi EK
    ev2 = {
        "event": "payment.captured", "id": "evt_subtest_002",
        "payload": {"payment": {"entity": {
            "id": "pay_SUBTEST1", "amount": 235882, "currency": "INR",
            "subscription_id": "sub_TEST456",
            "notes": {"tenant_slug": "test-subpay", "plan": "pro", "cycle": "monthly"},
        }}},
    }
    async with async_session_factory() as db:
        assert await billing.handle_event(db, ev2) == "activated"
        n = (
            await db.execute(
                select(Invoice).where(Invoice.rzp_payment_id == "pay_SUBTEST1")
            )
        ).scalars().all()
        assert len(n) == 1, "ek payment ki do receipts ban gayin!"


async def test_payment_failed_moves_to_past_due() -> None:
    t = await _make_tenant()
    async with async_session_factory() as db:
        t2 = await db.get(Tenant, t.id)
        t2.status = TENANT_ACTIVE
        t2.rzp_subscription_id = "sub_TEST456"
        await db.commit()
    ev = {
        "event": "payment.failed", "id": "evt_subtest_003",
        "payload": {"payment": {"entity": {
            "id": "pay_FAIL1", "amount": 235882,
            "subscription_id": "sub_TEST456", "notes": {},
        }}},
    }
    async with async_session_factory() as db:
        assert await billing.handle_event(db, ev) == "past_due"
    async with async_session_factory() as db:
        assert (await db.get(Tenant, t.id)).status == TENANT_PAST_DUE


async def test_invoices_endpoint_is_tenant_scoped(client) -> None:
    t = await _make_tenant()
    now = int(datetime.now(timezone.utc).timestamp())
    ev = _charged_event(
        "evt_subtest_004", slug="test-subpay", payment_id="pay_SUBTEST9",
        start=now, end=now + 31 * 86400,
    )
    async with async_session_factory() as db:
        await billing.handle_event(db, ev)
        u = User(
            tenant_id=t.id, name="SP Owner", email=EMAIL,
            password_hash=auth.hash_password("subpay-pw-123"), role="OWNER",
        )
        db.add(u)
        await db.commit()
        token = await auth.start_session(db, u, ip="127.0.0.1", user_agent="pytest")

    client.cookies.set("kk_session", token)
    try:
        r = await client.get("/api/billing/invoices")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["payment_id"] == "pay_SUBTEST9"
        assert rows[0]["plan"] == "Premium"
        assert rows[0]["amount_inr"] == 2358.82
    finally:
        client.cookies.delete("kk_session")
