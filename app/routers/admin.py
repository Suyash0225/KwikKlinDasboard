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
import re
import uuid as uuid_module
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import structlog
from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import (
    CampaignRecipient,
    Conversation,
    CouponRedemption,
    Customer,
    Escalation,
    Expense,
    OpenQuestion,
    Order,
    OrderStatus,
    OrderStatusHistory,
    Payment,
    PaymentStatus,
    Rate,
    Staff,
    StaffRole,
)
from app.services import audit, pay_link, tenant_context
from app.utils.phone import normalize_phone as _norm_phone
from app.routers.orders import require_admin_key, require_admin_owner, require_feature
from app.services.order_service import ACTIVE_STATUSES, get_active_orders_for_phone
from app.services.whatsapp import SendError, WindowClosedError, send_image, send_message
from app.utils.phone import normalize_phone
from app.services.tenant_context import manager_phone

_MEDIA_DIR = Path(__file__).resolve().parent.parent / "media"

# Dukaan IST mein jeeti hai. "5 August ke bill" ka matlab wahan ka poora
# din hai, UTC ka nahi — warna subah 5:30 se pehle ke bill pichhle din
# mein gin jaate.
IST = timezone(timedelta(hours=5, minutes=30))

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
async def customers_list(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=300, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    """Customers with order counts + money ledger, recently active first.

    Paged (limit/offset) so every customer stays reachable past page one."""
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
            .limit(limit)
            .offset(offset)
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


@router.get("/api/bills", dependencies=[Depends(require_admin_key)])
async def bills_page(
    db: AsyncSession = Depends(get_db),
    q: str = Query(default="", max_length=60),
    status: str = Query(default=""),
    payment: str = Query(default=""),
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Bills ka EK page — chunaav, chhantai aur ginti sab DB par.

    Pehle ye page sabse naye 200 order utaar kar browser mein chhaanta
    tha. Ek dukaan ke shuruati mahinon tak wo chalta hai, phir do tarah se
    tootta hai: 201-wa bill kabhi milta hi nahi (na search se, na page
    badalne se), aur har baar poora 200 ka bojh phone par utarta hai.

    Ab page wahi maangta hai jo dikhana hai — 25 rows — aur `total` alag
    se aata hai taaki "Page 3 of 47" sach bata sake. Saare filter (search,
    status, payment, tareekh) DB tak jaate hain, isliye purana bill bhi
    utni hi aasani se milta hai jitna aaj ka.

    Tenant ka pehra alag se lagane ki zaroorat nahi — wo har query par
    apne aap lagta hai (loader criteria + RLS).
    """
    where = []
    term = q.strip()
    if term:
        digits = re.sub(r"\D", "", term)
        like = f"%{term}%"
        # Order number, naam, ya number — teenon ek hi khaane se
        conds = [Order.order_number.ilike(like), Customer.name.ilike(like)]
        if digits and len(digits) >= 3:
            conds.append(Customer.phone.ilike(f"%{digits}%"))
        where.append(or_(*conds))
    if status:
        try:
            where.append(Order.status == OrderStatus[status.upper()])
        except KeyError:
            raise HTTPException(status_code=400, detail=f"unknown status {status!r}")
    if payment:
        try:
            where.append(Order.payment_status == PaymentStatus[payment.upper()])
        except KeyError:
            raise HTTPException(status_code=400, detail=f"unknown payment {payment!r}")
    for raw, op in ((date_from, ">="), (date_to, "<=")):
        if not raw:
            continue
        try:
            d = date.fromisoformat(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"bad date {raw!r}")
        # Din ki seema IST mein soch kar banti hai, kyunki dukaan wahi jeeti hai
        edge = datetime.combine(d, time.min if op == ">=" else time.max, tzinfo=IST)
        where.append(Order.created_at >= edge if op == ">=" else Order.created_at <= edge)

    base = select(Order, Customer).join(Customer, Order.customer_id == Customer.id)
    if where:
        base = base.where(*where)
    total = (
        await db.execute(
            select(func.count()).select_from(
                base.with_only_columns(Order.id).order_by(None).subquery()
            )
        )
    ).scalar_one()
    rows = (
        await db.execute(base.order_by(Order.created_at.desc()).limit(limit).offset(offset))
    ).all()
    return {
        "total": total,
        "items": [
            {
                "order_number": o.order_number,
                "created_at": o.created_at.isoformat() if o.created_at else None,
                "customer_name": c.name,
                "customer_phone": c.phone,
                "items": o.items,
                "total_amount": str(o.total_amount) if o.total_amount is not None else None,
                "amount_paid": str(o.amount_paid or 0),
                "status": o.status.name,
                "payment_status": o.payment_status.name,
                "expected_delivery": (
                    o.expected_delivery.isoformat() if o.expected_delivery else None
                ),
                "priority": o.priority,
            }
            for o, c in rows
        ],
    }


@router.get("/api/events", dependencies=[Depends(require_admin_key)])
async def admin_events(request: Request) -> StreamingResponse:
    """Live updates ka connection — dashboard khud ko taaza rakhta hai.

    Yahi wo cheez hai jiske na hone se "Ajit ne bill banaya par dashboard
    par dikha hi nahi" hota tha. Data hamesha sahi tha; khuli hui page ne
    dobara poocha hi nahi tha.

    Is route par jaan-boojh kar `Depends(get_db)` NAHI hai. Wo session poore
    stream ke waqt tak — yani ghanton — pakda rehta, aur do-chaar dashboard
    khulte hi pool khali ho jaata; baaki har request wahin ruk jaati. Tenant
    middleware se aa chuka hota hai, isliye yahan DB ki zaroorat hi nahi.
    """
    from app.services import events, tenant_context

    tid = tenant_context.current_tenant_id.get()
    return StreamingResponse(
        events.stream(tid, request.is_disconnected),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # nginx/cloudflared ko: is stream ko buffer mat karo, warna
            # khabar tabhi pahunchti hai jab kaafi jama ho jaye
            "X-Accel-Buffering": "no",
        },
    )


class ReminderIn(BaseModel):
    phone: str = Field(min_length=8, max_length=20)


@router.post("/api/customers/reminder", dependencies=[Depends(require_admin_key)])
async def customer_reminder(body: ReminderIn, db: AsyncSession = Depends(get_db)) -> dict:
    """Ek grahak ka paisa maangne wala message — banao, aur ho sake to bhejo.

    Text SERVER par banta hai, browser mein nahi. Browser ke paas sirf kul
    rakam hoti hai; kaunse bill, kis din ke, kitne ke — wo yahan hai. Aur
    jab tak text browser mein tha, usmein dukaan ka naam hardcode tha
    ("Kwik Klin"), yaani har doosri dukaan kisi aur ke naam se paisa maang
    rahi thi.

    WhatsApp API se ja sake to bhej dete hain; na ja sake to text wapas
    kar dete hain taaki dashboard wa.me link khol sake. Paisa maangna wo
    kaam hai jo Meta ka integration set na hone par rukna nahi chahiye.
    """
    from app.models.tenant import Tenant as _T
    from app.services import app_settings, tenant_context
    from app.services.whatsapp import SendError, send_message

    phone = body.phone.strip()
    cust = (
        await db.execute(select(Customer).where(Customer.phone == phone))
    ).scalar_one_or_none()
    if cust is None:
        raise HTTPException(status_code=404, detail="Customer not found")

    rows = (
        await db.execute(
            select(Order)
            .where(
                Order.customer_id == cust.id,
                Order.status != OrderStatus.CANCELLED,
                Order.total_amount.isnot(None),
                Order.total_amount > Order.amount_paid,
            )
            .order_by(Order.created_at)
        )
    ).scalars().all()
    if not rows:
        raise HTTPException(status_code=400, detail="Is grahak ka koi paisa baaki nahi")

    def rupees(x: Decimal | float) -> str:
        # "₹630.00" nahi. Dukaan ke message mein paise ka hisaab paison
        # tak nahi hota, aur .00 machine ka likha lagta hai.
        d = Decimal(str(x)).quantize(Decimal("0.01"))
        return f"₹{d:,.0f}" if d == d.to_integral_value() else f"₹{d:,.2f}"

    tenant = await db.get(_T, tenant_context.current_tenant_id.get())
    shop = (tenant.shop_name if tenant and tenant.shop_name else "").strip()
    total = sum((Decimal(str(o.total_amount)) - Decimal(str(o.amount_paid or 0))) for o in rows)

    who = (cust.name or "").strip()
    lines = [f"Dear {who}," if who else "Dear Customer,", ""]
    lines.append(
        f"Payment is pending for {len(rows)} of your bills:" if len(rows) > 1
        else "Payment is pending for your bill:"
    )
    lines.append("")
    # Lambi list WhatsApp par deewar ban jaati hai. Chhe dikhao, baaki gino.
    for o in rows[:6]:
        due = Decimal(str(o.total_amount)) - Decimal(str(o.amount_paid or 0))
        day = o.created_at.strftime("%d %b %Y") if o.created_at else "-"
        lines.append(f"{o.order_number} · {day} · {rupees(due)}")
    if len(rows) > 6:
        lines.append(f"...and {len(rows) - 6} more")

    lines += ["", f"Total due: {rupees(total)}", ""]

    # UPI Settings -> Business Profile se aata hai (upi_vpa / upi_payee) —
    # wahi do keys jo bill ke receipt par chhapti hain. Number saamne ho to
    # paisa aaj hi aa sakta hai; na ho to grahak ko poochhna padta hai aur
    # wahin ruk jaata hai.
    upi = (await app_settings.get(db, "upi_vpa") or "").strip()
    payee = (await app_settings.get(db, "upi_payee") or "").strip()
    if upi:
        # Link https par jaata hai kyunki WhatsApp SIRF http/https ko tap-able
        # banata hai. `upi://` seedha likhne par wo plain text rehta hai —
        # lamba, badsurat, aur phir bhi tap nahi hota. Ye link kholte hi
        # /pay wala page upi:// fire karta hai aur GPay/PhonePe/Paytm khulta
        # hai, amount bhara hua. Paisa seedha dukaan ke VPA mein — beech
        # mein koi gateway nahi.
        base = (await app_settings.get(db, "public_base_url") or "").strip().rstrip("/")
        tid = tenant_context.current_tenant_id.get() or tenant_context.cached_home_tenant_id()
        if base and tid is not None:
            link = f"{base}/pay/{pay_link.make(tid, total)}"
            lines.append(f"Pay {rupees(total)}: {link}")
        # VPA hamesha saath mein. Link kaam na kare — public URL badla ho,
        # ya iPhone par UPI app na khule — to grahak phir bhi paisa bhej
        # sakta hai. Yahi wo halat hai jismein wo abhi tak kaam chalata tha.
        lines.append(f"UPI: {upi}" + (f" ({payee})" if payee else ""))
        lines.append("Or pay at the shop.")
    else:
        lines.append("Payment can be made at the shop.")

    # Dukaan ka naam sign-off mein. Ye owner ka chuna hua roop hai — ek
    # business letter ki tarah, jahan naam neeche aata hai.
    lines += ["", "Please pay as soon as possible.", "", "Thank you,", shop or "Laundry"]

    text = "\n".join(lines)

    sent = False
    try:
        await send_message(db, to_phone=cust.phone, text=text)
        sent = True
    except SendError as exc:
        log.info("dashboard_reminder_api_failed", customer=cust.id, error=str(exc))

    return {
        "sent": sent,
        "phone": cust.phone,
        "name": cust.name or "",
        "bills": len(rows),
        "total": str(total),
        "text": text,
    }


@router.get("/api/customers/search", dependencies=[Depends(require_admin_key)])
async def customers_search(
    db: AsyncSession = Depends(get_db),
    q: str = Query(default="", max_length=60),
    limit: int = Query(default=8, ge=1, le=25),
) -> list[dict]:
    """Naam ya number ka tukda -> chand milte-julte customer. Bill ke liye.

    Ye kaam pehle BROWSER karta tha: poori customer list utar kar wahin
    filter. Ek dukaan aur teen sau customer tak wo chalta hai; pandrah
    dukaanon aur lakhon rows par wo do tarah se tootta hai — page bhaari ho
    jaata hai, aur jo customer pehle 300 mein nahi tha wo mila hi nahi
    karta tha (staff ko lagta tha "naya customer hai", aur duplicate ban
    jaata tha).

    Ab chunaav DB karta hai: tenant ka pehra RLS/loader-criteria se apne
    aap lagta hai, aur `limit` DB tak jaata hai — network par sirf aath
    row aati hain, chahe customer paanch lakh hon.

    Number likha ho to number se, warna naam se. Dono par index hai
    (ix_customers_tenant_phone_prefix / ix_customers_name_trgm).
    """
    term = q.strip()
    if len(term) < 2:
        return []       # ek akshar par poori dukaan lautana bekaar hai
    digits = re.sub(r"\D", "", term)
    if digits and len(digits) >= 3:
        # Number ka tukda: aage se bhi mile aur beech se bhi (log 98765...
        # bhi likhte hain aur +91 98765... bhi)
        where = Customer.phone.ilike(f"%{digits}%")
    else:
        where = Customer.name.ilike(f"%{term}%")
    rows = (
        await db.execute(
            select(Customer)
            .where(where, Customer.is_active)
            .order_by(Customer.last_message_at.desc().nulls_last())
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "name": c.name,
            "phone": c.phone,
            "address": c.address,
            "last_message_at": c.last_message_at.isoformat() if c.last_message_at else None,
        }
        for c in rows
    ]


class CustomerEditIn(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, min_length=6, max_length=20)
    address: str | None = Field(default=None, max_length=400)


async def _customer_by_phone(db: AsyncSession, phone: str) -> Customer:
    try:
        norm = _norm_phone(phone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    cust = (
        await db.execute(select(Customer).where(Customer.phone == norm))
    ).scalar_one_or_none()
    if cust is None:
        raise HTTPException(status_code=404, detail=f"{norm} koi customer nahi hai")
    return cust


@router.put("/api/customers/{phone}", dependencies=[Depends(require_admin_key)])
async def customer_edit(
    phone: str, body: CustomerEditIn, db: AsyncSession = Depends(get_db)
) -> dict:
    """Fix a customer's name, number or address."""
    cust = await _customer_by_phone(db, phone)
    if body.name is not None:
        cust.name = body.name.strip() or None
    if body.address is not None:
        cust.address = body.address.strip() or None
    if body.phone is not None:
        try:
            new_phone = _norm_phone(body.phone)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if new_phone != cust.phone:
            clash = (
                await db.execute(
                    select(Customer).where(Customer.phone == new_phone, Customer.id != cust.id)
                )
            ).scalar_one_or_none()
            if clash is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"{new_phone} pehle se {clash.name or 'ek customer'} ka number hai",
                )
            cust.phone = new_phone
    await db.commit()
    await audit.record(
        actor_role="admin", actor="dashboard", action="customer_edited",
        args={"phone": cust.phone}, result=f"name={cust.name}",
    )
    log.info("customer_edited", phone=cust.phone)
    return {"ok": True, "phone": cust.phone}


@router.delete("/api/customers/{phone}", dependencies=[Depends(require_admin_owner)])
async def customer_delete(
    phone: str,
    db: AsyncSession = Depends(get_db),
    force: bool = Query(default=False, description="also delete their orders"),
) -> dict:
    """Delete a customer. Refuses to silently take their order history with
    them: if they have bills, the caller must pass force=true (the UI makes
    the owner type the name and shows exactly what will go)."""
    cust = await _customer_by_phone(db, phone)
    order_ids = (
        (await db.execute(select(Order.id).where(Order.customer_id == cust.id)))
        .scalars()
        .all()
    )
    if order_ids and not force:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{cust.name or cust.phone} ke {len(order_ids)} bill hain. "
                "Delete karne par wo bhi chale jayenge."
            ),
        )

    if order_ids:
        await db.execute(delete(Payment).where(Payment.order_id.in_(order_ids)))
        await db.execute(
            delete(OrderStatusHistory).where(OrderStatusHistory.order_id.in_(order_ids))
        )
        await db.execute(
            delete(CouponRedemption).where(CouponRedemption.order_id.in_(order_ids))
        )
    await db.execute(delete(CouponRedemption).where(CouponRedemption.customer_id == cust.id))
    await db.execute(delete(CampaignRecipient).where(CampaignRecipient.customer_id == cust.id))
    await db.execute(delete(OpenQuestion).where(OpenQuestion.customer_id == cust.id))
    await db.execute(delete(Escalation).where(Escalation.customer_id == cust.id))
    # conversations need a participant (XOR constraint) — they go too
    await db.execute(delete(Conversation).where(Conversation.customer_id == cust.id))
    await db.execute(delete(Order).where(Order.customer_id == cust.id))
    name, ph = cust.name, cust.phone
    await db.delete(cust)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        log.exception("customer_delete_failed", phone=ph)
        raise HTTPException(
            status_code=409, detail="Purana record juda hua hai — delete nahi ho paya."
        )

    await audit.record(
        actor_role="admin", actor="dashboard", action="customer_deleted",
        args={"phone": ph, "orders": len(order_ids)},
        result=f"{name or ph} deleted",
    )
    log.info("customer_deleted", phone=ph, orders=len(order_ids))
    return {"ok": True, "deleted": name or ph, "orders_deleted": len(order_ids)}


