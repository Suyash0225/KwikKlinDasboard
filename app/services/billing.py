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


async def _ensure_rzp_plan(db: AsyncSession, plan_code: str, *, annual: bool) -> str:
    """Razorpay Plan id — pehli baar API se banao, phir settings_kv cache.

    Manual dashboard setup ki zaroorat nahi: naya plan/price plans.py mein
    aaya to yahan apne-aap Razorpay par ban jayega. Test/live mode keys se
    decide hota hai (rzp_test_... = test mode).
    """
    from app.database import async_session_factory
    from app.services import app_settings, tenant_context

    p = plans.get(plan_code)
    cycle = "annual" if annual else "monthly"
    cache_key = f"{p.code}:{cycle}"
    # Cache GLOBAL vendor data hai (Razorpay plan ids sab tenants ke liye
    # same) — system context mein padho/likho warna non-home tenant ke
    # request mein RLS use chhupa dega aur har checkout naya plan banayega.
    ctx = tenant_context.current_tenant_id.set(None)
    try:
        async with async_session_factory() as sysdb:
            cached = await app_settings.get(sysdb, "rzp_plan_ids") or {}
    finally:
        tenant_context.current_tenant_id.reset(ctx)
    if cache_key in cached:
        return cached[cache_key]

    payload = {
        "period": "yearly" if annual else "monthly",
        "interval": 1,
        "item": {
            "name": f"KwikKlin {p.name} ({cycle})",
            # GST included in charge amount — breakup receipt par dikhta hai
            "amount": round((p.annual_inr if annual else p.price_inr) * (100 + GST_PERCENT)),
            "currency": "INR",
        },
        "notes": {"plan": p.code, "cycle": cycle},
    }
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{_API}/plans", json=payload, auth=_auth())
    if r.status_code >= 300:
        log.error("rzp_plan_create_failed", status=r.status_code, body=r.text[:200])
        raise RuntimeError(f"Razorpay plan nahi bana: {r.text[:150]}")
    plan_id = r.json()["id"]
    cached[cache_key] = plan_id
    ctx = tenant_context.current_tenant_id.set(None)
    try:
        async with async_session_factory() as sysdb:
            await app_settings.set_value(sysdb, "rzp_plan_ids", cached)
    finally:
        tenant_context.current_tenant_id.reset(ctx)
    log.info("rzp_plan_created", plan=cache_key, rzp_plan_id=plan_id)
    return plan_id


