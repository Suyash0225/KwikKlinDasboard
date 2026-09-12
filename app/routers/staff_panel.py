"""Staff panel API — dukaan ke aadmi ke liye, owner ke dashboard se alag.

Har query par teen shart, teenon server par:
  tenant  : token se (kabhi request se nahi)
  role    : manager ko poori dukaan, baaki ko sirf apna kaam
  plan    : kaunsi suvidha khuli hai — plans.py hi ek sach

Customer ka poora number staff ko kabhi nahi dikhta (masked). Kaam ke liye
call button alag endpoint se chalta hai, aur wo bhi audit hota hai.
"""

import asyncio
import re
import uuid as uuid_module
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import (
    APIRouter, Cookie, Depends, File, Form, HTTPException, Query, Request, Response,
    UploadFile,
)
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import (
    TASK_CANCELLED,
    TASK_DONE,
    TASK_OPEN,
    Customer,
    Order,
    OrderStatus,
    Staff,
    StaffRole,
    Task,
)
from app.services import audit, plans
from app.services.auth import verify_password
from app.services.staff_auth import (
    STAFF_COOKIE,
    SESSION_HOURS,
    StaffPrincipal,
    current_staff,
    login as staff_login,
    logout as staff_logout,
    require_manager,
    require_staff_feature,
    set_password,
    staff_for_token,
)
from app.services.work_orders import items_summary

IST = timezone(timedelta(hours=5, minutes=30))

router = APIRouter(prefix="/staff/api", tags=["staff-panel"])
log = structlog.get_logger()


def mask_phone(phone: str | None) -> str:
    """+91987xxxx210 — pehchan ke liye kaafi, chura kar le jaane ke liye nahi."""
    p = (phone or "").strip()
    if len(p) < 7:
        return p or "-"
    return f"{p[:6]}xxxx{p[-3:]}"


# ---------------------------------------------------------------- auth ----


class LoginIn(BaseModel):
    phone: str = Field(min_length=8, max_length=20)
    password: str = Field(min_length=1, max_length=200)


