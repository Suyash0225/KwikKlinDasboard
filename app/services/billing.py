"""Razorpay: client paisa deta hai, account chalu ho jaata hai.

Flow (PRD §7 ka "pay karke login"):

    pricing page -> signup -> Razorpay checkout (setup fee + pehla mahina)
    -> webhook `payment.captured` -> tenant ACTIVE + owner user + password
    -> client login karta hai -> Suyash WhatsApp setup poora karta hai

Do ground rules, WhatsApp webhook wali hi:
- **Signature verify kiye bina kuch nahi.** Nakli "paisa aa gaya" call se
  koi account chalu nahi kar sakta.
- **Har event pehle DB mein (`billing_events`), tab action.** Razorpay
  retry karta hai; `event_id` unique hai, isliye dobara chalu/charge nahi hoga.

Keys na hon to sab kuch chalta hai, bas checkout ki jagah "trial" milta hai —
local dev aur demo dono bina Razorpay ke chalte rehte hain.
"""

import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    TENANT_ACTIVE,
    TENANT_PAST_DUE,
    TENANT_READ_ONLY,
    BillingEvent,
    Tenant,
)
from app.services import plans

log = structlog.get_logger()

_API = "https://api.razorpay.com/v1"
GST_PERCENT = 18


def enabled() -> bool:
    return bool(settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET)


def _auth() -> tuple[str, str]:
    return (settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)


def price_breakup(plan_code: str, *, annual: bool, with_setup: bool) -> dict:
    """Kya-kya charge ho raha hai — paise (1 rupee = 100 paise) mein.

    GST alag se dikhta hai; chhupa hua tax bharosa todta hai.
    """
    p = plans.get(plan_code)
    base = p.annual_inr if annual else p.price_inr
    setup = plans.SETUP_FEE_INR if with_setup else 0
    subtotal = base + setup
    gst = round(subtotal * GST_PERCENT / 100)
    return {
        "plan": p.code,
        "plan_name": p.name,
        "cycle": "annual" if annual else "monthly",
        "plan_inr": base,
        "setup_inr": setup,
        "subtotal_inr": subtotal,
        "gst_percent": GST_PERCENT,
        "gst_inr": gst,
        "total_inr": subtotal + gst,
        "total_paise": (subtotal + gst) * 100,
    }


async def create_order(tenant: Tenant, plan_code: str, *, annual: bool) -> dict:
    """Razorpay order banao (checkout isi par khulta hai).

    Returns {order_id, amount_paise, key_id, breakup}. Keys na hon to
    {"disabled": True} — caller trial par bhej deta hai.
    """
    breakup = price_breakup(plan_code, annual=annual, with_setup=not tenant.setup_fee_paid)
    if not enabled():
        log.info("billing_disabled_no_keys", tenant=tenant.slug)
        return {"disabled": True, "breakup": breakup}

    payload = {
        "amount": breakup["total_paise"],
        "currency": "INR",
        "receipt": f"kk-{tenant.slug}-{plan_code}"[:40],
        "notes": {
            "tenant_id": str(tenant.id),
            "tenant_slug": tenant.slug,
            "plan": plan_code,
            "cycle": breakup["cycle"],
        },
    }
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{_API}/orders", json=payload, auth=_auth())
    if r.status_code >= 300:
        log.error("rzp_order_failed", status=r.status_code, body=r.text[:200])
        raise RuntimeError(f"Razorpay order nahi bana: {r.text[:150]}")
    data = r.json()
    log.info("rzp_order_created", order=data.get("id"), amount=data.get("amount"))
    return {
        "order_id": data["id"],
        "amount_paise": data["amount"],
        "key_id": settings.RAZORPAY_KEY_ID,
        "breakup": breakup,
    }


def verify_webhook(raw_body: bytes, signature: str) -> bool:
    """Razorpay ka X-Razorpay-Signature — HMAC-SHA256 raw body par."""
    secret = settings.RAZORPAY_WEBHOOK_SECRET
    if not secret:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def verify_checkout_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """Browser se aaya success callback — HMAC(order_id|payment_id)."""
    if not settings.RAZORPAY_KEY_SECRET:
        return False
    expected = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode(),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")


