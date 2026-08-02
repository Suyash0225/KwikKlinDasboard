"""Manager dashboard (full CRM view over the admin API).

GET /admin                 -> the page (app/static/dashboard.html; asks for
                              the admin key once, stores in localStorage)
GET /admin/api/dashboard   -> counts + active orders + conversations
GET /admin/api/customers   -> customer list with order counts

All writes (create order, status, payment, dates) go through /orders — the
page is a client of the same API, so every business rule (state machine,
payment derivation, notifications) applies automatically.
"""

from datetime import datetime, time, timezone
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Conversation, Customer, Order, Staff
from app.routers.orders import require_admin_key
from app.services.order_service import ACTIVE_STATUSES

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


@router.get("")
async def dashboard_page() -> FileResponse:
    """The dashboard page itself — data stays key-protected behind the API."""
    return FileResponse(_DASHBOARD_FILE, media_type="text/html")