# ---------- Expenses ----------

class ExpenseIn(BaseModel):
    category: str = Field(min_length=1, max_length=60)
    amount: Decimal = Field(gt=0)
    spent_on: date
    description: str | None = Field(default=None, max_length=300)


@router.get("/api/expenses", dependencies=[Depends(require_admin_owner)])
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


@router.post("/api/expenses", dependencies=[Depends(require_admin_owner)], status_code=201)
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


@router.delete("/api/expenses/{expense_id}", dependencies=[Depends(require_admin_owner)])
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

@router.get("/api/reports/summary", dependencies=[Depends(require_admin_owner), Depends(require_feature("reports"))])
async def reports_summary(db: AsyncSession = Depends(get_db)) -> dict:
    """Money overview. NOTE: revenue is approximated as payments recorded on
    orders CREATED in the period (a proper payments ledger is in ROADMAP).
    """
    now = datetime.now(timezone.utc)
    today0 = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    week0 = today0 - timedelta(days=7)
    month0 = today0.replace(day=1)

    # last calendar month, as a closed [start, end) range — powers the
    # "vs last month" indicators on the Expenses KPIs
    last_month_end = month0
    last_month_start = (month0 - timedelta(days=1)).replace(day=1)

    async def money_between(since: datetime, until: datetime | None = None) -> dict:
        ocond = [Order.created_at >= since]
        econd = [Expense.spent_on >= since.date()]
        if until is not None:
            ocond.append(Order.created_at < until)
            econd.append(Expense.spent_on < until.date())
        row = (
            await db.execute(
                select(
                    func.coalesce(func.sum(Order.amount_paid), 0),
                    func.count(),
                ).where(*ocond)
            )
        ).one()
        exp = (
            await db.execute(
                select(func.coalesce(func.sum(Expense.amount), 0)).where(*econd)
            )
        ).scalar_one()
        revenue, orders_count = row
        return {
            "revenue": str(revenue),
            "expenses": str(exp),
            "profit": str(Decimal(revenue) - Decimal(exp)),
            "orders": orders_count,
        }

    async def money_since(since: datetime) -> dict:
        return await money_between(since)

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
        "last_month": await money_between(last_month_start, last_month_end),
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


