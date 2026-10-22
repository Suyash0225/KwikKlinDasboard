"""/billcreation — grahak ka apna order page (app/services/self_order.py).

Bina login ka public rasta. Dukaan kaun si hai, ye URL se tay hota hai:
  ?t=<token>  AI ka bheja link — dukaan + grahak dono token mein (signed)
  ?s=<slug>   kisi aur dukaan ka saada link
  (kuch nahi) home dukaan — kwikklin.online/billcreation
Request ka apna tenant context yahan kabhi nahi hota (anonymous), isliye har
query integrations.tenant_db(<dukaan>) ke andar.
"""

from __future__ import annotations

from pathlib import Path

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.services import self_order
from app.utils.client_ip import get_client_ip
from app.utils.throttle import IPThrottle

log = structlog.get_logger()

router = APIRouter(include_in_schema=False)

_PAGE = Path(__file__).resolve().parent.parent / "site" / "templates" / "billcreation.html"
# Ek IP se ghante mein itne order/draft — asli grahak 1-2 banata hai; ye
# sirf kisi script ko dukaan ka pickup queue bharne se rokta hai.
_SUBMITS = IPThrottle(max_failures=12, window_secs=3600, name="self_order")
_COUPON_TRIES = IPThrottle(max_failures=10, window_secs=600, name="self_order_coupon")
_BLOCKED_STATUSES = ("locked", "suspended", "cancelled")
_NO_STORE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}


async def _resolve(t: str, s: str):
    """(tenant, customer_id | None). Galat link = 404, sab ek jaisa."""
    from app.database import async_session_factory
    from app.models.tenant import Tenant
    from app.services import tenant_context

    customer_id = None
    async with tenant_context.as_system_context():
        async with async_session_factory() as db:
            if t:
                parsed = self_order.parse_token(t)
                if parsed is None:
                    raise HTTPException(status_code=404, detail="This order link has expired. Please ask us for a new one on WhatsApp.")
                tid, customer_id = parsed
                tenant = await db.get(Tenant, tid)
            elif s:
                tenant = (
                    await db.execute(select(Tenant).where(Tenant.slug == s[:40].lower()))
                ).scalar_one_or_none()
            else:
                home = await tenant_context.get_home_tenant_id()
                tenant = await db.get(Tenant, home) if home else None
    if tenant is None or tenant.status in _BLOCKED_STATUSES:
        raise HTTPException(status_code=404, detail="This order page is not available.")
    return tenant, customer_id


@router.get("/billcreation", response_class=HTMLResponse)
async def page() -> HTMLResponse:
    # Saara data API se — HTML static hai, token sirf browser ke URL mein
    return HTMLResponse(_PAGE.read_text(encoding="utf-8"), headers=_NO_STORE)


@router.get("/billcreation/api/form")
async def form(t: str = "", s: str = "") -> dict:
    from app.models import Customer
    from app.services import app_settings, integrations

    tenant, customer_id = await _resolve(t, s)
    async with integrations.tenant_db(tenant.id) as db:
        rates = await self_order.rate_card(db)
        cfg = await app_settings.get_many(db, "turnaround_days", "shop_hours")
        customer = None
        if customer_id is not None:
            c = await db.get(Customer, customer_id)
            if c is None or c.tenant_id != tenant.id:
                raise HTTPException(status_code=404, detail="This order link has expired. Please ask us for a new one on WhatsApp.")
            # Link forward ho sakta hai — poora number nahi, sirf aakhri 4
            customer = {
                "name": c.name or "",
                "address": c.address or "",
                "phone_masked": "••••••" + (c.phone or "")[-4:],
            }
        wa = "" if customer else await self_order.shop_whatsapp_number(db, tenant)
    return {
        "shop": tenant.shop_name,
        "rates": rates,
        "customer": customer,
        "whatsapp": wa,
        "turnaround_days": int(cfg.get("turnaround_days") or 2),
        "shop_hours": str(cfg.get("shop_hours") or ""),
        "today": self_order._today().isoformat(),
        "max_days_ahead": self_order.PICKUP_DAYS_AHEAD,
    }


class _Piece(BaseModel):
    type: str = Field(min_length=1, max_length=60)
    qty: int = Field(ge=1, le=self_order.MAX_PIECES)


class _Item(BaseModel):
    service: str = Field(min_length=1, max_length=60)
    garment: str = Field(default="", max_length=60)
    qty: float = Field(gt=0, le=self_order.MAX_PIECES)
    pieces: list[_Piece] = Field(default_factory=list, max_length=40)


class OrderIn(BaseModel):
    t: str = Field(default="", max_length=200)
    s: str = Field(default="", max_length=40)
    name: str = Field(default="", max_length=120)
    address: str = Field(default="", max_length=500)
    pickup_date: str = Field(max_length=10)
    notes: str = Field(default="", max_length=300)
    coupon: str = Field(default="", max_length=30)
    items: list[_Item] = Field(min_length=1, max_length=self_order.MAX_LINES)


