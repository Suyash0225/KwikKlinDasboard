"""Grahak khud apna order banaye — /billcreation.

Staff panel ka "New bill" jaisa hi page, par grahak ke haath mein: rate card
se kapde chuno, pickup ka din, pata — aur order ban gaya. AI chat mein
details poochta hai; ye page unke liye jo khud dekh kar chunna pasand karte
hain.

Grahak ka number kahan se aata hai — do raaste:

1. AI ka bheja link (`/billcreation?t=<token>`): token mein dukaan + grahak,
   HMAC se signed (bill_link jaisa, alag key). Number token se — grahak
   likhta hi nahi, aur kisi aur ke naam par order ban hi nahi sakta.
2. Saada link (`/billcreation`, owner kahin bhi share kare): browser ko
   grahak ka number pata hi nahi hota. Page order ka draft bana kar ek code
   deta hai aur WhatsApp kholta hai ("Order code: K7M2QX ..."). Wo message
   grahak ke APNE number se aata hai — webhook code pehchaan kar usi number
   par order bana deta hai. Number bhi sahi, aur koi doosre ka number daal
   kar order nahi bana sakta.

Daam hamesha rate card se (bhejne wala koi rate bhej hi nahi sakta), aur
rakam sirf ANDAAZA hai: order bina total ke banta hai (PICKUP_ASSIGNED),
delivery wala pickup par kapde gin kar "Create bill & pick up" se asli bill
banata hai — grahak ke chune kapde wahan pehle se bhare milte hain.

Drafts process ki memory mein (pending_store jaisa — ek uvicorn worker).
Restart par draft gaya to bhi kuch nahi tootta: message mein kapdon ki list
hai, AI use aam order ki tarah sambhal leta hai.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

log = structlog.get_logger()

PATH = "/billcreation"
# Link chat mein pada rehta hai — mahine bhar baad bhi wahi grahak wahi
# link dabaye to chalna chahiye.
TOKEN_TTL_SECONDS = 30 * 24 * 3600
_TAG = b"o"
_SEP = "."

MAX_LINES = 40
MAX_PIECES = 200          # ek line par kapde
MAX_KG = Decimal("50")
PICKUP_DAYS_AHEAD = 14


class SelfOrderError(ValueError):
    """Grahak ko dikhane laayak galti (galat kapda, purani date...)."""


# ---------------------------------------------------------------- token --


def _key() -> bytes:
    base = (settings.TOKEN_ENCRYPTION_KEY or settings.ADMIN_API_KEY or "").encode()
    return hashlib.sha256(b"kk-self-order:" + base).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(tenant_id: uuid.UUID, customer_id: uuid.UUID, now: float | None = None) -> str:
    exp = int((time.time() if now is None else now) + TOKEN_TTL_SECONDS)
    body = _TAG + tenant_id.bytes + customer_id.bytes + exp.to_bytes(5, "big")
    sig = hmac.new(_key(), body, hashlib.sha256).digest()[:9]
    return _b64(body) + _SEP + _b64(sig)


def parse_token(token: str, now: float | None = None) -> tuple[uuid.UUID, uuid.UUID] | None:
    """(tenant_id, customer_id) — ya None (galat, chheda hua, ya purana)."""
    try:
        body_b64, sig_b64 = (token or "").split(_SEP)
        body = _unb64(body_b64)
        if len(body) != 38 or body[:1] != _TAG:
            return None
        want = hmac.new(_key(), body, hashlib.sha256).digest()[:9]
        if not hmac.compare_digest(want, _unb64(sig_b64)):
            return None
        if (time.time() if now is None else now) > int.from_bytes(body[33:], "big"):
            return None
        return uuid.UUID(bytes=body[1:17]), uuid.UUID(bytes=body[17:33])
    except Exception:
        return None


async def _base(db: AsyncSession) -> str:
    from app.services.google_auth import public_base

    return (await public_base(db) or "").rstrip("/")


async def plain_url(db: AsyncSession, tenant=None) -> str:
    """Saada link — home dukaan ka bina kisi parameter ke, baaki ka ?s=<slug>."""
    from app.services import tenant_context

    base = await _base(db)
    if not base:
        return ""
    if tenant is not None and tenant.id != await tenant_context.get_home_tenant_id():
        return f"{base}{PATH}?s={tenant.slug}"
    return f"{base}{PATH}"


async def url_for(db: AsyncSession, customer) -> str:
    """Isi grahak ka link — number token se, page par likhna nahi padta."""
    base = await _base(db)
    if not base or customer is None or customer.tenant_id is None:
        return ""
    return f"{base}{PATH}?t={make_token(customer.tenant_id, customer.id)}"


# ---------------------------------------------------------------- items --


async def rate_card(db: AsyncSession) -> list[dict]:
    from app.models import Rate

    rows = (
        await db.execute(select(Rate).where(Rate.is_active).order_by(Rate.service, Rate.garment))
    ).scalars().all()
    return [
        {"service": r.service, "garment": r.garment, "unit": r.unit, "rate": float(r.rate)}
        for r in rows
    ]


async def price_items(db: AsyncSession, raw_items: list[dict]) -> tuple[list[dict], Decimal]:
    """Grahak ki list -> bill ki lines, daam rate card se. (items, andaazan rakam)."""
    from app.models import Rate

    if not raw_items:
        raise SelfOrderError("Add at least one item")
    if len(raw_items) > MAX_LINES:
        raise SelfOrderError("Too many items in one order")
    rows = (await db.execute(select(Rate).where(Rate.is_active))).scalars().all()
    by_key = {(r.service.strip().lower(), r.garment.strip().lower()): r for r in rows}
    items, gross = [], Decimal("0")
    for it in raw_items:
        service = str(it.get("service") or "").strip()
        garment = str(it.get("garment") or "").strip()
        row = by_key.get((service.lower(), garment.lower()))
        if row is None:
            raise SelfOrderError(f"{garment or service} is not on the rate card any more")
        try:
            qty = Decimal(str(it.get("qty")))
        except Exception:
            raise SelfOrderError("Quantity looks wrong") from None
        if row.unit == "kg":
            if not (Decimal("0.5") <= qty <= MAX_KG):
                raise SelfOrderError(f"{row.service}: weight must be between 0.5 and {MAX_KG} kg")
            qty = qty.quantize(Decimal("0.1"))
        else:
            if qty != qty.to_integral_value() or not (1 <= qty <= MAX_PIECES):
                raise SelfOrderError(f"{row.garment}: count must be between 1 and {MAX_PIECES}")
        rate = Decimal(str(row.rate))
        amount = (rate * qty).quantize(Decimal("0.01"))
        gross += amount
        line = {
            "type": row.garment or row.service, "service": row.service, "garment": row.garment,
            "qty": float(qty), "rate": float(rate), "amount": float(amount), "unit": row.unit,
        }
        if row.unit == "kg":
            pieces = []
            for pc in (it.get("pieces") or [])[:40]:
                name = str((pc or {}).get("type") or "").strip()[:60]
                try:
                    n = int((pc or {}).get("qty") or 0)
                except (TypeError, ValueError):
                    n = 0
                if name and 0 < n <= MAX_PIECES:
                    pieces.append({"type": name, "qty": n})
            if pieces:
                line["pieces"] = pieces
        items.append(line)
    return items, gross


def check_pickup_date(value: str) -> date:
    try:
        day = date.fromisoformat((value or "").strip()[:10])
    except ValueError:
        raise SelfOrderError("Pick the pickup day") from None
    today = _today()
    if day < today:
        raise SelfOrderError("Pickup day is in the past")
    if day > today + timedelta(days=PICKUP_DAYS_AHEAD):
        raise SelfOrderError(f"Pickup can be booked up to {PICKUP_DAYS_AHEAD} days ahead")
    return day


def _today() -> date:
    # Dukaan IST mein chalti hai — server UTC par ho to raat 12-5:30 "kal" galat
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()


# --------------------------------------------------------------- coupon --

COUPON_NOTE_RE = re.compile(r"· Coupon: ([A-Z0-9_\-]{1,30})")


async def check_coupon(db: AsyncSession, code: str, customer_id, estimate: Decimal) -> tuple[str, Decimal]:
    """(CODE, chhoot) — wahi jaanch jo counter ke bill par lagti hai.

    Yahan sirf dikhaya jaata hai; coupon REDEEM pickup ke asli bill par hota
    hai (staff panel), kyunki rakam tabhi pakki hoti hai."""
    from app.services.marketing_agent import validate_coupon

    code = (code or "").strip().upper()[:30]
    if not code:
        return "", Decimal("0")
    if customer_id is None:
        # Saada link: grahak abhi anjaan hai. Personal coupon (AI offer,
        # reward) ka "kisi aur ke naam ka" galti ulta lagta — saaf batao.
        from app.services.marketing import get_coupon

        found = await get_coupon(db, code)
        if found is not None and found.customer_id is not None:
            raise SelfOrderError(
                f"Coupon {code} is your personal offer — open the order link we sent you on WhatsApp to use it."
            )
    coupon, discount, err = await validate_coupon(db, code, customer_id, estimate)
    if err or coupon is None:
        raise SelfOrderError(f"Coupon {code}: {err or 'not valid'}")
    return coupon.code.upper(), min(Decimal(discount), estimate)


# --------------------------------------------------------------- drafts --

# Code mein 0/O, 1/I/L nahi — grahak padh kar bhi likhe to galti na ho
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_RE = re.compile(r"order\s*code\s*[:#-]?\s*([A-Z0-9]{6})\b", re.I)
DRAFT_TTL_SECONDS = 24 * 3600
_MAX_DRAFTS = 5000
_DRAFTS: dict[tuple, dict] = {}


def _sweep(now: float) -> None:
    for k in [k for k, v in _DRAFTS.items() if now - v["at"] > DRAFT_TTL_SECONDS]:
        _DRAFTS.pop(k, None)
    while len(_DRAFTS) >= _MAX_DRAFTS:
        _DRAFTS.pop(next(iter(_DRAFTS)))


def save_draft(tenant_id: uuid.UUID, draft: dict) -> str:
    now = time.time()
    _sweep(now)
    while True:
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
        if (tenant_id, code) not in _DRAFTS:
            break
    _DRAFTS[(tenant_id, code)] = {**draft, "at": now}
    return code


def find_code(text: str) -> str | None:
    m = CODE_RE.search(text or "")
    return m.group(1).upper() if m else None


def take_draft(tenant_id: uuid.UUID, code: str) -> dict | None:
    """Ek code = ek order: nikaalte hi hat jaata hai (dohra message dohra order nahi)."""
    d = _DRAFTS.pop((tenant_id, (code or "").upper()), None)
    if d is None or time.time() - d["at"] > DRAFT_TTL_SECONDS:
        return None
    return d


def whatsapp_text(code: str, items: list[dict], estimate: Decimal, pickup: date,
                  coupon: str = "", discount: Decimal = Decimal("0")) -> str:
    """Grahak ke phone se jaane wala message — code ke saath poori list bhi,
    taaki draft kho jaye to bhi dukaan ko pata ho ki kya chahiye."""
    lines = [f"Order code: {code}", "I want to place this order:"]
    for it in items:
        qty = it["qty"]
        qty = int(qty) if float(qty).is_integer() else qty
        unit = " kg" if it["unit"] == "kg" else " x"
        lines.append(f"- {qty}{unit} {it['type']} ({it['service']})")
    if coupon:
        lines.append(f"Coupon: {coupon} (-Rs {_rupees(discount)})")
    lines.append(f"Estimate: Rs {_rupees(estimate - discount)}")
    lines.append(f"Pickup: {pickup.strftime('%a %d %b')}")
    return "\n".join(lines)


def _rupees(amount: Decimal) -> str:
    q = Decimal(amount).quantize(Decimal("0.01"))
    return f"{q:.0f}" if q == q.to_integral_value() else f"{q:.2f}"


# -------------------------------------------------------- shop WhatsApp --

_WA_NUMBER: dict = {}
_WA_TTL = 3600


async def shop_whatsapp_number(db: AsyncSession, tenant) -> str:
    """Wo number jis par grahak ka message bot tak pahunche (sirf ank, cc ke saath).

    WAHA session ka apna number sabse sahi hai (home dukaan). Warna dukaan
    ka contact number (Settings). Kuch na mile to "" — page phir saaf bolta
    hai ki WhatsApp par message karein."""
    from app.services import app_settings, tenant_context, waha

    cached = _WA_NUMBER.get(tenant.id)
    if cached and time.monotonic() - cached[1] < _WA_TTL:
        return cached[0]
    number = ""
    if waha.enabled() and tenant.id == await tenant_context.get_home_tenant_id():
        try:
            me = await waha._get(f"/api/sessions/{settings.WAHA_SESSION}/me")
            number = re.sub(r"\D", "", str((me or {}).get("id") or "").split("@")[0])
        except Exception:
            log.info("self_order_waha_me_failed")
    if not number:
        number = re.sub(r"\D", "", str(await app_settings.get(db, "shop_contact_phone") or ""))
        if len(number) == 10:
            number = settings.DEFAULT_COUNTRY_CODE + number
    if number:
        _WA_NUMBER[tenant.id] = (number, time.monotonic())
    return number


# ---------------------------------------------------------------- order --


async def place_order(
    db: AsyncSession,
    *,
    customer_phone: str,
    name: str,
    address: str,
    items: list[dict],
    estimate: Decimal,
    pickup_day: date,
    notes: str = "",
    via: str,
    coupon: str = "",
    discount: Decimal = Decimal("0"),
):
    """Order + pickup task (ops agent). (order, created) — dohra tap = wahi order."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models import Customer, Order, OrderStatus
    from app.services import tenant_context
    from app.services.order_service import create_order

    # Grahak pehle (create_order jaisa hi upsert) — naam/pata order banne se
    # PEHLE chahiye: ops agent pickup task banate hi boy ko pata bhejta hai.
    await db.execute(
        pg_insert(Customer)
        .values(phone=customer_phone, name=name[:120] or None,
                tenant_id=tenant_context.effective_tenant_id())
        .on_conflict_do_nothing(index_elements=["tenant_id", "phone"])
    )
    customer = (
        await db.execute(select(Customer).where(Customer.phone == customer_phone))
    ).scalar_one()
    if address:
        # Page par pata pehle se bhara hota hai — grahak ne badla to wahi
        # sahi hai (aaj kapde wahan se uthane hain)
        customer.address = address[:500]
    if name and not (customer.name or "").strip():
        customer.name = name[:120]
    db.add(customer)
    await db.commit()

    # Do baar "Place order" (ya WhatsApp message do baar) — naya order nahi,
    # wahi. 10 minute mein usi grahak ka wahi web wala pickup order.
    recent = (
        await db.execute(
            select(Order).where(
                Order.customer_id == customer.id,
                Order.status == OrderStatus.PICKUP_ASSIGNED,
                Order.created_at >= datetime.now(timezone.utc) - timedelta(minutes=10),
                Order.notes.like("Customer self-order%"),
            ).order_by(Order.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    from app.services.item_savings import without_list_rate

    if recent is not None and without_list_rate(recent.items) == items:
        return recent, False

    note = f"Customer self-order ({via}) · estimate ₹{_rupees(estimate)} · pickup {pickup_day.isoformat()}"
    if address:
        note += f" · Address: {address[:300]}"
    if coupon:
        # Staff ke pickup bill par yahi coupon pehle se laga aata hai
        # (staff_panel bill-context) — redeem wahin, asli rakam par.
        note += f" · Coupon: {coupon} (-₹{_rupees(discount)})"
    if notes:
        note += f" · Note: {notes[:300]}"
    order = await create_order(
        db,
        customer_phone=customer_phone,
        customer_name=name[:120] or None,
        items=items,
        notes=note,
        created_by="customer-web",
        needs_pickup=True,
        pickup_date=pickup_day,
        notify_customer=False,   # apna saaf confirmation neeche (andaazan rakam ke saath)
    )
    log.info("self_order_created", order=order.order_number, via=via, lines=len(items))
    return order, True


def confirmation_text(order, customer_name: str, estimate: Decimal, pickup_day: date,
                      coupon: str = "", discount: Decimal = Decimal("0")) -> str:
    from app.services.messages import get_message
    from app.services.work_orders import items_summary

    first = (customer_name or "").split()
    return get_message(
        "self_order_confirmed",
        name=first[0] if first else "ji",
        order_number=order.order_number,
        items=items_summary(order)[:300],
        estimate=_rupees(estimate - discount) + (f" (coupon {coupon} −₹{_rupees(discount)})" if coupon else ""),
        pickup=pickup_day.strftime("%a %d %b"),
    )


async def handle_whatsapp_code(db: AsyncSession, customer, text: str) -> str | None:
    """Webhook: grahak ka "Order code: XXXXXX" message. Jawab ka text, ya None
    (code nahi hai — baaki raasta jaise chalta tha)."""
    from app.services import tenant_context
    from app.services.messages import get_message

    code = find_code(text)
    if not code:
        return None
    tid = getattr(customer, "tenant_id", None) or tenant_context.effective_tenant_id()
    draft = take_draft(tid, code)
    if draft is None:
        # Code to hai par draft nahi (purana / restart). Agar message mein
        # kapdon ki list hai to AI aam order ki tarah sambhal lega.
        if "\n-" in (text or ""):
            return None
        from app.models.tenant import Tenant

        return get_message("self_order_code_missing", link=await plain_url(db, await db.get(Tenant, tid)))
    try:
        pickup_day = check_pickup_date(draft["pickup_date"])
    except SelfOrderError:
        pickup_day = _today()
    estimate = Decimal(str(draft["estimate"]))
    coupon, discount = "", Decimal("0")
    if draft.get("coupon"):
        # Page par grahak ka number pata nahi tha — ab asli grahak par dobara
        # jaancho (kisi aur ke naam ka / pehle use ho chuka). Na chale to
        # order phir bhi banta hai, bas bina coupon ke.
        try:
            coupon, discount = await check_coupon(db, draft["coupon"], customer.id, estimate)
        except SelfOrderError:
            log.info("self_order_coupon_dropped", code=draft["coupon"])
    order, _created = await place_order(
        db,
        coupon=coupon,
        discount=discount,
        customer_phone=customer.phone,
        name=draft.get("name") or customer.name or "",
        address=draft.get("address") or "",
        items=draft["items"],
        estimate=estimate,
        pickup_day=pickup_day,
        notes=draft.get("notes") or "",
        via="whatsapp-code",
    )
    return confirmation_text(order, draft.get("name") or customer.name or "", estimate, pickup_day, coupon, discount)