@router.post("/login")
async def staff_login_api(
    body: LoginIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> dict:
    from app.utils.phone import normalize_phone

    ip = request.client.host if request.client else "?"
    token, staff = await staff_login(
        db, phone=normalize_phone(body.phone), password=body.password, ip=ip,
        user_agent=request.headers.get("user-agent", ""),
    )
    response.set_cookie(
        STAFF_COOKIE, token,
        max_age=SESSION_HOURS * 3600,
        httponly=True,          # JS ise padh hi na sake
        samesite="strict",      # doosri site se request par cookie na jaye
        secure=request.url.scheme == "https",
        path="/staff",          # dashboard ke raston par ye cookie jaati hi nahi
    )
    return {
        "name": staff.name,
        "role": staff.role.name,
        "must_change_password": staff.must_change_password,
    }


@router.post("/logout")
async def staff_logout_api(
    response: Response, kk_staff: str = Cookie(default=""), db: AsyncSession = Depends(get_db)
) -> dict:
    await staff_logout(db, kk_staff)
    response.delete_cookie(STAFF_COOKIE, path="/staff")
    return {"ok": True}


@router.get("/me")
async def me(p: StaffPrincipal = Depends(current_staff)) -> dict:
    """Panel ka pehla call — kaun hoon main aur kya-kya khula hai.

    UI isi ke `features` se tabs dikhata/chhupata hai; asli rok API par
    hi hai, isliye dono kabhi alag nahi ho sakte.
    """
    return {
        "name": p.staff.name,
        "phone": mask_phone(p.staff.phone),
        "role": p.staff.role.name,
        "is_manager": p.is_manager,
        "must_change_password": p.staff.must_change_password,
        "shop": p.tenant.shop_name if p.tenant else "",
        "plan": plans.get(p.plan).name,
        "features": p.features,
    }


@router.get("/events")
async def staff_events(request: Request, kk_staff: str = Cookie(default="")):
    """Staff ke phone par live updates — manager ne kuch kiya to turant dikhe.

    Yahan `Depends(current_staff)` aur `Depends(get_db)` dono jaan-boojh kar
    nahi hain. Dono session ko poore stream tak — yani puri shift tak —
    pakde rehte; do-teen phone khulte hi DB pool khatam ho jaata aur baaki
    sab request wahin ruk jaati. Isliye pehchan ek chhote session mein hoti
    hai jo stream shuru hone se PEHLE band ho jaata hai.
    """
    from fastapi.responses import StreamingResponse

    from app.database import async_session_factory
    from app.services import events

    async with async_session_factory() as db:
        staff = await staff_for_token(db, kk_staff)
        if staff is None:
            raise HTTPException(status_code=401, detail="Please log in")
        tid = staff.tenant_id
    # session yahan band — ab connection ghanton khula rahe to bhi koi
    # DB resource nahi rukta

    return StreamingResponse(
        events.stream(tid, request.is_disconnected),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class PasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=6, max_length=200)


@router.post("/password")
async def change_own_password(
    body: PasswordIn,
    p: StaffPrincipal = Depends(current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Sirf APNA password. Role ya kisi aur ka password yahan se nahi badalta."""
    if not (p.staff.password_hash and verify_password(body.old_password, p.staff.password_hash)):
        raise HTTPException(status_code=403, detail="Your current password is wrong")
    await set_password(db, p.staff, body.new_password, temp=False)
    await audit.record(
        actor_role="staff", actor=p.staff.name, action="staff_password_changed",
        args={}, result="ok", tenant_id=p.staff.tenant_id,
    )
    return {"ok": True}


# --------------------------------------------------------------- tasks ----


async def _visible_tasks(db: AsyncSession, p: StaffPrincipal, tab: str) -> list[Task]:
    """Ye aadmi kaunse kaam dekh sakta hai.

    Non-manager ke liye shart DB query mein hai, UI mein nahi — isliye
    URL se doosre ka code daal kar bhi kuch nahi milta.
    """
    q = select(Task).order_by(Task.urgent.desc(), Task.created_at.desc()).limit(200)
    if not p.is_manager:
        q = q.where(Task.assigned_staff_id == p.staff.id)
    if tab == "pending":
        q = q.where(Task.status == TASK_OPEN)
    elif tab == "mine":
        q = q.where(Task.assigned_staff_id == p.staff.id, Task.status == TASK_OPEN)
    elif tab == "done":
        q = q.where(Task.status == TASK_DONE)
    elif tab == "cancelled":
        q = q.where(Task.status == TASK_CANCELLED)
    return list((await db.execute(q)).scalars().all())


async def _task_card(db: AsyncSession, t: Task, p: StaffPrincipal) -> dict:
    order = await db.get(Order, t.order_id) if t.order_id else None
    card: dict = {
        "code": t.code,
        "title": t.title,
        "status": t.status,
        "urgent": t.urgent,
        "age_hours": int((datetime.now(timezone.utc) - t.created_at).total_seconds() // 3600),
        "eta_text": t.eta_text,
        "reply": t.reply,
        "cancel_requested": t.cancel_requested_at is not None,
        "cancel_reason": t.cancel_reason,
        "assignee": None,
    }
    if p.is_manager and t.assigned_staff_id:
        st = await db.get(Staff, t.assigned_staff_id)
        card["assignee"] = st.name if st else None
    if order is not None:
        cust = await db.get(Customer, order.customer_id)
        card["order"] = {
            "number": order.order_number,
            "customer": (cust.name or "Customer") if cust else "?",
            # Poora number kabhi nahi — kaam ke liye itna kaafi hai
            "phone_masked": mask_phone(cust.phone if cust else ""),
            "items": items_summary(order),
            "delivery": order.expected_delivery.isoformat() if order.expected_delivery else None,
            "status": order.status.name,
            "notes": order.notes,
            "total": float(order.total_amount or 0),
            "due": float((order.total_amount or 0) - (order.amount_paid or 0)),
        }
    return card


@router.get("/tasks")
async def my_tasks(
    tab: str = "mine",
    p: StaffPrincipal = Depends(current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await _visible_tasks(db, p, tab if tab in ("pending", "mine", "done", "cancelled") else "mine")
    cards = [await _task_card(db, t, p) for t in rows]
    counts = {
        "mine": (
            await db.execute(
                select(func.count()).select_from(Task).where(
                    Task.assigned_staff_id == p.staff.id, Task.status == TASK_OPEN
                )
            )
        ).scalar_one(),
    }
    if p.is_manager:
        counts["pending"] = (
            await db.execute(
                select(func.count()).select_from(Task).where(Task.status == TASK_OPEN)
            )
        ).scalar_one()
    return {"tasks": cards, "counts": counts, "is_manager": p.is_manager}


async def _my_task(db: AsyncSession, p: StaffPrincipal, code: str) -> Task:
    """Code se task — par sirf wahi jo is aadmi ka hai (ya manager hai)."""
    t = (
        await db.execute(select(Task).where(Task.code == code.strip().upper()))
    ).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail=f"{code} not found")
    if not p.is_manager and t.assigned_staff_id != p.staff.id:
        # 403, 404 nahi: manager ke logs mein ye dikhna chahiye
        log.info("staff_task_forbidden", staff=p.staff.name, code=code)
        raise HTTPException(status_code=403, detail="This job is not assigned to you")
    return t


class NoteIn(BaseModel):
    note: str = Field(default="", max_length=300)


@router.post("/tasks/{code}/done")
async def mark_done(
    code: str,
    body: NoteIn,
    p: StaffPrincipal = Depends(current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import tasks as task_service

    t = await _my_task(db, p, code)
    if t.status != TASK_OPEN:
        raise HTTPException(status_code=409, detail=f"{t.code} is already {t.status}")
    await task_service.complete_task(db, t, reply=body.note or None, by=p.staff.name)
    from app.services import team

    await team.notify_admins(
        db, f"✅ {p.staff.name} ne [{t.code}] pura kiya"
        + (f" — {body.note[:150]}" if body.note else ""),
        skip_phone=p.staff.phone,
    )
    return {"ok": True, "code": t.code}


class AskIn(BaseModel):
    text: str = Field(min_length=2, max_length=300)


@router.post("/tasks/{code}/ask")
async def ask_about(
    code: str,
    body: AskIn,
    p: StaffPrincipal = Depends(current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Sawal seedha owner/manager tak — panel se bhi, WhatsApp ki tarah."""
    from app.services import team

    t = await _my_task(db, p, code)
    await team.notify_admins(
        db, f"❓ {p.staff.name} ka sawal [{t.code}] par: {body.text.strip()[:280]}",
        skip_phone=p.staff.phone,
    )
    await audit.record(
        actor_role="staff", actor=p.staff.name, action="staff_asked",
        args={"code": t.code}, result=body.text[:150], tenant_id=p.staff.tenant_id,
    )
    return {"ok": True}


class CancelIn(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


@router.post("/tasks/{code}/cancel-request", dependencies=[Depends(require_staff_feature("cancel_approval"))])
async def request_cancel(
    code: str,
    body: CancelIn,
    p: StaffPrincipal = Depends(current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Staff cancel MAANGTA hai — karta nahi. Manager tay karega.

    Seedha cancel dene par wo kaam chupchaap gayab ho jaate jo koi karna
    nahi chahta, aur owner ko pata bhi na chalta.
    """
    t = await _my_task(db, p, code)
    if t.status != TASK_OPEN:
        raise HTTPException(status_code=409, detail=f"{t.code} is already {t.status}")
    t.cancel_requested_at = datetime.now(timezone.utc)
    t.cancel_requested_by = p.staff.name[:80]
    t.cancel_reason = body.reason.strip()[:300]
    db.add(t)
    await db.commit()
    from app.services import team

    await team.notify_admins(
        db, f"🛑 {p.staff.name} ne [{t.code}] cancel karne ko kaha: {t.cancel_reason}\n"
            f"Panel se approve ya reject kijiye.",
        skip_phone=p.staff.phone,
    )
    await audit.record(
        actor_role="staff", actor=p.staff.name, action="task_cancel_requested",
        args={"code": t.code}, result=t.cancel_reason[:150], tenant_id=p.staff.tenant_id,
    )
    return {"ok": True, "status": "sent to your manager"}


class DecisionIn(BaseModel):
    approve: bool


@router.post("/tasks/{code}/cancel-decide", dependencies=[Depends(require_staff_feature("cancel_approval"))])
async def decide_cancel(
    code: str,
    body: DecisionIn,
    p: StaffPrincipal = Depends(require_manager),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import tasks as task_service

    t = (
        await db.execute(select(Task).where(Task.code == code.strip().upper()))
    ).scalar_one_or_none()
    if t is None or t.cancel_requested_at is None:
        raise HTTPException(status_code=404, detail="There is no cancel request for this")
    if body.approve:
        await task_service.cancel_task(db, t, by=p.staff.name)
        result = "approved"
    else:
        t.cancel_requested_at = None
        t.cancel_requested_by = None
        t.cancel_reason = None
        db.add(t)
        await db.commit()
        result = "rejected"
    await audit.record(
        actor_role="admin", actor=p.staff.name, action="task_cancel_decided",
        args={"code": t.code}, result=result, tenant_id=p.staff.tenant_id,
    )
    return {"ok": True, "result": result}


# -------------------------------------------------------------- orders ----


@router.get("/orders/{number}")
async def order_detail(
    number: str,
    p: StaffPrincipal = Depends(current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Order ki poori tasveer — par phone masked, aur timeline sirf us
    plan mein jisme wo suvidha hai."""
    order = (
        await db.execute(select(Order).where(Order.order_number == number.strip().upper()))
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail=f"{number} not found")
    if not p.is_manager:
        mine = (
            await db.execute(
                select(func.count()).select_from(Task).where(
                    Task.order_id == order.id, Task.assigned_staff_id == p.staff.id
                )
            )
        ).scalar_one()
        if not mine and order.assigned_washer_id != p.staff.id and order.assigned_delivery_id != p.staff.id:
            raise HTTPException(status_code=403, detail="This order is not yours")
    cust = await db.get(Customer, order.customer_id)
    out = {
        "number": order.order_number,
        "customer": (cust.name or "Customer") if cust else "?",
        "phone_masked": mask_phone(cust.phone if cust else ""),
        "items": order.items or [],
        "items_text": items_summary(order),
        "status": order.status.name,
        "delivery": order.expected_delivery.isoformat() if order.expected_delivery else None,
        "notes": order.notes,
        "total": float(order.total_amount or 0),
        "paid": float(order.amount_paid or 0),
        "due": float((order.total_amount or 0) - (order.amount_paid or 0)),
        "can_collect": p.has("cod_collection"),
    }
    if p.has("order_timeline"):
        from app.models import OrderStatusHistory

        rows = (
            await db.execute(
                select(OrderStatusHistory)
                .where(OrderStatusHistory.order_id == order.id)
                .order_by(OrderStatusHistory.changed_at)
            )
        ).scalars().all()
        out["timeline"] = [
            {"status": r.new_status.name, "at": r.changed_at.isoformat(), "by": r.changed_by}
            for r in rows
        ]
    return out


class CollectIn(BaseModel):
    amount: float = Field(gt=0)
    method: str = Field(pattern="^(cash|upi)$")


@router.post("/orders/{number}/collect", dependencies=[Depends(require_staff_feature("cod_collection"))])
async def collect_payment(
    number: str,
    body: CollectIn,
    p: StaffPrincipal = Depends(current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delivery wale ne paisa liya — ledger turant update.

    Card/bank ka koi number yahan na aata hai na store hota hai: sirf
    kitna aur kis tarike se.
    """
    from app.models import PaymentMethod
    from app.services.order_service import record_payment

    order = (
        await db.execute(select(Order).where(Order.order_number == number.strip().upper()))
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail=f"{number} not found")
    due = float((order.total_amount or 0) - (order.amount_paid or 0))
    if body.amount > due + 0.01:
        raise HTTPException(status_code=400, detail=f"Only ₹{due:.0f} is due")
    from decimal import Decimal

    await record_payment(
        db, order, amount=Decimal(str(body.amount)),
        method=PaymentMethod.CASH if body.method == "cash" else PaymentMethod.UPI,
        recorded_by=p.staff.name,
    )
    await audit.record(
        actor_role="staff", actor=p.staff.name, action="cod_collected",
        args={"order": order.order_number, "amount": body.amount, "method": body.method},
        result="ok", tenant_id=p.staff.tenant_id,
    )
    log.info("cod_collected", staff=p.staff.name, order=order.order_number, amount=body.amount)
    return {"ok": True, "due": max(0.0, due - body.amount)}


# ---------------------------------------------------------- naya bill ----


@router.get("/rates")
async def rate_card(
    p: StaffPrincipal = Depends(require_staff_feature("billing")),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Dukaan ka rate card — daam yahin se aate hain, kahin aur se nahi."""
    from app.models import Rate

    rows = (
        await db.execute(
            select(Rate).where(Rate.is_active).order_by(Rate.service, Rate.garment)
        )
    ).scalars().all()
    return [
        {"service": r.service, "garment": r.garment, "unit": r.unit, "rate": float(r.rate)}
        for r in rows
    ]


@router.get("/customers/search", dependencies=[Depends(require_staff_feature("billing"))])
async def customer_search(
    q: str = Query(default="", max_length=60),
    p: StaffPrincipal = Depends(current_staff),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Bill banate waqt purana customer dhoondho — naam ya number se.

    Do baatein jaan-boojh kar aisi hain:

    1. **Number yahan bhi masked hi jaata hai.** Panel ka poora usool yahi
       hai: kisi ek staff ke phone se poori customer list nikal jana sabse
       aasan leak hai. Isliye list mein hamesha masked, aur poora number
       tabhi jab wo ek order ke liye maange (/orders/{n}/call, jo audit
       hota hai).
    2. **Chunne par `ref` milta hai, number nahi.** Bill isi ref se banta
       hai. Yani staff bina number dekhe bhi sahi customer par bill bana
       leta hai — aur naya duplicate customer nahi banta.

    Do akshar se kam par kuch nahi lautta: aadha akshar likhte hi poori
    dukaan lautana na staff ke kaam ka hai, na server ke.
    """
    term = q.strip()
    if len(term) < 2:
        return []
    digits = re.sub(r"\D", "", term)
    where = (
        Customer.phone.ilike(f"%{digits}%")
        if digits and len(digits) >= 3
        else Customer.name.ilike(f"%{term}%")
    )
    rows = (
        await db.execute(
            select(Customer)
            .where(where, Customer.is_active)
            .order_by(Customer.last_message_at.desc().nulls_last())
            .limit(8)
        )
    ).scalars().all()
    return [
        {
            "ref": str(c.id),
            "name": c.name or "",
            "phone_masked": mask_phone(c.phone),
        }
        for c in rows
    ]


class BillItemIn(BaseModel):
    service: str = Field(min_length=1, max_length=60)
    garment: str = Field(min_length=1, max_length=60)
    qty: float = Field(gt=0, le=999)


class BillIn(BaseModel):
    customer_name: str = Field(default="", max_length=120)
    # Do mein se ek: naya customer ho to number, purana ho to suggestion ka
    # ref. ref isliye ki panel kabhi poora number dikhata hi nahi — bina
    # number dekhe bhi staff sahi customer par bill bana sake.
    customer_phone: str = Field(default="", max_length=20)
    customer_ref: str = Field(default="", max_length=64)
    items: list[BillItemIn] = Field(min_length=1, max_length=30)
    advance: float = Field(default=0, ge=0)


@router.post("/bills", dependencies=[Depends(require_staff_feature("billing"))], status_code=201)
async def create_bill(
    body: BillIn,
    p: StaffPrincipal = Depends(current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Counter par khada aadmi apne phone se bill bana sake.

    Daam SIRF rate card se — bhejne wala koi rate bhej hi nahi sakta, wo
    field hi nahi hai. Wahi niyam jo WhatsApp wale bill par hai, taaki
    dono raston se ek hi hisaab bane.

    Aur sirf hisaab hi nahi — POORA raasta wahi hai jo dashboard ke New
    Bill ka hai: delivery date turnaround se, aur washerman ko WhatsApp
    par work order turant. Pehle panel ka bill "aadha" banta tha — na
    date, na kisi ko kaam ki khabar — aur wo order chupchaap pada rehta
    tha jab tak koi dashboard par dekh na le.
    """
    from datetime import date as _date, timedelta as _td
    from decimal import Decimal

    from app.models import Rate
    from app.services import app_settings
    from app.services.order_service import create_order
    from app.utils.phone import normalize_phone

    # Purana customer suggestion se chuna gaya? To uska number DB se aata
    # hai — staff ne dekha bhi nahi, likha bhi nahi, isliye typo ka sawal
    # hi nahi. Lookup tenant-scoped hai, to doosri dukaan ka ref yahan
    # kabhi khulta nahi (404, aur ye 404 hi sahi jawab hai).
    phone = (body.customer_phone or "").strip()
    name = (body.customer_name or "").strip()
    if body.customer_ref:
        try:
            ref = uuid_module.UUID(body.customer_ref)
        except ValueError:
            raise HTTPException(status_code=400, detail="Pick the customer again")
        chosen = (
            await db.execute(select(Customer).where(Customer.id == ref))
        ).scalar_one_or_none()
        if chosen is None:
            raise HTTPException(status_code=404, detail="That customer was not found")
        phone = chosen.phone
        name = name or (chosen.name or "")
    if not phone:
        raise HTTPException(status_code=400, detail="Customer number is needed")

    rows = (await db.execute(select(Rate).where(Rate.is_active))).scalars().all()
    by_key = {(r.service.strip().lower(), r.garment.strip().lower()): r for r in rows}
    items, total = [], Decimal("0")
    for it in body.items:
        row = by_key.get((it.service.strip().lower(), it.garment.strip().lower()))
        if row is None:
            raise HTTPException(
                status_code=400,
                detail=f"{it.garment} ({it.service}) is not on the rate card",
            )
        qty = Decimal(str(it.qty))
        amount = row.rate * qty
        total += amount
        items.append(
            {
                "type": row.garment, "service": row.service, "garment": row.garment,
                "qty": float(qty), "rate": float(row.rate), "amount": float(amount),
            }
        )
    if body.advance > float(total):
        raise HTTPException(status_code=400, detail="Advance cannot be more than the bill")

    # Delivery date dashboard wale bill ki tarah: aaj + turnaround.
    # Bina iske customer se "kab milega" ka koi jawab hi nahi hota tha.
    try:
        turnaround = int(await app_settings.get(db, "turnaround_days"))
    except Exception:
        turnaround = 2
    order = await create_order(
        db,
        customer_phone=normalize_phone(phone),
        customer_name=name or None,
        items=items,
        total_amount=total,
        expected_delivery=_date.today() + _td(days=max(turnaround, 1)),
        advance_hint=Decimal(str(body.advance)) if body.advance else None,
        created_by=p.staff.name,
    )
    # Advance ko paisa maankar ledger mein likhte hain — create ke baad,
    # wahi rasta jo dashboard ke New Bill par hai.
    if body.advance:
        from app.models import PaymentMethod
        from app.services.order_service import record_payment

        await record_payment(
            db, order, amount=Decimal(str(body.advance)),
            method=PaymentMethod.CASH, recorded_by=p.staff.name,
        )
    # Washerman ko WhatsApp par work order — wahi jo dashboard ka New Bill
    # bhejta hai. Best-effort: khabar na ja paye to bhi bill ban chuka hai.
    try:
        from app.services.work_orders import send_work_order

        sent_status = await send_work_order(db, order, headline="Naya order aaya")
        if sent_status == "no_staff":
            log.info("panel_bill_no_washer", order=order.order_number)
    except Exception:
        log.exception("panel_bill_work_order_failed", order=order.order_number)

    await audit.record(
        actor_role="staff", actor=p.staff.name, action="bill_created_from_panel",
        args={"order": order.order_number, "total": float(total)},
        result="ok", tenant_id=p.staff.tenant_id,
    )
    log.info("panel_bill_created", staff=p.staff.name, order=order.order_number)
    return {
        "order_number": order.order_number,
        "total": float(total),
        "due": float(total) - body.advance,
    }


# --------------------------------------------------- manager ka hissa ----


@router.get("/team", dependencies=[Depends(require_staff_feature("staff_reports"))])
async def team_view(
    p: StaffPrincipal = Depends(require_manager), db: AsyncSession = Depends(get_db)
) -> list[dict]:
    """Kis aadmi par kitna kaam hai, aur aaj usne kitna nipta diya."""
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    rows = (
        await db.execute(select(Staff).where(Staff.is_active).order_by(Staff.name))
    ).scalars().all()
    out = []
    for s in rows:
        open_n = (
            await db.execute(
                select(func.count()).select_from(Task).where(
                    Task.assigned_staff_id == s.id, Task.status == TASK_OPEN
                )
            )
        ).scalar_one()
        done_n = (
            await db.execute(
                select(func.count()).select_from(Task).where(
                    Task.assigned_staff_id == s.id,
                    Task.status == TASK_DONE,
                    Task.completed_at >= since,
                )
            )
        ).scalar_one()
        out.append(
            {
                "name": s.name, "role": s.role.name,
                "phone_masked": mask_phone(s.phone),
                "open": open_n, "done_24h": done_n,
                "has_login": bool(s.password_hash),
            }
        )
    return out


# ------------------------------------------------- rozmarra ke 4 kaam ----


async def _my_order(db: AsyncSession, p: StaffPrincipal, number: str) -> Order:
    """Order — par sirf wahi jo is aadmi ka hai (manager ko sab)."""
    order = (
        await db.execute(select(Order).where(Order.order_number == number.strip().upper()))
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail=f"{number} not found")
    if p.is_manager:
        return order
    mine = (
        await db.execute(
            select(func.count()).select_from(Task).where(
                Task.order_id == order.id, Task.assigned_staff_id == p.staff.id
            )
        )
    ).scalar_one()
    if (
        not mine
        and order.assigned_washer_id != p.staff.id
        and order.assigned_delivery_id != p.staff.id
    ):
        raise HTTPException(status_code=403, detail="This order is not yours")
    return order


@router.get("/orders/{number}/call")
async def call_customer(
    number: str,
    p: StaffPrincipal = Depends(current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Customer ka POORA number — sirf apne order ke liye, aur har baar audit.

    List mein number masked rehta hai (kisi ke phone se poori customer
    list nikal jana sabse aasan leak hai), par delivery wale ko call to
    karna hi padta hai. Isliye poora number tabhi milta hai jab wo
    maange, aur uska record rehta hai.
    """
    order = await _my_order(db, p, number)
    cust = await db.get(Customer, order.customer_id)
    if cust is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    await audit.record(
        actor_role="staff", actor=p.staff.name, action="customer_number_viewed",
        args={"order": order.order_number}, result="call", tenant_id=p.staff.tenant_id,
    )
    log.info("staff_called_customer", staff=p.staff.name, order=order.order_number)
    return {"phone": cust.phone, "name": cust.name or "Customer"}


@router.post("/orders/{number}/photo", status_code=201)
async def attach_photo(
    number: str,
    photo: UploadFile = File(...),
    note: str = Form(default=""),
    p: StaffPrincipal = Depends(current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Kapde ki halat ka saboot — daag, phata hua, kitne peace.

    Baad mein "aisa to nahi tha" wali baat par yahi photo jawab deti hai.
    Photo order ke notes se judti hai aur owner ko turant khabar jaati hai.
    """
    import uuid as _uuid
    from pathlib import Path as _P

    order = await _my_order(db, p, number)
    kinds = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    if (photo.content_type or "") not in kinds:
        raise HTTPException(status_code=400, detail="Photos only (jpg/png)")
    blob = await photo.read()
    if not blob:
        raise HTTPException(status_code=400, detail="That photo is empty")
    if len(blob) > 8_000_000:
        raise HTTPException(status_code=400, detail="That photo is too big (8MB max)")

    name = f"job-{_uuid.uuid4().hex}{kinds[photo.content_type]}"
    media_dir = _P(__file__).resolve().parent.parent / "media"
    media_dir.mkdir(exist_ok=True)
    # Disk par likhna blocking hai. Ek phone ke liye ye do milliseconds hai,
    # par jab pandrah dukaanon ke staff ek saath photo bhejenge to yahi
    # milliseconds poore event loop ko rokte hain — isliye thread par.
    await asyncio.to_thread((media_dir / name).write_bytes, blob)

    stamp = datetime.now(timezone.utc).strftime("%d %b %H:%M")
    line = f"[{stamp} {p.staff.name}] photo /admin/media/{name}"
    if note.strip():
        line += f" - {note.strip()[:200]}"
    order.notes = f"{order.notes}\n{line}" if order.notes else line
    db.add(order)
    await db.commit()

    # Owner ko khabar background mein. Pehle ye yahin inline hoti thi, yani
    # request WhatsApp ke jawab ka intezaar karti thi — dheeme network par
    # delivery wale ka phone tab tak "atka" dikhta tha, jabki uska kaam ho
    # chuka tha. Photo mehfooz hai; khabar ek pal baad chali jayegi.
    from app.services import background, team

    msg = (
        f"📷 {p.staff.name} added a photo on {order.order_number}"
        + (f": {note.strip()[:150]}" if note.strip() else "")
        + "\nYou can see it on the dashboard."
    )
    _from = p.staff.phone
    background.run_after(
        lambda bg: team.notify_admins(bg, msg, skip_phone=_from),
        tenant_id=p.staff.tenant_id,
        label="photo_notify",
    )
    await audit.record(
        actor_role="staff", actor=p.staff.name, action="job_photo_added",
        args={"order": order.order_number, "file": name},
        result=note[:100] or "photo", tenant_id=p.staff.tenant_id,
    )
    return {"ok": True, "file": name}


@router.get("/today")
async def today_summary(
    p: StaffPrincipal = Depends(current_staff), db: AsyncSession = Depends(get_db)
) -> dict:
    """Aaj ka apna hisaab — kitna kaam, kitna nipta, kitna paisa liya."""
    from app.models import Payment

    # "Aaj" dukaan ka aaj hai, UTC ka nahi. UTC ki aadhi raat IST mein subah
    # 5:30 hai — pehle 5:30 baje ye counter khud reset ho jaata tha, aur raat
    # 12 se 5:30 ke beech ka kaam pichhle din mein gina jaata tha. Baaki app
    # (scheduler, agent_tools) pehle se IST par hai; ye ek jagah chhoot gayi thi.
    start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)

    async def _count(*where):
        return (
            await db.execute(select(func.count()).select_from(Task).where(*where))
        ).scalar_one()

    out = {
        "pending": await _count(
            Task.assigned_staff_id == p.staff.id, Task.status == TASK_OPEN
        ),
        "done_today": await _count(
            Task.assigned_staff_id == p.staff.id,
            Task.status == TASK_DONE,
            Task.completed_at >= start,
        ),
        "collected_today": float(
            (
                await db.execute(
                    select(func.coalesce(func.sum(Payment.amount), 0)).where(
                        Payment.recorded_by == p.staff.name, Payment.received_at >= start
                    )
                )
            ).scalar_one()
            or 0
        ),
        "can_collect": p.has("cod_collection"),
    }
    if p.is_manager:
        out["shop_pending"] = await _count(Task.status == TASK_OPEN)
        out["shop_done_today"] = await _count(
            Task.status == TASK_DONE, Task.completed_at >= start
        )
    return out


@router.get("/route")
async def my_route(
    p: StaffPrincipal = Depends(current_staff), db: AsyncSession = Depends(get_db)
) -> dict:
    """Aaj kahan-kahan jaana hai — ek hi list mein, kaam ke kram se.

    Delivery wale ko pickup aur delivery dono; washerman ko uski dhulai
    ki kataar. Sabse pehle urgent, phir jiski delivery date sabse paas
    hai — taaki koi order neeche daba na rah jaye.
    """
    is_delivery = p.staff.role is StaffRole.DELIVERY
    stages = (
        (OrderStatus.PICKUP_ASSIGNED, OrderStatus.READY, OrderStatus.OUT_FOR_DELIVERY)
        if is_delivery
        else (
            OrderStatus.RECEIVED, OrderStatus.PICKED_UP, OrderStatus.IN_WASH,
            OrderStatus.IN_DRY, OrderStatus.IN_IRON,
        )
    )
    col = Order.assigned_delivery_id if is_delivery else Order.assigned_washer_id
    q = (
        select(Order)
        .where(Order.status.in_(stages))
        .order_by(
            Order.priority.desc(),
            Order.expected_delivery.asc().nullslast(),
            Order.created_at,
        )
        .limit(50)
    )
    if not p.is_manager:
        q = q.where(col == p.staff.id)
    rows = (await db.execute(q)).scalars().all()

    stops = []
    for o in rows:
        cust = await db.get(Customer, o.customer_id)
        kind = (
            "Pickup"
            if o.status is OrderStatus.PICKUP_ASSIGNED
            else ("Delivery" if is_delivery else "Dhulai")
        )
        stops.append(
            {
                "number": o.order_number,
                "kind": kind,
                "customer": (cust.name or "Customer") if cust else "?",
                "phone_masked": mask_phone(cust.phone if cust else ""),
                "address": (cust.address or "").strip() if cust else "",
                "items": items_summary(o),
                "status": o.status.name,
                "urgent": o.priority == "urgent",
                "due": float((o.total_amount or 0) - (o.amount_paid or 0)),
                "delivery": o.expected_delivery.isoformat() if o.expected_delivery else None,
            }
        )
    return {"stops": stops, "kind": "delivery" if is_delivery else "wash"}
