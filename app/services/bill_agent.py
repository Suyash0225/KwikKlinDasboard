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
from app.models import (
    Conversation,
    Customer,
    Direction,
    Order,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    Rate,
    Staff,
)
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

# owner's WhatsApp sandbox: phone -> {"last_q": last test question}
# ('test customer' se on, 'test band' se off; sikhao: se Correction banti hai)
_TEST_MODE: dict[str, dict] = {}

_STATUS_NAMES = [s.name for s in OrderStatus]

_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "new_bill", "delay_update", "status_update", "relay",
                "set_priority", "assign_staff", "add_note", "record_payment",
                "standup_reply", "other",
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
        # standup replies: list positions ("1","2") or order numbers
        "done_refs": {"type": "array", "items": {"type": "string"}},
        "pending_refs": {"type": "array", "items": {"type": "string"}},
        "problem": {"type": "string"},
    },
    "required": [
        "action", "customer_name", "customer_phone", "items", "advance",
        "expected_delivery", "order_number", "new_date", "reason", "new_status",
        "relay_to", "relay_message", "priority", "staff_name", "note",
        "amount", "method", "done_refs", "pending_refs", "problem",
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
    "- relay: they ask to pass a message or a question to a person — staff, "
    "manager OR a customer (e.g. 'Ravi ko bata do ...', 'Anmol ko bolo kal "
    "tak ho jayega', 'Superman se pucho pickup hua ya nahi'). relay_to = the "
    "person's name as written; relay_message = the message REWRITTEN as if "
    "speaking DIRECTLY to that person (aap/tum form). NEVER copy the "
    "sender's imperative words (pucho/bolo/bata do/usse) into "
    "relay_message. Example: 'Superman se pucho kya usne Rahul ka pickup "
    "kia' -> relay_message: 'Kya aapne Rahul ka pickup kar liya? Update "
    "bata dijiye.'\n"
    "- set_priority: an order is urgent / no longer urgent ('Sharma ji ka "
    "urgent hai') — order_number (if named) + customer_name + priority.\n"
    "- assign_staff: give an order to a staff member ('ye Ravi ko de do') — "
    "order_number + staff_name.\n"
    "- add_note: an internal instruction about an order ('collar pe daag "
    "hai, dhyan se') — order_number + note.\n"
    "- record_payment: money received for an order ('KK-... ka 200 cash "
    "mila') — order_number, amount, method (cash/upi/other).\n"
    "- standup_reply: a STAFF member reporting on their work list ('1 aur 2 "
    "ho gaya, 3 ka pant pending hai, blanket kal karunga') — done_refs = "
    "list positions or order numbers that are FINISHED, pending_refs = ones "
    "explicitly still pending, problem = any issue mentioned (machine "
    "kharab, paani nahi...) or ''.\n"
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

    # "done KK-20260803-01" — staff quick-confirm, zero LLM (owner's spec)
    if text:
        m_done = re.match(r"^\s*done\s+(KK-\S+)\s*$", text, re.I)
        if m_done and sender_label != "manager":
            return await _apply_done(db, sender_phone, sender_label, m_done.group(1).upper())
        # "done T-14" — closes an assigned task, also zero LLM
        m_task = re.match(r"^\s*done\s+(T-?\d+)\s*$", text, re.I)
        if m_task:
            return await _close_task_by_code(db, sender_phone, sender_label, m_task.group(1))

    # Campaign approvals are deterministic commands, no LLM needed.
    if sender_label == "manager" and text:
        m = re.match(r"^\s*campaign\s+(yes|haan|nahi|no|skip)\s*$", text, re.I)
        if m:
            from app.services.marketing_agent import approve_latest_suggestion

            return await approve_latest_suggestion(
                db, approved=m.group(1).lower() in ("yes", "haan")
            )
        # WhatsApp sandbox: the owner tests + trains the agents in chat.
        test_reply = await _handle_test_mode(db, sender_phone, text)
        if test_reply is not None:
            return test_reply

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
        reply = await _apply_status(db, sender_label, extracted, sender_phone)
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
    elif action == "standup_reply" and sender_label != "manager":
        reply = await _apply_standup_reply(db, sender_phone, sender_label, extracted)

    # Whatever a staff member says while they owe us an answer belongs on
    # the task — the owner should see their words, not just "no reply yet".
    if sender_label != "manager" and text and not text.startswith("["):
        try:
            from app.services import tasks as task_service

            staff = (
                await db.execute(select(Staff).where(Staff.phone == sender_phone))
            ).scalar_one_or_none()
            if staff is not None:
                await task_service.note_reply(db, staff.id, text)
        except Exception:
            log.exception("task_note_reply_failed")
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
        advance_hint=Decimal(str(d["advance"])) if d["advance"] else None,
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


async def _handle_test_mode(db: AsyncSession, phone: str, text: str) -> str | None:
    """Owner's in-chat sandbox. Returns None when not a test interaction."""
    t = text.strip()
    if re.match(r"^(test customer|customer bano|test service)$", t, re.I):
        _TEST_MODE[phone] = {"last_q": ""}
        return (
            "🧪 Test mode ON — ab aap customer ho. Jo chaho pucho, main "
            "customer-agent ki tarah jawaab dunga (kuch bhi asli nahi hoga — "
            "na escalation, na alerts).\n"
            "Galat jawaab pe: 'sikhao: <sahi jawaab>' — turant seekh lunga.\n"
            "Wapas aane ke liye: 'test band'"
        )
    if re.match(r"^(test band|test stop|stop test)$", t, re.I):
        if _TEST_MODE.pop(phone, None) is not None:
            return "🧪 Test mode OFF — wapas malik mode mein. 👑"
        return None
    if re.match(r"^(test marketing|marketing test)$", t, re.I):
        from app.services.marketing_agent import preview_suggestion

        return await preview_suggestion(db)
    if re.match(r"^(social bhejo|test social|post banao)$", t, re.I):
        from app.services.social import run_daily_social

        status = await run_daily_social(force=True)
        return {
            "posted": "✅ Aaj ka poster Instagram par post ho gaya + aapko pack bheja.",
            "skipped": "📸 Poster + caption aapko bhej diya. (Instagram abhi linked nahi — Settings mein IG id/token daalo to wahan bhi khud jayega.)",
            "disabled": "Daily social Settings mein OFF hai.",
            "already_done": "Aaj ka post pehle hi ban chuka — 'social bhejo' kal phir chalega.",
        }.get(status, f"⚠️ Kuch gadbad: {status[:120]}")

    state = _TEST_MODE.get(phone)
    if state is None:
        return None

    # teaching: 'sikhao: <right answer>' after a question -> Correction+FAQ;
    # 'sikhao: <policy/rule>' on its own -> straight knowledge (FAQ). Both
    # land in the TRUSTED facts block, so taught prices/times actually stick.
    m = re.match(r"^(sikhao|sikho|teach)\s*[:\-]\s*(.+)$", t, re.I | re.S)
    if m:
        taught = m.group(2).strip()[:2000]
        from app.models import Correction, FaqEntry

        last_q = (state.get("last_q") or "").strip()
        if last_q and last_q.lower() != taught.lower():
            db.add(Correction(question=last_q[:2000], correct_reply=taught))
            db.add(FaqEntry(question=last_q[:2000], answer=taught))
            what = f"'{last_q[:80]}' ka jawaab"
        else:
            # no paired question — treat it as a shop rule / knowledge note
            db.add(FaqEntry(question=taught[:300], answer=taught))
            what = "naya niyam"
        await db.commit()
        await audit.record(
            actor_role="admin", actor="manager", action="taught_via_whatsapp",
            args={"question": last_q[:150] or "(rule)"}, result=taught[:150],
        )
        return (
            f"✅ Seekh liya ({what})! Ab yahi jawaab dunga.\n"
            "(Dashboard → AI training mein dikh jayega.)\n"
            "Dobara puch ke check kar lo. 🧪"
        )

    # anything else in test mode = a customer question -> sandboxed brain
    from app.services.ai_agent import build_ai_reply

    cust = (
        await db.execute(select(Customer).where(Customer.phone == phone))
    ).scalar_one_or_none()
    if cust is None:
        return "🧪 Test ke liye aapka customer record nahi mila — 'test band' karke dobara try karo."
    state["last_q"] = t
    try:
        reply = await build_ai_reply(db, cust, t, sandbox=True)
    except LLMError:
        reply = None
    if reply is None:
        return "🧪 (AI abhi jawaab nahi de paya — LLM down ya samajh nahi aaya. Real mein customer ko rule-based ack jata.)"
    return f"🧪 Customer ko ye jata:\n\n{reply}\n\n(sikhao: <sahi jawaab> | test band)"


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


async def _apply_standup_reply(
    db: AsyncSession, sender_phone: str, sender_label: str, extracted: dict
) -> str:
    """Map 'ho gaya' refs to real orders, update statuses, brief the admin.

    Refs are list positions from the morning standup (re-derived — same
    deterministic query) or explicit KK- numbers. The confirmation names
    every order number, so a mis-mapped ref is immediately visible.
    """
    from app.services.scheduler import _pending_orders_for

    staff = (
        await db.execute(select(Staff).where(Staff.phone == sender_phone))
    ).scalar_one_or_none()
    if staff is None:
        return get_message("staff_cmd_unknown")
    default_phone = await app_settings.get(db, "default_washer_phone")
    my_orders = await _pending_orders_for(db, staff, default_phone)

    def _resolve(ref: str) -> Order | None:
        ref = ref.strip().upper()
        if ref.startswith("KK-"):
            return next((o for o in my_orders if o.order_number == ref), None)
        if ref.isdigit():
            idx = int(ref) - 1
            if 0 <= idx < len(my_orders):
                return my_orders[idx]
        return None

    done_lines, unclear = [], []
    for ref in extracted["done_refs"]:
        order = _resolve(ref)
        if order is None:
            unclear.append(ref)
            continue
        try:
            await update_status(db, order, OrderStatus.READY, changed_by=staff.name)
            done_lines.append(f"{order.order_number} → READY")
        except InvalidTransitionError:
            done_lines.append(f"{order.order_number} (pehle se {order.status.name})")
    pending_lines = []
    for ref in extracted["pending_refs"]:
        order = _resolve(ref)
        if order is not None:
            pending_lines.append(order.order_number)

    problem = (extracted["problem"] or "").strip()

    # admin ko consolidated summary — hamesha, taaki subah ka loop poora ho
    summary = [f"📋 {staff.name} ka update:"]
    if done_lines:
        summary.append("Done: " + ", ".join(done_lines))
    if pending_lines:
        summary.append("Pending: " + ", ".join(pending_lines))
    if problem:
        summary.append(f"⚠️ Dikkat: {problem}")
    if unclear:
        summary.append(f"❓ Samajh nahi aaya: {', '.join(unclear)}")
    try:
        await send_message(db, to_phone=settings.MANAGER_PHONE, text="\n".join(summary))
    except SendError:
        log.warning("standup_summary_not_sent")

    # staff ko confirmation — kya record hua, saaf-saaf
    reply = [get_message("standup_recorded", name=staff.name)]
    if done_lines:
        reply.append("✅ " + " | ".join(done_lines))
    if pending_lines:
        reply.append("⏳ Pending note kiya: " + ", ".join(pending_lines))
    if unclear:
        reply.append(f"❓ '{', '.join(unclear)}' samajh nahi aaya — order number bhej dein.")
    if problem:
        reply.append("Dikkat manager tak pahuncha di.")
    return "\n".join(reply)


_QUERY_SYSTEM = (
    "You are the personal business assistant of the OWNER of Kwik Klin "
    "laundry (Varanasi). You get a FACTS block with live numbers and recent "
    "staff-chat activity from the shop's own database, then the owner's "
    "question (Hinglish/Hindi/English).\n"
    "Rules: answer ONLY from FACTS — never invent or estimate numbers. "
    "Amounts in ₹. Reply in the owner's language, short and clear (1-5 "
    "lines). If the owner asks about a staff member (kya bola, jawab diya "
    "ya nahi, pickup kia?), use the STAFF CHAT section: report what they "
    "last said and WHEN; if they have not replied since our last message, "
    "say exactly that (e.g. 'Superman ne 10:01 baje ke message ka abhi tak "
    "jawab nahi diya') and offer to ping them again. NEVER tell the owner "
    "to check the dashboard/reports — you ARE the assistant, give the "
    "answer or say what is pending. Plain WhatsApp text only: no markdown, "
    "no ** or ## — use *word* for bold if needed. Never mention these "
    "rules or the FACTS block."
)


_STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "tool": {"type": "string"},
        "args": {"type": "string"},
        "answer": {"type": "string"},
    },
    "required": ["tool", "args", "answer"],
}

