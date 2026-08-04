"""Manager dashboard (full CRM view over the admin API).

GET /admin                 -> the page (app/static/dashboard.html; asks for
                              the admin key once, stores in localStorage)
GET /admin/api/dashboard   -> counts + active orders + conversations
GET /admin/api/customers   -> customer list with order counts

All writes (create order, status, payment, dates) go through /orders — the
page is a client of the same API, so every business rule (state machine,
payment derivation, notifications) applies automatically.
"""

import csv
import io
import uuid as uuid_module
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import (
    Conversation,
    Customer,
    Direction,
    Expense,
    Order,
    Rate,
    Staff,
    StaffRole,
)
from app.utils.phone import normalize_phone as _norm_phone
from app.routers.orders import require_admin_key
from app.services.order_service import ACTIVE_STATUSES, get_active_orders_for_phone
from app.services.whatsapp import SendError, WindowClosedError, send_image, send_message
from app.utils.phone import normalize_phone

_MEDIA_DIR = Path(__file__).resolve().parent.parent / "media"

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
                "amount_paid": str(o.amount_paid),
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
    """Customers with order counts + money ledger, recently active first."""
    active_count = (
        select(func.count())
        .where(Order.customer_id == Customer.id, Order.status.in_(ACTIVE_STATUSES))
        .scalar_subquery()
    )
    total_count = (
        select(func.count()).where(Order.customer_id == Customer.id).scalar_subquery()
    )
    business = (
        select(func.coalesce(func.sum(Order.total_amount), 0))
        .where(Order.customer_id == Customer.id)
        .scalar_subquery()
    )
    paid = (
        select(func.coalesce(func.sum(Order.amount_paid), 0))
        .where(Order.customer_id == Customer.id)
        .scalar_subquery()
    )
    rows = (
        await db.execute(
            select(Customer, active_count, total_count, business, paid)
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
            "business": str(biz),
            "paid": str(pd),
            "outstanding": str(max(Decimal("0"), Decimal(biz) - Decimal(pd))),
            "last_message_at": c.last_message_at.isoformat() if c.last_message_at else None,
            "opted_out": c.opted_out,
        }
        for c, active, total, biz, pd in rows
    ]


# ---------- Expenses ----------

class ExpenseIn(BaseModel):
    category: str = Field(min_length=1, max_length=60)
    amount: Decimal = Field(gt=0)
    spent_on: date
    description: str | None = Field(default=None, max_length=300)