def _canonical_name(existing: list[str], name: str) -> str:
    """Jo spelling pehle se card par hai, wahi wapas do.

    Dukaandar "shirt" type karta hai, card par "Shirt" hai. DB ka unique
    constraint case dekhta hai, aadmi nahi — bina iske do alag rows ban
    jaati hain aur matrix mein ek hi kapde ki do lines dikhti hain.
    """
    key = name.strip().casefold()
    for e in existing:
        if e.strip().casefold() == key:
            return e.strip()
    return name.strip()


@router.post("/api/rates", dependencies=[Depends(require_admin_owner)], status_code=201)
async def rate_create(body: RateIn, db: AsyncSession = Depends(get_db)) -> dict:
    service = body.service.strip()
    garment = body.garment.strip()
    if not service:
        raise HTTPException(status_code=400, detail="Service name is needed")
    if body.unit == "pc" and not garment:
        raise HTTPException(
            status_code=400, detail="Per-piece rate needs a garment name"
        )

    rows = (await db.execute(select(Rate))).scalars().all()
    service = _canonical_name([r.service for r in rows], service)
    garment = _canonical_name([r.garment for r in rows if r.garment], garment)

    dupe = next(
        (
            r
            for r in rows
            if r.service.strip().casefold() == service.casefold()
            and r.garment.strip().casefold() == garment.casefold()
        ),
        None,
    )
    if dupe is not None:
        if dupe.is_active:
            label = f"{garment} ({service})" if garment else service
            raise HTTPException(
                status_code=409,
                detail=f"{label} is already on the rate card at ₹{dupe.rate}",
            )
        # Band padi row ko zinda karo. Nayi row banate to unique constraint
        # waise bhi todti, aur user ko 409 milta jabki uski nazar mein wo
        # cheez card par hai hi nahi.
        dupe.rate = body.rate
        dupe.unit = body.unit
        dupe.is_active = True
        await db.commit()
        log.info("rate_reactivated", service=service, garment=garment, rate=str(body.rate))
        return {"id": str(dupe.id), "reactivated": True}

    rate = Rate(service=service, garment=garment, unit=body.unit, rate=body.rate)
    db.add(rate)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=409, detail="This service + item is already on the rate card")
    log.info("rate_created", service=service, garment=garment, rate=str(body.rate))
    return {"id": str(rate.id)}


@router.put("/api/rates/{rate_id}", dependencies=[Depends(require_admin_owner)])
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


# Purane bill par asar kyun nahi padta: staff_panel.bill_create har line ka
# daam us waqt hi orders.items (JSONB) mein likh deta hai — garment, service,
# qty, rate, amount, aur override hua to card_rate bhi. Bill kabhi rate_card
# ko dobara nahi padhta. Isliye yahan se row hatana safe hai: sirf naye bill
# ke dropdown se wo option gayab hota hai, purana bill jaisa tha waisa hi
# chhapta rahega.