class CouponIn(BaseModel):
    t: str = Field(default="", max_length=200)
    s: str = Field(default="", max_length=40)
    code: str = Field(min_length=1, max_length=30)
    items: list[_Item] = Field(min_length=1, max_length=self_order.MAX_LINES)


@router.post("/billcreation/api/coupon")
async def coupon(body: CouponIn, request: Request) -> dict:
    """Coupon ka daam dikhana — rakam aur jaanch dono server ki (rate card se)."""
    from app.services import integrations

    ip = get_client_ip(request) or "unknown"
    if _COUPON_TRIES.throttled(ip):
        raise HTTPException(status_code=429, detail="Too many tries. Please wait a few minutes.")
    tenant, customer_id = await _resolve(body.t, body.s)
    async with integrations.tenant_db(tenant.id) as db:
        try:
            _items, estimate = await self_order.price_items(db, [i.model_dump() for i in body.items])
            code, discount = await self_order.check_coupon(db, body.code, customer_id, estimate)
        except self_order.SelfOrderError as exc:
            # Galat code ginte hain — code andaaza lagane wale ko rokna
            _COUPON_TRIES.note_failure(ip)
            raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"code": code, "discount": float(discount), "estimate": float(estimate - discount)}


@router.post("/billcreation/api/order")
async def place(body: OrderIn, request: Request) -> dict:
    from app.models import Customer
    from app.services import integrations, shop_identity
    from app.services.order_service import OrderError, PlanLimitError
    from app.services.whatsapp import SendError, send_message

    ip = get_client_ip(request) or "unknown"
    if _SUBMITS.throttled(ip):
        raise HTTPException(status_code=429, detail="Too many orders from this phone. Please message us on WhatsApp.")
    tenant, customer_id = await _resolve(body.t, body.s)
    name = " ".join(body.name.split())
    address = " ".join(body.address.split())
    notes = " ".join(body.notes.split())
    async with integrations.tenant_db(tenant.id) as db:
        await shop_identity.current(db, tenant.id)   # {shop} messages mein isi dukaan ka naam
        try:
            items, estimate = await self_order.price_items(db, [i.model_dump() for i in body.items])
            pickup_day = self_order.check_pickup_date(body.pickup_date)
        except self_order.SelfOrderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        customer = None
        if customer_id is not None:
            customer = await db.get(Customer, customer_id)
            if customer is None or customer.tenant_id != tenant.id:
                raise HTTPException(status_code=404, detail="This order link has expired. Please ask us for a new one on WhatsApp.")
        try:
            coupon_code, discount = await self_order.check_coupon(
                db, body.coupon, customer.id if customer else None, estimate
            )
        except self_order.SelfOrderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if not (name or (customer and customer.name)):
            raise HTTPException(status_code=400, detail="Please enter your name")
        if not (address or (customer and customer.address)):
            raise HTTPException(status_code=400, detail="Please enter the pickup address")
        _SUBMITS.note_failure(ip)

        if customer is None:
            # Saada link: number abhi pata nahi. Draft + code; grahak ke apne
            # WhatsApp se aaya message hi order banata hai (webhook).
            wa = await self_order.shop_whatsapp_number(db, tenant)
            if not wa:
                raise HTTPException(status_code=503, detail="Please send us your order on WhatsApp.")
            code = self_order.save_draft(tenant.id, {
                "items": items, "estimate": str(estimate), "name": name, "address": address,
                "pickup_date": pickup_day.isoformat(), "notes": notes, "coupon": coupon_code,
            })
            log.info("self_order_draft", tenant=str(tenant.id), code=code, lines=len(items))
            return {
                "status": "confirm_on_whatsapp",
                "code": code,
                "whatsapp": wa,
                "text": self_order.whatsapp_text(code, items, estimate, pickup_day, coupon_code, discount),
                "estimate": float(estimate - discount),
            }

        try:
            order, created = await self_order.place_order(
                db, customer_phone=customer.phone, name=name or customer.name or "",
                address=address, items=items, estimate=estimate, pickup_day=pickup_day,
                notes=notes, via="web-link", coupon=coupon_code, discount=discount,
            )
        except PlanLimitError:
            raise HTTPException(status_code=503, detail="We could not take the order online right now. Please message us on WhatsApp.") from None
        except OrderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        # Send fail par session rollback hota hai aur order expire — number pehle
        number = order.order_number
        if created:
            try:
                await send_message(
                    db, to_phone=customer.phone,
                    text=self_order.confirmation_text(
                        order, name or customer.name or "", estimate, pickup_day, coupon_code, discount
                    ),
                    intent="transactional", outbound_kind="service_reply", urgent=True,
                )
            except SendError:
                # Page par order number dikh raha hai — message na jaana order nahi rokta
                log.info("self_order_confirm_not_sent", order=number)
            except Exception:
                log.exception("self_order_confirm_failed", order=number)
    return {
        "status": "placed",
        "order_number": number,
        "estimate": float(estimate - discount),
        "pickup": pickup_day.strftime("%a %d %b"),
    }

