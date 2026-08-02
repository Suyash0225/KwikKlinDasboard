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

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OrderStatus, PaymentMethod, Rate
from app.services import llm_client
from app.services.llm_client import LLMError
from app.services.messages import get_message, status_label
from app.services.order_service import (
    InvalidTransitionError,
    OrderNotFoundError,
    create_order,
    get_order,
    record_payment,
    set_expected_delivery,
    update_status,
)
from app.utils.phone import normalize_phone

log = structlog.get_logger()

_DRAFT_TTL = timedelta(minutes=30)
_CONFIRM_RE = re.compile(r"^\s*(haan?|ha(n|nji)?|yes|y|ok(ay)?|theek( hai)?|confirm|✅|done)\s*$", re.I)
_CANCEL_RE = re.compile(r"^\s*(nahi+|no|na|cancel|rehne do|❌|mat( banao)?)\s*$", re.I)


@dataclass
class PendingBill:
    draft: dict
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def expired(self) -> bool:
        return datetime.now(timezone.utc) - self.created_at > _DRAFT_TTL


# sender phone -> draft awaiting 'haan'
_PENDING: dict[str, PendingBill] = {}

_STATUS_NAMES = [s.name for s in OrderStatus]

_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["new_bill", "delay_update", "status_update", "other"],
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
        "new_status": {"type": "string", "enum": [*_STATUS_NAMES, ""]},
    },
    "required": [
        "action", "customer_name", "customer_phone", "items", "advance",
        "expected_delivery", "order_number", "new_date", "reason", "new_status",
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
    "- other: anything else (greetings, questions, chatter).\n"
    "If a CURRENT DRAFT is provided, the message is an edit to it: return "
    "action=new_bill with the FULL corrected draft (unchanged fields kept). "
    "Fill every unused field with '' / [] / 0. Never invent items, phones or "
    "prices."
)


async def handle_staff_message(
    db: AsyncSession, *, sender_phone: str, sender_label: str, text: str
) -> str | None:
    """Reply for a staff/manager inbound, or None to stay silent."""
    if not text or text.startswith("["):
        return None

    pending = _PENDING.get(sender_phone)
    if pending and pending.expired:
        _PENDING.pop(sender_phone, None)
        pending = None

    if pending and _CONFIRM_RE.match(text):
        return await _finalize_bill(db, sender_phone, sender_label, pending)
    if pending and _CANCEL_RE.match(text):
        _PENDING.pop(sender_phone, None)
        return get_message("bill_cancelled")

    try:
        extracted = await _extract(db, text, pending)
    except LLMError as exc:
        log.warning("staff_extract_failed", error=str(exc)[:150])
        # Never leave the MANAGER wondering — staff chatter can stay silent.
        return get_message("ai_down_staff") if sender_label == "manager" else None

    action = extracted["action"]
    if action == "new_bill" and extracted["items"]:
        draft = await _price_draft(db, extracted)
        _PENDING[sender_phone] = PendingBill(draft=draft)
        return _draft_summary(draft)
    if action == "delay_update":
        return await _apply_delay(db, sender_label, extracted)
    if action == "status_update":
        return await _apply_status(db, sender_label, extracted)

    # 'other': only nag the sender when they're mid-draft or they're the
    # manager trying to talk to the bot; plain staff chatter stays silent.
    if pending or sender_label == "manager":
        return get_message("staff_cmd_unknown")
    return None


async def _extract(db: AsyncSession, text: str, pending: PendingBill | None) -> dict:
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
    prompt += f"STAFF MESSAGE:\n{text[:1000]}"
    return await llm_client.ask_json(
        system=_EXTRACT_SYSTEM,
        user_text=prompt,
        schema=_EXTRACT_SCHEMA,
        model=llm_client.MODEL_CHEAP,
        max_tokens=700,
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
            db, order, amount=Decimal(str(d["advance"])), method=PaymentMethod.CASH
        )
    _PENDING.pop(sender_phone, None)
    log.info("bill_created_via_whatsapp", order_number=order.order_number, by=sender_label)
    return get_message(
        "bill_created",
        order_number=order.order_number,
        total=f"{d['total']:g}" if d["total"] else "—",
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


async def _apply_status(db: AsyncSession, sender_label: str, extracted: dict) -> str:
    number = extracted["order_number"].upper()
    if not number or not extracted["new_status"]:
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