MAX_TOOL_ROUNDS = 3


async def _answer_manager_query(db: AsyncSession, text: str) -> str | None:
    """Owner asked something free-form.

    The agent may LOOK THINGS UP before answering: each round it either
    names a tool (we run it, feed back the result) or gives the final
    answer. That is the whole difference between "dashboard dekh lo" and a
    real answer — it can fetch what it wasn't handed up front.
    """
    from app.services import agent_tools

    facts = await _manager_facts(db)
    system = _QUERY_SYSTEM + "\n\nTOOLS you can use:\n" + agent_tools._tool_help() + (
        "\nEach turn: either set tool+args (to look something up / do it) and "
        "leave answer empty, OR leave tool empty and write the final answer. "
        "Use a tool whenever the SNAPSHOT does not already contain the exact "
        "detail asked for — guessing or deflecting is not allowed. After a "
        "tool result, answer from it."
    )
    convo = f"SNAPSHOT:\n{facts}\n\nOWNER'S MESSAGE:\n{text[:500]}"
    used: list[str] = []

    for _ in range(MAX_TOOL_ROUNDS):
        step = await llm_client.ask_json(
            system=system, user_text=convo, schema=_STEP_SCHEMA,
            model=llm_client.MODEL_SMART, max_tokens=700,
        )
        tool = (step.get("tool") or "").strip()
        answer = (step.get("answer") or "").strip()
        if not tool:
            if answer:
                log.info("manager_query_answered", chars=len(answer), tools=used)
                return answer
            break
        args = (step.get("args") or "").strip()
        observation = await agent_tools.run_tool(db, tool, args)
        used.append(tool)
        convo += f"\n\nTOOL {tool}({args}) ka result:\n{observation}"

    # Out of rounds — force a final answer from whatever we gathered.
    reply = (
        await llm_client.ask(
            system=system + "\nAb koi tool nahi. Jo mila hai usi se jawab do.",
            user_text=convo,
            model=llm_client.MODEL_SMART,
            max_tokens=500,
        )
    ).strip()
    log.info("manager_query_answered", chars=len(reply), tools=used, forced=True)
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
    ]

    # STAFF CHAT: last exchange per staff member, so "Superman ne jawab
    # diya?" has a real answer instead of a dashboard deflection.
    ist = timezone(timedelta(hours=5, minutes=30))
    staff_rows = (
        await db.execute(select(Staff).where(Staff.is_active))
    ).scalars().all()
    if staff_rows:
        lines.append("STAFF CHAT (aakhri messages, IST time ke saath):")
    for st in staff_rows:
        msgs = (
            await db.execute(
                select(Conversation)
                .where(Conversation.staff_id == st.id)
                .order_by(Conversation.created_at.desc())
                .limit(3)
            )
        ).scalars().all()
        if not msgs:
            lines.append(f"- {st.name}: koi baat-cheet nahi hui")
            continue
        for m in reversed(msgs):
            who = st.name if m.direction is Direction.INBOUND else "humne bheja"
            at = m.created_at.astimezone(ist).strftime("%d %b %H:%M")
            lines.append(f"- [{at}] {who}: {m.message_text[:110]}")
        last = msgs[0]
        if last.direction is Direction.OUTBOUND:
            lines.append(
                f"  ({st.name} ne iske baad se KOI JAWAB NAHI diya hai)"
            )
    return "\n".join(lines)


