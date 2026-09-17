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
from datetime import date, datetime, timedelta, timezone

import structlog
from fastapi import (
    APIRouter, Cookie, Depends, File, Form, HTTPException, Query, Request, Response,
    UploadFile,
)
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import (
    PaymentStatus,
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
from app.services import audit, delivery, plans, receipt
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
from app.schemas.orders import DeliverIn, PieceIn
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


# ------------------------------------------------------- bill banane ka haq ----

# Bill kaun bana sakta hai. Plan `billing` khol deta hai ki DUKAAN bill bana
# sakti hai; ye tay karta hai ki us dukaan ka KAUN. Washerman nahi: wo kapdon
# ke saath hai, counter par nahi, aur ab rate bhi badla ja sakta hai — jo
# haath rate badal sakta hai wo dukaan ka paisa badal sakta hai.
#
# SUPERVISOR ("Washerman / Manager") is list mein hai kyunki wo dhulai ke
# saath counter bhi sambhalta hai — chhoti dukaan ka sabse aam sach, aur
# wahi aadmi shaam ko akela hota hai.
BILLING_ROLES = (StaffRole.DELIVERY, StaffRole.MANAGER, StaffRole.SUPERVISOR, StaffRole.ADMIN)


def can_bill(p: StaffPrincipal) -> bool:
    return p.staff.role in BILLING_ROLES


# Paisa (bill ka total, baaki, collect, bill share) sirf unhe jo paisa
# sambhalte hain — washerman ko kapde dikhte hain, rakam nahi. Server yahin
# rok deta hai; UI mein chhupana kaafi nahi tha (API se sab aa jaata tha).
can_money = can_bill

# Grahak se baat (poora number, pata, WhatsApp message) bhi unhi ki — washerman
# ka grahak se koi lena-dena nahi: use kapde, status aur photo chahiye, bas.
can_contact = can_bill


def require_biller(feature: str = "billing"):
    """Plan ka pehra + role ka pehra, ek hi jagah.

    Dono alag jawab dete hain aur ye jaan-boojh kar hai: 402 ka matlab hai
    "owner se plan upgrade karwao", 403 ka matlab "ye kaam tumhara nahi".
    Ek hi code dono ke liye bhejna staff ko galat aadmi ke paas bhejta hai.
    """

    plan_gate = require_staff_feature(feature)

    def _dep(p: StaffPrincipal = Depends(plan_gate)) -> StaffPrincipal:
        if not can_bill(p):
            raise HTTPException(
                status_code=403,
                detail="Bill counter par banta hai — aapke role mein ye nahi hai",
            )
        return p

    _dep.__name__ = f"require_biller_{feature}"
    return _dep


# Jo status paise ke hisaab se "zinda" nahi hain. Cancelled order ka due
# maangna galat hai; udhaar ki ginti mein wo aana hi nahi chahiye.
DEAD_FOR_MONEY = (OrderStatus.CANCELLED,)


async def customer_outstanding(
    db: AsyncSession, customer_id, *, exclude_order_id=None
) -> tuple[float, int]:
    """Us grahak ka kul purana udhaar — (rakam, kitne bill).

    Sirf isi tenant ke order ginne jaate hain: SELECT par tenant filter
    apne aap lagta hai (database.py ka ORM event), isliye yahan alag se
    kuch nahi likhna padta aur galti se doosri dukaan ka udhaar jud nahi
    sakta.
    """
    q = select(Order).where(
        Order.customer_id == customer_id,
        Order.status.notin_(DEAD_FOR_MONEY),
    )
    if exclude_order_id is not None:
        q = q.where(Order.id != exclude_order_id)
    rows = (await db.execute(q)).scalars().all()
    due, n = 0.0, 0
    for o in rows:
        d = float(o.total_amount or 0) - float(o.amount_paid or 0)
        if d > 0.009:          # paisa-bhar ka rounding udhaar nahi hai
            due += d
            n += 1
    return round(due, 2), n


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
async def me(
    p: StaffPrincipal = Depends(current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Panel ka pehla call — kaun hoon main aur kya-kya khula hai.

    UI isi ke `features` se tabs dikhata/chhupata hai; asli rok API par
    hi hai, isliye dono kabhi alag nahi ho sakte.
    """
    from app.services import app_settings, urgent as urgent_svc
    from app.services.whatsapp import shop_can_send

    return {
        # Owner ne Settings mein band kiya ho to pata + Route button nahi
        "show_route": bool(await app_settings.get(db, "staff_show_route")),
        # ⚡ Urgent ka default charge/din — panel preview dikhaye, server phir
        # bhi khud hisaab lagata hai
        "urgent": await urgent_svc.config(db),
        # "Ask" sawaal owner ke WhatsApp par bhejta hai — API juda na ho to
        # sawaal kahin nahi pahunchta, isliye button hi nahi
        "can_ask": shop_can_send(p.tenant),
        "name": p.staff.name,
        "phone": mask_phone(p.staff.phone),
        "role": p.staff.role.name,
        "is_manager": p.is_manager,
        # Plan bill khol sakta hai par role phir bhi mana kar sakta hai.
        # Panel isi ek jhande se Bill/New tab dikhata hai — do jagah do
        # hisaab rakhne par hi "dikh raha hai par chalta nahi" hota hai.
        "can_bill": can_bill(p),
        "can_money": can_money(p),
        "can_contact": can_contact(p),
        "can_expense": can_expense(p),
        "must_change_password": p.staff.must_change_password,
        "shop": p.tenant.shop_name if p.tenant else "",
        "plan": plans.get(p.plan).name,
        "features": p.features,
    }


@router.get("/boot")
async def boot(
    p: StaffPrincipal = Depends(current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Panel khulte hi jo paanch cheezein chahiye, ek hi request mein.

    Phone se server tak ek round trip 300-900 ms ka hai (server door hai,
    signal kamzor). Pehle panel /me, /route, /tasks, /today, /notifications
    alag-alag maangta tha — paanch round trips, 3-4 second sirf khulne mein.
    Ab ek. Koi ek hissa fail ho to baaki phir bhi aaye (panel khali na rahe).
    """
    out: dict = {"me": await me(p, db)}
    for key, fn in (
        ("route", lambda: my_route(limit=30, offset=0, tab="todo", p=p, db=db)),
        ("tasks", lambda: my_tasks(tab="mine", limit=30, offset=0, p=p, db=db)),
        ("today", lambda: today_summary(p, db)),
        ("notifications", lambda: notifications(p, db)),
    ):
        try:
            out[key] = await fn()
        except Exception:
            log.exception("staff_boot_part_failed", part=key, staff=p.staff.name)
            out[key] = None
    return out


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


def _tasks_query(p: StaffPrincipal, tab: str):
    """Tab ke hisaab se query — count aur page dono isi se bante hain,
    taaki 'Page 3 of 9' aur list kabhi alag baat na kahein."""
    q = select(Task)
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
    return q



async def _prefetch(db: AsyncSession, tasks: list[Task], p: StaffPrincipal) -> dict:
    """Ek baar mein sab orders, customers aur staff — na ki har task par.

    Pehle _task_card har task ke liye alag se order, uska customer aur
    assignee maangta tha: 200 task = 386 SQL queries aur ~300ms. Ek dukaan
    ke liye chalta tha; 200 khule kaam wali dukaan par panel ka har refresh
    DB par 386 baar jaata tha. Ab teen query, chahe kitne bhi task hon.
    """
    order_ids = {t.order_id for t in tasks if t.order_id}
    orders = {}
    customers = {}
    staff = {}
    if order_ids:
        rows = (await db.execute(select(Order).where(Order.id.in_(order_ids)))).scalars().all()
        orders = {o.id: o for o in rows}
        cust_ids = {o.customer_id for o in rows if o.customer_id}
        if cust_ids:
            crows = (
                await db.execute(select(Customer).where(Customer.id.in_(cust_ids)))
            ).scalars().all()
            customers = {c.id: c for c in crows}
    if p.is_manager:
        sids = {t.assigned_staff_id for t in tasks if t.assigned_staff_id}
        if sids:
            srows = (await db.execute(select(Staff).where(Staff.id.in_(sids)))).scalars().all()
            staff = {st.id: st for st in srows}
    return {"orders": orders, "customers": customers, "staff": staff}


def _task_card(t: Task, p: StaffPrincipal, pre: dict) -> dict:
    order = pre["orders"].get(t.order_id) if t.order_id else None
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
        st = pre["staff"].get(t.assigned_staff_id)
        card["assignee"] = st.name if st else None
    if order is not None:
        cust = pre["customers"].get(order.customer_id)
        card["order"] = {
            "number": order.order_number,
            "customer": (cust.name or "Customer") if cust else "?",
            # Poora number kabhi nahi — kaam ke liye itna kaafi hai
            "phone_masked": mask_phone(cust.phone if cust else ""),
            "items": items_summary(order),
            "delivery": order.expected_delivery.isoformat() if order.expected_delivery else None,
            "status": order.status.name,
            "notes": order.notes,
            "clothes": delivery.counts(order),
        }
        if can_money(p):
            card["order"]["total"] = float(order.total_amount or 0)
            card["order"]["due"] = float((order.total_amount or 0) - (order.amount_paid or 0))
    return card


@router.get("/tasks")
async def my_tasks(
    tab: str = "mine",
    limit: int = 40,
    offset: int = 0,
    p: StaffPrincipal = Depends(current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Ek page bhar kaam.

    Pehle 200 tak sab ek saath jaate the — 79 KB, jo 3G par do second sirf
    utarne mein lagta hai, aur panel ye har 30 second par maangta hai. Ab
    ek page (40) aata hai aur `total` alag se, taaki "aur dikhayein" sach
    bole.
    """
    tab = tab if tab in ("pending", "mine", "done", "cancelled") else "mine"
    limit = max(1, min(limit, 100))
    base = _tasks_query(p, tab)
    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    rows = (
        await db.execute(
            base.order_by(Task.urgent.desc(), Task.created_at.desc())
            .limit(limit).offset(max(0, offset))
        )
    ).scalars().all()
    pre = await _prefetch(db, rows, p)
    cards = [_task_card(t, p, pre) for t in rows]
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
    return {
        "tasks": cards, "counts": counts, "is_manager": p.is_manager,
        "total": total, "offset": max(0, offset), "limit": limit,
    }


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
    """Sawal seedha owner/manager tak — panel se bhi, WhatsApp ki tarah.

    Sirf tab jab dukaan ka WhatsApp API juda ho: sawaal owner ke WhatsApp
    par jaata hai. Bina API ke wo kahin nahi pahunchta tha aur staff
    samajhta tha ki pooch liya — isliye saaf mana (panel button bhi chhupata).
    """
    from app.services import team
    from app.services.whatsapp import shop_can_send

    from app.models import TaskMessage

    if not shop_can_send(p.tenant):
        raise HTTPException(
            status_code=409,
            detail="WhatsApp is not connected — please call the owner or manager",
        )
    t = await _my_task(db, p, code)
    text = body.text.strip()
    # Sawaal ab store hota hai — pehle sirf WhatsApp par ek line jaati thi,
    # to staff ko apna hi sawaal wapas nahi dikhta tha aur jawab kabhi
    # panel mein nahi aata tha.
    msg = TaskMessage(
        task_id=t.id, author_kind="staff", author_name=p.staff.name,
        text=text[:1000], read_by_staff_at=datetime.now(timezone.utc),
    )
    db.add(msg)
    await db.commit()
    await team.notify_admins(
        db, f"❓ {p.staff.name} ka sawal [{t.code}] par: {text[:280]}",
        skip_phone=p.staff.phone,
    )
    await audit.record(
        actor_role="staff", actor=p.staff.name, action="staff_asked",
        args={"code": t.code}, result=text[:150], tenant_id=p.staff.tenant_id,
    )
    return {"ok": True}


@router.get("/tasks/{code}/messages")
async def task_thread(
    code: str,
    p: StaffPrincipal = Depends(current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Is kaam par hui poori baat — sawaal aur jawab, kram se.

    Kholte hi jo unread the wo read ho jaate hain (badge isi se ghatta hai).
    """
    from app.models import TaskMessage

    t = await _my_task(db, p, code)
    rows = (
        await db.execute(
            select(TaskMessage).where(TaskMessage.task_id == t.id).order_by(TaskMessage.at)
        )
    ).scalars().all()
    now = datetime.now(timezone.utc)
    changed = False
    for m in rows:
        if m.author_kind == "owner" and m.read_by_staff_at is None:
            m.read_by_staff_at = now
            changed = True
    if changed:
        await db.commit()
    return {
        "code": t.code,
        "title": t.title,
        "messages": [
            {
                "who": m.author_kind,
                "name": m.author_name,
                "text": m.text,
                "at": m.at.astimezone(IST).strftime("%d %b, %I:%M %p"),
            }
            for m in rows
        ],
    }


@router.get("/notifications")
async def notifications(
    p: StaffPrincipal = Depends(current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Jo baatein mere liye nayi hain — abhi: owner ke bina padhe jawab.

    Panel ke ghante (bell) ka badge isi count se banta hai. Manager ko
    apne hi kaam ke jawab dikhte hain, poori dukaan ke nahi — warna badge
    hamesha laal rehta aur koi use dekhta hi nahi.
    """
    from app.models import TaskMessage

    rows = (
        await db.execute(
            select(TaskMessage, Task)
            .join(Task, Task.id == TaskMessage.task_id)
            .where(
                TaskMessage.author_kind == "owner",
                TaskMessage.read_by_staff_at.is_(None),
                Task.assigned_staff_id == p.staff.id,
            )
            .order_by(TaskMessage.at.desc())
            .limit(20)
        )
    ).all()
    return {
        "unread": len(rows),
        "items": [
            {
                "code": t.code,
                "title": t.title,
                "text": m.text,
                "from": m.author_name,
                "at": m.at.astimezone(IST).strftime("%d %b, %I:%M %p"),
            }
            for m, t in rows
        ],
    }


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
    plan mein jisme wo suvidha hai.

    Ijazat wahi jo baaki order endpoints ki hai (_my_order): pehle yahan
    apna alag niyam tha jisme "kisi ke naam nahi, par dukaan ke delivery
    wale ka" order nahi aata tha — row dikhti thi, detail 403 deti thi."""
    order = await _my_order(db, p, number)
    cust = await db.get(Customer, order.customer_id)
    from app.services import urgent as urgent_svc

    out = {
        "number": order.order_number,
        "customer": (cust.name or "Customer") if cust else "?",
        **({"phone_masked": mask_phone(cust.phone if cust else "")} if can_contact(p) else {}),
        "items": order.items or [],
        "items_text": items_summary(order),
        # har kapde ki line: kitne, kitne diye, kitne baaki (KG bore ke andar bhi)
        "lines": delivery.lines(order),
        "clothes": delivery.counts(order),
        "urgent": order.priority == "urgent",
        "status": order.status.name,
        "delivery": order.expected_delivery.isoformat() if order.expected_delivery else None,
        "notes": order.notes,
    }
    if can_money(p):
        out.update({
            "urgent_charge": sum(
                float(i.get("amount") or 0) for i in (order.items or []) if urgent_svc.is_charge_line(i)
            ),
            "total": float(order.total_amount or 0),
            "paid": float(order.amount_paid or 0),
            "due": float((order.total_amount or 0) - (order.amount_paid or 0)),
            "can_collect": p.has("cod_collection"),
        })
    # Kitni der se is stage par, aur deri hai to kyon (milestones sirf
    # order_timeline wale plan mein — neeche timeline ke saath)
    from app.services import turnaround

    tr = (await turnaround.track_many(db, [order]))[order.id]
    out["tracking"] = {k: v for k, v in tr.items()
                       if k != "milestones" or p.has("order_timeline")}
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


# ------------------------------------------------ order aage badhana ----

# Kaun kya kar sakta hai — bill_agent ke "done" command wala hi niyam:
# delivery wala pickup/delivery, washerman dhulai. Manager/owner sab.
_DELIVERY_ROLES = (StaffRole.DELIVERY, StaffRole.MANAGER, StaffRole.SUPERVISOR, StaffRole.ADMIN)
_WASH_ROLES = (StaffRole.WASHER, StaffRole.MANAGER, StaffRole.SUPERVISOR, StaffRole.ADMIN)


def _role_or_403(p: StaffPrincipal, roles, what: str) -> None:
    if p.staff.role not in roles:
        raise HTTPException(status_code=403, detail=f"{what} is not your role's job")


@router.post("/orders/{number}/deliver")
async def deliver_order(
    number: str,
    body: DeliverIn,
    p: StaffPrincipal = Depends(current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Grahak ko kapde diye — sab, ya kuch (baaki pending rehte hain)."""
    _role_or_403(p, _DELIVERY_ROLES, "Delivery")
    order = await _my_order(db, p, number)
    try:
        out = await delivery.deliver(
            db, order, [pk.model_dump() for pk in body.items] if body.items is not None else None,
            by=p.staff.name,
        )
    except delivery.DeliveryError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    from app.services import team

    await team.notify_admins(
        db, f"🚚 {p.staff.name}: {order.order_number} — {out['delivered_now']} kapde diye"
        + (f", {out['pending']} baaki" if out["pending"] else " (poora order)"),
        skip_phone=p.staff.phone,
    )
    return out


@router.post("/orders/{number}/picked-up")
async def order_picked_up(
    number: str,
    p: StaffPrincipal = Depends(current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _role_or_403(p, _DELIVERY_ROLES, "Pickup")
    order = await _my_order(db, p, number)
    try:
        return await delivery.picked_up(db, order, by=p.staff.name)
    except delivery.DeliveryError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/orders/{number}/washing")
async def order_washing(
    number: str,
    p: StaffPrincipal = Depends(current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Washerman: dhulai shuru (grahak ka page bhi 'Cleaning' par aa jaata hai)."""
    _role_or_403(p, _WASH_ROLES, "Washing")
    order = await _my_order(db, p, number)
    try:
        return await delivery.washing(db, order, by=p.staff.name)
    except delivery.DeliveryError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/orders/{number}/ready")
async def order_ready(
    number: str,
    p: StaffPrincipal = Depends(current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _role_or_403(p, _WASH_ROLES, "Washing")
    order = await _my_order(db, p, number)
    try:
        return await delivery.ready(db, order, by=p.staff.name)
    except delivery.DeliveryError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/orders/{number}/dues")
async def order_dues(
    number: str,
    p: StaffPrincipal = Depends(require_biller()),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Is order ka due + us grahak ka purana baaki.

    Darwaze par khada delivery wala ek hi baar poochta hai "kitna dena
    hai" — aur uska sahi jawab sirf is bill ka due nahi hai. Alag endpoint
    isliye ki collect wali sheet ise maang sake bina poora order detail
    utaare (`_my_order` wahi pehra lagata hai: apna order hi khulta hai).
    """
    order = await _my_order(db, p, number)
    due = float((order.total_amount or 0) - (order.amount_paid or 0))
    prev, bills = await customer_outstanding(db, order.customer_id, exclude_order_id=order.id)
    return {
        "number": order.order_number,
        "due": max(0.0, round(due, 2)),
        "previous_due": prev,
        "previous_bills": bills,
        "grand_total": round(max(0.0, due) + prev, 2),
    }


class CollectIn(BaseModel):
    amount: float = Field(gt=0)
    method: str = Field(pattern="^(cash|upi)$")
    # Grahak ne "kul dena hai" wala poora paisa diya. Default False: bina
    # maange kisi doosre order ko chhoona nahi — delivery wale ne jo bola
    # wahi hona chahiye, aur us bill ka due jitna hi ceiling rehta hai.
    settle_previous: bool = False


@router.post("/orders/{number}/collect")
async def collect_payment(
    number: str,
    body: CollectIn,
    p: StaffPrincipal = Depends(require_biller("cod_collection")),
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
    from decimal import Decimal

    due = float((order.total_amount or 0) - (order.amount_paid or 0))
    prev_due, _ = await customer_outstanding(db, order.customer_id, exclude_order_id=order.id)
    ceiling = due + (prev_due if body.settle_previous else 0.0)
    if body.amount > ceiling + 0.01:
        raise HTTPException(status_code=400, detail=f"Only ₹{ceiling:.0f} is due")

    method = PaymentMethod.CASH if body.method == "cash" else PaymentMethod.UPI
    left = Decimal(str(body.amount))
    settled = []

    # Sabse purana bill pehle. Ye grahak ka apna hisaab-kitaab hai: koi bhi
    # dukaandar naya bill chukta karke purana udhaar khula nahi chhodta.
    # Aur bina iske "kul dena hai" wala paisa is ek order par overpayment
    # ban jaata aur purane bill month-end tak due dikhte rehte.
    if body.settle_previous and left > Decimal(str(due)):
        older = (
            await db.execute(
                select(Order)
                .where(
                    Order.customer_id == order.customer_id,
                    Order.id != order.id,
                    Order.status.notin_(DEAD_FOR_MONEY),
                )
                .order_by(Order.created_at)
            )
        ).scalars().all()
        for o in older:
            if left <= 0:
                break
            o_due = (o.total_amount or Decimal("0")) - (o.amount_paid or Decimal("0"))
            if o_due <= Decimal("0.009"):
                continue
            take = min(left, o_due)
            # purane bill par alag-alag message nahi — ek hi paisa hai, ek hi raseed
            # (neeche is order par, ya aakhri purane bill par)
            left -= take
            await record_payment(
                db, o, amount=take, method=method, recorded_by=p.staff.name,
                notify_customer=left <= 0,
            )
            settled.append({"order": o.order_number, "amount": float(take)})

    if left > 0:
        await record_payment(db, order, amount=left, method=method, recorded_by=p.staff.name)

    await audit.record(
        actor_role="staff", actor=p.staff.name, action="cod_collected",
        args={
            "order": order.order_number, "amount": body.amount, "method": body.method,
            "settled_older": settled,
        },
        result="ok", tenant_id=p.staff.tenant_id,
    )
    log.info(
        "cod_collected", staff=p.staff.name, order=order.order_number,
        amount=body.amount, older=len(settled),
    )
    await db.refresh(order)
    new_due = float((order.total_amount or 0) - (order.amount_paid or 0))
    new_prev, _ = await customer_outstanding(db, order.customer_id, exclude_order_id=order.id)
    return {
        "ok": True,
        "due": max(0.0, new_due),
        "previous_due": new_prev,
        "settled_older": settled,
    }


# ---------------------------------------------------------- naya bill ----


@router.get("/rates")
async def rate_card(
    p: StaffPrincipal = Depends(require_biller()),
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


class NewRateIn(BaseModel):
    service: str = Field(min_length=1, max_length=60)
    # KG service ka item naam khali hota hai (poora bora tulta hai) — dashboard
    # jaisa hi. Piece wali service par naam zaroori.
    garment: str = Field(default="", max_length=60)
    unit: str = Field(default="pc", pattern="^(pc|kg)$")
    rate: float = Field(gt=0, le=100000)

    @model_validator(mode="after")
    def _piece_needs_a_name(self):
        if self.unit == "pc" and not self.garment.strip():
            raise ValueError("Item name is required for per-piece services")
        if self.unit == "kg":
            self.garment = ""
        return self


@router.post("/rates", status_code=201)
async def rate_add(
    body: NewRateIn,
    p: StaffPrincipal = Depends(require_biller()),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Counter par grahak aisa kapda laaya jo rate card par nahi — bill
    chhod kar owner ko phone karne ke bajaye yahin jod do.

    Jo bill bana sakta hai wahi jod sakta hai (owner ka faisla). Rate card
    har agle bill ka daam hai, isliye har jod audit hota hai aur owner ko
    dikhta hai kisne kya joda.
    """
    from decimal import Decimal

    from app.services.rate_card import DuplicateRate, add_rate

    try:
        row, revived = await add_rate(
            db, service=body.service, garment=body.garment, unit=body.unit,
            rate=Decimal(str(body.rate)),
        )
    except DuplicateRate as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await audit.record(
        actor_role="staff", actor=p.staff.name, action="rate_added",
        args={"service": row.service, "garment": row.garment, "unit": row.unit,
              "rate": body.rate, "revived": revived},
        result=f"{row.service} / {row.garment} ₹{body.rate}",
    )
    log.info("staff_rate_added", staff=str(p.staff.id), service=row.service,
             garment=row.garment, rate=body.rate, revived=revived)
    return {"service": row.service, "garment": row.garment, "unit": row.unit,
            "rate": float(row.rate)}


# ------------------------------------------------------------- kharcha ----

# Kharcha wahi likhe jo paisa haath mein rakhta hai: counter (manager) aur
# raaste ka kharcha (delivery — petrol). Washerman nahi. Bill wali list hi
# hai, jaan-boojh kar: dono ka matlab "dukaan ka paisa sambhalta hai".
EXPENSE_ROLES = BILLING_ROLES
# Purani tareekh ka kharcha owner dashboard se — staff sirf haal ka likhe,
# taaki pichhle mahine ka hisaab chupke se na badle.
EXPENSE_BACKDATE_DAYS = 7


def can_expense(p: StaffPrincipal) -> bool:
    return p.staff.role in EXPENSE_ROLES


def require_expense_role(p: StaffPrincipal = Depends(current_staff)) -> StaffPrincipal:
    if not can_expense(p):
        raise HTTPException(status_code=403, detail="Kharcha likhna aapke role mein nahi hai")
    return p


class StaffExpenseIn(BaseModel):
    category: str = Field(min_length=1, max_length=60)
    amount: float = Field(gt=0, le=1_000_000)
    spent_on: date | None = None
    description: str = Field(default="", max_length=300)


@router.get("/expenses")
async def my_expenses(
    p: StaffPrincipal = Depends(require_expense_role),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Categories + sirf APNE daale kharche (pichhle 30 din)."""
    from app.models import Expense
    from app.services import expenses as exp_svc

    since = datetime.now(IST).date() - timedelta(days=30)
    rows = (
        await db.execute(
            select(Expense)
            .where(Expense.staff_id == p.staff.id, Expense.spent_on >= since)
            .order_by(Expense.spent_on.desc(), Expense.created_at.desc())
            .limit(100)
        )
    ).scalars().all()
    return {
        "categories": await exp_svc.all_categories(db),
        "expenses": [exp_svc.row(e) for e in rows],
        "total": float(sum((e.amount for e in rows), 0)),
        "backdate_days": EXPENSE_BACKDATE_DAYS,
    }


@router.post("/expenses", status_code=201)
async def add_my_expense(
    body: StaffExpenseIn,
    p: StaffPrincipal = Depends(require_expense_role),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from decimal import Decimal

    from app.services import expenses as exp_svc

    category = await exp_svc.canonical_category(db, body.category)
    if category is None:
        # Nayi category owner banata hai — warna har staff apni spelling
        # bana deta aur report ka donut tukdon mein bikhar jaata
        raise HTTPException(status_code=400, detail="Pick a category from the list")
    today = datetime.now(IST).date()
    spent_on = body.spent_on or today
    if spent_on > today:
        raise HTTPException(status_code=400, detail="Date cannot be in the future")
    if (today - spent_on).days > EXPENSE_BACKDATE_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"Only the last {EXPENSE_BACKDATE_DAYS} days — ask the owner for older ones",
        )
    who = f"{p.staff.name} ({p.staff.role.name.title()})"
    exp = await exp_svc.record(
        db, category=category, amount=Decimal(str(body.amount)), spent_on=spent_on,
        description=body.description, added_by=who, staff_id=p.staff.id,
    )
    await audit.record(
        actor_role="staff", actor=p.staff.name, action="expense_added",
        args={"category": category, "amount": body.amount, "spent_on": spent_on.isoformat()},
        result=f"₹{body.amount} {category}",
    )
    log.info("staff_expense_added", staff=str(p.staff.id), category=category, amount=body.amount)
    return exp_svc.row(exp)


@router.get("/customers/search", dependencies=[Depends(require_biller())])
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
    out = []
    for c in rows:
        # Purana udhaar yahin bata dete hain. Counter par yahi wo pal hai
        # jab grahak saamne khada hai aur paisa maanga ja sakta hai — bill
        # ban jaane ke baad wo ja chuka hota hai.
        due, bills = await customer_outstanding(db, c.id)
        out.append(
            {
                "ref": str(c.id),
                "name": c.name or "",
                "phone_masked": mask_phone(c.phone),
                "due": due,
                "due_bills": bills,
            }
        )
    return out


class BillItemIn(BaseModel):
    service: str = Field(min_length=1, max_length=60)
    # KG wali service ka rate-card row bina kapde ke naam ka hota hai
    # ("Wash & Fold (kg)", garment ""). Pehle yahan min_length=1 tha, yaani
    # panel se kg ka bill ban hi nahi sakta tha — 422.
    garment: str = Field(default="", max_length=60)
    qty: float = Field(gt=0, le=999)
    # Rate card ka daam hi default hai. Ye bharne par us EK line ka daam
    # badalta hai — rate card chhua nahi jaata, agla bill phir se card se
    # banta hai. Har override audit hota hai (neeche), kyunki counter par
    # chupke se daam girana hi wo chori hai jo kisi report mein nahi dikhti.
    rate: float | None = Field(default=None, ge=0, le=100000)
    # Sirf KG line par: bore mein kaunse kapde kitne (daam nahi, ginti).
    pieces: list[PieceIn] = Field(default_factory=list, max_length=50)


class BillIn(BaseModel):
    customer_name: str = Field(default="", max_length=120)
    # Do mein se ek: naya customer ho to number, purana ho to suggestion ka
    # ref. ref isliye ki panel kabhi poora number dikhata hi nahi — bina
    # number dekhe bhi staff sahi customer par bill bana sake.
    customer_phone: str = Field(default="", max_length=20)
    customer_ref: str = Field(default="", max_length=64)
    items: list[BillItemIn] = Field(min_length=1, max_length=30)
    advance: float = Field(default=0, ge=0)
    # Chhoot do tareeke se maangi ja sakti hai. Dono aaye to PERCENT chalta
    # hai aur rakam nazarandaz hoti hai — "10% ya ₹50, jo bhi zyada ho"
    # jaisa jugaad server par nahi hona chahiye; ek bill, ek hisaab.
    discount_percent: float = Field(default=0, ge=0, le=100)
    discount_amount: float = Field(default=0, ge=0)
    # Kapde dukaan mein hain (grahak khud laaya) ya lene jaana hai?
    # Isi ek jawab se tay hota hai ki order delivery wale ke panel mein
    # aayega ya washer ke. Default: dukaan mein — counter par yahi aam hai.
    needs_pickup: bool = False
    # ⚡ Urgent. urgent_charge None = owner ki setting ka default, 0 = maaf.
    urgent: bool = False
    # "New bill" kholne se Save tak (turnaround tracking)
    bill_seconds: int | None = None
    urgent_charge: float | None = Field(default=None, ge=0, le=100000)
    # Reward/coupon code — server validate karta hai (grahak-locked bhi)
    coupon_code: str = Field(default="", max_length=30)


@router.get("/customers/{ref}/rewards", dependencies=[Depends(require_biller())])
async def customer_rewards(ref: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Bill banate waqt: is grahak ke jo reward lag sakte hain (ek tap Apply)."""
    from app.services import rewards

    try:
        cid = uuid_module.UUID(ref)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad ref")
    cust = await db.get(Customer, cid)
    if cust is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return {"available": await rewards.available(db, cid)}


@router.post("/bills", dependencies=[Depends(require_biller())], status_code=201)
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
    items, gross = [], Decimal("0")
    overrides = []          # audit ke liye: kis line par card se kitna alag
    for it in body.items:
        row = by_key.get((it.service.strip().lower(), it.garment.strip().lower()))
        if row is None:
            raise HTTPException(
                status_code=400,
                detail=f"{it.garment} ({it.service}) is not on the rate card",
            )
        qty = Decimal(str(it.qty))
        card_rate = Decimal(str(row.rate))
        rate = card_rate if it.rate is None else Decimal(str(it.rate))
        amount = (rate * qty).quantize(Decimal("0.01"))
        gross += amount
        line = {
            "type": row.garment or row.service, "service": row.service, "garment": row.garment,
            "qty": float(qty), "rate": float(rate), "amount": float(amount), "unit": row.unit,
        }
        if row.unit == "kg" and it.pieces:
            # pc line par ginti ka koi matlab nahi (qty hi ginti hai) —
            # wahan aaye to chup-chaap chhod dete hain
            line["pieces"] = [pc.model_dump() for pc in it.pieces]
        if rate != card_rate:
            # Bill par hamesha dikhega ki card ka daam kya tha. Baad mein
            # "maine to poora liya tha" wali baat ka jawab yahi line hai.
            line["card_rate"] = float(card_rate)
            overrides.append(
                {"garment": row.garment, "from": float(card_rate), "to": float(rate)}
            )
        items.append(line)

    # Chhoot: percent pehle, warna rakam. Bill se zyada chhoot = 400, warna
    # total rinaatmak ho jaata aur "due" ulta paisa dikhane lagta.
    if body.discount_percent:
        discount = (gross * Decimal(str(body.discount_percent)) / 100).quantize(Decimal("0.01"))
    else:
        discount = Decimal(str(body.discount_amount or 0)).quantize(Decimal("0.01"))
    if discount > gross:
        raise HTTPException(status_code=400, detail="Discount is more than the bill")

    # ⚡ Urgent: charge chhoot ke BAAD judta hai (chhoot kapdon par hai, jaldi
    # karne ki fees par nahi). urgent_charge na bheja = owner ki setting ka
    # default; 0 = maaf. Order phir bhi urgent rehta hai.
    from app.services import urgent as urgent_svc

    urgent_cfg = await urgent_svc.config(db)
    urgent_amt = Decimal("0")
    if body.urgent:
        urgent_amt = (
            urgent_svc.default_charge(urgent_cfg, gross) if body.urgent_charge is None
            else Decimal(str(body.urgent_charge)).quantize(Decimal("0.01"))
        )
        if urgent_amt > 0:
            items.append(urgent_svc.line(urgent_amt))
    total = gross - discount + urgent_amt

    if body.advance > float(total):
        raise HTTPException(status_code=400, detail="Advance cannot be more than the bill")

    # Delivery date dashboard wale bill ki tarah: aaj + turnaround.
    # Bina iske customer se "kab milega" ka koi jawab hi nahi hota tha.
    # Urgent = owner ki "urgent delivery days" (default kal).
    try:
        turnaround = int(await app_settings.get(db, "turnaround_days"))
    except Exception:
        turnaround = 2
    delivery = (
        urgent_svc.delivery_date(urgent_cfg) if body.urgent
        else _date.today() + _td(days=max(turnaround, 1))
    )
    order = await create_order(
        db,
        customer_phone=normalize_phone(phone),
        customer_name=name or None,
        items=items,
        # total_amount hamesha CHHOOT KE BAAD ka hai — wahi convention jo
        # dashboard ke coupon raste par hai. Do jagah do matlab rakhne par
        # har report do jawab dene lagti hai.
        total_amount=total,
        discount_amount=discount or None,
        expected_delivery=delivery,
        advance_hint=Decimal(str(body.advance)) if body.advance else None,
        created_by=p.staff.name,
        needs_pickup=body.needs_pickup,
        priority="urgent" if body.urgent else "normal",
        bill_seconds=body.bill_seconds,
        coupon_code=body.coupon_code.strip() or None,
    )
    # Advance ko paisa maankar ledger mein likhte hain — create ke baad,
    # wahi rasta jo dashboard ke New Bill par hai.
    if body.advance:
        from app.models import PaymentMethod
        from app.services.order_service import record_payment

        await record_payment(
            db, order, amount=Decimal(str(body.advance)),
            method=PaymentMethod.CASH, recorded_by=p.staff.name,
            notify_customer=False,   # "order received" message mein advance pehle se hai
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
        args={
            "order": order.order_number, "total": float(total),
            "gross": float(gross), "discount": float(discount),
            "urgent": body.urgent, "urgent_charge": float(urgent_amt),
            # Khali list bhi likhi jaati hai — "is bill par koi rate nahi
            # badla" ek jawab hai, aur uska na hona sawal.
            "rate_overrides": overrides,
        },
        result="ok", tenant_id=p.staff.tenant_id,
    )
    if overrides:
        log.info(
            "panel_bill_rate_override", staff=p.staff.name,
            order=order.order_number, lines=len(overrides),
        )
    log.info("panel_bill_created", staff=p.staff.name, order=order.order_number)

    # Purana udhaar: ISI order ko chhod kar. Grand total sirf batane ke liye
    # hai — kisi order ka total_amount usse nahi badalta, warna wahi paisa
    # do bill par ginta aur mahine ki kamai jhooth bolne lagti.
    prev_due, prev_bills = await customer_outstanding(
        db, order.customer_id, exclude_order_id=order.id
    )
    this_due = round(float(total) - body.advance, 2)
    return {
        "order_number": order.order_number,
        "total": float(total),
        "gross": float(gross),
        "discount": float(discount),
        "due": this_due,
        "previous_due": prev_due,
        "previous_bills": prev_bills,
        "grand_total": round(this_due + prev_due, 2),
    }


@router.get("/bills", dependencies=[Depends(require_biller())])
async def my_bills(
    q: str = "",
    pay: str = "",
    days: int = 14,
    mine: int = 0,
    limit: int = 30,
    offset: int = 0,
    p: StaffPrincipal = Depends(current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Jo bill maine banaye — taaki bana kar share/collect kar sakoon.

    Pehle bill banane ke baad wo kahin dikhta hi nahi tha: task washer ko
    jaata hai, aur delivery ka Route sirf pickup/delivery stage dikhata hai.
    Banaya kisne ye order_status_history (RECEIVED row ka changed_by) mein
    pehle se likha hai — bas usi se apne bill nikaalte hain. Manager ko
    dukaan ke saare haal ke bill.
    """
    from app.models.order import OrderStatusHistory

    since = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 90)))
    sel = select(Order).where(Order.created_at >= since).order_by(Order.created_at.desc())
    # Worker ko sirf apne banaye bill. Manager ko poori dukaan ke — wahi
    # to dekh-rekh karta hai — aur mine=1 se wo bhi apne tak simat sakta hai.
    if not p.is_manager or mine:
        made_by_me = select(OrderStatusHistory.order_id).where(
            OrderStatusHistory.old_status.is_(None),
            OrderStatusHistory.changed_by == p.staff.name,
        )
        sel = sel.where(Order.id.in_(made_by_me))
    if pay == "due":
        sel = sel.where(Order.payment_status != PaymentStatus.PAID)
    elif pay == "paid":
        sel = sel.where(Order.payment_status == PaymentStatus.PAID)
    term = q.strip()
    if term:
        # Number se, ya grahak ke naam/phone se — jo counter par yaad ho.
        cust_ids = select(Customer.id).where(
            or_(Customer.name.ilike(f"%{term}%"), Customer.phone.ilike(f"%{term}%"))
        )
        sel = sel.where(
            or_(Order.order_number.ilike(f"%{term}%"), Order.customer_id.in_(cust_ids))
        )
    # `n_total` — `total` naam neeche har bill ki rakam ke liye use hota hai;
    # ek hi naam rakhne par aakhri bill ki rakam page-count ban kar jaati hai
    # (response mein "total": 510.0 aa raha tha, 510 bills ke bajaye).
    n_total = (await db.execute(select(func.count()).select_from(sel.subquery()))).scalar_one()
    rows = (
        await db.execute(sel.limit(max(1, min(limit, 100))).offset(max(0, offset)))
    ).scalars().all()
    cust_ids = {o.customer_id for o in rows if o.customer_id}
    customers = {}
    if cust_ids:
        customers = {
            c.id: c for c in (
                await db.execute(select(Customer).where(Customer.id.in_(cust_ids)))
            ).scalars().all()
        }
    out = []
    for o in rows:
        cust = customers.get(o.customer_id)
        total = float(o.total_amount or 0)
        paid = float(o.amount_paid or 0)
        out.append(
            {
                "number": o.order_number,
                "customer": (cust.name or "Customer") if cust else "?",
                "phone_masked": mask_phone(cust.phone if cust else ""),
                "items": items_summary(o),
                "status": o.status.name,
                "total": total,
                "due": max(0.0, total - paid),
                "delivery": o.expected_delivery.isoformat() if o.expected_delivery else None,
                "created": o.created_at.astimezone(IST).strftime("%d %b, %I:%M %p"),
            }
        )
    return {"bills": out, "total": n_total, "offset": max(0, offset), "limit": limit}


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


async def _shop_default_for(db: AsyncSession, role: str) -> Staff | None:
    """Dukaan ka default delivery/washer aadmi — wahi jise work order jaata
    hai jab order kisi ke naam par nahi hai (work_orders.resolve_worker)."""
    from app.services import team

    try:
        if role == "DELIVERY":
            return await team.delivery_staff(db)
        from app.services import app_settings

        phone = (await app_settings.get(db, "default_washer_phone") or "").strip()
        if phone:
            return (
                await db.execute(select(Staff).where(Staff.phone == phone))
            ).scalar_one_or_none()
        washers = [s for s in await team.active_staff(db) if s.role is StaffRole.WASHER]
        return washers[0] if len(washers) == 1 else None
    except Exception:
        log.exception("shop_default_lookup_failed", role=role)
        return None


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
        # Jo bill maine khud banaya wo bhi mera hai — chahe abhi kisi ko
        # assign na hua ho. Warna delivery wala apna banaya bill share/
        # collect nahi kar paata tha ("not yours").
        from app.models.order import OrderStatusHistory

        made_by_me = (
            await db.execute(
                select(func.count()).select_from(OrderStatusHistory).where(
                    OrderStatusHistory.order_id == order.id,
                    OrderStatusHistory.old_status.is_(None),
                    OrderStatusHistory.changed_by == p.staff.name,
                )
            )
        ).scalar_one()
        # ...aur jo order kisi ke naam par hai hi nahi, wo dukaan ke us
        # role wale aadmi ka hai — bilkul wahi shart jo /route lagata hai.
        # Dono jagah ek hi niyam hona ZAROORI hai: warna panel stop dikhata
        # hai par uspar Call/Collect/Share 403 dete hain, jo dikhne se bhi
        # bura hai. (Ye live test mein isi tarah pakda gaya.)
        if not made_by_me and not await _unclaimed_and_mine(db, p, order):
            raise HTTPException(status_code=403, detail="This order is not yours")
    return order


async def _unclaimed_and_mine(db: AsyncSession, p: StaffPrincipal, order: Order) -> bool:
    """Kya ye order kisi ke naam par nahi hai AUR is aadmi ke role ka kaam
    hai? Tabhi TRUE jab ye dukaan ka default delivery/washer wala hai."""
    is_delivery = p.staff.role is StaffRole.DELIVERY
    col_value = order.assigned_delivery_id if is_delivery else order.assigned_washer_id
    if col_value is not None:
        return False
    default_staff = await _shop_default_for(db, "DELIVERY" if is_delivery else "WASHER")
    return default_staff is not None and default_staff.id == p.staff.id


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
    if not can_contact(p):
        raise HTTPException(status_code=403, detail="Grahak se baat delivery/manager karte hain")
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


@router.get("/orders/{number}/receipt")
async def order_receipt(
    number: str,
    p: StaffPrincipal = Depends(require_biller()),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Bill ka text + customer ka poora number — WhatsApp par share ke liye.

    Delivery wala darwaze par khada hai, customer bill maang raha hai, aur
    dukaan ka WhatsApp API juda nahi (ya 24h window band). Tab uske apne
    phone se wa.me link hi rasta hai. Text yahin banta hai taaki dashboard
    ke print/share wale bill se ek akshar alag na ho — settings (pata,
    GSTIN, UPI, footer) wahi ek jagah se aati hain.

    Number sirf apne order ka, aur /call ki tarah har baar audit — kyunki
    ye bhi poora number dikhane wala raasta hai.
    """
    from app.models.tenant import Tenant
    from app.services import app_settings

    order = await _my_order(db, p, number)
    cust = await db.get(Customer, order.customer_id)
    if cust is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    s = await app_settings.all_settings(db)
    tenant = await db.get(Tenant, p.staff.tenant_id)
    # Pichhle bilon ka baaki. Grahak ko ek hi number chahiye — "kitna dena
    # hai" — isliye kul bill par jodkar likhte hain. Order ka apna total
    # waisa ka waisa rehta hai; ye sirf padhne wali line hai.
    from app.services import customer_balance

    prev = await customer_balance.previous(db, cust.id, exclude_order_id=order.id)
    out = receipt.payload(order, cust, tenant, s, prev["due"], prev["bills"], prev["clothes"])

    await audit.record(
        actor_role="staff", actor=p.staff.name, action="customer_number_viewed",
        args={"order": order.order_number}, result="share_bill", tenant_id=p.staff.tenant_id,
    )
    log.info("staff_shared_bill", staff=p.staff.name, order=order.order_number)
    return {"phone": cust.phone, "name": cust.name or "Customer", **out}


@router.get("/orders/{number}/message")
async def order_message(
    number: str,
    kind: str = Query(pattern="^(payment_thanks|service_thanks|review_request|delivery_update)$"),
    p: StaffPrincipal = Depends(require_biller()),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Grahak ko thank you / payment mila / review ka message — apne bill par.

    Receipt ki tarah poora number deta hai (wa.me ke liye), isliye wahi
    niyam: sirf apna order, aur har baar audit.
    """
    from app.models.tenant import Tenant
    from app.services import customer_messages

    order = await _my_order(db, p, number)
    cust = await db.get(Customer, order.customer_id)
    if cust is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    tenant = await db.get(Tenant, p.staff.tenant_id)
    try:
        text = await customer_messages.compose(db, kind=kind, order=order, customer=cust, tenant=tenant)
    except customer_messages.MessageError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await audit.record(
        actor_role="staff", actor=p.staff.name, action="customer_number_viewed",
        args={"order": order.order_number, "kind": kind}, result="message",
        tenant_id=p.staff.tenant_id,
    )
    return {"kind": kind, "text": text, "phone": cust.phone, "name": cust.name or "Customer"}


@router.post("/orders/{number}/remind")
async def send_payment_reminder(
    number: str,
    p: StaffPrincipal = Depends(require_manager),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Us grahak ko paise ki yaad dilao — abhi, haath se.

    SIRF manager/owner. Bill banana delivery wale ka kaam ho sakta hai,
    par paisa MAANGNA dukaan ke naam par bola gaya vaakya hai — wo
    grahak ke saath dukaan ka rishta hai, ek delivery ke aadmi ka faisla
    nahi. Aur ye button bina throttle ke hai, isliye jitne kam haathon
    mein ho utna achha.

    Scheduler khud 3 din / 15 din par yaad dilata hai, par wo dono cheezein
    maanta hai jo yahan sach nahi hoti: ki order deliver ho chuka hai, aur
    ki dukaan ka WhatsApp API juda hai. Counter par khada aadmi in dono ka
    intezaar nahi kar sakta.

    Text WAHI hai jo scheduler bhejta hai (`get_message`) — do raston se do
    alag bhasha nahi. Aur agar API se na jaye to number + text wapas aata
    hai taaki panel wa.me link de sake: staff ke apne phone ka WhatsApp
    hamesha hai, dukaan ka API bhale na ho.
    """
    from app.services.messages import get_message
    from app.services.whatsapp import SendError, send_message

    order = await _my_order(db, p, number)
    cust = await db.get(Customer, order.customer_id)
    if cust is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    # Jisne mana kar diya use yaad nahi dilate — chahe panel se ho ya
    # scheduler se. Ye grahak ka faisla hai, raste ka nahi.
    if cust.opted_out or not cust.is_active:
        raise HTTPException(status_code=400, detail="Is grahak ne message band karwa diye hain")

    due = float((order.total_amount or 0) - (order.amount_paid or 0))
    if due <= 0.009:
        raise HTTPException(status_code=400, detail="Is bill ka paisa chukta hai")
    prev, prev_bills = await customer_outstanding(db, cust.id, exclude_order_id=order.id)

    # lang="en" — dashboard ka reminder English mein hai, aur grahak ko ye
    # pata nahi chalna chahiye ki dono mein se kisne yaad dilaya. Catalogue
    # chheda nahi: "en" variant pehle se maujood hai. Baaki customer
    # messages (order confirm, ready) abhi bhi Hindi mein hain — wo alag
    # faisla hai, DEFAULT_LANG ka.
    text = get_message(
        "payment_reminder", lang="en",
        order_number=order.order_number, amount=f"{due:.0f}",
    )
    if prev > 0:
        # Ek hi message mein poora sach — warna grahak is bill ka paisa
        # dekar samajhta hai ki hisaab saaf ho gaya.
        text += f"\n\nPrevious balance: ₹{prev:.0f} ({prev_bills} bill). Total due: ₹{due + prev:.0f}"
    from app.services import bill_link

    text += bill_link.message_line(await bill_link.url_for(db, order))

    sent = False
    try:
        await send_message(db, to_phone=cust.phone, text=text)
        sent = True
    except SendError as exc:
        log.info("panel_reminder_api_failed", order=order.order_number, error=str(exc))

    await audit.record(
        actor_role="staff", actor=p.staff.name, action="payment_reminder_manual",
        args={"order": order.order_number, "due": due, "previous_due": prev,
              "via": "api" if sent else "wa_link"},
        result="sent" if sent else "fallback", tenant_id=p.staff.tenant_id,
    )
    return {
        "sent": sent,
        "due": round(due, 2),
        "previous_due": prev,
        # API se chala gaya to number bhejne ki zaroorat nahi — panel
        # hamesha masked dikhata hai, aur poora number sirf tab jab wo
        # sach mein chahiye (aur tab wo audit ho chuka hota hai).
        "phone": None if sent else cust.phone,
        "name": cust.name or "Customer",
        "text": None if sent else text,
    }


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
        "can_collect": can_money(p) and p.has("cod_collection"),
    }
    # Late kaam ki GINTI server se, list se nahi. Panel ke paas sirf pehla
    # page hota hai (30 rows), to wahan gin kar banner banate to badi dukaan
    # par wo jhooth bolta: "2 late" jabki asli mein bees. Ginti WAHI query
    # se aati hai jo list banati hai (_route_query), isliye banner par tap
    # karke jo dikhe wo ginti se mel khaye.
    route_q, _ = await _route_query(db, p)
    out["late"] = (
        await db.execute(
            select(func.count()).select_from(
                route_q.where(Order.expected_delivery < date.today()).subquery()
            )
        )
    ).scalar_one()
    if p.is_manager:
        out["shop_pending"] = await _count(Task.status == TASK_OPEN)
        out["shop_done_today"] = await _count(
            Task.status == TASK_DONE, Task.completed_at >= start
        )
    return out


async def _route_query(db: AsyncSession, p: StaffPrincipal, tab: str = "todo"):
    """Is aadmi ka kaam kaunsa hai — ek hi jagah.

    tab="done": jo nipat gaya (washer: Ready/nikla/diya; delivery: diya),
    pichhle 7 din ka — Ready dabate hi order gayab ho jaata tha aur aadmi
    ke paas apne kaam ka koi record nahi bachta tha.

    Ye pehle `my_route` ke andar likha tha. Ab do jagah chahiye (list aur
    late ki ginti), aur do copy rakhna sabse bura vikalp hai: banner "2
    late" kahe aur list teen dikhaye, to aadmi dono par bharosa chhod
    deta hai.
    """
    is_delivery = p.staff.role is StaffRole.DELIVERY
    wash_stages = (
        OrderStatus.RECEIVED, OrderStatus.PICKED_UP, OrderStatus.IN_WASH,
        OrderStatus.IN_DRY, OrderStatus.IN_IRON,
    )
    road_stages = (OrderStatus.PICKUP_ASSIGNED, OrderStatus.READY, OrderStatus.OUT_FOR_DELIVERY)
    if p.is_manager:
        # Manager/owner/supervisor ko dukaan ka POORA chalta kaam — dhulai bhi,
        # aur jo Ready/nikla hua hai wo bhi. Pehle unhe sirf dhulai wali kataar
        # dikhti thi, isliye Ready order (jise deliver karna hai — poora ya
        # kuch kapde) unke panel mein aata hi nahi tha.
        stages = wash_stages + road_stages
    else:
        stages = road_stages if is_delivery else wash_stages
    col = Order.assigned_delivery_id if is_delivery else Order.assigned_washer_id
    if tab == "done":
        from datetime import timedelta

        stages = (
            (OrderStatus.DELIVERED,) if is_delivery
            else (OrderStatus.READY, OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED)
        )
        q = select(Order).where(
            Order.status.in_(stages),
            Order.updated_at >= datetime.now(timezone.utc) - timedelta(days=7),
        )
    else:
        q = select(Order).where(Order.status.in_(stages))
    if not p.is_manager:
        # "Mera kaam" ka matlab sirf explicitly assigned nahi hai.
        #
        # Zyadatar bill kisi ko assign kiye bina bante hain (counter par
        # manager banata hai, ya delivery boy khud). Aise order par
        # assigned_delivery_id NULL rehta hai — par work order phir bhi
        # dukaan ke delivery wale ko hi jaata hai (work_orders.resolve_worker
        # ka fallback). Panel purane filter se un orders ko chhupa deta tha:
        # WhatsApp par "pickup karo" aata tha aur app mein kuch nahi dikhta
        # tha. Ab: mere naam wale + jo kisi ke naam nahi hain, jab ye aadmi
        # hi dukaan ka delivery/washer wala hai.
        default_staff = await _shop_default_for(db, "DELIVERY" if is_delivery else "WASHER")
        if default_staff is not None and default_staff.id == p.staff.id:
            q = q.where(or_(col == p.staff.id, col.is_(None)))
        else:
            q = q.where(col == p.staff.id)
    return q, is_delivery


@router.get("/route")
async def my_route(
    limit: int = 40,
    offset: int = 0,
    tab: str = Query(default="todo", pattern="^(todo|done)$"),
    p: StaffPrincipal = Depends(current_staff),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Aaj kahan-kahan jaana hai — ek hi list mein, kaam ke kram se.

    Delivery wale ko pickup aur delivery dono; washerman ko uski dhulai
    ki kataar. Sabse pehle urgent, phir jiski delivery date sabse paas
    hai — taaki koi order neeche daba na rah jaye.
    """
    base, is_delivery = await _route_query(db, p, tab)
    q = base.order_by(Order.updated_at.desc()) if tab == "done" else base.order_by(
        Order.priority.desc(),
        Order.expected_delivery.asc().nullslast(),
        Order.created_at,
    )
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (
        await db.execute(
            q.limit(max(1, min(limit, 100))).offset(max(0, offset))
        )
    ).scalars().all()
    # Customers ek hi query mein — pehle har stop par alag get() tha
    cust_ids = {o.customer_id for o in rows if o.customer_id}
    customers = {}
    if cust_ids:
        customers = {
            c.id: c for c in (
                await db.execute(select(Customer).where(Customer.id.in_(cust_ids)))
            ).scalars().all()
        }

    from app.services import app_settings, delivery

    # Route band ho to pata bhejte hi nahi — UI mein chhupana kaafi nahi
    show_route = bool(await app_settings.get(db, "staff_show_route"))
    stops = []
    for o in rows:
        cust = customers.get(o.customer_id)
        kind = (
            "Done" if tab == "done"
            else "Pickup" if o.status is OrderStatus.PICKUP_ASSIGNED
            else "Delivery" if o.status in (OrderStatus.READY, OrderStatus.OUT_FOR_DELIVERY)
            else "Dhulai"
        )
        stops.append(
            {
                "number": o.order_number,
                "kind": kind,
                "customer": (cust.name or "Customer") if cust else "?",
                # Number/pata sirf unhe jo grahak tak jaate hain; washerman ko nahi
                **({"phone_masked": mask_phone(cust.phone if cust else ""),
                    "address": (cust.address or "").strip() if cust and show_route else ""}
                   if can_contact(p) else {"address": ""}),
                "items": items_summary(o),
                "status": o.status.name,
                "urgent": o.priority == "urgent",
                **({"due": float((o.total_amount or 0) - (o.amount_paid or 0))} if can_money(p) else {}),
                "delivery": o.expected_delivery.isoformat() if o.expected_delivery else None,
                # kitne kapde, kitne diye — thodi delivery ho chuki ho to row par "4 pending"
                "clothes": delivery.counts(o),
                **({"done_at": o.updated_at.isoformat()} if tab == "done" and o.updated_at else {}),
            }
        )
    return {
        "stops": stops, "kind": "delivery" if is_delivery else "wash",
        "total": total, "offset": max(0, offset), "limit": limit,
    }
