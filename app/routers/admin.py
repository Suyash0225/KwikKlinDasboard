"""Manager dashboard (full CRM view over the admin API).

GET /admin                 -> the page (app/static/dashboard.html; asks for
                              the admin key once, stores in localStorage)
GET /admin/api/dashboard   -> counts + active orders + conversations
GET /admin/api/customers   -> customer list with order counts

All writes (create order, status, payment, dates) go through /orders — the
page is a client of the same API, so every business rule (state machine,
payment derivation, notifications) applies automatically.
"""

from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Conversation, Customer, Direction, Order, Staff
from app.routers.orders import require_admin_key
from app.services.order_service import ACTIVE_STATUSES, get_active_orders_for_phone
from app.services.whatsapp import SendError, WindowClosedError, send_message
from app.utils.phone import normalize_phone

router = APIRouter(prefix="/admin", tags=["admin"])
log = structlog.get_logger()

_DASHBOARD_FILE = Path(__file__).resolve().parent.parent / "static" / "dashboard.html"


@router.get("/api/dashboard", dependencies=[Depends(require_admin_key)])
async def dashboard_data(db: AsyncSession = Depends(get_db)) -> dict:
    """Counts + active orders + recent conversations, one round trip."""
    today_start = datetime.combine(
        datetime.now(timezone.utc).date(), time.min, tzinfo=timezone.utc
    )

    by_status_rows = (
        await db.execute(
            select(Order.status, func.count())
            .where(Order.status.in_(ACTIVE_STATUSES))
            .group_by(Order.status)
        )
    ).all()
    today_new = (
        await db.execute(
            select(func.count()).select_from(Order).where(Order.created_at >= today_start)
        )
    ).scalar_one()

    active_orders = (
        await db.execute(
            select(Order, Customer)
            .join(Customer, Customer.id == Order.customer_id)
            .where(Order.status.in_(ACTIVE_STATUSES))
            .order_by(Order.created_at.desc())
            .limit(100)
        )
    ).all()

    convs = (
        await db.execute(
            select(Conversation, Customer, Staff)
            .outerjoin(Customer, Customer.id == Conversation.customer_id)
            .outerjoin(Staff, Staff.id == Conversation.staff_id)
            .order_by(Conversation.created_at.desc())
            .limit(30)
        )
    ).all()

    return {
        "counts": {
            "by_status": {s.name: c for s, c in by_status_rows},
            "active_total": sum(c for _, c in by_status_rows),
            "today_new": today_new,
        },
        "active_orders": [
            {
                "order_number": o.order_number,
                "status": o.status.name,
                "customer": cu.name or cu.phone,
                "phone": cu.phone,
                "items": o.items,
                "total_amount": str(o.total_amount) if o.total_amount is not None else None,
                "payment_status": o.payment_status.name,
                "expected_delivery": o.expected_delivery.isoformat() if o.expected_delivery else None,
                "created_at": o.created_at.isoformat(),
            }
            for o, cu in active_orders
        ],
        "conversations": [
            {
                "who": (st.name if st else (cu.name or cu.phone if cu else "?")),
                "kind": "staff" if st else "customer",
                "direction": c.direction.name,
                "text": c.message_text,
                "at": c.created_at.isoformat(),
            }
            for c, cu, st in convs
        ],
    }


@router.get("/api/customers", dependencies=[Depends(require_admin_key)])
async def customers_list(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """Customers with order counts, most recently active first."""
    active_count = (
        select(func.count())
        .where(Order.customer_id == Customer.id, Order.status.in_(ACTIVE_STATUSES))
        .scalar_subquery()
    )
    total_count = (
        select(func.count()).where(Order.customer_id == Customer.id).scalar_subquery()
    )
    rows = (
        await db.execute(
            select(Customer, active_count, total_count)
            .order_by(Customer.last_message_at.desc().nulls_last())
            .limit(300)
        )
    ).all()
    return [
        {
            "name": c.name,
            "phone": c.phone,
            "active_orders": active,
            "total_orders": total,
            "last_message_at": c.last_message_at.isoformat() if c.last_message_at else None,
            "opted_out": c.opted_out,
        }
        for c, active, total in rows
    ]


SERVICE_WINDOW = timedelta(hours=24)


def _window_state(last_inbound: datetime | None) -> dict:
    """24h customer-service-window state for the UI."""
    if last_inbound is None:
        return {"open": False, "expires_at": None}
    expires = last_inbound + SERVICE_WINDOW
    return {
        "open": datetime.now(timezone.utc) < expires,
        "expires_at": expires.isoformat(),
    }


@router.get("/api/inbox/threads", dependencies=[Depends(require_admin_key)])
async def inbox_threads(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """Every participant with conversation history, newest activity first."""
    threads: list[dict] = []

    # last message per customer
    last_at = (
        select(
            Conversation.customer_id,
            func.max(Conversation.created_at).label("last_at"),
        )
        .where(Conversation.customer_id.isnot(None))
        .group_by(Conversation.customer_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(Customer, Conversation)
            .join(last_at, last_at.c.customer_id == Customer.id)
            .join(
                Conversation,
                (Conversation.customer_id == Customer.id)
                & (Conversation.created_at == last_at.c.last_at),
            )
        )
    ).all()
    for cust, conv in rows:
        threads.append(
            {
                "kind": "customer",
                "phone": cust.phone,
                "name": cust.name or cust.phone,
                "last_text": conv.message_text[:80],
                "last_at": conv.created_at.isoformat(),
                "last_direction": conv.direction.name,
                "window": _window_state(cust.last_message_at),
            }
        )

    last_at_s = (
        select(
            Conversation.staff_id,
            func.max(Conversation.created_at).label("last_at"),
        )
        .where(Conversation.staff_id.isnot(None))
        .group_by(Conversation.staff_id)
        .subquery()
    )
    rows_s = (
        await db.execute(
            select(Staff, Conversation)
            .join(last_at_s, last_at_s.c.staff_id == Staff.id)
            .join(
                Conversation,
                (Conversation.staff_id == Staff.id)
                & (Conversation.created_at == last_at_s.c.last_at),
            )
        )
    ).all()
    for st, conv in rows_s:
        threads.append(
            {
                "kind": "staff",
                "phone": st.phone,
                "name": st.name,
                "last_text": conv.message_text[:80],
                "last_at": conv.created_at.isoformat(),
                "last_direction": conv.direction.name,
                "window": _window_state(st.last_message_at),
            }
        )

    threads.sort(key=lambda t: t["last_at"], reverse=True)
    return threads


@router.get("/api/inbox/thread", dependencies=[Depends(require_admin_key)])
async def inbox_thread(
    phone: str = Query(...),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=60, ge=1, le=200),
) -> dict:
    """One participant's messages (latest `limit`, oldest-first for display)."""
    # '+' in a query string decodes to a space — canonicalize whatever came in.
    try:
        phone = normalize_phone(phone)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"invalid phone {phone!r}")
    staff = (
        await db.execute(select(Staff).where(Staff.phone == phone))
    ).scalar_one_or_none()
    customer = None
    if staff is None:
        customer = (
            await db.execute(select(Customer).where(Customer.phone == phone))
        ).scalar_one_or_none()
        if customer is None:
            raise HTTPException(status_code=404, detail=f"no thread for {phone}")

    participant = staff or customer
    cond = (
        Conversation.staff_id == staff.id
        if staff
        else Conversation.customer_id == customer.id
    )
    msgs = (
        await db.execute(
            select(Conversation).where(cond).order_by(Conversation.created_at.desc()).limit(limit)
        )
    ).scalars().all()

    active_orders = []
    if customer:
        active_orders = [
            {"order_number": o.order_number, "status": o.status.name,
             "expected_delivery": o.expected_delivery.isoformat() if o.expected_delivery else None}
            for o in await get_active_orders_for_phone(db, phone)
        ]

    return {
        "kind": "staff" if staff else "customer",
        "phone": phone,
        "name": (staff.name if staff else (customer.name or customer.phone)),
        "window": _window_state(participant.last_message_at),
        "active_orders": active_orders,
        "messages": [
            {
                "direction": m.direction.name,
                "text": m.message_text,
                "sent_by": m.sent_by,
                "at": m.created_at.isoformat(),
            }
            for m in reversed(msgs)
        ],
    }


class InboxSendIn(BaseModel):
    phone: str
    text: str = Field(min_length=1, max_length=4000)


@router.post("/api/inbox/send", dependencies=[Depends(require_admin_key)])
async def inbox_send(body: InboxSendIn, db: AsyncSession = Depends(get_db)) -> dict:
    """Manager sends a free-form message from the Inbox."""
    try:
        to_phone = normalize_phone(body.phone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        wa_id = await send_message(db, to_phone=to_phone, text=body.text, sent_by="manager")
    except WindowClosedError:
        raise HTTPException(
            status_code=409,
            detail="24h window band hai — free-form nahi ja sakta. Template bhejo ya customer ke message ka intezaar karo.",
        )
    except SendError as exc:
        raise HTTPException(status_code=502, detail=f"WhatsApp send fail hua: {exc}")
    return {"wa_message_id": wa_id, "at": datetime.now(timezone.utc).isoformat()}


@router.get("")
async def dashboard_page() -> FileResponse:
    """The dashboard page itself — data stays key-protected behind the API."""
    return FileResponse(_DASHBOARD_FILE, media_type="text/html")