# "jaldi", "urgent", "abhi" -> the task gets a tighter follow-up clock
_URGENT_RE = re.compile(
    r"\b(urgent|jaldi|jldi|turant|abhi|asap|emergency|maang|manga|chahiye)\b", re.I
)
# same shape as the webhook's matcher — order ids look like KK-20260801-01
ORDER_NUMBER_RE = re.compile(r"\bKK-\d{8}-\d{2,}\b", re.IGNORECASE)


async def _close_task_by_code(
    db: AsyncSession, sender_phone: str, sender_label: str, code: str
) -> str:
    from app.services import tasks as task_service

    task = await task_service.get_by_code(db, code)
    if task is None:
        return get_message("task_unknown_code", code=code.upper())
    await task_service.complete_task(db, task, by=sender_label)
    # keep the owner in the loop without him having to ask
    try:
        staff = await db.get(Staff, task.assigned_staff_id) if task.assigned_staff_id else None
        await send_message(
            db, to_phone=settings.MANAGER_PHONE,
            text=f"✅ {staff.name if staff else sender_label} ne {task.code} kar diya: {task.title}",
        )
    except SendError:
        log.info("task_done_owner_notify_failed", code=task.code)
    return get_message("task_done_ack", code=task.code)


async def _order_in_text(db: AsyncSession, text: str) -> Order | None:
    """Pull a KK-... order out of free text so the task links to the bill."""
    m = ORDER_NUMBER_RE.search(text or "")
    if not m:
        return None
    return (
        await db.execute(select(Order).where(Order.order_number == m.group(0).upper()))
    ).scalar_one_or_none()


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
            # A message to STAFF is work, not chatter: make it a tracked task
            # so the agent chases it and the dashboard shows who owes what.
            from app.services import tasks as task_service

            urgent = bool(_URGENT_RE.search(message))
            order = await _order_in_text(db, f"{message} {extracted.get('order_number', '')}")
            task = await task_service.create_task(
                db, title=message, staff=matches[0], order=order,
                urgent=urgent, created_by=sender_label,
            )
            # last_ping_at is only stamped when the message actually went out
            key = "task_assigned" if task.last_ping_at else "task_assigned_undelivered"
            return get_message(
                key, name=matches[0].name, code=task.code, message=message
            )
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


# Role scoping (owner's spec): washerman delivery ka status nahi badal
# sakta, delivery boy dhulai ka nahi. Manager sab kuch.
_ROLE_STATUSES = {
    "WASHER": {
        OrderStatus.IN_WASH, OrderStatus.IN_DRY, OrderStatus.IN_IRON,
        OrderStatus.READY,
    },
    "DELIVERY": {
        OrderStatus.PICKUP_ASSIGNED, OrderStatus.PICKED_UP,
        OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED,
    },
}


async def _sender_role(db: AsyncSession, sender_phone: str) -> str:
    st = (
        await db.execute(select(Staff).where(Staff.phone == sender_phone))
    ).scalar_one_or_none()
    return st.role.name if st else "manager"


async def _apply_done(
    db: AsyncSession, sender_phone: str, sender_label: str, number: str
) -> str:
    """'done KK-x': the role decides what 'done' means."""
    try:
        order = await get_order(db, number)
    except OrderNotFoundError:
        return get_message("order_not_found_staff", order_number=number)
    role = await _sender_role(db, sender_phone)
    if role == "DELIVERY":
        target = (
            OrderStatus.DELIVERED
            if order.status is OrderStatus.OUT_FOR_DELIVERY
            else OrderStatus.PICKED_UP
        )
    elif role == "WASHER":
        target = OrderStatus.READY
    else:
        return get_message("staff_cmd_unknown")
    try:
        await update_status(db, order, target, changed_by=sender_label)
    except InvalidTransitionError:
        return get_message(
            "status_invalid", order_number=number,
            old=order.status.name, new=target.name,
        )
    await audit.record(
        actor_role="staff", actor=sender_label, action="done_command",
        args={"order": number}, result=target.name,
    )
    return get_message("status_done", order_number=number, status_name=target.name)


async def _apply_status(
    db: AsyncSession, sender_label: str, extracted: dict, sender_phone: str = ""
) -> str:
    number = extracted["order_number"].upper()
    if not number or extracted["new_status"] not in _STATUS_NAMES:
        return get_message("staff_cmd_unknown")
    new_status = OrderStatus[extracted["new_status"]]
    if new_status in (OrderStatus.CANCELLED,):
        return get_message("cancel_needs_dashboard", order_number=number)
    if sender_label != "manager" and sender_phone:
        role = await _sender_role(db, sender_phone)
        allowed = _ROLE_STATUSES.get(role, set())
        if allowed and new_status not in allowed:
            return get_message("role_not_allowed", status_name=new_status.name)
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