async def create_subscription(
    db: AsyncSession, tenant: Tenant, plan_code: str, *, annual: bool
) -> dict:
    """RECURRING subscription banao — har cycle Razorpay khud charge karega.

    Returns {subscription_id, key_id, breakup} — frontend Razorpay Checkout
    isi subscription_id par kholta hai. Setup fee (agar due hai) pehle
    payment ke saath ek addon ki tarah lagti hai.
    Keys na hon to {"disabled": True} — dev/demo trial par chalta hai.
    """
    breakup = price_breakup(plan_code, annual=annual, with_setup=not tenant.setup_fee_paid)
    if not enabled():
        log.info("billing_disabled_no_keys", tenant=tenant.slug)
        return {"disabled": True, "breakup": breakup}

    plan_id = await _ensure_rzp_plan(db, plan_code, annual=annual)
    payload = {
        "plan_id": plan_id,
        "total_count": 10 if annual else 120,  # 10 saal tak auto-renew
        "customer_notify": 1,
        "notes": {
            "tenant_id": str(tenant.id),
            "tenant_slug": tenant.slug,
            "plan": plans.get(plan_code).code,
            "cycle": breakup["cycle"],
        },
    }
    if not tenant.setup_fee_paid:
        gst_setup = round(plans.SETUP_FEE_INR * (100 + GST_PERCENT))
        payload["addons"] = [{
            "item": {"name": "One-time setup fee", "amount": gst_setup, "currency": "INR"}
        }]
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{_API}/subscriptions", json=payload, auth=_auth())
    if r.status_code >= 300:
        log.error("rzp_subscription_failed", status=r.status_code, body=r.text[:200])
        raise RuntimeError(f"Razorpay subscription nahi bana: {r.text[:150]}")
    data = r.json()
    tenant.rzp_subscription_id = data["id"]
    await db.commit()
    log.info("rzp_subscription_created", tenant=tenant.slug, sub=data["id"])
    return {
        "subscription_id": data["id"],
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


def verify_subscription_checkout_signature(
    payment_id: str, subscription_id: str, signature: str
) -> bool:
    """Subscription checkout ka browser callback — HMAC(payment_id|sub_id).

    (Razorpay ka formula subscriptions ke liye order wale se ULTA hai:
    payment_id pehle aata hai.) Ye sirf UI ke liye hai — asli activation
    hamesha signed webhook se hoti hai.
    """
    if not settings.RAZORPAY_KEY_SECRET:
        return False
    expected = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode(),
        f"{payment_id}|{subscription_id}".encode(),
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
    pay_ent = payload.get("payment", {}).get("entity") or {}
    sub_ent = payload.get("subscription", {}).get("entity") or {}
    entity = pay_ent or sub_ent
    # subscription.charged mein notes SUBSCRIPTION par hote hain, payment
    # par nahi — dono jagah dekho.
    notes = pay_ent.get("notes") or sub_ent.get("notes") or {}
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

    tenant = await _tenant_from_notes(db, notes)
    if tenant is None:
        # subscription id se milao — sub entity ka apna id, ya payment ka
        # subscription_id field
        sub_id = sub_ent.get("id") or pay_ent.get("subscription_id")
        if sub_id:
            tenant = (
                await db.execute(
                    select(Tenant).where(Tenant.rzp_subscription_id == sub_id)
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
        cycle = notes.get("cycle", "monthly")
        days = 365 if cycle == "annual" else 31
        # Recurring charge apna sahi period khud batata hai (epoch secs) —
        # ho to wahi use karo, warna ab-se-ek-cycle.
        period_start = period_end = None
        if sub_ent.get("current_start"):
            period_start = datetime.fromtimestamp(sub_ent["current_start"], tz=timezone.utc)
        if sub_ent.get("current_end"):
            period_end = datetime.fromtimestamp(sub_ent["current_end"], tz=timezone.utc)
        base = max(tenant.current_period_end or now, now)
        tenant.status = TENANT_ACTIVE
        tenant.current_period_end = period_end or (base + timedelta(days=days))
        tenant.setup_fee_paid = True
        plan = notes.get("plan")
        if plan:
            tenant.plan = plans.get(plan).code
        sub_id = sub_ent.get("id") or pay_ent.get("subscription_id")
        if sub_id:
            tenant.rzp_subscription_id = sub_id

        # 3. receipt — per tenant, ek payment = ek invoice (kabhi delete nahi)
        payment_id = pay_ent.get("id")
        if payment_id:
            from app.models import Invoice

            dup = (
                await db.execute(
                    select(Invoice).where(Invoice.rzp_payment_id == payment_id)
                )
            ).scalar_one_or_none()
            if dup is None:
                db.add(
                    Invoice(
                        tenant_id=tenant.id,
                        rzp_payment_id=payment_id,
                        rzp_subscription_id=sub_id,
                        plan=tenant.plan,
                        cycle=cycle,
                        amount_paise=pay_ent.get("amount") or 0,
                        currency=pay_ent.get("currency") or "INR",
                        period_start=period_start,
                        period_end=period_end or tenant.current_period_end,
                    )
                )
        await db.commit()
        from app.services import kpis as _kpis

        _kpis.invalidate()
        log.info(
            "tenant_activated", tenant=tenant.slug, plan=tenant.plan,
            until=str(tenant.current_period_end),
        )
        return "activated"

    if etype in ("subscription.activated", "subscription.authenticated"):
        # Mandate ban gaya; charge alag event mein aayega. Status abhi se
        # active — pehla payment isi flow ka hissa hai.
        sub_id = sub_ent.get("id")
        if sub_id:
            tenant.rzp_subscription_id = sub_id
        tenant.status = TENANT_ACTIVE
        await db.commit()
        log.info("subscription_activated", tenant=tenant.slug, sub=sub_id)
        return "subscription_active"

    if etype in ("payment.failed", "subscription.pending", "subscription.halted"):
        # abhi band mat karo — dunning chalne do, data likhna chalu rehta hai
        tenant.status = TENANT_PAST_DUE
        await db.commit()
        log.warning("tenant_past_due", tenant=tenant.slug)
        return "past_due"

    if etype in ("subscription.cancelled", "subscription.completed"):
        # Read-only grace mein bhejo (past_due) — data safe, sweep aage
        # locked karega agar payment kabhi nahi aayi.
        tenant.status = TENANT_PAST_DUE
        await db.commit()
        log.warning("tenant_past_due_cancelled", tenant=tenant.slug)
        return "past_due"

    return "ignored"


def _grace_reference(t: Tenant) -> datetime | None:
    """past_due grace kab se ginna hai — jo bhi aakhri paid/trial boundary thi."""
    candidates = [d for d in (t.trial_ends_at, t.current_period_end) if d is not None]
    return max(candidates) if candidates else t.created_at


async def get_grace_days(db: AsyncSession) -> int:
    """Configurable grace (app_settings 'grace_days'), fallback GRACE_DAYS=30."""
    from app.models.tenant import GRACE_DAYS
    from app.services import app_settings

    try:
        return int(await app_settings.get(db, "grace_days"))
    except Exception:
        return GRACE_DAYS


def _billing_event(db: AsyncSession, tenant: Tenant, etype: str, note: str = "") -> None:
    """Lifecycle ka nishaan Billing Events feed mein (panel isi ko dikhata
    hai). event_id date ke saath unique — ek din mein ek hi baar."""
    db.add(
        BillingEvent(
            tenant_id=tenant.id,
            event_id=f"sys:{tenant.slug}:{etype}:{datetime.now(timezone.utc).date()}"[:80],
            event_type=etype[:60],
            payload={"tenant": tenant.slug, "note": note} if note else {"tenant": tenant.slug},
        )
    )


async def _send_dunning_reminder(db: AsyncSession, t: Tenant, day: int, grace: int) -> bool:
    """Owner ko WhatsApp reminder (best-effort). sent_events se idempotent —
    ek din wala reminder do baar kabhi nahi jaata."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models import SentEvent

    key = f"dunning:{t.slug}:day{day}"
    claimed = await db.execute(
        pg_insert(SentEvent).values(event_key=key).on_conflict_do_nothing(
            index_elements=["event_key"]
        )
    )
    if claimed.rowcount == 0:
        return False  # aaj/pehle ja chuka
    left = max(0, grace - day)
    try:
        from app.services.whatsapp import send_message

        await send_message(
            db, to_phone=t.owner_phone,
            text=f"Namaste {t.owner_name}! Aapka KwikKlin subscription due hai — "
                 f"account abhi read-only hai. {left} din mein lock ho jayega. "
                 f"Renew karte hi sab wapas chalu; aapka data poora safe hai.",
            sent_by="system", enqueue_on_fail=False,
        )
    except Exception:
        log.warning("dunning_reminder_send_failed", tenant=t.slug, day=day)
    _billing_event(db, t, "dunning_reminder", note=f"day {day}/{grace}")
    log.info("dunning_reminder", tenant=t.slug, day=day)
    return True


async def run_subscription_sweep(db: AsyncSession) -> dict:
    """Subscription state machine ka chowkidar (nightly + control panel se).

    trial    + trial_ends_at guzar gayi      -> past_due  (READ-ONLY shuru)
    active   + current_period_end guzar gaya -> past_due  (READ-ONLY shuru)
    past_due + GRACE_DAYS (30 din) guzar gaye-> locked    (dashboard band)

    DATA KABHI DELETE NAHI HOTA — sirf status badalta hai. Payment aate hi
    process_webhook_event tenant ko wapas active kar deta hai.
    """
    from app.models.tenant import TENANT_LOCKED, TENANT_TRIAL
    from app.services import app_settings

    now = datetime.now(timezone.utc)
    grace_days = await get_grace_days(db)
    try:
        reminder_days = [
            int(d) for d in (await app_settings.get(db, "dunning_reminder_days"))
        ]
    except Exception:
        reminder_days = [1, 3, 7]
    moved = {"past_due": 0, "locked": 0, "reminders": 0}

    expired_trials = (
        await db.execute(
            select(Tenant).where(
                Tenant.status == TENANT_TRIAL,
                Tenant.trial_ends_at.isnot(None),
                Tenant.trial_ends_at < now,
            )
        )
    ).scalars().all()
    for t in expired_trials:
        t.status = TENANT_PAST_DUE
        moved["past_due"] += 1
        _billing_event(db, t, "trial_expired")
        log.warning("tenant_trial_expired", tenant=t.slug)

    lapsed = (
        await db.execute(
            select(Tenant).where(
                Tenant.status == TENANT_ACTIVE,
                Tenant.current_period_end.isnot(None),
                Tenant.current_period_end < now,
            )
        )
    ).scalars().all()
    for t in lapsed:
        t.status = TENANT_PAST_DUE
        moved["past_due"] += 1
        _billing_event(db, t, "subscription_lapsed")
        log.warning("tenant_subscription_lapsed", tenant=t.slug)

    grace_cutoff = now - timedelta(days=grace_days)
    overdue = (
        await db.execute(
            select(Tenant).where(
                Tenant.status == TENANT_PAST_DUE, Tenant.deleted_at.is_(None)
            )
        )
    ).scalars().all()
    for t in overdue:
        ref = _grace_reference(t)
        if ref is None:
            continue
        if ref < grace_cutoff:
            t.status = TENANT_LOCKED
            moved["locked"] += 1
            _billing_event(db, t, "tenant_locked", note="grace over")
            log.warning("tenant_locked_grace_over", tenant=t.slug)
            continue
        # Dunning ladder: day 1/3/7 (configurable) par owner ko reminder.
        days_since = (now - ref).days
        due = [d for d in sorted(reminder_days) if d <= days_since]
        if due and await _send_dunning_reminder(db, t, due[-1], grace_days):
            moved["reminders"] += 1

    await db.commit()
    if moved["past_due"] or moved["locked"]:
        from app.services import kpis as _kpis

        _kpis.invalidate()
    return moved