@router.delete("/api/rates/{rate_id}", dependencies=[Depends(require_admin_owner)])
async def rate_delete(rate_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Ek cell (service × garment) ko rate card se poori tarah hata do."""
    try:
        rid = uuid_module.UUID(rate_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid rate id")
    rate = await db.get(Rate, rid)
    if rate is None:
        raise HTTPException(status_code=404, detail="rate not found")
    service, garment = rate.service, rate.garment
    await db.delete(rate)
    await db.commit()
    log.info("rate_deleted", rate_id=rate_id, service=service, garment=garment)
    return {"ok": True, "deleted": 1}


@router.delete("/api/rates", dependencies=[Depends(require_admin_owner)])
async def rate_delete_group(
    garment: str = Query(default="", max_length=60),
    service: str = Query(default="", max_length=60),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Poora kapda (row) ya poori service (column) ek saath hatao.

    Naam se chalta hai, id se nahi — matrix mein ek garment ki kai rows
    hoti hain (har service ke liye ek). Match case-insensitive hai, warna
    "Shirt" delete karne par "shirt" peeche reh jaata.
    """
    g, s = garment.strip().casefold(), service.strip().casefold()
    if not g and not s:
        raise HTTPException(
            status_code=400, detail="Tell me which garment or service to remove"
        )

    # Select pehle: tenant filter ORM SELECT par lagta hai (app/database.py
    # ka do_orm_execute), to id nikaal kar delete karna doosri dukaan ki row
    # chhoone ke khatre ko poori tarah khatam kar deta hai.
    rows = (await db.execute(select(Rate))).scalars().all()
    hits = [
        r for r in rows
        if (not g or r.garment.strip().casefold() == g)
        and (not s or r.service.strip().casefold() == s)
    ]
    if not hits:
        raise HTTPException(status_code=404, detail="Nothing on the rate card by that name")

    await db.execute(delete(Rate).where(Rate.id.in_([r.id for r in hits])))
    await db.commit()
    log.info("rate_group_deleted", garment=garment, service=service, count=len(hits))
    return {"ok": True, "deleted": len(hits)}


# ---------- Settings: Staff ----------

class StaffIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone: str
    role: str = Field(pattern="^(WASHER|DELIVERY|SUPERVISOR|MANAGER|ADMIN)$")


class StaffUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    phone: str | None = Field(default=None, min_length=6, max_length=20)
    role: str | None = Field(default=None, pattern="^(WASHER|DELIVERY|SUPERVISOR|MANAGER|ADMIN)$")
    is_active: bool | None = None


async def _active_order_count(db: AsyncSession, staff_id: uuid_module.UUID) -> int:
    """Orders still in flight that this person is on the hook for."""
    return (
        await db.execute(
            select(func.count())
            .select_from(Order)
            .where(
                or_(
                    Order.assigned_washer_id == staff_id,
                    Order.assigned_delivery_id == staff_id,
                ),
                Order.status.in_(ACTIVE_STATUSES),
            )
        )
    ).scalar_one()


async def _customer_clash(db: AsyncSession, phone: str) -> str | None:
    """Ye number kisi customer ka bhi to nahi?

    Number badalna owner ka haq hai — isliye ye rokta nahi, sirf batata hai.
    Wajah: ek hi number staff aur customer dono ho, to uske WhatsApp message
    STAFF ke roop mein handle hote hain — customer wala AI jawab nahi milta.
    Ye chup-chaap hota tha (Kiran ka message bina reply ke reh gaya tha).
    """
    row = (
        await db.execute(select(Customer).where(Customer.phone == phone))
    ).scalar_one_or_none()
    if row is None:
        return None
    who = row.name or phone
    return (
        f"Dhyan dein: {phone} customer list mein bhi hai ({who}). "
        "Is number se aane wale message ab STAFF ke message maane jayenge, "
        "customer ka AI reply nahi milega."
    )


def _staff_ready_note(staff: Staff) -> str:
    """Save ke turant baad saaf-saaf batao ki agent ab kya kar sakta hai.

    Number badalna aasan hona chahiye — aur uske baad owner ko andaaza
    lagana na pade ki system ne pakda ya nahi. Lookup har baar DB se hota
    hai (koi cache nahi), isliye pehchan turant hai; sirf WhatsApp ki 24
    ghante wali window ka farq batana zaroori hai.
    """
    role = {
        "WASHER": "Washerman", "DELIVERY": "Delivery",
        "SUPERVISOR": "Washerman / Manager", "MANAGER": "Manager", "ADMIN": "Admin",
    }.get(staff.role.name, staff.role.name)
    if not staff.is_active:
        return f"{staff.name} ({role}) band hai — agent inhe kaam nahi bhejega."
    who = f"{staff.name} ({role}) ab {staff.phone} par hai. Agent turant pehchan lega"
    if staff.last_message_at is None:
        return (
            f"{who}; pehla message approved template se jayega. "
            "Wo ek baar WhatsApp par likh denge to normal chat chalu."
        )
    return f"{who} — WhatsApp chat abhi khuli hai."


@router.get("/api/staff", dependencies=[Depends(require_admin_key)])
async def staff_list(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """Active first, then inactive, alphabetical within each group."""
    rows = (
        await db.execute(select(Staff).order_by(Staff.is_active.desc(), Staff.name))
    ).scalars().all()
    from app.services import app_settings

    default_washer = await app_settings.get(db, "default_washer_phone")
    default_delivery = await app_settings.get(db, "default_delivery_phone")
    # Jo number customer list mein bhi hai — uske message staff ke maane
    # jate hain, customer ka AI jawab band. Ye sirf save ke waqt batana
    # kaafi nahi; list mein hamesha dikhna chahiye.
    also_customer = set(
        (
            await db.execute(
                select(Customer.phone).where(
                    Customer.phone.in_([s.phone for s in rows] or [""])
                )
            )
        ).scalars().all()
    )
    out = []
    for s in rows:
        out.append(
            {
                "id": str(s.id), "name": s.name, "phone": s.phone,
                "role": s.role.name, "is_active": s.is_active,
                # the UI needs these to explain WHY delete is blocked
                "active_orders": await _active_order_count(db, s.id),
                "is_default": s.phone in (default_washer, default_delivery),
                "also_customer": s.phone in also_customer,
                # panel login hai ya nahi — Settings mein button isi se badalta hai
                "has_login": bool(s.password_hash),
            }
        )
    return out


@router.post("/api/staff", dependencies=[Depends(require_admin_owner)], status_code=201)
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
    return {
        "id": str(staff.id),
        "ready": _staff_ready_note(staff),
        "warning": await _customer_clash(db, phone),
    }


@router.put("/api/staff/{staff_id}", dependencies=[Depends(require_admin_owner)])
async def staff_update(staff_id: str, body: StaffUpdateIn, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        sid = uuid_module.UUID(staff_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid staff id")
    staff = await db.get(Staff, sid)
    if staff is None:
        raise HTTPException(status_code=404, detail="staff not found")
    if body.name is not None:
        name = body.name.strip()
        if len(name) < 2:
            raise HTTPException(status_code=400, detail="Name must be at least 2 characters")
        staff.name = name
    warning: str | None = None
    if body.phone is not None:
        try:
            phone = _norm_phone(body.phone)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if phone != staff.phone:
            warning = await _customer_clash(db, phone)
            clash = (
                await db.execute(select(Staff).where(Staff.phone == phone, Staff.id != sid))
            ).scalar_one_or_none()
            if clash is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"{phone} is already {clash.name}'s number",
                )
            old_phone = staff.phone
            staff.phone = phone
            # 24-ghante ki WhatsApp window NUMBER ki hoti hai, insaan ki
            # nahi. Purane number ka waqt naye number par rakh dete to
            # system samajhta "window khuli hai", free-form message bhejta,
            # Meta use thukra deta aur wo chupchaap retry-queue mein pada
            # rehta. Reset karne se pehla message seedhe approved template
            # se jata hai — turant pahunchta hai.
            staff.last_message_at = None
            # keep the default-washer/delivery settings pointing at them
            from app.services import app_settings

            for key in ("default_washer_phone", "default_delivery_phone"):
                if await app_settings.get(db, key) == old_phone:
                    await app_settings.set_value(db, key, phone)
            log.info(
                "staff_phone_changed", staff=staff.name,
                old=old_phone, new=phone,
            )
    if body.role is not None:
        staff.role = StaffRole[body.role]
    if body.is_active is not None:
        staff.is_active = body.is_active
    await db.commit()
    log.info("staff_updated_via_settings", staff_id=staff_id)
    return {"ok": True, "ready": _staff_ready_note(staff), "warning": warning}


class PanelAccessIn(BaseModel):
    role: str | None = Field(default=None, pattern="^(WASHER|DELIVERY|SUPERVISOR|MANAGER)$")


@router.post("/api/staff/{staff_id}/access", dependencies=[Depends(require_admin_owner)])
async def grant_panel_access(
    staff_id: str, body: PanelAccessIn, db: AsyncSession = Depends(get_db)
) -> dict:
    """Staff ko panel ka login do (ya password reset karo).

    Staff KHUD account nahi bana sakta — ye jaan-boojh kar owner ke haath
    mein hai. Password ek baar dikhta hai aur hashed hi store hota hai;
    pehli baar login par usse badalna padta hai.

    Seat limit plan se aati hai (plans.effective_limits) — wahi ek jagah
    jise billing, quota aur ye sab padhte hain.
    """
    from app.models.tenant import Tenant as _T
    from app.services import auth as auth_service
    from app.services import plans, staff_auth, tenant_context

    try:
        sid = uuid_module.UUID(staff_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid staff id")
    staff = await db.get(Staff, sid)
    if staff is None:
        raise HTTPException(status_code=404, detail="staff not found")

    tid = tenant_context.current_tenant_id.get()
    tenant = (await db.get(_T, tid)) if tid else await auth_service.home_tenant(db)
    plan_code = tenant.plan if tenant else plans.DEFAULT_PLAN
    if not plans.feature_on(plan_code, "staff_panel"):
        raise HTTPException(status_code=402, detail="Is plan mein staff panel nahi hai")

    # Seat: sirf wo log ginte hain jinke paas panel login hai
    limit = plans.effective_limits(tenant).get("max_staff")
    if limit is not None and staff.password_hash is None:
        used = (
            await db.execute(
                select(func.count()).select_from(Staff).where(Staff.password_hash.is_not(None))
            )
        ).scalar_one()
        if used >= limit:
            need = plans.plan_with_feature("staff_roles")
            raise HTTPException(
                status_code=402,
                detail=(
                    f"{plans.get(plan_code).name} plan mein {limit} staff login "
                    f"ho sakte hain. Aur chahiye to {plans.get(need).name} lijiye."
                ),
            )

    if body.role and staff.role is not StaffRole.ADMIN:
        # Role ka batwara upar ke plan ki suvidha hai
        if body.role != "WASHER" and not plans.feature_on(plan_code, "staff_roles"):
            raise HTTPException(
                status_code=402,
                detail="Role ka batwara Premium plan se milta hai",
            )
        staff.role = StaffRole[body.role]
    # ADMIN ko yahan se KABHI utara nahi jaata.
    #
    # Is modal ke dropdown mein ADMIN hai hi nahi (owner koi "role" nahi
    # hai, wo maalik hai). Owner ne khud ko panel login diya to wahi
    # dropdown use chup-chaap MANAGER bana deta tha — aur uske saath hi
    # team.is_admin_phone() ne unhe pehchanna band kar diya, yani owner
    # ke apne WhatsApp par order/payment ki khabar aani band. Login dena
    # aur owner ka darja chheenna do alag baatein hain; ye endpoint sirf
    # pehla kaam karta hai.

    temp = auth_service.temp_password()
    await staff_auth.set_password(db, staff, temp, temp=True)
    await staff_auth.revoke_all(db, staff.id)   # purane phone ke session khatam
    await audit.record(
        actor_role="admin", actor="dashboard", action="staff_panel_access_granted",
        args={"staff": staff.name, "role": staff.role.name}, result="temp password issued",
    )
    return {
        "ok": True,
        "name": staff.name,
        "role": staff.role.name,
        # Ek baar dikhega — DB mein sirf hash jaata hai
        "temp_password": temp,
        "login_url": "/staff",
    }


class ShareAccessIn(BaseModel):
    # Wahi password jo abhi issue hua — server ise hash se milaakar hi bhejta hai
    password: str = Field(min_length=6, max_length=64)


@router.post("/api/staff/{staff_id}/access/share", dependencies=[Depends(require_admin_owner)])
async def share_panel_access(
    staff_id: str, body: ShareAccessIn, db: AsyncSession = Depends(get_db)
) -> dict:
    """Abhi bane hue login creds staff ke WhatsApp par bhej do.

    Owner ko password haath se likhkar bhejna na pade — ek tap mein chala
    jaye. Teen baatein jaan-boojh kar aisi hain:

    1. Password body mein AATA hai, par server use hash se milaata hai.
       Warna ye endpoint "kisi bhi staff ko koi bhi text bhejo" ban jaata.
    2. Sirf TEMP password share hota hai (must_change_password). Staff ne
       apna khud ka rakh liya, to wo hum kabhi nahi bhejenge.
    3. Hamare apne message log mein password NAHI jaata (log_as), aur
       transient fail par queue mein bhi nahi (enqueue_on_fail=False).
       WhatsApp use le jayega — hamari database nahi.
    """
    from app.models.tenant import Tenant as _T
    from app.services import auth as auth_service
    from app.services import google_auth, tenant_context

    try:
        sid = uuid_module.UUID(staff_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid staff id")
    staff = await db.get(Staff, sid)
    if staff is None:
        raise HTTPException(status_code=404, detail="staff not found")
    if not staff.phone:
        raise HTTPException(status_code=400, detail="This staff member has no phone number")
    if not staff.password_hash or not staff.must_change_password:
        raise HTTPException(
            status_code=409,
            detail="Nothing to share — create a fresh password first",
        )
    if not auth_service.verify_password(body.password, staff.password_hash):
        # Purani modal khuli reh gayi aur beech mein naya password ban gaya
        raise HTTPException(
            status_code=409,
            detail="That password is no longer current — create a new one",
        )

    tid = tenant_context.current_tenant_id.get()
    tenant = (await db.get(_T, tid)) if tid else None
    shop = (tenant.shop_name if tenant and tenant.shop_name else "Kwik Klin")
    base = (await google_auth.public_base(db)).rstrip("/")
    url = f"{base}/staff"

    text = (
        f"{staff.name}, your {shop} work panel is ready.\n\n"
        f"Open: {url}\n"
        f"Number: {staff.phone}\n"
        f"Password: {body.password}\n\n"
        "Log in and set your own password the first time. "
        "Please don't forward this message."
    )
    # Log mein sirf ye jaayega — password kabhi nahi
    redacted = f"[panel login sent to {staff.name} — password hidden]"

    try:
        await send_message(
            db, to_phone=staff.phone, text=text, sent_by="manager",
            log_as=redacted, enqueue_on_fail=False,
        )
        how = "text"
    except WindowClosedError:
        try:
            await send_message(
                db, to_phone=staff.phone,
                template_name="kk_staff_alert",
                template_params=[" ".join(text.split())[:600]],
                sent_by="manager", log_as=redacted, enqueue_on_fail=False,
            )
            how = "template"
        except SendError as exc:
            log.warning("panel_creds_share_failed", staff=staff.name)
            raise HTTPException(
                status_code=502,
                detail=f"Could not send on WhatsApp ({exc}) — copy it and send by hand",
            ) from exc
    except SendError as exc:
        log.warning("panel_creds_share_failed", staff=staff.name)
        raise HTTPException(
            status_code=502,
            detail=f"Could not send on WhatsApp ({exc}) — copy it and send by hand",
        ) from exc

    await audit.record(
        actor_role="admin", actor="dashboard", action="staff_panel_creds_shared",
        args={"staff": staff.name, "to": staff.phone}, result=f"sent via {how}",
    )
    log.info("panel_creds_shared", staff=staff.name, how=how)
    return {"ok": True, "to": staff.phone, "how": how}


@router.post("/api/staff/{staff_id}/access/revoke", dependencies=[Depends(require_admin_owner)])
async def revoke_panel_access(staff_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Panel access wapas lo — phone kho gaya ya aadmi chala gaya."""
    from app.services import staff_auth

    try:
        sid = uuid_module.UUID(staff_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid staff id")
    staff = await db.get(Staff, sid)
    if staff is None:
        raise HTTPException(status_code=404, detail="staff not found")
    staff.password_hash = None
    db.add(staff)
    await db.commit()
    killed = await staff_auth.revoke_all(db, staff.id)
    await audit.record(
        actor_role="admin", actor="dashboard", action="staff_panel_access_revoked",
        args={"staff": staff.name}, result=f"{killed} sessions killed",
    )
    return {"ok": True, "sessions_killed": killed}


@router.delete("/api/staff/{staff_id}/permanent", dependencies=[Depends(require_admin_owner)])
async def staff_delete_permanent(
    staff_id: str, db: AsyncSession = Depends(get_db)
) -> dict:
    """Hard delete. Refused while they still own active work — the orders
    would lose their assignee. Deactivate is always available instead."""
    try:
        sid = uuid_module.UUID(staff_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid staff id")
    staff = await db.get(Staff, sid)
    if staff is None:
        raise HTTPException(status_code=404, detail="staff not found")

    n = await _active_order_count(db, sid)
    if n:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{staff.name} is assigned to {n} active order"
                f"{'s' if n != 1 else ''}. Reassign or complete those orders first."
            ),
        )

    from app.services import app_settings

    for key in ("default_washer_phone", "default_delivery_phone"):
        if await app_settings.get(db, key) == staff.phone:
            await app_settings.set_value(db, key, "")

    # history rows point at this staff member — detach, don't cascade-delete
    await db.execute(
        update(Order)
        .where(Order.assigned_washer_id == sid)
        .values(assigned_washer_id=None)
    )
    await db.execute(
        update(Order)
        .where(Order.assigned_delivery_id == sid)
        .values(assigned_delivery_id=None)
    )
    # Tasks BUSINESS ka record hain — aadmi jaane par wo mitne nahi chahiye.
    # Unka naam hata dete hain, kaam ka itihaas rehne dete hain. Yahi wo
    # kadam tha jo chhoot gaya tha: FK tootti thi aur owner ko sirf
    # "purana record juda hua hai" dikh kar delete ruk jata tha.
    from app.models import Task as _Task

    await db.execute(
        update(_Task).where(_Task.assigned_staff_id == sid).values(assigned_staff_id=None)
    )
    # conversations require a participant (XOR check constraint), so their
    # chat history goes with them — that is what a hard delete means here.
    await db.execute(delete(Conversation).where(Conversation.staff_id == sid))
    await db.execute(delete(Escalation).where(Escalation.staff_id == sid))
    name = staff.name
    await db.delete(staff)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"{name} ka purana record juda hua hai — Deactivate kar dijiye.",
        )
    log.info("staff_deleted_permanently", staff_id=staff_id, name=name)
    return {"ok": True, "deleted": name}


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


@router.delete("/api/staff/{staff_id}", dependencies=[Depends(require_admin_owner)])
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


@router.get("/api/export/orders.csv", dependencies=[Depends(require_admin_owner), Depends(require_feature("reports"))])
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


@router.get("/api/export/customers.csv", dependencies=[Depends(require_admin_owner), Depends(require_feature("reports"))])
async def export_customers(db: AsyncSession = Depends(get_db)) -> Response:
    # reuse the ledger query — export means ALL customers, not one page
    data = await customers_list(db, limit=1_000_000, offset=0)
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


@router.get("/api/inbox/threads", dependencies=[Depends(require_admin_key), Depends(require_feature("inbox"))])
async def inbox_threads(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    q: str = Query(default=""),
) -> dict:
    """Participants with conversation history, newest activity first.

    Paged: the list loads 50 at a time as the owner scrolls, so a shop with
    500+ imported contacts opens as fast as one with five.

    q searches name and phone ACROSS the whole customer/staff book, not just
    the page in hand — including people who have never messaged (a fresh
    Excel import), so the owner can find them and start a chat.

    One PERSON is one thread. The owner (and any staff member who ever
    messaged as a customer) has both a customer row and a staff row for the
    same number; listing both showed him twice with half his history each.
    """
    # phone -> thread, merged across the customer and staff sides
    by_phone: dict[str, dict] = {}

    def _add(entry: dict, last_inbound) -> None:
        entry["_last_inbound"] = last_inbound
        old = by_phone.get(entry["phone"])
        if old is None:
            by_phone[entry["phone"]] = entry
            return
        # newest message wins the preview; the staff/admin row wins the name
        newest = entry if entry["last_at"] > old["last_at"] else old
        ident = entry if entry["kind"] in ("staff", "admin") else old
        stamps = [s for s in (entry["_last_inbound"], old["_last_inbound"]) if s]
        by_phone[entry["phone"]] = {
            **newest,
            "kind": ident["kind"],
            "name": ident["name"],
            "_last_inbound": max(stamps) if stamps else None,
        }

    term = (q or "").strip()
    digits = re.sub(r"\D", "", term)

    def _match(model):
        like = f"%{term}%"
        conds = [model.name.ilike(like)]
        if digits:
            conds.append(model.phone.ilike(f"%{digits}%"))
        return or_(*conds)

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
    q_cust = (
        select(Customer, Conversation)
        .join(last_at, last_at.c.customer_id == Customer.id)
        .join(
            Conversation,
            (Conversation.customer_id == Customer.id)
            & (Conversation.created_at == last_at.c.last_at),
        )
    )
    if term:
        q_cust = q_cust.where(_match(Customer))
    rows = (await db.execute(q_cust)).all()
    mgr_phone = normalize_phone(manager_phone())
    for cust, conv in rows:
        _add(
            {
                # The owner's own number has a customer row from his tests —
                # label him as the boss, not a customer.
                "kind": "admin" if cust.phone == mgr_phone else "customer",
                "phone": cust.phone,
                "name": cust.name or cust.phone,
                "last_text": conv.message_text[:80],
                "last_at": conv.created_at.isoformat(),
                "last_direction": conv.direction.name,
            },
            cust.last_message_at,
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
    q_staff = (
        select(Staff, Conversation)
        .join(last_at_s, last_at_s.c.staff_id == Staff.id)
        .join(
            Conversation,
            (Conversation.staff_id == Staff.id)
            & (Conversation.created_at == last_at_s.c.last_at),
        )
    )
    if term:
        q_staff = q_staff.where(_match(Staff))
    rows_s = (await db.execute(q_staff)).all()
    for st, conv in rows_s:
        _add(
            {
                "kind": "admin" if st.role is StaffRole.ADMIN else "staff",
                "phone": st.phone,
                "name": st.name,
                "last_text": conv.message_text[:80],
                "last_at": conv.created_at.isoformat(),
                "last_direction": conv.direction.name,
            },
            st.last_message_at,
        )

    threads = [
        {**t, "window": _window_state(t.pop("_last_inbound"))}
        for t in by_phone.values()
    ]
    threads.sort(key=lambda t: t["last_at"], reverse=True)

    # Searching also reaches contacts who have NEVER messaged — a fresh
    # Excel import is 500 people with no history, and "not in the chat list"
    # must not mean "unreachable". They sort after real conversations.
    if term:
        found = {t["phone"] for t in threads}
        silent = (
            (
                await db.execute(
                    select(Customer)
                    .where(_match(Customer))
                    .order_by(Customer.name.nulls_last())
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        for c in silent:
            if c.phone in found:
                continue
            threads.append(
                {
                    "kind": "admin" if c.phone == mgr_phone else "customer",
                    "phone": c.phone,
                    "name": c.name or c.phone,
                    "last_text": "",
                    "last_at": "",
                    "last_direction": "",
                    "no_messages": True,
                    "window": _window_state(c.last_message_at),
                }
            )

    total = len(threads)
    page = threads[offset : offset + limit]
    return {
        "threads": page,
        "total": total,
        "has_more": offset + len(page) < total,
        "offset": offset,
    }


@router.get("/api/inbox/thread", dependencies=[Depends(require_admin_key), Depends(require_feature("inbox"))])
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
    # One number can be BOTH a staff member and a customer (the owner, or a
    # staff member who once wrote in as one). Show the whole conversation,
    # not the half that happens to match the row we looked up first.
    staff = (
        await db.execute(select(Staff).where(Staff.phone == phone))
    ).scalar_one_or_none()
    customer = (
        await db.execute(select(Customer).where(Customer.phone == phone))
    ).scalar_one_or_none()
    if staff is None and customer is None:
        raise HTTPException(status_code=404, detail=f"no thread for {phone}")

    sides = []
    if staff is not None:
        sides.append(Conversation.staff_id == staff.id)
    if customer is not None:
        sides.append(Conversation.customer_id == customer.id)
    cond = or_(*sides) if len(sides) > 1 else sides[0]
    msgs = (
        await db.execute(
            select(Conversation).where(cond).order_by(Conversation.created_at.desc()).limit(limit)
        )
    ).scalars().all()

    # the window is open if EITHER row heard from them inside 24h
    stamps = [
        s for s in (
            staff.last_message_at if staff else None,
            customer.last_message_at if customer else None,
        ) if s
    ]
    last_inbound = max(stamps) if stamps else None

    active_orders = []
    if customer:
        active_orders = [
            {"order_number": o.order_number, "status": o.status.name,
             "expected_delivery": o.expected_delivery.isoformat() if o.expected_delivery else None}
            for o in await get_active_orders_for_phone(db, phone)
        ]

    if staff:
        kind = "admin" if staff.role is StaffRole.ADMIN else "staff"
    elif phone == normalize_phone(manager_phone()):
        kind = "admin"
    else:
        kind = "customer"
    return {
        "kind": kind,
        "phone": phone,
        "name": (staff.name if staff else (customer.name or customer.phone)),
        "window": _window_state(last_inbound),
        "active_orders": active_orders,
        "messages": [
            {
                "direction": m.direction.name,
                "text": m.message_text,
                "sent_by": m.sent_by,
                "at": m.created_at.isoformat(),
                # ✓ sent / ✓✓ delivered / blue ✓✓ read — Meta's word, not a guess
                "status": m.status,
                "wamid": m.wa_message_id,
                "reply_to": m.reply_to_wamid,
            }
            for m in reversed(msgs)
        ],
    }


class InboxSendIn(BaseModel):
    phone: str
    text: str = Field(min_length=1, max_length=4000)
    # wamid being quoted — WhatsApp shows it above the reply
    reply_to: str | None = Field(default=None, max_length=120)


@router.post("/api/inbox/send", dependencies=[Depends(require_admin_key), Depends(require_feature("inbox"))])
async def inbox_send(body: InboxSendIn, db: AsyncSession = Depends(get_db)) -> dict:
    """Manager sends a free-form message from the Inbox."""
    try:
        to_phone = normalize_phone(body.phone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        wa_id = await send_message(
            db, to_phone=to_phone, text=body.text, sent_by="manager",
            reply_to=(body.reply_to or None),
        )
    except WindowClosedError:
        raise HTTPException(
            status_code=409,
            detail="The 24h window is closed — free-form messages can't be sent. Use a template or wait for the customer to message first.",
        )
    except SendError as exc:
        raise HTTPException(status_code=502, detail=f"WhatsApp send failed: {exc}")
    return {"wa_message_id": wa_id, "at": datetime.now(timezone.utc).isoformat()}


class InboxPingIn(BaseModel):
    phone: str


@router.post("/api/inbox/ping", dependencies=[Depends(require_admin_key), Depends(require_feature("inbox"))])
async def inbox_ping(body: InboxPingIn, db: AsyncSession = Depends(get_db)) -> dict:
    """Nudge this person — 'bhai, jawab do'.

    A staff member with open work is reminded of that work by name; anyone
    else gets a short, polite poke. Outside the 24h window we fall back to
    the approved template, so a nudge is never silently swallowed.
    """
    try:
        to_phone = normalize_phone(body.phone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    staff = (
        await db.execute(select(Staff).where(Staff.phone == to_phone))
    ).scalar_one_or_none()
    text = "🔔 Namaste! Ek chhota sa reminder — jawab ka intezaar hai. 🙏"
    if staff is not None:
        from app.services import tasks as task_service

        open_tasks = await task_service.open_tasks_for_staff(db, staff.id)
        if open_tasks:
            t = open_tasks[0]
            text = (
                f"🔔 {staff.name}, yaad dila raha hoon [{t.code}]: {t.title}\n"
                f"Ho gaya ho to reply karein: done {t.code}"
            )
        else:
            text = f"🔔 {staff.name}, ek update chahiye tha — kya status hai?"

    try:
        wa_id = await send_message(db, to_phone=to_phone, text=text, sent_by="manager")
    except WindowClosedError:
        try:
            wa_id = await send_message(
                db, to_phone=to_phone,
                template_name="kk_staff_alert",
                template_params=[" ".join(text.split())[:600]],
                sent_by="manager",
            )
        except SendError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Window band hai aur template bhi nahi gaya: {exc}",
            )
    except SendError as exc:
        raise HTTPException(status_code=502, detail=f"WhatsApp send failed: {exc}")
    return {"wa_message_id": wa_id, "text": text}


class TemplateSendIn(BaseModel):
    phone: str
    template_name: str = Field(min_length=2)
    params: list[str] = Field(default_factory=list)


@router.post("/api/inbox/send-template", dependencies=[Depends(require_admin_key), Depends(require_feature("inbox"))])
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


@router.post("/api/inbox/new-chat", dependencies=[Depends(require_admin_key), Depends(require_feature("inbox"))])
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


@router.post("/api/customers/bulk", dependencies=[Depends(require_admin_owner)])
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


# Header names the owner's own files actually use — Excel exports from a
# phone, a shop register, a previous CRM. Matched case/space-insensitively.
_IMPORT_HEADERS = {
    "phone": ("phone", "mobile", "number", "mobileno", "phoneno", "contact",
              "whatsapp", "mob", "no", "cell", "नंबर", "मोबाइल"),
    "name": ("name", "customer", "customername", "fullname", "party", "client",
             "naam", "नाम"),
    "address": ("address", "addr", "pata", "location", "पता"),
}


def _import_col(header: str) -> str | None:
    key = re.sub(r"[^a-z0-9ऀ-ॿ]", "", str(header or "").lower())
    for field, names in _IMPORT_HEADERS.items():
        if key in names:
            return field
    return None


def _import_rows(raw: bytes, filename: str) -> list[list[str]]:
    """Rows of cells from a .csv/.xlsx upload. Raises HTTPException on junk."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        try:
            import openpyxl
        except ImportError:
            raise HTTPException(
                status_code=400,
                detail="Excel padhne ki library nahi hai — file ko CSV mein save karke bhejein.",
            )
        try:
            wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        except Exception:
            raise HTTPException(status_code=400, detail="Excel file kharab lag rahi hai.")
        ws = wb[wb.sheetnames[0]]
        return [
            ["" if c is None else str(c).strip() for c in row]
            for row in ws.iter_rows(values_only=True)
        ]
    if name.endswith(".xls"):
        raise HTTPException(
            status_code=400,
            detail="Purana .xls format nahi padh sakta — Excel se 'Save as .xlsx' ya CSV karein.",
        )
    # CSV / TSV: try the common encodings before giving up
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise HTTPException(status_code=400, detail="File ka text padha nahi ja saka.")
    sample = text[:2000]
    delim = "\t" if sample.count("\t") > sample.count(",") else ","
    return [
        [str(c).strip() for c in row]
        for row in csv.reader(io.StringIO(text), delimiter=delim)
    ]


@router.post("/api/customers/import-file", dependencies=[Depends(require_admin_owner)])
async def customers_import_file(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Import contacts from an Excel/CSV file.

    Works with or without a header row: with one we read the phone/name/
    address columns by name, without one we take the first cell that looks
    like a number as the phone and the longest other cell as the name. A row
    we cannot read is REPORTED back, never silently dropped — the owner must
    know which of his 500 lines did not make it.
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="File khali hai.")
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File 5MB se badi hai.")
    rows = _import_rows(raw, file.filename or "")
    if not rows:
        raise HTTPException(status_code=400, detail="File mein koi row nahi mili.")

    # header row? only if at least one cell maps to a known column AND that
    # row has no valid phone in it (a data row can carry the word "mobile")
    cols: dict[int, str] = {}
    start = 0
    first = rows[0]
    mapped = {i: _import_col(c) for i, c in enumerate(first)}
    if any(v for v in mapped.values()):
        cols = {i: v for i, v in mapped.items() if v}
        start = 1

    added = updated = skipped = 0
    bad: list[str] = []
    seen: set[str] = set()
    for n, row in enumerate(rows[start:], start=start + 1):
        if not any(str(c).strip() for c in row):
            continue
        phone_raw, name, address = "", "", ""
        if cols:
            for i, field in cols.items():
                val = row[i].strip() if i < len(row) else ""
                if field == "phone":
                    phone_raw = val
                elif field == "name":
                    name = val
                else:
                    address = val
        if not phone_raw:
            # no header (or an empty phone cell): first number-ish cell wins
            for cell in row:
                digits = re.sub(r"\D", "", str(cell))
                if 10 <= len(digits) <= 15:
                    phone_raw = str(cell)
                    break
            if not name:
                others = [
                    str(c).strip() for c in row
                    if str(c).strip() and str(c).strip() != phone_raw
                    and not str(c).strip().replace(" ", "").isdigit()
                ]
                name = max(others, key=len) if others else ""
        try:
            phone = normalize_phone(re.sub(r"[^\d+]", "", phone_raw))
        except ValueError:
            bad.append(f"line {n}: {' | '.join(str(c) for c in row)[:60]}")
            continue
        if phone in seen:
            skipped += 1
            continue
        seen.add(phone)

        existing = (
            await db.execute(select(Customer).where(Customer.phone == phone))
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                Customer(
                    phone=phone,
                    name=(name[:120] or None),
                    address=(address[:400] or None),
                )
            )
            added += 1
        else:
            # only FILL blanks — an import must not overwrite what the shop
            # already knows about a customer
            touched = False
            if name and not existing.name:
                existing.name, touched = name[:120], True
            if address and not existing.address:
                existing.address, touched = address[:400], True
            updated += touched
            skipped += not touched
    await db.commit()
    log.info(
        "customers_file_import",
        file=file.filename, added=added, updated=updated,
        skipped=skipped, bad=len(bad),
    )
    await audit.record(
        actor_role="admin", actor="dashboard", action="customers_import_file",
        args={"file": (file.filename or "")[:80], "rows": len(rows) - start},
        result=f"added={added} updated={updated} skipped={skipped} bad={len(bad)}",
    )
    return {
        "added": added,
        "updated": updated,
        "skipped_existing": skipped,
        "invalid": bad[:15],
        "invalid_total": len(bad),
        "rows_read": len(rows) - start,
    }


@router.post("/api/inbox/send-media", dependencies=[Depends(require_admin_key), Depends(require_feature("inbox"))])
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
async def serve_media(
    name: str,
    key: str = Query(default=""),
    kk_session: str = Cookie(default=""),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """Serve chat media to the Inbox.

    Do raste, dono is dukaan tak seemit:
    1. login session ka cookie — browser <img> tag ke saath khud bhejta hai
    2. ?key=<ADMIN_API_KEY> — scripts aur purana rasta

    Session wala rasta isliye zaroori hai ki jab se asli login aaya, browser
    ke paas admin key hoti hi nahi — har photo 401 deti thi aur Inbox mein
    toota hua dabba dikhta tha.
    """
    import hmac as _hmac

    allowed = bool(key) and _hmac.compare_digest(key, settings.ADMIN_API_KEY)
    if not allowed and kk_session:
        from app.services import auth as auth_service

        # Self-serve gate: koi bhi valid session. Filenames random-UUID hain
        # (unguessable); asli per-tenant media partitioning backlog note mein.
        user = await auth_service.user_for_token(db, kk_session)
        allowed = user is not None
    if not allowed:
        raise HTTPException(status_code=401, detail="key ya login chahiye")
    # basename() guard: no traversal
    safe = Path(name).name
    path = _MEDIA_DIR / safe
    if not path.is_file():
        raise HTTPException(status_code=404, detail="media nahi mila")
    return FileResponse(path)


# Phone se aayi UI reports. File hi kaafi hai — ye diagnostic hai, business
# data nahi. Ring buffer: sirf aakhri 60 rakhi jaati hain, taaki koi ise
# bhar kar disk na bhar de.
_UI_REPORTS = Path(__file__).resolve().parent.parent / "media" / "ui-reports.jsonl"
_UI_KEEP = 60


@router.post("/api/ui-report")
async def ui_report(request: Request) -> dict:
    """`?probe=1` se aayi report — kya viewport se bahar nikla, kaunsa
    tap-target chhota hai, kya JS error aaya.

    Jaan-boojh kar bina login ke: public signup page bhi mobile par toot
    sakta hai, aur wahan session hota hi nahi. Size aur count dono capped
    hain, isliye ise bhar kar disk nahi bhari ja sakti.
    """
    import json as _json

    raw = await request.body()
    if len(raw) > 24_000:
        raise HTTPException(status_code=413, detail="report too big")
    try:
        body = _json.loads(raw.decode())
    except Exception:
        raise HTTPException(status_code=400, detail="bad json")

    body["at"] = datetime.now(timezone.utc).isoformat()
    try:
        _UI_REPORTS.parent.mkdir(exist_ok=True)
        lines = []
        if _UI_REPORTS.exists():
            lines = _UI_REPORTS.read_text(encoding="utf-8").splitlines()[-(_UI_KEEP - 1):]
        lines.append(_json.dumps(body, ensure_ascii=False))
        _UI_REPORTS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        log.exception("ui_report_write_failed")
        return {"ok": False}

    log.info(
        "ui_report",
        section=body.get("section"), vw=(body.get("viewport") or {}).get("w"),
        overflow=len(body.get("overflow") or []),
        tiny=len(body.get("tiny_taps") or []),
        errors=len(body.get("errors") or []),
    )
    return {"ok": True}


@router.get("")
async def dashboard_page(
    kk_session: str = Cookie(default=""), db: AsyncSession = Depends(get_db)
) -> Response:
    """The dashboard page.

    This deployment holds ONE shop's data. A logged-in user from any other
    tenant (i.e. anyone who just signed up on the public page) is sent to
    their own welcome page instead — they must never even see this screen,
    let alone the data behind it.

    Asset links get an mtime version stamp so the browser can never serve a
    stale app.js/app.css against fresh HTML (that mix = dead buttons +
    broken styling).
    """
    if kk_session:
        from app.models.tenant import Tenant as _T
        from app.services import auth as auth_service

        # Self-serve gate: har tenant apna dashboard. Sirf band accounts
        # (locked/suspended/cancelled) welcome par jaate hain — wahan renew
        # CTA hai; unka API waise bhi 402 deta.
        user = await auth_service.user_for_token(db, kk_session)
        if user is not None:
            t = await db.get(_T, user.tenant_id)
            if t is not None and t.status in ("locked", "suspended", "cancelled"):
                log.info("closed_account_dashboard_redirect", user=user.email)
                return RedirectResponse(url="/welcome", status_code=303)

    html = _DASHBOARD_FILE.read_text(encoding="utf-8")
    static_dir = _DASHBOARD_FILE.parent
    v = int(max(
        (static_dir / "app.js").stat().st_mtime,
        (static_dir / "app.css").stat().st_mtime,
        # tokens.css teenon surface ka source of truth hai. Isko version
        # mein na ginne par ek brand-rang badalne par bhi browser purani
        # file pakde rehta — aur dikkat "kabhi-kabhi purana orange" jaisi
        # dikhti, jo dhoondhne mein sabse mehngi hoti hai.
        (static_dir / "tokens.css").stat().st_mtime,
        # Sprite bhi: icon badla aur version na badla to browser purana
        # sprite pakde rehta hai aur nav aadha purana aadha naya dikhta.
        (static_dir / "icons.svg").stat().st_mtime,
    ))
    import re as _re

    html = _re.sub(r"((?:app|tokens)\.(?:js|css)|icons\.svg)\?v=[\w]+", rf"\1?v={v}", html)
    return Response(
        content=html, media_type="text/html",
        headers={
            "Cache-Control": "no-cache",
            # REPORT-ONLY, abhi enforce nahi. /control par yahi policy asli
            # mein lagi hui hai (dekho main.py ka control_page) — wahan
            # markup mein ek bhi inline handler ya style nahi hai. Yahan
            # abhi ~264 inline handler aur ~242 style="" bache hain, to
            # enforce karte hi dashboard ka har button mar jaata.
            #
            # Report-only ka faayda: browser policy tod-tod kar batata hai
            # aur page chalta rehta hai. Handlers hatte-hatte report khali
            # hoti jayegi; jis din khali ho, header ka naam badal kar
            # Content-Security-Policy kar dena — aur kuch nahi badalna.
            #
            # style-src par 'unsafe-inline' jaan-bujh kar hai: asli XSS
            # rasta script hai, aur 242 style attributes hatana alag kaam
            # hai jo is header ko rok nahi sakta.
            "Content-Security-Policy-Report-Only": (
                "default-src 'self'; script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
                "report-uri /admin/csp-report"
            ),
        },
    )

# Ek din ka bacha hua kaam yahan dikhta hai: jab tak ye endpoint chup na ho
# jaye, /admin par CSP enforce nahi ho sakti.
_CSP_SEEN: dict[tuple[str, str], int] = {}


@router.post("/csp-report", include_in_schema=False)
async def csp_report(request: Request) -> Response:
    """Browser ki CSP violation reports — sirf ginti ke liye.

    Bina auth ke hai kyunki browser ye report bina cookie/API-key ke bhejta
    hai; ismein koi data padha nahi jaata, sirf likha jaata hai.

    Har violation alag se log karne par ek dashboard load 260+ lines ugal
    deta. Isliye (directive, blocked-uri) par gin kar rakhte hain aur pehli
    baar hi log karte hain — report ka kaam "kya-kya baaki hai" batana hai,
    "kitni baar" nahi.
    """
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=204)
    if not isinstance(body, dict):
        return Response(status_code=204)
    r = body.get("csp-report") or body.get("body") or body
    if not isinstance(r, dict):
        return Response(status_code=204)
    key = (
        str(r.get("effective-directive") or r.get("violated-directive") or "?")[:60],
        str(r.get("blocked-uri") or "?")[:120],
    )
    first = key not in _CSP_SEEN
    _CSP_SEEN[key] = _CSP_SEEN.get(key, 0) + 1
    if first:
        log.info(
            "csp_violation",
            directive=key[0], blocked=key[1],
            document=str(r.get("document-uri") or "")[:200],
            line=r.get("line-number"),
        )
    return Response(status_code=204)


@router.get("/csp-report", dependencies=[Depends(require_admin_key)])
async def csp_report_summary() -> dict:
    """Ab tak kya-kya CSP todta hai, ginti ke saath. Khali = enforce karo."""
    return {
        "distinct": len(_CSP_SEEN),
        "total": sum(_CSP_SEEN.values()),
        "violations": sorted(
            ({"directive": d, "blocked": b, "count": n} for (d, b), n in _CSP_SEEN.items()),
            key=lambda x: -x["count"],
        ),
    }