async def _tenant_from_notes(db: AsyncSession, notes: dict) -> Tenant | None:
    slug = (notes or {}).get("tenant_slug")
    tid = (notes or {}).get("tenant_id")
    if tid:
        try:
            t = await db.get(Tenant, tid)
            if t:
                return t
        except Exception:
            pass
    if slug:
        return (
            await db.execute(select(Tenant).where(Tenant.slug == slug))
        ).scalar_one_or_none()
    return None


async def handle_event(db: AsyncSession, event: dict) -> str:
    """Ek Razorpay webhook. Returns kya kiya. Kabhi raise nahi karta."""
    etype = event.get("event", "")
    payload = event.get("payload", {}) or {}
    entity = (
        payload.get("payment", {}).get("entity")
        or payload.get("subscription", {}).get("entity")
        or {}
    )
    event_id = (
        event.get("id")
        or entity.get("id")
        or f"{etype}:{event.get('created_at', '')}"
    )

    # 1. journal FIRST — replay-safe, aur paisa ka trail kabhi nahi khota
    exists = (
        await db.execute(select(BillingEvent).where(BillingEvent.event_id == event_id))
    ).scalar_one_or_none()
    if exists is not None:
        log.info("billing_event_duplicate", event_id=event_id)
        return "duplicate"

    tenant = await _tenant_from_notes(db, entity.get("notes") or {})
    if tenant is None and entity.get("id"):
        tenant = (
            await db.execute(
                select(Tenant).where(Tenant.rzp_subscription_id == entity["id"])
            )
        ).scalar_one_or_none()

    db.add(
        BillingEvent(
            tenant_id=tenant.id if tenant else None,
            event_id=str(event_id)[:80],
            event_type=etype[:60],
            amount_paise=entity.get("amount"),
            payload=event,
        )
    )
    await db.commit()

    if tenant is None:
        log.warning("billing_event_no_tenant", event=etype, event_id=event_id)
        return "no_tenant"

    # 2. act
    now = datetime.now(timezone.utc)
    if etype in ("payment.captured", "order.paid", "subscription.charged"):
        cycle = (entity.get("notes") or {}).get("cycle", "monthly")
        days = 365 if cycle == "annual" else 31
        base = max(tenant.current_period_end or now, now)
        tenant.status = TENANT_ACTIVE
        tenant.current_period_end = base + timedelta(days=days)
        tenant.setup_fee_paid = True
        plan = (entity.get("notes") or {}).get("plan")
        if plan:
            tenant.plan = plans.get(plan).code
        await db.commit()
        log.info(
            "tenant_activated", tenant=tenant.slug, plan=tenant.plan,
            until=str(tenant.current_period_end),
        )
        return "activated"

    if etype in ("payment.failed", "subscription.pending", "subscription.halted"):
        # abhi band mat karo — dunning chalne do, data likhna chalu rehta hai
        tenant.status = TENANT_PAST_DUE
        await db.commit()
        log.warning("tenant_past_due", tenant=tenant.slug)
        return "past_due"

    if etype in ("subscription.cancelled", "subscription.completed"):
        tenant.status = TENANT_READ_ONLY
        await db.commit()
        log.warning("tenant_read_only", tenant=tenant.slug)
        return "read_only"

    return "ignored"


async def run_dunning(db: AsyncSession) -> int:
    """Paisa ruke hue 7 din ho gaye -> read-only. Data kabhi delete nahi.

    Returns kitne tenant read-only hue.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    rows = (
        (
            await db.execute(
                select(Tenant).where(
                    Tenant.status == TENANT_PAST_DUE,
                    Tenant.current_period_end.isnot(None),
                    Tenant.current_period_end < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    for t in rows:
        t.status = TENANT_READ_ONLY
        log.warning("tenant_read_only_dunning", tenant=t.slug)
    if rows:
        await db.commit()
    return len(rows)