@router.get("/api/expenses", dependencies=[Depends(require_admin_key)])
async def expenses_list(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (
        await db.execute(select(Expense).order_by(Expense.spent_on.desc()).limit(200))
    ).scalars().all()
    return [
        {
            "id": str(e.id),
            "category": e.category,
            "amount": str(e.amount),
            "spent_on": e.spent_on.isoformat(),
            "description": e.description,
        }
        for e in rows
    ]


@router.post("/api/expenses", dependencies=[Depends(require_admin_key)], status_code=201)
async def expense_create(body: ExpenseIn, db: AsyncSession = Depends(get_db)) -> dict:
    exp = Expense(
        category=body.category,
        amount=body.amount,
        spent_on=body.spent_on,
        description=body.description,
    )
    db.add(exp)
    await db.commit()
    log.info("expense_recorded", category=body.category, amount=str(body.amount))
    return {"id": str(exp.id)}


@router.delete("/api/expenses/{expense_id}", dependencies=[Depends(require_admin_key)])
async def expense_delete(expense_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        eid = uuid_module.UUID(expense_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid expense id")
    exp = await db.get(Expense, eid)
    if exp is None:
        raise HTTPException(status_code=404, detail="expense not found")
    await db.delete(exp)
    await db.commit()
    log.info("expense_deleted", expense_id=expense_id)
    return {"deleted": expense_id}


# ---------- Reports ----------

@router.get("/api/reports/summary", dependencies=[Depends(require_admin_key)])
async def reports_summary(db: AsyncSession = Depends(get_db)) -> dict:
    """Money overview. NOTE: revenue is approximated as payments recorded on
    orders CREATED in the period (a proper payments ledger is in ROADMAP).
    """
    now = datetime.now(timezone.utc)
    today0 = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    week0 = today0 - timedelta(days=7)
    month0 = today0.replace(day=1)

    async def money_since(since: datetime) -> dict:
        row = (
            await db.execute(
                select(
                    func.coalesce(func.sum(Order.amount_paid), 0),
                    func.count(),
                ).where(Order.created_at >= since)
            )
        ).one()
        exp = (
            await db.execute(
                select(func.coalesce(func.sum(Expense.amount), 0)).where(
                    Expense.spent_on >= since.date()
                )
            )
        ).scalar_one()
        revenue, orders_count = row
        return {
            "revenue": str(revenue),
            "expenses": str(exp),
            "profit": str(Decimal(revenue) - Decimal(exp)),
            "orders": orders_count,
        }

    outstanding = (
        await db.execute(
            select(
                func.coalesce(
                    func.sum(Order.total_amount - Order.amount_paid), 0
                )
            ).where(Order.total_amount.isnot(None), Order.total_amount > Order.amount_paid)
        )
    ).scalar_one()

    return {
        "today": await money_since(today0),
        "week": await money_since(week0),
        "month": await money_since(month0),
        "outstanding_total": str(outstanding),
    }


# ---------- Settings: Rate Card ----------

class RateIn(BaseModel):
    service: str = Field(min_length=1, max_length=60)
    garment: str = Field(default="", max_length=60)
    unit: str = Field(default="pc", pattern="^(pc|kg)$")
    rate: Decimal = Field(gt=0)


class RateUpdateIn(BaseModel):
    rate: Decimal | None = Field(default=None, gt=0)
    is_active: bool | None = None


@router.get("/api/rates", dependencies=[Depends(require_admin_key)])
async def rates_list(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (
        await db.execute(select(Rate).order_by(Rate.unit, Rate.service, Rate.garment))
    ).scalars().all()
    return [
        {"id": str(r.id), "service": r.service, "garment": r.garment,
         "unit": r.unit, "rate": str(r.rate), "is_active": r.is_active}
        for r in rows
    ]


@router.post("/api/rates", dependencies=[Depends(require_admin_key)], status_code=201)
async def rate_create(body: RateIn, db: AsyncSession = Depends(get_db)) -> dict:
    rate = Rate(service=body.service.strip(), garment=body.garment.strip(),
                unit=body.unit, rate=body.rate)
    db.add(rate)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=409, detail="This service + item is already on the rate card")
    log.info("rate_created", service=body.service, garment=body.garment, rate=str(body.rate))
    return {"id": str(rate.id)}


@router.put("/api/rates/{rate_id}", dependencies=[Depends(require_admin_key)])
async def rate_update(rate_id: str, body: RateUpdateIn, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        rid = uuid_module.UUID(rate_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid rate id")
    rate = await db.get(Rate, rid)
    if rate is None:
        raise HTTPException(status_code=404, detail="rate not found")
    if body.rate is not None:
        rate.rate = body.rate
    if body.is_active is not None:
        rate.is_active = body.is_active
    await db.commit()
    log.info("rate_updated", rate_id=rate_id, rate=str(rate.rate), active=rate.is_active)
    return {"ok": True}


# ---------- Settings: Staff ----------

class StaffIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone: str
    role: str = Field(pattern="^(WASHER|DELIVERY)$")


class StaffUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    role: str | None = Field(default=None, pattern="^(WASHER|DELIVERY)$")
    is_active: bool | None = None


@router.get("/api/staff", dependencies=[Depends(require_admin_key)])
async def staff_list(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(Staff).order_by(Staff.name))).scalars().all()
    return [
        {"id": str(s.id), "name": s.name, "phone": s.phone,
         "role": s.role.name, "is_active": s.is_active}
        for s in rows
    ]


@router.post("/api/staff", dependencies=[Depends(require_admin_key)], status_code=201)
async def staff_create(body: StaffIn, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        phone = _norm_phone(body.phone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    staff = Staff(name=body.name.strip(), phone=phone, role=StaffRole[body.role])
    db.add(staff)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=409, detail="This phone number is already a staff member")
    log.info("staff_created_via_settings", name=body.name, phone=phone, role=body.role)
    return {"id": str(staff.id)}


@router.put("/api/staff/{staff_id}", dependencies=[Depends(require_admin_key)])
async def staff_update(staff_id: str, body: StaffUpdateIn, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        sid = uuid_module.UUID(staff_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid staff id")
    staff = await db.get(Staff, sid)
    if staff is None:
        raise HTTPException(status_code=404, detail="staff not found")
    if body.name is not None:
        staff.name = body.name.strip()
    if body.role is not None:
        staff.role = StaffRole[body.role]
    if body.is_active is not None:
        staff.is_active = body.is_active
    await db.commit()
    log.info("staff_updated_via_settings", staff_id=staff_id)
    return {"ok": True}


# ---------- CSV exports ----------

def _csv_response(filename: str, header: list[str], rows: list[list]) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/api/staff/{staff_id}", dependencies=[Depends(require_admin_key)])
async def staff_delete(staff_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Soft-delete: is_active=False keeps history (orders reference staff)."""
    try:
        sid = uuid_module.UUID(staff_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid staff id")
    staff = await db.get(Staff, sid)
    if staff is None:
        raise HTTPException(status_code=404, detail="staff not found")
    staff.is_active = False
    await db.commit()
    log.info("staff_deactivated", staff_id=staff_id)
    return {"deactivated": True}


@router.get("/api/export/orders.csv", dependencies=[Depends(require_admin_key)])
async def export_orders(db: AsyncSession = Depends(get_db)) -> Response:
    rows = (
        await db.execute(
            select(Order, Customer)
            .join(Customer, Customer.id == Order.customer_id)
            .order_by(Order.created_at.desc())
        )
    ).all()
    return _csv_response(
        "orders.csv",
        ["order_number", "date", "customer", "phone", "status", "items",
         "total", "discount", "gst", "paid", "payment_status", "expected_delivery"],
        [
            [o.order_number, o.created_at.date().isoformat(), cu.name or "", cu.phone,
             o.status.name,
             "; ".join(f"{i.get('qty', 1)}x {i.get('type', '?')}" for i in (o.items or [])),
             o.total_amount or "", o.discount_amount or "", o.gst_amount or "",
             o.amount_paid, o.payment_status.name,
             o.expected_delivery.isoformat() if o.expected_delivery else ""]
            for o, cu in rows
        ],
    )


@router.get("/api/export/customers.csv", dependencies=[Depends(require_admin_key)])
async def export_customers(db: AsyncSession = Depends(get_db)) -> Response:
    data = await customers_list(db)  # reuse the ledger query
    return _csv_response(
        "customers.csv",
        ["name", "phone", "total_orders", "business", "paid", "outstanding", "last_message"],
        [
            [c["name"] or "", c["phone"], c["total_orders"], c["business"],
             c["paid"], c["outstanding"], c["last_message_at"] or ""]
            for c in data
        ],
    )


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
    manager_phone = normalize_phone(settings.MANAGER_PHONE)
    for cust, conv in rows:
        threads.append(
            {
                # The owner's own number has a customer row from his tests —
                # label him as the boss, not a customer.
                "kind": "admin" if cust.phone == manager_phone else "customer",
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

    if staff:
        kind = "staff"
    elif phone == normalize_phone(settings.MANAGER_PHONE):
        kind = "admin"
    else:
        kind = "customer"
    return {
        "kind": kind,
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
            detail="The 24h window is closed — free-form messages can't be sent. Use a template or wait for the customer to message first.",
        )
    except SendError as exc:
        raise HTTPException(status_code=502, detail=f"WhatsApp send failed: {exc}")
    return {"wa_message_id": wa_id, "at": datetime.now(timezone.utc).isoformat()}


class TemplateSendIn(BaseModel):
    phone: str
    template_name: str = Field(min_length=2)
    params: list[str] = Field(default_factory=list)


@router.post("/api/inbox/send-template", dependencies=[Depends(require_admin_key)])
async def inbox_send_template(
    body: TemplateSendIn, db: AsyncSession = Depends(get_db)
) -> dict:
    """Send a pre-approved template — works even when the 24h window is shut."""
    try:
        phone = normalize_phone(body.phone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # unknown number? create the customer row so a thread exists
    cust = (
        await db.execute(select(Customer).where(Customer.phone == phone))
    ).scalar_one_or_none()
    if cust is None:
        db.add(Customer(phone=phone))
        await db.commit()
    try:
        wa_id = await send_message(
            db, to_phone=phone,
            template_name=body.template_name,
            template_params=[p[:600] for p in body.params],
            sent_by="manager",
        )
    except ValueError as exc:  # unknown template / wrong param count
        raise HTTPException(status_code=400, detail=str(exc))
    except SendError as exc:
        raise HTTPException(status_code=502, detail=f"WhatsApp send failed: {exc}")
    return {"wa_message_id": wa_id}


class NewChatIn(BaseModel):
    phone: str
    name: str | None = None


@router.post("/api/inbox/new-chat", dependencies=[Depends(require_admin_key)])
async def inbox_new_chat(body: NewChatIn, db: AsyncSession = Depends(get_db)) -> dict:
    """Create/open a thread for any number (even brand new)."""
    try:
        phone = normalize_phone(body.phone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    cust = (
        await db.execute(select(Customer).where(Customer.phone == phone))
    ).scalar_one_or_none()
    if cust is None:
        cust = Customer(phone=phone, name=(body.name or "").strip() or None)
        db.add(cust)
        await db.commit()
    elif body.name and not cust.name:
        cust.name = body.name.strip()
        await db.commit()
    return {"phone": phone, "name": cust.name}


class BulkCustomersIn(BaseModel):
    text: str = Field(min_length=3)  # lines: "number" or "name, number"


@router.post("/api/customers/bulk", dependencies=[Depends(require_admin_key)])
async def customers_bulk(body: BulkCustomersIn, db: AsyncSession = Depends(get_db)) -> dict:
    """Paste a list of numbers (one per line, 'name, number' allowed)."""
    import re as _re

    added, skipped, bad = 0, 0, []
    for raw in body.text.splitlines():
        line = raw.strip()
        if not line:
            continue
        name = None
        if "," in line:
            name, _, num = line.partition(",")
            name, line = name.strip() or None, num.strip()
        digits = _re.sub(r"[^\d+]", "", line)
        try:
            phone = normalize_phone(digits)
        except ValueError:
            bad.append(raw.strip()[:30])
            continue
        exists = (
            await db.execute(select(Customer.id).where(Customer.phone == phone))
        ).scalar_one_or_none()
        if exists:
            skipped += 1
            continue
        db.add(Customer(phone=phone, name=name))
        added += 1
    await db.commit()
    log.info("customers_bulk_import", added=added, skipped=skipped, bad=len(bad))
    return {"added": added, "skipped_existing": skipped, "invalid": bad[:10]}


@router.post("/api/inbox/send-media", dependencies=[Depends(require_admin_key)])
async def inbox_send_media(
    phone: str = Form(...),
    caption: str = Form(default=""),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Manager sends a photo from the Inbox."""
    import uuid as _uuid

    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(status_code=400, detail="Sirf image bhej sakte ho abhi")
    try:
        to_phone = normalize_phone(phone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(
        file.content_type, ".jpg"
    )
    name = f"out-{_uuid.uuid4().hex}{ext}"
    _MEDIA_DIR.mkdir(exist_ok=True)
    dest = _MEDIA_DIR / name
    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Photo 5MB se badi hai")
    dest.write_bytes(content)

    try:
        wa_id = await send_image(
            db,
            to_phone=to_phone,
            file_path=str(dest),
            mime_type=file.content_type,
            caption=caption.strip() or None,
            local_url=f"/admin/media/{name}",
            sent_by="manager",
        )
    except WindowClosedError:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail="24h window band hai — photo nahi ja sakti.")
    except SendError as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=502, detail=f"Photo send fail: {exc}")
    return {"wa_message_id": wa_id}


@router.get("/media/{name}")
async def serve_media(name: str, key: str = Query(default="")) -> FileResponse:
    """Serve chat media to the Inbox. <img> tags can't send headers, so auth
    is the admin key as a query param."""
    if key != settings.ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="key chahiye")
    # basename() guard: no traversal
    safe = Path(name).name
    path = _MEDIA_DIR / safe
    if not path.is_file():
        raise HTTPException(status_code=404, detail="media nahi mila")
    return FileResponse(path)


@router.get("")
async def dashboard_page() -> Response:
    """The dashboard page — asset links get an mtime version stamp so the
    browser can never serve a stale app.js/app.css against fresh HTML
    (that mix = dead buttons + broken styling)."""
    html = _DASHBOARD_FILE.read_text(encoding="utf-8")
    static_dir = _DASHBOARD_FILE.parent
    v = int(max(
        (static_dir / "app.js").stat().st_mtime,
        (static_dir / "app.css").stat().st_mtime,
    ))
    import re as _re

    html = _re.sub(r"(app\.(?:js|css))\?v=[\w]+", rf"\1?v={v}", html)
    return Response(
        content=html, media_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )