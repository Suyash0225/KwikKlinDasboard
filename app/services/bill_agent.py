"""Staff/manager WhatsApp commands (Phase 4, group c).

Three things a staff/manager message can do, all extracted by the cheap
model but EXECUTED only by our own code:
- new_bill:      free text -> priced draft -> explicit 'haan' -> create_order()
- delay_update:  order number + new date + reason -> set_expected_delivery()
                 (reason goes to orders.notes only — never to the customer)
- status_update: order number + status -> update_status() (state machine
                 still decides; CANCELLED is dashboard-only, too risky here)

Safety:
- NOTHING is written to the DB without the confirm word ('haan') for bills;
  prices come ONLY from the rate card lookup in code, never from the model.
- Drafts live in memory per sender phone for 30 minutes (single-process
  uvicorn — documented limitation, lost on restart, which is safe: worst
  case the manager re-sends the bill text).
- Any LLM failure returns None -> sender gets no reply (pre-Phase-4
  behavior), never a wrong action.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import func

from app.config import settings
from app.models import Customer, Order, OrderStatus, PaymentMethod, PaymentStatus, Rate, Staff
from app.services import app_settings, audit, llm_client
from app.services.llm_client import LLMError
from app.services.messages import get_message, status_label
from app.services.order_service import (
    ACTIVE_STATUSES,
    InvalidTransitionError,
    OrderNotFoundError,
    create_order,
    get_order,
    record_payment,
    set_expected_delivery,
    update_status,
)
from app.services.whatsapp import SendError, WindowClosedError, send_message
from app.utils.phone import normalize_phone

log = structlog.get_logger()

_DRAFT_TTL = timedelta(minutes=30)
# Inbound photos land as "[image:/admin/media/<file>] optional caption"
_IMAGE_MARKER_RE = re.compile(r"^\[image:/admin/media/([A-Za-z0-9._\-]+)\]\s*(.*)$", re.DOTALL)
_MEDIA_DIR = Path(__file__).resolve().parent.parent / "media"
_CONFIRM_RE = re.compile(r"^\s*(haan?|ha(n|nji)?|yes|y|ok(ay)?|theek( hai)?|confirm|✅|done)\s*$", re.I)
_CANCEL_RE = re.compile(r"^\s*(nahi+|no|na|cancel|rehne do|❌|mat( banao)?)\s*$", re.I)


@dataclass
class PendingBill:
    draft: dict
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def expired(self) -> bool:
        return datetime.now(timezone.utc) - self.created_at > _DRAFT_TTL


@dataclass
class PendingPayment:
    """A payment awaiting the manager's 'haan' — money writes are gated."""

    order_number: str
    amount: float
    method: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def expired(self) -> bool:
        return datetime.now(timezone.utc) - self.created_at > _DRAFT_TTL


# sender phone -> action awaiting 'haan' (a bill draft or a payment)
_PENDING: dict[str, PendingBill | PendingPayment] = {}

_STATUS_NAMES = [s.name for s in OrderStatus]

_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "new_bill", "delay_update", "status_update", "relay",
                "set_priority", "assign_staff", "add_note", "record_payment",
                "other",
            ],
        },
        "customer_name": {"type": "string"},
        "customer_phone": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "garment": {"type": "string"},
                    "qty": {"type": "number"},
                },
                "required": ["service", "garment", "qty"],
                "additionalProperties": False,
            },
        },
        "advance": {"type": "number"},
        "expected_delivery": {"type": "string"},
        "order_number": {"type": "string"},
        "new_date": {"type": "string"},
        "reason": {"type": "string"},
        # "NONE" = not a status update (Gemini rejects "" inside an enum)
        "new_status": {"type": "string", "enum": [*_STATUS_NAMES, "NONE"]},
        "relay_to": {"type": "string"},
        "relay_message": {"type": "string"},
        "priority": {"type": "string", "enum": ["urgent", "normal", "NONE"]},
        "staff_name": {"type": "string"},
        "note": {"type": "string"},
        "amount": {"type": "number"},
        "method": {"type": "string", "enum": ["cash", "upi", "other", "NONE"]},
    },
    "required": [
        "action", "customer_name", "customer_phone", "items", "advance",
        "expected_delivery", "order_number", "new_date", "reason", "new_status",
        "relay_to", "relay_message", "priority", "staff_name", "note",
        "amount", "method",
    ],
    "additionalProperties": False,
}

_EXTRACT_SYSTEM = (
    "You extract structured commands from WhatsApp messages sent by the "
    "OWNER or STAFF of Kwik Klin laundry (Varanasi). Messages are Hinglish/"
    "Hindi/English.\n"
    "Actions:\n"
    "- new_bill: they describe a customer's clothes to bill (e.g. 'Sharma ji "
    "3 shirt dry clean 2 saree'). Extract items; for service and garment use "
    "the EXACT strings from the RATE CARD when they match; qty defaults to 1. "
    "advance = money already taken (0 if unsaid). Dates in ISO YYYY-MM-DD "
    "using TODAY for words like kal/parso; '' if unsaid. customer_phone: "
    "digits only as written, '' if unsaid.\n"
    "- delay_update: an order (KK-...) will be late — extract order_number, "
    "new_date (ISO, '' if unsaid) and the internal reason.\n"
    "- status_update: they state an order's new stage (dhul gaya, ready hai, "
    "nikal gaya, deliver ho gaya...) — map to one of the status names.\n"
    "- relay: they ask to pass a message to a person — staff, manager OR a "
    "customer (e.g. 'Ravi ko bata do ...', 'Anmol ko bolo kal tak ho '"
    "'jayega'). relay_to = the person's name as written; relay_message = a "
    "short clear Hinglish message carrying their full instruction.\n"
    "- set_priority: an order is urgent / no longer urgent ('Sharma ji ka "
    "urgent hai') — order_number (if named) + customer_name + priority.\n"
    "- assign_staff: give an order to a staff member ('ye Ravi ko de do') — "
    "order_number + staff_name.\n"
    "- add_note: an internal instruction about an order ('collar pe daag "
    "hai, dhyan se') — order_number + note.\n"
    "- record_payment: money received for an order ('KK-... ka 200 cash "
    "mila') — order_number, amount, method (cash/upi/other).\n"
    "- other: anything else (greetings, questions, chatter).\n"
    "If a CURRENT DRAFT is provided, the message is an edit to it: return "
    "action=new_bill with the FULL corrected draft (unchanged fields kept). "
    "Fill every unused field with '' / [] / 0, and new_status with 'NONE' "
    "unless it is a status_update. Never invent items, phones or prices."
)


async def handle_staff_message(
    db: AsyncSession, *, sender_phone: str, sender_label: str, text: str
) -> str | None:
    """Reply for a staff/manager inbound, or None to stay silent."""
    pending = _PENDING.get(sender_phone)
    if pending and pending.expired:
        _PENDING.pop(sender_phone, None)
        pending = None

    photo = _IMAGE_MARKER_RE.match(text or "")
    if photo is None:
        # Non-photo markers (buttons etc.) are not conversational text.
        if not text or text.startswith("["):
            return None
        if pending and _CONFIRM_RE.match(text):
            if isinstance(pending, PendingPayment):
                return await _finalize_payment(db, sender_phone, sender_label, pending)
            return await _finalize_bill(db, sender_phone, sender_label, pending)
        if pending and _CANCEL_RE.match(text):
            _PENDING.pop(sender_phone, None)
            return get_message("bill_cancelled")

    # Thread memory: the last few messages, so "haan wahi wala" makes sense.
    history = await _sender_history(db, sender_phone)

    try:
        if photo is not None:
            extracted = await _extract_from_photo(
                db, photo.group(1), photo.group(2).strip(), _as_bill(pending)
            )
            if extracted is None:  # file missing on disk — nothing to read
                return None
        else:
            extracted = await _extract(db, text, _as_bill(pending), history)
    except LLMError as exc:
        log.warning("staff_extract_failed", error=str(exc)[:150])
        # Never leave the MANAGER wondering — staff chatter can stay silent.
        return get_message("ai_down_staff") if sender_label == "manager" else None

    action = extracted["action"]
    reply: str | None = None
    if action == "new_bill" and extracted["items"]:
        draft = await _price_draft(db, extracted)
        _PENDING[sender_phone] = PendingBill(draft=draft)
        reply = _draft_summary(draft)
    elif action == "delay_update":
        reply = await _apply_delay(db, sender_label, extracted)
    elif action == "status_update":
        reply = await _apply_status(db, sender_label, extracted)
    elif action == "relay":
        reply = await _apply_relay(db, sender_label, extracted)
    elif action == "set_priority":
        reply = await _apply_priority(db, sender_label, extracted)
    elif action == "assign_staff":
        reply = await _apply_assign(db, sender_label, extracted)
    elif action == "add_note":
        reply = await _apply_note(db, sender_label, extracted)
    elif action == "record_payment":
        reply = await _stage_payment(db, sender_phone, sender_label, extracted)
    if reply is not None:
        await audit.record(
            actor_role="admin" if sender_label == "manager" else "staff",
            actor=sender_label,
            action=action,
            args={k: v for k, v in extracted.items() if v not in ("", [], 0, "NONE")},
            result=reply[:300],
        )
        return reply

    # 'other' from the MANAGER: probably a business question ("kitna
    # revenue?", "kitne order pending?") — answer from real DB numbers.
    if sender_label == "manager":
        try:
            answer = await _answer_manager_query(db, text)
            if answer:
                return answer
        except LLMError as exc:
            log.warning("manager_query_failed", error=str(exc)[:150])
        return get_message("staff_cmd_unknown")
    # plain staff chatter stays silent unless they're mid-draft
    if pending:
        return get_message("staff_cmd_unknown")
    return None


def _as_bill(pending) -> PendingBill | None:
    """Only bill drafts flow into extraction context, not pending payments."""
    return pending if isinstance(pending, PendingBill) else None


async def _sender_history(db: AsyncSession, sender_phone: str) -> str:
    """Last few messages of this sender's thread — never raises."""
    try:
        from app.services.knowledge import thread_history

        staff = (
            await db.execute(select(Staff).where(Staff.phone == sender_phone))
        ).scalar_one_or_none()
        if staff is not None:
            return await thread_history(db, staff_id=staff.id, limit=4)
        cust = (
            await db.execute(select(Customer).where(Customer.phone == sender_phone))
        ).scalar_one_or_none()
        if cust is not None:
            return await thread_history(db, customer_id=cust.id, limit=4)
    except Exception:
        log.exception("sender_history_failed")
    return ""


async def _extract(
    db: AsyncSession, text: str, pending: PendingBill | None, history: str = ""
) -> dict:
    rates = (
        (await db.execute(select(Rate).where(Rate.is_active).order_by(Rate.service, Rate.garment)))
        .scalars()
        .all()
    )
    card = "\n".join(
        f"- service={r.service!r} garment={r.garment!r} ₹{r.rate}/{r.unit}" for r in rates
    )
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = f"RATE CARD:\n{card}\nTODAY: {today}\n"
    if history:
        prompt += f"{history}\n"
    if pending:
        prompt += f"CURRENT DRAFT:\n{json.dumps(pending.draft, default=str)}\n"
    prompt += f"STAFF MESSAGE:\n{text[:1000]}"
    return await llm_client.ask_json(
        system=_EXTRACT_SYSTEM,
        user_text=prompt,
        schema=_EXTRACT_SCHEMA,
        model=llm_client.MODEL_CHEAP,
        max_tokens=700,
    )


_PHOTO_ADDENDUM = (
    "\nA PHOTO of a handwritten Kwik Klin bill slip is attached. Read the "
    "customer name, phone number (if written) and every item line (qty + "
    "garment + service) from the slip; match items to the RATE CARD's exact "
    "strings where possible. IGNORE any prices/amounts written on the slip — "
    "the system prices everything from the rate card itself. The caption may "
    "add corrections; it wins over the slip. Return action=new_bill."
)


async def _extract_from_photo(
    db: AsyncSession, filename: str, caption: str, pending: PendingBill | None
) -> dict | None:
    """Vision extraction from a bill photo. None if the file is gone."""
    path = _MEDIA_DIR / Path(filename).name  # traversal-safe
    if not path.exists():
        log.warning("bill_photo_missing", filename=filename)
        return None
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"

    rates = (
        (await db.execute(select(Rate).where(Rate.is_active).order_by(Rate.service, Rate.garment)))
        .scalars()
        .all()
    )
    card = "\n".join(
        f"- service={r.service!r} garment={r.garment!r} ₹{r.rate}/{r.unit}" for r in rates
    )
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = f"RATE CARD:\n{card}\nTODAY: {today}\n"
    if pending:
        prompt += f"CURRENT DRAFT:\n{json.dumps(pending.draft, default=str)}\n"
    prompt += f"STAFF CAPTION:\n{caption or '(none)'}"
    return await llm_client.ask_json_image(
        system=_EXTRACT_SYSTEM + _PHOTO_ADDENDUM,
        user_text=prompt,
        image_bytes=path.read_bytes(),
        mime_type=mime,
        schema=_EXTRACT_SCHEMA,
        model=llm_client.MODEL_SMART,
        max_tokens=900,
    )


async def _price_draft(db: AsyncSession, extracted: dict) -> dict:
    """Price every item from the rate card — the model never sets prices."""
    rates = (
        (await db.execute(select(Rate).where(Rate.is_active))).scalars().all()
    )
    by_key = {(r.service.lower(), r.garment.lower()): r for r in rates}
    items = []
    total = Decimal("0")
    for it in extracted["items"]:
        qty = Decimal(str(it["qty"] or 1))
        rate_row = by_key.get((it["service"].lower(), it["garment"].lower()))
        if rate_row is None:
            # garment-only fallback, but only when unambiguous
            matches = [r for r in rates if r.garment.lower() == it["garment"].lower() and it["garment"]]
            rate_row = matches[0] if len(matches) == 1 else None
        rate = rate_row.rate if rate_row else None
        amount = (rate * qty) if rate is not None else None
        if amount is not None:
            total += amount
        service = rate_row.service if rate_row else it["service"]
        garment = rate_row.garment if rate_row else it["garment"]
        items.append(
            {
                # "type" is the canonical item key the dashboard/orders API
                # already uses — keep both shapes in sync.
                "type": garment or service,
                "service": service,
                "garment": garment,
                "qty": float(qty),
                "rate": float(rate) if rate is not None else None,
                "amount": float(amount) if amount is not None else None,
            }
        )
    return {
        "customer_name": extracted["customer_name"],
        "customer_phone": extracted["customer_phone"],
        "items": items,
        "advance": extracted["advance"] or 0,
        "expected_delivery": extracted["expected_delivery"],
        "total": float(total),
    }


def _draft_summary(draft: dict) -> str:
    lines = [get_message("bill_draft_header", customer_name=draft["customer_name"] or "?")]
    for it in draft["items"]:
        qty = int(it["qty"]) if float(it["qty"]).is_integer() else it["qty"]
        label = f"{it['service']}{' / ' + it['garment'] if it['garment'] else ''}"
        if it["amount"] is not None:
            lines.append(f"• {qty} × {label} — ₹{it['amount']:g}")
        else:
            lines.append(f"• {qty} × {label} — ⚠️ rate card mein nahi")
    lines.append(get_message("bill_draft_total", total=f"{draft['total']:g}"))
    if draft["advance"]:
        lines.append(get_message("bill_draft_advance", advance=f"{draft['advance']:g}"))
    if draft["expected_delivery"]:
        lines.append(get_message("bill_draft_delivery", date=draft["expected_delivery"]))
    if not draft["customer_phone"]:
        lines.append(get_message("bill_need_phone"))
    lines.append(get_message("bill_draft_confirm"))
    return "\n".join(lines)


async def _finalize_bill(
    db: AsyncSession, sender_phone: str, sender_label: str, pending: PendingBill
) -> str:
    d = pending.draft
    try:
        phone = normalize_phone(d["customer_phone"])
    except ValueError:
        return get_message("bill_need_phone")

    exp = None
    if d["expected_delivery"]:
        try:
            exp = date.fromisoformat(d["expected_delivery"])
        except ValueError:
            exp = None
    if exp is None:
        # default = today + the shop's standard turnaround (Settings)
        days = int(await app_settings.get(db, "turnaround_days"))
        exp = date.today() + timedelta(days=days)

    total = Decimal(str(d["total"])) if d["total"] else None
    order = await create_order(
        db,
        customer_phone=phone,
        items=d["items"],
        customer_name=d["customer_name"] or None,
        total_amount=total,
        expected_delivery=exp,
        created_by=sender_label,
    )
    if d["advance"]:
        await record_payment(
            db, order, amount=Decimal(str(d["advance"])), method=PaymentMethod.CASH,
            recorded_by=sender_label, note="advance at booking",
        )
    _PENDING.pop(sender_phone, None)

    # instant work order to the responsible staff member (spec 7.5a)
    from app.services.work_orders import send_work_order

    outcome = await send_work_order(db, order, headline="Naya order aaya")
    await audit.record(
        actor_role="admin" if sender_label == "manager" else "staff",
        actor=sender_label,
        action="create_bill",
        args={"order": order.order_number, "total": d["total"], "items": len(d["items"])},
        result=f"created; work_order={outcome}",
    )
    log.info("bill_created_via_whatsapp", order_number=order.order_number, by=sender_label)
    staff_note = {
        "sent": "Staff ko work order bhej diya.",
        "sent_template": "Staff ko work order (template se) bhej diya.",
        "no_staff": "⚠️ Koi staff assigned nahi — Settings mein default washer set karein.",
        "failed": "⚠️ Staff ko message nahi ja paya.",
    }[outcome]
    return (
        get_message(
            "bill_created",
            order_number=order.order_number,
            total=f"{d['total']:g}" if d["total"] else "—",
        )
        + "\n" + staff_note
    )


async def _apply_delay(db: AsyncSession, sender_label: str, extracted: dict) -> str:
    number = extracted["order_number"].upper()
    if not number:
        return get_message("staff_cmd_unknown")
    try:
        order = await get_order(db, number)
    except OrderNotFoundError:
        return get_message("order_not_found_staff", order_number=number)
    if not extracted["new_date"]:
        return get_message("delay_needs_date", order_number=number)
    try:
        new_date = date.fromisoformat(extracted["new_date"])
    except ValueError:
        return get_message("delay_needs_date", order_number=number)
    await set_expected_delivery(
        db, order, new_date,
        changed_by=sender_label,
        internal_reason=extracted["reason"] or None,
    )
    return get_message(
        "delay_done", order_number=number, date=new_date.strftime("%d %b %Y")
    )


async def _find_order_flex(
    db: AsyncSession, extracted: dict
) -> tuple[Order | None, str | None]:
    """Resolve an order by number, or by customer name's single active order.

    Returns (order, error_reply). Ambiguity -> ask, never guess (spec 7.4).
    """
    number = (extracted.get("order_number") or "").upper()
    if number:
        try:
            return await get_order(db, number), None
        except OrderNotFoundError:
            return None, get_message("order_not_found_staff", order_number=number)
    name = (extracted.get("customer_name") or "").strip()
    if not name:
        return None, get_message("staff_cmd_unknown")
    rows = (
        (
            await db.execute(
                select(Order)
                .join(Customer, Customer.id == Order.customer_id)
                .where(
                    Order.status.in_(ACTIVE_STATUSES),
                    Customer.name.ilike(f"%{name}%"),
                )
                .order_by(Order.created_at.desc())
                .limit(5)
            )
        )
        .scalars()
        .all()
    )
    if len(rows) == 1:
        return rows[0], None
    if not rows:
        return None, get_message("order_for_customer_not_found", name=name)
    listing = "\n".join(f"- {o.order_number}" for o in rows)
    return None, get_message("order_ambiguous", name=name, listing=listing)


async def _apply_priority(db: AsyncSession, sender_label: str, extracted: dict) -> str:
    from app.services.work_orders import send_work_order

    order, err = await _find_order_flex(db, extracted)
    if err:
        return err
    priority = extracted["priority"] if extracted["priority"] in ("urgent", "normal") else "urgent"
    order.priority = priority
    await db.commit()
    outcome = await send_work_order(
        db, order,
        headline="Priority update" if priority == "urgent" else "Priority normal hui",
        extra="Ise sabse pehle karna hai — aaj hi." if priority == "urgent" else "-",
    )
    key = {
        "sent": "priority_set_notified",
        "sent_template": "priority_set_notified",
        "no_staff": "priority_set_no_staff",
        "failed": "priority_set_notify_failed",
    }[outcome]
    return get_message(key, order_number=order.order_number, priority=priority.upper())


async def _apply_assign(db: AsyncSession, sender_label: str, extracted: dict) -> str:
    from app.services.work_orders import send_work_order

    order, err = await _find_order_flex(db, extracted)
    if err:
        return err
    target = (extracted["staff_name"] or extracted["relay_to"] or "").strip()
    staff_rows = (await db.execute(select(Staff))).scalars().all()
    matches = [
        s for s in staff_rows
        if s.name and target and (s.name.lower() in target.lower() or target.lower() in s.name.lower())
    ]
    if len(matches) != 1:
        names = ", ".join(s.name for s in staff_rows if s.name) or "-"
        return get_message("relay_target_unknown", target=target or "?", names=names)
    staff = matches[0]
    if staff.role.name == "DELIVERY":
        order.assigned_delivery_id = staff.id
    else:
        order.assigned_washer_id = staff.id
    await db.commit()
    outcome = await send_work_order(db, order, headline="Naya kaam mila")
    return get_message(
        "assign_done", order_number=order.order_number, name=staff.name,
        notified="✓" if outcome in ("sent", "sent_template") else "✗ (message nahi gaya)",
    )


async def _apply_note(db: AsyncSession, sender_label: str, extracted: dict) -> str:
    from app.services.work_orders import send_work_order

    order, err = await _find_order_flex(db, extracted)
    if err:
        return err
    note = (extracted["note"] or "").strip()
    if not note:
        return get_message("staff_cmd_unknown")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    line = f"[{stamp} {sender_label}] {note}"
    order.notes = f"{order.notes}\n{line}" if order.notes else line
    await db.commit()
    # instructions are for the worker too — forward as a work order
    outcome = await send_work_order(db, order, headline="Instruction", extra=note)
    return get_message(
        "note_done", order_number=order.order_number,
        notified="staff ko bhej bhi diya" if outcome in ("sent", "sent_template") else "staff ko nahi bhej paya",
    )


async def _stage_payment(
    db: AsyncSession, sender_phone: str, sender_label: str, extracted: dict
) -> str:
    """Money writes are confirm-gated: stage it, ask for 'haan'."""
    order, err = await _find_order_flex(db, extracted)
    if err:
        return err
    amount = float(extracted["amount"] or 0)
    if amount <= 0:
        return get_message("staff_cmd_unknown")
    method = extracted["method"] if extracted["method"] in ("cash", "upi", "other") else "cash"
    _PENDING[sender_phone] = PendingPayment(
        order_number=order.order_number, amount=amount, method=method
    )
    due = (order.total_amount or Decimal("0")) - (order.amount_paid or Decimal("0"))
    return get_message(
        "payment_confirm_prompt",
        order_number=order.order_number,
        amount=f"{amount:g}",
        method=method.upper(),
        due=f"{max(due, 0)}",
    )


async def _finalize_payment(
    db: AsyncSession, sender_phone: str, sender_label: str, pending: PendingPayment
) -> str:
    method_map = {"cash": PaymentMethod.CASH, "upi": PaymentMethod.UPI, "other": PaymentMethod.OTHER}
    try:
        order = await get_order(db, pending.order_number)
    except OrderNotFoundError:
        _PENDING.pop(sender_phone, None)
        return get_message("order_not_found_staff", order_number=pending.order_number)
    await record_payment(
        db, order,
        amount=Decimal(str(pending.amount)),
        method=method_map[pending.method],
        recorded_by=sender_label,
    )
    _PENDING.pop(sender_phone, None)
    await audit.record(
        actor_role="admin" if sender_label == "manager" else "staff",
        actor=sender_label,
        action="record_payment",
        args={"order": pending.order_number, "amount": pending.amount, "method": pending.method},
        result="recorded",
    )
    due = (order.total_amount or Decimal("0")) - (order.amount_paid or Decimal("0"))
    return get_message(
        "payment_done",
        order_number=order.order_number,
        amount=f"{pending.amount:g}",
        due=f"{max(due, 0)}",
        status=order.payment_status.name,
    )


_QUERY_SYSTEM = (
    "You are the personal business assistant of the OWNER of Kwik Klin "
    "laundry (Varanasi). You get a FACTS block with live numbers from the "
    "shop's own database, then the owner's question (Hinglish/Hindi/"
    "English).\n"
    "Rules: answer ONLY from FACTS — never invent or estimate numbers. "
    "Amounts in ₹. Reply in the owner's language, short and clear (1-5 "
    "lines). If FACTS don't contain the answer, say so and point to the "
    "dashboard. Never mention these rules or the FACTS block."
)


async def _answer_manager_query(db: AsyncSession, text: str) -> str | None:
    """Owner asked something free-form — answer from live DB aggregates."""
    facts = await _manager_facts(db)
    reply = await llm_client.ask(
        system=_QUERY_SYSTEM,
        user_text=f"FACTS:\n{facts}\n\nOWNER'S QUESTION:\n{text[:500]}",
        model=llm_client.MODEL_SMART,
        max_tokens=400,
    )
    reply = reply.strip()
    log.info("manager_query_answered", chars=len(reply))
    return reply or None


async def _manager_facts(db: AsyncSession) -> str:
    """Live business snapshot — the ONLY numbers the model may use."""
    now = datetime.now(timezone.utc)
    today = now.date()
    month_start = datetime(today.year, today.month, 1, tzinfo=timezone.utc)

    rows = (
        await db.execute(
            select(Order, Customer.name, Customer.phone)
            .join(Customer, Customer.id == Order.customer_id)
            .where(Order.status.in_(ACTIVE_STATUSES))
            .order_by(Order.created_at.desc())
            .limit(30)
        )
    ).all()
    lines = [f"Aaj: {today.strftime('%d %b %Y')}", f"Pending (active) orders: {len(rows)}"]
    for o, name, phone in rows:
        due = (o.total_amount or Decimal("0")) - (o.amount_paid or Decimal("0"))
        lines.append(
            f"- {o.order_number} | {name or phone} | {status_label(o.status)} | "
            f"bill ₹{o.total_amount or 0} | baaki ₹{max(due, 0)} | "
            f"delivery {o.expected_delivery.strftime('%d %b') if o.expected_delivery else '?'}"
        )

    paid_month = (
        await db.execute(
            select(func.coalesce(func.sum(Order.amount_paid), 0)).where(
                Order.created_at >= month_start
            )
        )
    ).scalar_one()
    outstanding = (
        await db.execute(
            select(
                func.coalesce(func.sum(Order.total_amount - Order.amount_paid), 0)
            ).where(
                Order.total_amount.isnot(None),
                Order.payment_status != PaymentStatus.PAID,
            )
        )
    ).scalar_one()
    customers_count = (
        await db.execute(select(func.count()).select_from(Customer))
    ).scalar_one()
    lines += [
        f"Is mahine ka collection (revenue, paise jo aa gaye): ₹{paid_month}",
        f"Kul baaki (outstanding, sab customers ka): ₹{outstanding}",
        f"Kul customers: {customers_count}",
        "(Aur detail dashboard ke Reports/Customers section mein hai.)",
    ]
    return "\n".join(lines)


async def _apply_relay(db: AsyncSession, sender_label: str, extracted: dict) -> str:
    """Forward a message to a staff member or the manager — known phones only."""
    target = extracted["relay_to"].strip()
    message = extracted["relay_message"].strip()
    if not target or not message:
        return get_message("staff_cmd_unknown")

    is_customer_target = False
    if target.lower() in ("manager", "boss", "malik"):
        to_phone, to_name = settings.MANAGER_PHONE, "Manager"
    else:
        staff_rows = (await db.execute(select(Staff))).scalars().all()
        matches = [
            s for s in staff_rows
            if s.name and (s.name.lower() in target.lower() or target.lower() in s.name.lower())
        ]
        if len(matches) == 1:
            to_phone, to_name = matches[0].phone, matches[0].name
        elif sender_label == "manager":
            # Only the ADMIN may message customers through the bot.
            cust_matches = (
                (
                    await db.execute(
                        select(Customer)
                        .where(Customer.name.ilike(f"%{target}%"), Customer.is_active)
                        .limit(3)
                    )
                )
                .scalars()
                .all()
            )
            if len(cust_matches) != 1:
                names = ", ".join(s.name for s in staff_rows if s.name) or "-"
                return get_message("relay_target_unknown", target=target, names=names)
            to_phone, to_name = cust_matches[0].phone, cust_matches[0].name or cust_matches[0].phone
            is_customer_target = True
        else:
            names = ", ".join(s.name for s in staff_rows if s.name) or "-"
            return get_message("relay_target_unknown", target=target, names=names)

    try:
        out_text = (
            get_message("relay_message_customer", message=message)
            if is_customer_target
            else get_message("relay_message", sender=sender_label, message=message)
        )
        await send_message(db, to_phone=to_phone, text=out_text)
    except WindowClosedError:
        if is_customer_target:
            # the staff template is wrong for customers — be honest instead
            return get_message("relay_window_closed", name=to_name)
        # Window shut -> fall back to the pre-approved template. If Meta
        # hasn't approved it yet this raises SendError and we say so.
        try:
            # template params must be single-line (Meta rejects newlines)
            await send_message(
                db, to_phone=to_phone,
                template_name="kk_staff_alert",
                template_params=[" ".join(f"{sender_label}: {message}".split())[:600]],
            )
        except SendError:
            log.warning("relay_template_failed", to=to_phone)
            return get_message("relay_window_closed", name=to_name)
        log.info("relay_sent_via_template", to=to_phone, by=sender_label)
        return get_message("relay_done_template", name=to_name, message=message)
    except SendError:
        log.warning("relay_send_failed", to=to_phone)
        return get_message("relay_failed", name=to_name)

    if is_customer_target:
        # answering a customer closes their open question threads
        from app.models import OpenQuestion

        open_rows = (
            (
                await db.execute(
                    select(OpenQuestion)
                    .join(Customer, Customer.id == OpenQuestion.customer_id)
                    .where(Customer.phone == to_phone, OpenQuestion.status == "open")
                )
            )
            .scalars()
            .all()
        )
        for oq in open_rows:
            oq.status = "answered"
            oq.answer = message[:2000]
            oq.answered_at = datetime.now(timezone.utc)
        if open_rows:
            await db.commit()
            log.info("open_questions_closed", count=len(open_rows), customer=to_phone)
    log.info("relay_sent", to=to_phone, by=sender_label, kind="customer" if is_customer_target else "staff")
    return get_message("relay_done", name=to_name, message=message)


async def _apply_status(db: AsyncSession, sender_label: str, extracted: dict) -> str:
    number = extracted["order_number"].upper()
    if not number or extracted["new_status"] not in _STATUS_NAMES:
        return get_message("staff_cmd_unknown")
    new_status = OrderStatus[extracted["new_status"]]
    if new_status in (OrderStatus.CANCELLED,):
        return get_message("cancel_needs_dashboard", order_number=number)
    try:
        order = await get_order(db, number)
    except OrderNotFoundError:
        return get_message("order_not_found_staff", order_number=number)
    old = order.status
    try:
        await update_status(db, order, new_status, changed_by=sender_label)
    except InvalidTransitionError:
        return get_message(
            "status_invalid",
            order_number=number,
            old=old.name,
            new=new_status.name,
        )
    return get_message(
        "status_done", order_number=number, status_name=new_status.name
    )
