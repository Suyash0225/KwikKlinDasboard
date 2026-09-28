from app.utils.dates import today_ist
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
- A bill PHOTO is only TRANSCRIBED by the model (it is not shown the rate
  card); our code does the matching. A slip it cannot read asks the sender
  instead of filling the bill with the shop's usual garments.
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
from app.services.llm_client import LLMAuthError, LLMError, LLMRateLimited
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
from app.services.whatsapp import ListRow, SendError, WindowClosedError, send_message
from app.utils.phone import normalize_phone
from app.services.tenant_context import manager_phone

log = structlog.get_logger()

_DRAFT_TTL = timedelta(minutes=30)
# Inbound photos land as "[image:/admin/media/<file>] optional caption"
_IMAGE_MARKER_RE = re.compile(r"^\[image:/admin/media/([A-Za-z0-9._\-]+)\]\s*(.*)$", re.DOTALL)
_MEDIA_DIR = Path(__file__).resolve().parent.parent / "media"
_CONFIRM_RE = re.compile(r"^\s*(haan?|ha(n|nji)?|yes|y|ok(ay)?|theek( hai)?|confirm|✅|done)\s*$", re.I)
_CANCEL_RE = re.compile(r"^\s*(nahi+|no|na|cancel|rehne do|❌|mat( banao)?)\s*$", re.I)

# Fixed-choice prompts use WhatsApp list menus. Free-form answers (ETA, issue
# description, bill edits) remain normal text because those need real input.
_FIXED_BTN_RE = re.compile(
    r"^\s*\[button:(bill_confirm|payment_confirm|relay_confirm):([^\]]+)\]",
    re.I,
)

async def _send_fixed_choice_menu(
    db: AsyncSession,
    to_phone: str,
    text: str,
    rows: list[ListRow],
    *,
    button: str = "Select",
    title: str = "Kwik Klin",
) -> bool:
    """Send a bounded-choice prompt as a WhatsApp list; False means fallback."""
    try:
        await send_message(
            db, to_phone=to_phone, text=text, list_rows=rows,
            list_button=button, list_title=title, sent_by="bot",
        )
        return True
    except (WindowClosedError, SendError):
        log.info("fixed_choice_menu_not_sent", to=to_phone)
        return False



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


@dataclass
class PendingTaskEta:
    """Staff ne "⏳ Time lagega" dabaya — agla message uska ETA hai."""

    code: str
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def expired(self) -> bool:
        return (datetime.now(timezone.utc) - self.at) > timedelta(hours=6)


@dataclass
class PendingTaskIssue:
    """Staff ne "❓ Dikkat hai" dabaya — agla message wajah hai."""

    code: str
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def expired(self) -> bool:
        return (datetime.now(timezone.utc) - self.at) > timedelta(hours=6)


@dataclass
class PendingRelay:
    """"Ajit ko bhej do" — kiske paas bhejna hai pata hai, KYA bhejna hai
    nahi. Agla message hi wo baat hai."""

    target: str
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def expired(self) -> bool:
        return (datetime.now(timezone.utc) - self.at) > timedelta(minutes=30)


@dataclass
class PendingOrderEta:
    """Wahi baat order par — "⏳ Time lagega" ke baad ka jawab."""

    number: str
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def expired(self) -> bool:
        return (datetime.now(timezone.utc) - self.at) > timedelta(hours=6)


@dataclass
class PendingOrderIssue:
    """Order par "❓ Dikkat hai" — agla message wajah hai."""

    number: str
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def expired(self) -> bool:
        return (datetime.now(timezone.utc) - self.at) > timedelta(hours=6)


# sender phone -> action awaiting 'haan' (a bill draft or a payment)
_PENDING: dict[str, PendingBill | PendingPayment] = {}

# owner's WhatsApp sandbox: phone -> {"last_q": last test question}
# ('test customer' se on, 'test band' se off; sikhao: se Correction banti hai)
_TEST_MODE: dict[str, dict] = {}

# Staff reference memory: when a staff member asks "T-123 kahan hai?" and
# immediately follows with "kitne pcs?", the second message must still refer
# to T-123. This is short-lived, per phone, and never replaces DB truth.
_STAFF_REF_CONTEXT: dict[str, tuple[str, str, datetime]] = {}
_STAFF_REF_TTL = timedelta(minutes=20)
_TASK_REF_RE = re.compile(r"\bT\s*-?\s*(\d+)\b", re.I)
_ORDER_REF_RE_STAFF = re.compile(r"\bKK-\d{8}-\d{2,}\b", re.I)

_STATUS_NAMES = [s.name for s in OrderStatus]

# Khali extraction — jab hum khud (bina LLM ke) koi action banate hain.
_EMPTY_EXTRACT: dict = {
    "action": "other", "customer_name": "", "customer_phone": "", "items": [],
    "advance": 0, "expected_delivery": "", "order_number": "", "new_date": "",
    "reason": "", "new_status": "NONE", "relay_to": "", "relay_message": "",
    "priority": "NONE", "staff_name": "", "recipient_type": "UNKNOWN", "note": "", "amount": 0,
    "method": "NONE", "done_refs": [], "pending_refs": [], "problem": "",
}

_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "new_bill", "delay_update", "change_delivery_date", "status_update", "relay",
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
        "recipient_type": {"type": "string", "enum": ["STAFF", "CUSTOMER", "MANAGER", "SELF", "UNKNOWN"]},
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
        "relay_to", "relay_message", "recipient_type", "priority", "staff_name", "note",
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
    "- delay_update / change_delivery_date: change an order delivery promise — extract order_number when given; otherwise extract customer_name. Extract new_date as ISO YYYY-MM-DD. Resolve relative dates such as kal/parso/Friday from TODAY in India (IST). If multiple active orders match a customer name, execution must ask for the order number. Extract internal reason.\n"
    "- status_update: they state an order's new stage (dhul gaya, ready hai, "
    "nikal gaya, deliver ho gaya...) — map to one of the status names.\n"
    "- relay: first understand the OWNER/STAFF instruction, then identify the "
    "recipient_type as exactly STAFF, CUSTOMER, MANAGER, SELF or UNKNOWN. "
    "Words such as 'customer ko', 'grahak ko', 'customer', 'customer se', "
    "'buyer ko' ALWAYS mean CUSTOMER, never STAFF or SELF. 'manager/boss/malik' "
    "means MANAGER. A named staff member means STAFF. If ambiguous, use UNKNOWN "
    "and do not create a task for the sender.\n"
    "  relay_to is the named recipient when known. relay_message is the intended "
    "meaning, not a copy of the command. Never include 'bolo/bata do/pucho/usse/' "
    "in relay_message. The sending layer will make the message professional "
    "before it is sent. Example: 'Ajit se pucho Rahul ka pickup hua?' -> "
    "recipient_type=STAFF, relay_to=Ajit, relay_message='Rahul ka pickup hua "
    "ya nahi? Kripya update bata dijiye.'\n"
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


# Task ke teen buttons (tasks.py `_send_to_assignee`): tap = zero typing.
# Button id mein code hota hai — "task:T-11:done" — isliye 5-6 kaam ek saath
# hone par bhi kaunsa kaam hai ye kabhi galat nahi hota. Jise likhna hai wo
# likh sakta hai; ye sirf sabse aam jawab ka shortcut hai.
_TASK_BTN_RE = re.compile(
    r"^\s*\[button:task:(T-?\d+):(done|later|problem|wash|iron|ready|pending)\]",
    re.I,
)
# List se chuna gaya kaam ("pick:o:KK-...", "pick:t:T-11") aur order card ke
# apne teen button. Order ke button ko pehle parkha jata hai — warna
# "ord:KK-..:done" ko pick samajh liya jata.
_WORK_PICK_RE = re.compile(r"^\s*\[button:pick:(o|t):([^\]:]+)\]", re.I)
_ORDER_BTN_RE = re.compile(r"^\s*\[button:ord:(KK-\S+?):(done|later|problem)\]", re.I)


async def _handle_task_button(
    db: AsyncSession, sender_phone: str, sender_label: str, text: str
) -> str | None:
    """Handle deterministic task-menu replies. None means this is not a task menu."""
    m = _TASK_BTN_RE.match(text or "")
    if not m:
        return None

    code, action = m.group(1).upper(), m.group(2).lower()
    from app.services import tasks as task_service, team

    task = await task_service.get_by_code(db, code)
    if task is None:
        return get_message("task_unknown_code", code=code)

    # A task menu belongs only to its assignee (manager/admin can operate it).
    staff = (
        await db.execute(select(Staff).where(Staff.phone == sender_phone))
    ).scalar_one_or_none()
    if (
        staff is not None
        and task.assigned_staff_id is not None
        and staff.role.name not in {"ADMIN", "MANAGER"}
        and task.assigned_staff_id != staff.id
    ):
        return "Ye task aapke naam par assigned nahi hai. Kripya apne assigned task ka menu use karein."

    role = staff.role.name if staff is not None else "manager"

    # Delivery staff: the same Done/Pending menu is used for both pickup and delivery.
    if action == "done":
        if role not in {"DELIVERY", "ADMIN", "MANAGER"}:
            return "This task is assigned to a different role."
        if task.kind in {"pickup", "delivery"}:
            return await task_service.confirm_pickup(
                db, task, done=True, by=staff.name if staff else sender_label
            )
        return await _close_task_by_code(db, sender_phone, sender_label, code)

    if action == "pending":
        staff_id = staff.id if staff is not None else task.assigned_staff_id
        await task_service.note_reply(db, staff_id, "Pending")
        if task.kind in {"pickup", "delivery"}:
            _PENDING[sender_phone] = PendingTaskEta(code=code)
            await team.notify_admins(
                db,
                f"⏳ *Task pending — {task.code}*\n"
                f"Staff: {staff.name if staff else sender_label}\n"
                f"Task: {task.title}\n"
                "Current status: Pending. ETA will be requested from the staff member.",
            )
            return (
                f"⏳ *{task.code}* — Pending status recorded.\n"
                "Please tell me the expected completion time, for example: "
                "2 baje / sham tak / kal subah."
            )
        return f"⏳ *{task.code}* — Pending status recorded. Please update again when the task is completed."

    # Washer/supervisor menu: update the order stage deterministically.
    if action in {"wash", "iron", "ready"}:
        if role not in {"WASHER", "SUPERVISOR", "ADMIN", "MANAGER"}:
            return "Ye status option sirf washing staff ke liye hai."
        if task.order_id is None:
            return f"*{task.code}* order se linked nahi hai, isliye order status update nahi kiya ja sakta."

        order = await db.get(Order, task.order_id)
        if order is None:
            return get_message("order_not_found_staff", order_number="linked order")

        target = {
            "wash": OrderStatus.IN_WASH,
            "iron": OrderStatus.IN_IRON,
            "ready": OrderStatus.READY,
        }[action]
        try:
            old = order.status
            await update_status(db, order, target, changed_by=sender_label)
        except InvalidTransitionError:
            return get_message(
                "status_invalid",
                order_number=order.order_number,
                old=old.name,
                new=target.name,
            )

        label = {
            "wash": "In Wash",
            "iron": "In Iron",
            "ready": "Ready",
        }[action]
        await audit.record(
            actor_role="staff",
            actor=sender_label,
            action="task_menu_status_update",
            args={"task": code, "order": order.order_number, "status": target.name},
            result=target.name,
        )
        return (
            f"✅ *{order.order_number}* status updated to *{label}*.\n"
            f"Task: {code}\n"
            "The dashboard and next workflow step have been updated."
        )

    if action == "later":
        _PENDING[sender_phone] = PendingTaskEta(code=code)
        return (
            f"⏳ *{code}* — please tell me the expected completion time.\n"
            "Example: 2 baje / sham tak / kal subah."
        )

    # problem: owner ko turant khabar, aur staff se wajah poochho
    _PENDING[sender_phone] = PendingTaskIssue(code=code)
    await team.notify_admins(
        db,
        f"⚠️ *Task issue — {code}*\n"
        f"Staff: {staff.name if staff else sender_label}\n"
        f"Task: {task.title}\n"
        "Please review the issue and advise.",
    )
    return f"⚠️ *{code}* — please describe the issue briefly. I will notify the manager."


async def _send_with_buttons(
    db: AsyncSession, to_phone: str, text: str, buttons: list
) -> str | None:
    """Buttons ke saath bhejo. Na ja paye to wahi baat text mein wapas do —
    caller use normal reply ki tarah bhej dega (kuch bhi chupke se gire na)."""
    try:
        await send_message(db, to_phone=to_phone, text=text, buttons=buttons)
        return None
    except (WindowClosedError, SendError) as exc:
        log.warning("work_card_buttons_failed", to=to_phone, error=str(exc)[:120])
        return text


async def _handle_work_pick(
    db: AsyncSession, sender_phone: str, sender_label: str, text: str
) -> str | None:
    """List mein se chuna gaya order/task -> uska card + teen buttons."""
    m = _WORK_PICK_RE.match(text or "")
    if not m:
        return None
    kind, ref = m.group(1).lower(), m.group(2).strip()
    if kind == "t":
        from app.services import tasks as task_service

        task = await task_service.get_by_code(db, ref.upper())
        if task is None:
            return get_message("task_unknown_code", code=ref.upper())
        body = f"📝 [{task.code}] {task.title}"
        if task.urgent:
            body += "\n🔴 URGENT"
        body += f"\n\nKya status hai? Ya likh dijiye: done {task.code}"
        from app.services.work_orders import task_buttons

        return await _send_with_buttons(
            db, sender_phone, body, await task_buttons(db, task.code)
        ) or ""

    try:
        order = await get_order(db, ref.upper())
    except OrderNotFoundError:
        return get_message("order_not_found_staff", order_number=ref.upper())
    from app.services.work_orders import items_summary, order_buttons

    cust = await db.get(Customer, order.customer_id)
    body = (
        f"🧺 {order.order_number}\n"
        f"{cust.name or cust.phone if cust else '?'} — {items_summary(order)}\n"
        f"Abhi: {status_label(order.status)}"
    )
    if order.expected_delivery:
        body += f"\nDelivery: {order.expected_delivery.strftime('%d %b')}"
    if order.priority == "urgent":
        body += "\n🔴 URGENT"
    body += f"\n\nKya status hai? Ya likh dijiye: done {order.order_number}"
    return await _send_with_buttons(
        db, sender_phone, body, await order_buttons(db, order.order_number)
    ) or ""


async def _handle_order_button(
    db: AsyncSession, sender_phone: str, sender_label: str, text: str
) -> str | None:
    """Order card ke button ka jawab. None = ye button nahi hai."""
    m = _ORDER_BTN_RE.match(text or "")
    if not m:
        return None
    number, action = m.group(1).upper(), m.group(2).lower()
    try:
        # Sirf maujoodgi ki jaanch — ye call OrderNotFoundError uthata hai.
        # Nateeja aage kahin use nahi hota, isliye bindna hi nahi.
        await get_order(db, number)
    except OrderNotFoundError:
        return get_message("order_not_found_staff", order_number=number)

    if action == "done":
        # Wahi rasta jo "done KK-..." likhne par chalta hai — role hi tay
        # karta hai ki 'ho gaya' ka matlab READY hai ya DELIVERED.
        return await _apply_done(db, sender_phone, sender_label, number)

    if action == "later":
        _PENDING[sender_phone] = PendingOrderEta(number=number)
        return (
            f"Theek hai 👍 {number} kab tak ho jayega?\n"
            "Bas likh dijiye — jaise: 2 baje / sham tak / kal subah."
        )

    _PENDING[sender_phone] = PendingOrderIssue(number=number)
    from app.services import team

    await team.notify_admins(
        db, f"❓ {sender_label} ne {number} par dikkat batayi."
    )
    return f"Kya dikkat aa rahi hai {number} mein? Likh dijiye, main {settings.SHOP_NAME} ko bata deta hoon."


# Staff kisi ORDER par dikkat bata raha hai — "lehenga khrab hai", "service
# nahi hogi", "daag nahi gaya". Ye sirf 'note' nahi hai: kaam ruk gaya hai,
# aur customer ko shayad "ready" ka message ja bhi chuka hai. Isliye ye
# deterministic hai — LLM ke mood par nahi chhoda ja sakta (ek hi baat ek
# baar 'note' bani thi aur ek baar bilkul chup rah gayi thi).
_TROUBLE_RE = re.compile(
    r"kh?arab|khrab|kharaab|phat\s*ga|fat\s*ga|toot|tut\s*ga|"
    r"daag|dhabba|rang\s*(chala|nikal|utar)|sikud|jal\s*ga|"
    r"service\s*(nahi|nhi|na)|nahi\s*ho\s*(payega|paye|sakta|sakti)|"
    r"nhi\s*ho(g|ga|gi|payega)?\b|possible\s*nahi|mana\s*kar|"
    r"kam\s*hai|kapda\s*(kam|missing)|missing|gum\s*ga",
    re.I,
)
_ORDER_REF_RE = re.compile(r"\b(?:KK-)?(\d{8}-\d{2,})\b", re.I)


async def _order_from_text(db: AsyncSession, text: str) -> Order | None:
    """Message mein likha order — "KK-20260809-01" ya sirf "20260809-01"."""
    m = _ORDER_REF_RE.search(text or "")
    if not m:
        return None
    try:
        return await get_order(db, f"KK-{m.group(1)}")
    except OrderNotFoundError:
        return None


async def _shop_trouble_word(db: AsyncSession, text: str) -> bool:
    """Shop ke apne shabd (Settings se) — code wale default ke UPAR.

    Har dukaan ki bol-chaal alag hai; ek client "kharab" kehta hai, doosra
    "reject", teesra apni bhasha mein. Ye list wahi khud bharta hai.
    """
    try:
        words = await app_settings.get(db, "agent_trouble_words")
    except Exception:
        return False
    low = (text or "").casefold()
    return any(w.strip().casefold() in low for w in (words or []) if str(w).strip())


async def _handle_order_problem(
    db: AsyncSession, sender_phone: str, sender_label: str, text: str
) -> str | None:
    """Staff ne order par dikkat batayi -> owner ko turant, customer ko kuch nahi.

    Customer ko khud se kuch nahi bhejte: kapda kharab hona paise aur
    bharose ka mamla hai, wo faisla owner ka hai. Owner ko poori tasveer
    jaati hai — kaunsa order, kya hua, abhi status kya hai, aur customer
    ko pehle kya bataya ja chuka hai.
    """
    if sender_label == "manager" or not text or text.startswith("["):
        return None
    if not _TROUBLE_RE.search(text) and not await _shop_trouble_word(db, text):
        return None
    order = await _order_from_text(db, text)
    if order is None:
        return None

    said = text.strip()[:300]
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    line = f"[{stamp} {sender_label}] ⚠️ {said}"
    order.notes = f"{order.notes}\n{line}" if order.notes else line
    db.add(order)
    await db.commit()

    cust = await db.get(Customer, order.customer_id)
    who = (cust.name or cust.phone) if cust else "?"
    alert = [
        f"🚨 {sender_label} ne {order.order_number} par dikkat batayi:",
        f'"{said}"',
        "",
        f"Customer: {who}",
        f"Abhi status: {status_label(order.status)}",
    ]
    # Customer ko pehle hi "ready/nikal gaya/de diya" bola ja chuka hai to
    # ye sabse zaroori baat hai — warna wo aaj hi kapde maangega.
    if order.status in (
        OrderStatus.READY, OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED
    ):
        alert.append(
            f"⚠️ Customer ko '{status_label(order.status)}' ka message ja chuka hai."
        )
    alert.append("")
    alert.append("Customer ko maine kuch nahi bheja — aap batayein kya karna hai.")
    await _alert_owner_with_hold(db, order, "\n".join(alert))

    log.info("order_problem_reported", order=order.order_number, by=sender_label)
    await audit.record(
        actor_role="staff", actor=sender_label, action="order_problem",
        args={"order": order.order_number}, result=said[:150],
    )
    return (
        f"Samajh gaya 🙏 {order.order_number} ki dikkat maine {settings.SHOP_NAME} "
        f"ko bata di hai. Customer ko abhi kuch nahi bheja gaya."
    )


async def _alert_owner_with_hold(db: AsyncSession, order: Order, text: str) -> None:
    """Owner ko alert + ek tap ka faisla: order rok du ya chalne du?

    Status khud se nahi badalte — galat samajh par order chupchaap ruk
    jata. Faisla owner ka, bas ek button ki doori par.
    """
    from app.services import team
    from app.services.whatsapp import Button

    owner = await team.primary_admin_phone(db)
    buttons = [
        Button(f"hold:{order.order_number}:yes", "🛑 Order rok do"),
        Button(f"hold:{order.order_number}:no", "▶️ Chalne do"),
    ]
    try:
        await send_message(db, to_phone=owner, text=text, buttons=buttons)
        return
    except (WindowClosedError, SendError):
        log.info("owner_hold_buttons_failed", order=order.order_number)
    await team.notify_admins(db, text)


_HOLD_BTN_RE = re.compile(r"^\s*\[button:hold:(KK-\S+?):(yes|no)\]", re.I)


async def _handle_hold_button(
    db: AsyncSession, sender_phone: str, sender_label: str, text: str
) -> str | None:
    """Owner ka faisla: order hold par daalo ya chalne do."""
    m = _HOLD_BTN_RE.match(text or "")
    if not m:
        return None
    number, answer = m.group(1).upper(), m.group(2).lower()
    if sender_label != "manager":
        return None
    try:
        order = await get_order(db, number)
    except OrderNotFoundError:
        return get_message("order_not_found_staff", order_number=number)
    if answer == "no":
        return f"Theek hai 👍 {number} waise hi chalta rahega."
    try:
        await update_status(db, order, OrderStatus.ON_HOLD, changed_by=sender_label)
    except InvalidTransitionError:
        return f"{number} abhi {status_label(order.status)} hai — ise hold nahi kar sakte."
    # Ruke hue order ki delivery ka peechha karna band — warna Ajit ko us
    # kaam ke reminder jaate rehte jo ho hi nahi sakta.
    from app.services import tasks as task_service

    stopped = await task_service.cancel_open_tasks_for_order(db, order.id)
    tail = f" {stopped} pending kaam bhi roke." if stopped else ""
    return f"🛑 {number} hold par daal diya.{tail} Customer ko kuch nahi bheja gaya."


async def _handle_task_followup(
    db: AsyncSession, sender_phone: str, sender_label: str, text: str
) -> str | None:
    """Button ke baad ka free-text jawab (ETA ya dikkat ki wajah)."""
    pending = _PENDING.get(sender_phone)
    if not isinstance(
        pending,
        (PendingTaskEta, PendingTaskIssue, PendingOrderEta, PendingOrderIssue, PendingRelay),
    ) or not text:
        return None
    if text.startswith("["):          # media/button — ye jawab nahi hai
        return None
    _PENDING.pop(sender_phone, None)

    # "Ajit ko kya bhejun?" ka jawab — ab bhejne wale ke apne shabd hain
    if isinstance(pending, PendingRelay):
        return await _apply_relay(
            db, sender_label,
            {**_EMPTY_EXTRACT, "relay_to": pending.target, "relay_message": text.strip()},
            sender_text=f"{pending.target} {text}", sender_phone=sender_phone,
        )

    # Order wale jawab: ETA/dikkat order ke notes par chadhti hai aur owner
    # ko turant jaati hai — wahi behaviour jo task par hai.
    if isinstance(pending, (PendingOrderEta, PendingOrderIssue)):
        from app.services import team

        said = text.strip()[:300]
        try:
            order = await get_order(db, pending.number)
        except OrderNotFoundError:
            return None
        if isinstance(pending, PendingOrderEta):
            stamp = datetime.now(timezone.utc).strftime("%d %b %H:%M")
            order.notes = (
                (order.notes or "") + f"\n[{stamp}] {sender_label} — ETA: {said[:120]}"
            ).strip()
            db.add(order)
            await db.commit()
            await team.notify_admins(
                db, f"⏳ {sender_label}: {pending.number} — {said[:120]}"
            )
            return f"👍 Note kar liya: {said[:120]}"
        await team.notify_admins(
            db, f"❗ {sender_label} ki dikkat {pending.number}: {said}"
        )
        return "Samajh gaya 🙏 Maine owner ko bata diya hai."
    from app.services import tasks as task_service
    from app.services import team

    task = await task_service.get_by_code(db, pending.code)
    if task is None:
        return None
    if isinstance(pending, PendingTaskEta):
        task.eta_text = text.strip()[:120]
        db.add(task)
        await db.commit()
        await team.notify_admins(
            db, f"⏳ {sender_label}: [{task.code}] {task.title} — {task.eta_text}"
        )
        return f"👍 Note kar liya: {task.eta_text}"
    await team.notify_admins(
        db, f"❗ {sender_label} ki dikkat [{task.code}]: {text.strip()[:300]}"
    )
    return "Samajh gaya 🙏 Maine owner ko bata diya hai."


# "aaj ka kaam kya hai", "order details do", "kitne order pending hai" —
# staff apna kaam poochh raha hai. Pehle sirf MANAGER ka sawal answer hota
# tha aur staff ko CHUPPI milti thi: Ravi ne 5 baar "order details do"
# likha, ek baar bhi jawab nahi gaya. Ab ye deterministic hai — zero LLM,
# seedha DB se.
_WORK_ASK_RE = re.compile(
    r"(?:^|\s)(?:"
    r"order[\s-]*(?:ki\s*)?(?:detail|details|list|status)"
    r"|(?:detail|details|list)\s*(?:do|dijiye|bhejo|bhej|batao|bata)"
    r"|(?:aaj|aj|आज)\s*(?:ka|k)?\s*(?:kaam|kam|order|orders|work)"
    r"|kya\s*(?:kaam|kam|karna)\b"
    r"|(?:mera|apna)\s*(?:kaam|kam|order|orders)"
    r"|kitne?\s*order"
    r"|pending\s*(?:order|orders|kaam|kam)"
    r"|kaam\s*(?:kya|batao|bata|do|dijiye)"
    r")",
    re.I,
)


async def _staff_worklist(
    db: AsyncSession, sender_phone: str, sender_label: str, text: str
) -> str | None:
    """Staff ke apne pending order + khule task — unhi ke shabdon ke jawab mein.

    Wahi list jo roz subah standup mein jaati hai, bas jab wo khud maange.
    """
    if sender_label == "manager" or not text or text.startswith("["):
        return None
    if _PENDING.get(sender_phone) is not None:   # bill/payment draft beech mein
        return None
    if not _WORK_ASK_RE.search(text):
        return None
    staff = (
        await db.execute(select(Staff).where(Staff.phone == sender_phone))
    ).scalar_one_or_none()
    if staff is None or not staff.is_active:
        return None

    from app.services import tasks as task_service
    from app.services.scheduler import _pending_orders_for
    from app.services.work_orders import items_summary

    default_phone = await app_settings.get(db, "default_washer_phone")
    orders = await _pending_orders_for(db, staff, default_phone)
    open_tasks = await task_service.open_tasks_for_staff(db, staff.id)
    # Chhoti dukaan mein zyadatar order kisi ke naam par likhe hi nahi
    # jaate. Sirf "aapke naam par kuch nahi" keh dena galat lagta hai jab
    # shop mein kaam pada ho — bina assign wale order alag se dikhte hain.
    unclaimed = await _unclaimed_orders(db, staff, [o.id for o in orders])

    if not orders and not open_tasks and not unclaimed:
        return f"{staff.name} ji, abhi koi pending order ya kaam nahi hai 👍"

    async def _order_line(i: int, o: Order) -> str:
        cust = await db.get(Customer, o.customer_id)
        flags = []
        if o.priority == "urgent":
            flags.append("🔴 URGENT")
        if o.expected_delivery and o.expected_delivery <= today_ist():
            flags.append("aaj delivery")
        return (
            f"{i}. {o.order_number} — {cust.name or cust.phone if cust else '?'} — "
            f"{items_summary(o)} — {status_label(o.status)}"
            + (f" [{', '.join(flags)}]" if flags else "")
        )

    lines: list[str] = []
    if orders:
        lines.append(f"📋 {staff.name} ji, aapke {len(orders)} order:")
        for i, o in enumerate(orders[:10], 1):
            lines.append(await _order_line(i, o))
        if len(orders) > 10:
            lines.append(f"...aur {len(orders) - 10} aur")
    if unclaimed:
        if lines:
            lines.append("")
        lines.append(f"🧺 Bina assign ke {len(unclaimed)} order:")
        for i, o in enumerate(unclaimed[:10], 1):
            lines.append(await _order_line(i, o))
        if len(unclaimed) > 10:
            lines.append(f"...aur {len(unclaimed) - 10} aur")
    if open_tasks:
        if lines:
            lines.append("")
        lines.append(f"📝 Khule kaam ({len(open_tasks)}):")
        for t in open_tasks[:10]:
            lines.append(f"• [{t.code}] {t.title}" + (" 🔴" if t.urgent else ""))
    lines.append("")
    lines.append("Ho jaye to likh dijiye: done KK-... ya done T-..")
    blob = "\n".join(lines)
    log.info("staff_worklist_answered", staff=staff.name, orders=len(orders),
             unclaimed=len(unclaimed), tasks=len(open_tasks))

    # Tap-to-pick: ek list bhejte hain jisme har order/task apni line par
    # hai. Isse "kis par jawab diya" ka sawal hi khatam — chuni hui line ka
    # id wapas aata hai. List na ja paye (window band / provider) to wahi
    # baat text mein chali jati hai.
    from app.services.work_orders import list_button_label, work_rows

    rows = await work_rows(db, list(orders) + list(unclaimed), open_tasks)
    if rows:
        try:
            await send_message(
                db, to_phone=sender_phone,
                text=f"{staff.name} ji, ye raha aaj ka kaam 👇\nJispar update dena ho use chuniye.",
                list_rows=rows, list_button=await list_button_label(db),
                list_title="Aaj ka kaam",
            )
            return ""
        except (WindowClosedError, SendError) as exc:
            log.warning("worklist_list_failed", error=str(exc)[:120])
    return blob




async def _unclaimed_orders(
    db: AsyncSession, staff: Staff, mine: list
) -> list[Order]:
    """Active order jo abhi kisi ke naam par nahi hain — role ke hisaab se.

    Washer ko wo jinka washer khali hai; delivery wale ko wo jo Ready/
    Out-for-delivery hain aur jinka delivery khali hai.
    """
    rows = (
        await db.execute(
            select(Order)
            .where(Order.status.in_(ACTIVE_STATUSES))
            .order_by(Order.priority.desc(), Order.created_at)
        )
    ).scalars().all()
    seen = set(mine)
    out = []
    for o in rows:
        if o.id in seen:
            continue
        if staff.role.name == "DELIVERY":
            ready = o.status in (OrderStatus.READY, OrderStatus.OUT_FOR_DELIVERY)
            if ready and o.assigned_delivery_id is None:
                out.append(o)
        elif o.assigned_washer_id is None:
            out.append(o)
    return out


async def _staff_reference_lookup(
    db: AsyncSession, sender_phone: str, sender_label: str, text: str
) -> str | None:
    """Answer a staff's Task/Bill reference question from live DB facts.

    This is deliberately deterministic. A staff member asking "T-123 kahan
    ka pickup hai?" must never depend on an LLM remembering which task the
    code belongs to. A short-lived reference context also makes follow-ups
    such as "aur kitne pcs hain?" unambiguous.
    """
    if sender_label == "manager" or not text or text.startswith("["):
        return None
    if _PENDING.get(sender_phone) is not None:
        return None

    staff = (
        await db.execute(select(Staff).where(Staff.phone == sender_phone))
    ).scalar_one_or_none()
    if staff is None or not staff.is_active:
        return None

    raw = text.strip()
    task_match = _TASK_REF_RE.search(raw)
    order_match = _ORDER_REF_RE_STAFF.search(raw)

    ref_kind = ref_value = None
    if task_match:
        ref_kind, ref_value = "task", f"T-{task_match.group(1)}"
    elif order_match:
        ref_kind, ref_value = "order", order_match.group(0).upper()
    else:
        saved = _STAFF_REF_CONTEXT.get(sender_phone)
        if saved and datetime.now(timezone.utc) - saved[2] <= _STAFF_REF_TTL:
            ref_kind, ref_value = saved[0], saved[1]
        else:
            _STAFF_REF_CONTEXT.pop(sender_phone, None)
            return None

    low = raw.casefold()

    if ref_kind == "task":
        from app.services import tasks as task_service

        task = await task_service.get_by_code(db, ref_value)
        if task is None:
            return f"❌ {ref_value} ka koi task nahi mila. Task No. check karke bhej dijiye."
        if task.assigned_staff_id != staff.id:
            return f"ℹ️ {ref_value} aapke naam par assigned nahi hai, isliye main uski details nahi dikha sakta."

        _STAFF_REF_CONTEXT[sender_phone] = ("task", task.code, datetime.now(timezone.utc))
        order = await db.get(Order, task.order_id) if task.order_id else None

        if order is None:
            if re.search(r"kahan|pata|address|location|pickup", low):
                return f"📍 *{task.code}*\n{task.title}\n\nIs task ke saath koi bill/order linked nahi hai."
            return (
                f"📝 *{task.code}*\n"
                f"*Kaam:* {task.title}\n"
                f"*Status:* {task.status}"
                + (f"\n*Reply:* {task.reply}" if task.reply else "")
            )

        cust = await db.get(Customer, order.customer_id)
        from app.services.work_orders import items_summary

        customer_name = (cust.name or cust.phone) if cust else "Customer"
        address = cust.address if cust else None
        items = items_summary(order)
        due = max(
            (order.total_amount or Decimal("0")) - (order.amount_paid or Decimal("0")),
            Decimal("0"),
        )

        if re.search(r"kahan|pata|address|location|pickup|delivery", low):
            place = address or "Address database mein save nahi hai."
            return (
                f"📍 *{task.code} — Location*\n"
                f"*Customer:* {customer_name}\n"
                f"*Address:* {place}"
            )
        if re.search(r"kitne|pcs|piece|kapde|items|quantity", low):
            return f"👕 *{task.code} — Items:* {items}"
        if re.search(r"bill|order|amount|paisa|rupay|₹", low):
            return (
                f"🧾 *{task.code}*\n"
                f"*Bill:* {order.order_number}\n"
                f"*Amount:* ₹{order.total_amount or 0:.0f}\n"
                f"*Paid:* ₹{order.amount_paid or 0:.0f}\n"
                f"*Due:* ₹{due:.0f}\n"
                f"*Status:* {status_label(order.status)}"
            )
        return (
            f"📋 *{task.code} DETAILS*\n"
            f"*Customer:* {customer_name}\n"
            f"*Address:* {address or 'save nahi hai'}\n"
            f"*Bill:* {order.order_number}\n"
            f"*Items:* {items}\n"
            f"*Amount:* ₹{order.total_amount or 0:.0f} | *Due:* ₹{due:.0f}\n"
            f"*Order Status:* {status_label(order.status)}\n"
            f"*Task:* {task.status}"
        )

    # Direct Bill/Order reference.
    try:
        order = await get_order(db, ref_value)
    except OrderNotFoundError:
        return f"❌ {ref_value} ka koi bill/order nahi mila. Number check karke bhej dijiye."

    _STAFF_REF_CONTEXT[sender_phone] = ("order", order.order_number, datetime.now(timezone.utc))
    cust = await db.get(Customer, order.customer_id)
    from app.services.work_orders import items_summary

    customer_name = (cust.name or cust.phone) if cust else "Customer"
    address = cust.address if cust else None
    items = items_summary(order)
    due = max(
        (order.total_amount or Decimal("0")) - (order.amount_paid or Decimal("0")),
        Decimal("0"),
    )

    if re.search(r"kahan|pata|address|location|pickup|delivery", low):
        return (
            f"📍 *{order.order_number} — Location*\n"
            f"*Customer:* {customer_name}\n"
            f"*Address:* {address or 'Address database mein save nahi hai.'}"
        )
    if re.search(r"kitne|pcs|piece|kapde|items|quantity", low):
        return f"👕 *{order.order_number} — Items:* {items}"
    return (
        f"🧾 *{order.order_number} DETAILS*\n"
        f"*Customer:* {customer_name}\n"
        f"*Address:* {address or 'save nahi hai'}\n"
        f"*Items:* {items}\n"
        f"*Amount:* ₹{order.total_amount or 0:.0f} | *Paid:* ₹{order.amount_paid or 0:.0f} | *Due:* ₹{due:.0f}\n"
        f"*Status:* {status_label(order.status)}"
    )


async def handle_staff_message(
    db: AsyncSession, *, sender_phone: str, sender_label: str, text: str
) -> str | None:
    """Reply for a staff/manager inbound, or None to stay silent."""
    # Band kiya gaya aadmi: yahan tak pahunchna hi nahi chahiye (webhook
    # pehle hi rok deta hai), par ye doosra taala hai — bill, payment,
    # status, sab isi darwaze se guzarta hai.
    if sender_label != "manager":
        who = (
            await db.execute(select(Staff).where(Staff.phone == sender_phone))
        ).scalar_one_or_none()
        if who is not None and not who.is_active:
            log.info("inactive_staff_command_ignored", staff=who.name)
            return None

    pending = _PENDING.get(sender_phone)
    if pending and pending.expired:
        _PENDING.pop(sender_phone, None)
        pending = None

    # Fixed-choice confirmations are deterministic and handled before the LLM.
    fixed = _FIXED_BTN_RE.match(text or "")
    if fixed:
        kind, action = fixed.group(1).lower(), fixed.group(2).lower()
        if kind == "bill_confirm":
            pending_bill = _PENDING.get(sender_phone)
            if not isinstance(pending_bill, PendingBill):
                return "⚠️ Ye bill draft ab active nahi hai."
            if action == "yes":
                return await _finalize_bill(db, sender_phone, sender_label, pending_bill)
            if action == "cancel":
                _PENDING.pop(sender_phone, None)
                return get_message("bill_cancelled")
            if action == "edit":
                return (
                    "✏️ Theek hai — bill mein kya badalna hai likh dijiye. "
                    "Draft save hai; main update karke phir confirmation menu dikhaunga."
                )
        elif kind == "payment_confirm":
            pending_payment = _PENDING.get(sender_phone)
            if not isinstance(pending_payment, PendingPayment):
                return "⚠️ Ye payment confirmation ab active nahi hai."
            if action == "yes":
                return await _finalize_payment(db, sender_phone, sender_label, pending_payment)
            if action == "cancel":
                _PENDING.pop(sender_phone, None)
                return get_message("bill_cancelled")
        elif kind == "relay_confirm":
            from app.services import tasks as task_service
            code_m = re.search(r"T-\d+", action.upper())
            choice = "yes" if action.endswith(":yes") else "no"
            if code_m:
                task = await task_service.get_by_code(db, code_m.group(0))
                if task is not None:
                    if choice == "yes":
                        await task_service.note_reply(
                            db, task.assigned_staff_id, "Yes — fixed-choice reply",
                            by=sender_label,
                        )
                        await task_service.complete_task(
                            db, task, reply="Yes — fixed-choice reply", by=sender_label,
                            advance_order=False,
                        )
                        return f"✅ {task.code} ka response *Yes* record kar diya."
                    await task_service.note_reply(
                        db, task.assigned_staff_id, "No — fixed-choice reply",
                        by=sender_label,
                    )
                    return f"⏳ {task.code} ka response *No* record kar diya."
        return None

    # Task ke buttons + unka follow-up — dono deterministic, zero LLM.
    btn_reply = await _handle_task_button(db, sender_phone, sender_label, text or "")
    if btn_reply is not None:
        return btn_reply
    ord_reply = await _handle_order_button(db, sender_phone, sender_label, text or "")
    if ord_reply is not None:
        return ord_reply
    picked = await _handle_work_pick(db, sender_phone, sender_label, text or "")
    if picked is not None:
        return picked
    hold = await _handle_hold_button(db, sender_phone, sender_label, text or "")
    if hold is not None:
        return hold
    followup = await _handle_task_followup(db, sender_phone, sender_label, text or "")
    if followup is not None:
        return followup

    # "done KK-20260803-01" — staff quick-confirm, zero LLM (owner's spec)
    if text:
        m_done = re.match(r"^\s*done\s+(KK-\S+)\s*$", text, re.I)
        if m_done and sender_label != "manager":
            return await _apply_done(db, sender_phone, sender_label, m_done.group(1).upper())
        # "done T-14" — closes an assigned task, also zero LLM
        m_task = re.match(r"^\s*done\s+(T-?\d+)\s*$", text, re.I)
        if m_task:
            return await _close_task_by_code(db, sender_phone, sender_label, m_task.group(1))

    # Pickup conversation: their tap on Yes/No, and their answer to
    # "kab tak?". Both are deterministic — no LLM, no ambiguity.
    pickup_reply = await _handle_pickup_exchange(db, sender_phone, sender_label, text or "")
    if pickup_reply is not None:
        return pickup_reply

    # Order par dikkat — LLM se pehle, kyunki ye khone wali baat nahi hai.
    problem = await _handle_order_problem(db, sender_phone, sender_label, text or "")
    if problem is not None:
        return problem

    # Explicit Task/Bill references get deterministic DB lookup before the
    # generic task-context classifier. This makes "T-123 kahan hai?" and
    # "bill KK-... ka detail" answerable without asking staff to repeat context.
    ref_reply = await _staff_reference_lookup(
        db, sender_phone, sender_label, text or ""
    )
    if ref_reply is not None:
        return ref_reply

    # "order details do" — staff apna kaam poochh raha hai. Ye pickup ke
    # baad aata hai taaki chal rahi baat-cheet beech mein na kate.
    worklist = await _staff_worklist(db, sender_phone, sender_label, text or "")
    if worklist is not None:
        return worklist

    # Natural-language reply to the latest assigned task — task context first,
    # so staff does not need to repeat the task code.
    task_reply = await _handle_open_task_reply(db, sender_phone, sender_label, text or "")
    if task_reply is not None:
        return task_reply

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
            # Backward compatible with an older client or typed "haan".
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
                db, photo.group(1), photo.group(2).strip()
            )
            if extracted is None:  # file missing on disk — nothing to read
                return None
            if not extracted["items"]:
                # Could not read the slip. Say so and ask — NEVER fill the
                # bill with the usual shirt/pant just to have something.
                log.info("bill_photo_unreadable", note=extracted["note"][:120])
                reply = get_message("bill_photo_unreadable")
                if extracted["note"]:
                    reply += f"\n({extracted['note'][:140]})"
                return reply
        else:
            extracted = await _extract(db, text, _as_bill(pending), history)
    except LLMRateLimited as exc:
        log.warning("staff_extract_rate_limited", error=str(exc)[:150])
        return get_message("ai_down_staff_rate_limit") if sender_label == "manager" else None
    except LLMAuthError as exc:
        log.warning("staff_extract_quota_or_auth_failed", error=str(exc)[:150])
        return get_message("ai_down_staff_quota") if sender_label == "manager" else None
    except LLMError as exc:
        log.warning("staff_extract_parse_failed", error=str(exc)[:150])
        return get_message("ai_down_staff_parse") if sender_label == "manager" else None

    action = extracted["action"]
    reply: str | None = None
    if action == "new_bill" and extracted["items"]:
        draft = await _price_draft(db, extracted)
        pending_bill = PendingBill(draft=draft)
        _PENDING[sender_phone] = pending_bill
        reply = _draft_summary(draft)
        sent = await _send_fixed_choice_menu(
            db, sender_phone, reply,
            [
                ListRow("bill_confirm:yes", "✅ Haan, Bill Banao", "Create this bill"),
                ListRow("bill_confirm:edit", "✏️ Bill Badalna Hai", "Edit the draft"),
                ListRow("bill_confirm:cancel", "❌ Cancel", "Cancel this draft"),
            ],
            button="Bill action",
            title="Bill confirmation",
        )
        if sent:
            reply = None
    elif action == "delay_update":
        reply = await _apply_delay(db, sender_label, extracted)
    elif action == "status_update":
        reply = await _apply_status(db, sender_label, extracted, sender_phone)
    elif action == "relay":
        reply = await _apply_relay(
            db, sender_label, extracted, sender_text=text or "", sender_phone=sender_phone
        )
    elif action == "set_priority":
        reply = await _apply_priority(db, sender_label, extracted)
    elif action == "assign_staff":
        reply = await _apply_assign(db, sender_label, extracted)
    elif action == "add_note":
        reply = await _apply_note(db, sender_label, extracted, sender_phone)
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
    today = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")
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


# Reading a slip is TRANSCRIPTION, not billing. The rate card is
# deliberately NOT shown here: when it was, a hard-to-read slip made the
# model emit plausible rate-card rows (shirt, pant) instead of what was
# written. It reads the paper; our code does the rate-card matching after.
_PHOTO_SYSTEM = (
    "You are reading a PHOTO of a bill slip from Kwik Klin laundry "
    "(Varanasi, India). Slips are handwritten in Hindi/Hinglish/English, "
    "usually on a preprinted form.\n"
    "Your ONLY job is to TRANSCRIBE what is actually written on THIS slip. "
    "You are not making a bill and you have not been shown the shop's rate "
    "card, so you can never supply an item from general knowledge of what "
    "laundries wash.\n"
    "Rules:\n"
    "- One entry in `lines` per row that carries a HANDWRITTEN quantity or "
    "tick. A preprinted garment name with nothing handwritten next to it is "
    "part of the blank form — SKIP it. It is not an item.\n"
    "- `text`: that row exactly as written, in the slip's own words (Latin "
    "transliteration is fine). `garment`: just the garment word of that row. "
    "`service`: only if the slip states it (wash / dry clean / iron / "
    "press...), else \"\". `qty`: the handwritten number (1 if a row is "
    "only ticked).\n"
    "- Hard to read? Still write your best LITERAL reading and set "
    "unsure=true. Never replace an unreadable word with a common laundry "
    "item like shirt or pant.\n"
    "- Blurred, dark, not a bill slip, or no handwritten item row visible: "
    "set readable=false, lines=[] and say what the problem is in "
    "unreadable_note (Hinglish, one line). Returning nothing is correct and "
    "safe — inventing items is a serious error.\n"
    "- Also read customer_name, customer_phone (digits as written), the "
    "advance/jama amount (0 if none) and any delivery date as ISO "
    "YYYY-MM-DD using TODAY. Use \"\" when it is not written.\n"
    "- IGNORE every price, rate column and total on the slip — the system "
    "prices the bill from its own rate card.\n"
    "The staff member's caption wins wherever it conflicts with the slip."
)

_PHOTO_SCHEMA = {
    "type": "object",
    "properties": {
        "readable": {"type": "boolean"},
        "customer_name": {"type": "string"},
        "customer_phone": {"type": "string"},
        "advance": {"type": "number"},
        "expected_delivery": {"type": "string"},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "garment": {"type": "string"},
                    "service": {"type": "string"},
                    "qty": {"type": "number"},
                    "unsure": {"type": "boolean"},
                },
                "required": ["text", "garment", "service", "qty", "unsure"],
                "additionalProperties": False,
            },
        },
        "unreadable_note": {"type": "string"},
    },
    "required": [
        "readable", "customer_name", "customer_phone", "advance",
        "expected_delivery", "lines", "unreadable_note",
    ],
    "additionalProperties": False,
}


async def _extract_from_photo(
    db: AsyncSession, filename: str, caption: str
) -> dict | None:
    """Read a bill slip photo. None if the file is gone.

    Returns the same shape the text extractor produces (so the draft path
    is shared), plus what the slip literally said per item.
    """
    path = _MEDIA_DIR / Path(filename).name  # traversal-safe
    if not path.exists():
        log.warning("bill_photo_missing", filename=filename)
        return None
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    read = await llm_client.ask_json_image(
        system=_PHOTO_SYSTEM,
        user_text=f"TODAY: {today}\nSTAFF CAPTION:\n{caption or '(none)'}",
        image_bytes=path.read_bytes(),
        mime_type=mime,
        schema=_PHOTO_SCHEMA,
        model=llm_client.MODEL_SMART,
    )

    items = []
    for ln in read.get("lines") or []:
        qty = ln.get("qty") or 1
        garment = (ln.get("garment") or "").strip()
        text = (ln.get("text") or "").strip()
        if not garment and not text:
            continue
        items.append(
            {
                "service": (ln.get("service") or "").strip(),
                "garment": garment or text,
                "qty": qty,
                # what the paper actually said — shown back before 'haan'
                "read_as": text or garment,
                "unsure": bool(ln.get("unsure")),
            }
        )
    log.info(
        "bill_photo_read",
        readable=bool(read.get("readable")),
        lines=len(items),
        unsure=sum(1 for i in items if i["unsure"]),
    )
    return {
        "action": "new_bill",
        "customer_name": (read.get("customer_name") or "").strip(),
        "customer_phone": (read.get("customer_phone") or "").strip(),
        "items": items,
        "advance": read.get("advance") or 0,
        "expected_delivery": (read.get("expected_delivery") or "").strip(),
        "from_photo": True,
        "note": (read.get("unreadable_note") or "").strip(),
    }


# Spelling variants of the SAME word only — never a mapping from one
# garment to a different one (that would be guessing on the owner's behalf).
_SPELLING = {
    "pent": "pant", "paint": "pant", "pantt": "pant",
    "sari": "saree", "sadi": "saree",
    "jean": "jeans", "kurtha": "kurta", "shart": "shirt",
    "dryclean": "dryclean", "drycleaning": "dryclean",
}


def _key(word: str) -> str:
    """Match key: case/space/hyphen/plural-insensitive, applied to BOTH sides."""
    k = re.sub(r"[^a-z0-9]+", "", (word or "").lower())
    if len(k) > 3 and k.endswith("s"):
        k = k[:-1]
    return _SPELLING.get(k, k)


async def _price_draft(db: AsyncSession, extracted: dict) -> dict:
    """Price every item from the rate card — the model never sets prices.

    A garment we cannot match stays in the owner's own words with no price,
    and a garment that exists under several services asks which one. We do
    not silently pick a rate-card row that "looks close".
    """
    rates = (
        (await db.execute(select(Rate).where(Rate.is_active))).scalars().all()
    )
    by_key = {(_key(r.service), _key(r.garment)): r for r in rates}
    items = []
    total = Decimal("0")
    for it in extracted["items"]:
        qty = Decimal(str(it.get("qty") or 1))
        g_key, s_key = _key(it.get("garment")), _key(it.get("service"))
        rate_row = by_key.get((s_key, g_key)) if g_key else None
        options = [r for r in rates if g_key and _key(r.garment) == g_key]
        if rate_row is None and len(options) == 1:
            # only one service exists for this garment — no choice to make
            rate_row = options[0]
        rate = rate_row.rate if rate_row else None
        amount = (rate * qty) if rate is not None else None
        if amount is not None:
            total += amount
        service = rate_row.service if rate_row else (it.get("service") or "")
        garment = rate_row.garment if rate_row else (it.get("garment") or "")
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
                # review-only fields, stripped before the order is written
                "read_as": (it.get("read_as") or "").strip(),
                "unsure": bool(it.get("unsure")),
                "options": sorted({r.service for r in options}) if rate_row is None else [],
            }
        )
    return {
        "customer_name": extracted["customer_name"],
        "customer_phone": extracted["customer_phone"],
        "items": items,
        "advance": extracted["advance"] or 0,
        "expected_delivery": extracted["expected_delivery"],
        "total": float(total),
        "from_photo": bool(extracted.get("from_photo")),
    }


# Only these reach create_order — read_as/unsure/options are review aids.
_ORDER_ITEM_KEYS = ("type", "service", "garment", "qty", "rate", "amount")


def _draft_summary(draft: dict) -> str:
    lines = [get_message("bill_draft_header", customer_name=draft["customer_name"] or "?")]
    if draft.get("from_photo"):
        lines.append(get_message("bill_draft_from_photo"))
    needs_answer = False
    for it in draft["items"]:
        qty = int(it["qty"]) if float(it["qty"]).is_integer() else it["qty"]
        label = f"{it['service']}{' / ' + it['garment'] if it['garment'] else ''}".strip(" /")
        read_as = (it.get("read_as") or "").strip()
        # show the slip's own words whenever we mapped them to something else
        if read_as and _key(read_as) != _key(it["garment"] or "") and _key(read_as) not in _key(label):
            label += f' (parche pe: "{read_as[:40]}")'
        if it.get("unsure"):
            label += " ❓"
            needs_answer = True
        if it["amount"] is not None:
            lines.append(f"• {qty} × {label} — ₹{it['amount']:g}")
        elif it.get("options"):
            needs_answer = True
            lines.append(
                f"• {qty} × {label} — ❓ kaunsi service? ({' / '.join(it['options'])})"
            )
        else:
            needs_answer = True
            lines.append(f"• {qty} × {label} — ⚠️ rate card mein nahi")
    lines.append(get_message("bill_draft_total", total=f"{draft['total']:g}"))
    if needs_answer:
        lines.append(get_message("bill_draft_needs_answer"))
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
        # Automatic promise: use the same working-day calculator as the
        # dashboard/staff bill flow. Never trust the legacy turnaround_days value.
        from app.services.delivery_date import calculate as calculate_delivery_date
        cfg = await app_settings.get_many(
            db, "delivery_normal_days", "delivery_heavy_days", "delivery_holidays"
        )
        exp = calculate_delivery_date(
            today_ist(),
            d["items"],
            normal_days=int(cfg.get("delivery_normal_days") or 4),
            heavy_days=int(cfg.get("delivery_heavy_days") or 5),
            holidays=cfg.get("delivery_holidays") or [],
        )

    total = Decimal(str(d["total"])) if d["total"] else None
    order_items = [
        {k: v for k, v in it.items() if k in _ORDER_ITEM_KEYS} for it in d["items"]
    ]
    order = await create_order(
        db,
        customer_phone=phone,
        items=order_items,
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
            notify_customer=False,   # "order received" message mein advance pehle se hai
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
    number = (extracted.get("order_number") or "").strip().upper()
    customer_name = (extracted.get("customer_name") or "").strip()
    if not number and customer_name:
        rows = (
            await db.execute(
                select(Order)
                .join(Customer, Customer.id == Order.customer_id)
                .where(
                    Customer.name.ilike(customer_name),
                    Order.status.in_(ACTIVE_STATUSES),
                )
                .order_by(Order.created_at.desc())
            )
        ).scalars().all()
        if len(rows) == 1:
            number = rows[0].order_number
        elif len(rows) > 1:
            nums = ", ".join(o.order_number for o in rows[:5])
            return f"⚠️ {customer_name} ke {len(rows)} active orders hain ({nums}). Order number bata dijiye."
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


async def _apply_assign(db: AsyncSession, sender_label: str, extracted: dict) -> str | None:
    # Only the owner/manager can assign work to another staff member.
    if sender_label != "manager":
        log.info("staff_assign_ignored", sender=sender_label)
        return None
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


async def _apply_note(
    db: AsyncSession, sender_label: str, extracted: dict, sender_phone: str = ""
) -> str:
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

    if sender_label != "manager":
        # Staff ne order ke baare mein kuch kaha — wo owner tak jana hi
        # chahiye. Pehle ye sirf note mein dafan ho jata tha aur owner ko
        # pata hi nahi chalta tha ki kaam par kya chal raha hai. Aur use
        # wapas usi staff ko "work order" bana kar bhejne ka koi matlab
        # nahi tha — jawab bhi wahi confusing aata tha.
        from app.services import team

        await team.notify_admins(
            db,
            f"📝 {sender_label} — {order.order_number} ({status_label(order.status)}): {note}",
            skip_phone=sender_phone,
        )
        return get_message(
            "note_done", order_number=order.order_number,
            notified=f"{settings.SHOP_NAME} ko bata bhi diya",
        )

    # Owner ka instruction kaam karne wale tak jana chahiye
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
    prompt = get_message(
        "payment_confirm_prompt",
        order_number=order.order_number,
        amount=f"{amount:g}",
        method=method.upper(),
        due=f"{max(due, 0)}",
    )
    sent = await _send_fixed_choice_menu(
        db, sender_phone, prompt,
        [
            ListRow("payment_confirm:yes", "✅ Haan, Payment Record Karo", "Record this payment"),
            ListRow("payment_confirm:cancel", "❌ Cancel", "Do not record payment"),
        ],
        button="Payment action",
        title="Payment confirmation",
    )
    return None if sent else prompt


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
        from app.services import team
        await send_message(db, to_phone=await team.primary_admin_phone(db), text="\n".join(summary))
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
    "lines). When the owner asks whether anything came in (koi inquiry, koi "
    "message, kisi ne kuch pucha), answer from CUSTOMER MESSAGES and AAPKE "
    "JAWAB KA INTEZAAR — list who and what, with the time. Say 'nahi aayi' "
    "ONLY when those sections are actually empty; never assume nothing "
    "happened because you can't see it. If the owner asks about a staff "
    "NEVER say a job is done unless a TOOL result in this conversation says "
    "so. Kharcha likhna and customer save karna are YOUR OWN jobs — use "
    "add_expense / add_customer, never hand them to a staff member. If there "
    "is genuinely no tool for what the owner wants, say plainly that you "
    "cannot do it yet ('ye main abhi nahi kar sakta') and, if it is a shop "
    "SETTING (timings, rates, discounts), tell him the Settings page can do "
    "it — that is the ONLY time you may mention the dashboard. A false 'kar "
    "diya' is the worst thing you can do. If the owner asks about a staff "
    "member (kya bola, jawab diya "
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

# "add kar diya", "save kar liya", "note kar diya" — a claim that the shop's
# DATA changed. Owner-facing lies like this cost more than a wrong number:
# he stops checking. If no write tool ran, the claim is fiction.
_DONE_CLAIM = re.compile(
    r"(kharch|expense|customer|entry|register|database|record|note|save|add|"
    r"likh|update)[^.\n]{0,40}?"
    r"(kar\s*d[iy]|kr\s*d[iy]|kar\s*l[iy]|kr\s*l[iy]|likh\s*d[iy]|daal\s*d[iy]|"
    r"jod\s*d[iy]|bana\s*d[iy]|ho\s*gay|done|added|saved)",
    re.I,
)


def _unbacked_claim(answer: str, used: list[str]) -> bool:
    """Did it say the data changed without any tool having changed it?"""
    from app.services import agent_tools

    if set(used) & agent_tools.WRITE_TOOLS:
        return False
    return bool(_DONE_CLAIM.search(answer))


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
    challenged = False
    rounds, done = MAX_TOOL_ROUNDS, 0

    while done < rounds:
        done += 1
        step = await llm_client.ask_json(
            system=system, user_text=convo, schema=_STEP_SCHEMA,
            model=llm_client.MODEL_SMART, max_tokens=700,
        )
        tool = (step.get("tool") or "").strip()
        answer = (step.get("answer") or "").strip()
        if not tool:
            if answer and not challenged and _unbacked_claim(answer, used):
                # It just told the owner the books were updated while nothing
                # was written. Make it either do it or admit it can't.
                challenged = True
                rounds += 1   # the challenge gets its own round, not a tool's
                log.warning("manager_query_unbacked_claim", claim=answer[:160])
                convo += (
                    "\n\nSYSTEM: tumne kaha kaam ho gaya, lekin koi write tool "
                    "nahi chala — database mein KUCH NAHI badla. Ya to abhi "
                    "sahi tool chalao (add_expense / add_customer / assign_task), "
                    "ya owner ko saaf bolo ki ye tum nahi kar sakte. Jhooth mat bolo."
                )
                continue
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

    ist = timezone(timedelta(hours=5, minutes=30))
    # CUSTOMER INQUIRIES: without these the owner asking "koi inquiry aayi
    # hai?" got a confident "nahi" while a real one sat unanswered. Anyone
    # who messaged in the last 24h belongs here, order or no order.
    since = now - timedelta(hours=24)
    inq = (
        await db.execute(
            select(Conversation, Customer)
            .join(Customer, Customer.id == Conversation.customer_id)
            .where(
                Conversation.direction == Direction.INBOUND,
                Conversation.created_at >= since,
            )
            .order_by(Conversation.created_at.desc())
            .limit(15)
        )
    ).all()
    lines.append(
        f"CUSTOMER MESSAGES (pichhle 24 ghante, {len(inq)}):"
        if inq else "CUSTOMER MESSAGES (pichhle 24 ghante): ek bhi nahi"
    )
    for c, cust in inq:
        at = c.created_at.astimezone(ist).strftime("%d %b %H:%M")
        who = cust.name or cust.phone
        lines.append(f"- [{at}] {who} ({cust.phone}): {(c.message_text or '')[:140]}")

    # OPEN ESCALATIONS: things the agent could not answer and handed over.
    from app.models import Escalation, EscalationStatus

    esc_rows = (
        await db.execute(
            select(Escalation)
            .where(Escalation.status == EscalationStatus.OPEN)
            .order_by(Escalation.created_at.desc())
            .limit(10)
        )
    ).all()
    if esc_rows:
        lines.append(f"AAPKE JAWAB KA INTEZAAR ({len(esc_rows)}):")
        for (e,) in esc_rows:
            at = e.created_at.astimezone(ist).strftime("%d %b %H:%M")
            who = "?"
            if e.customer_id:
                cu = await db.get(Customer, e.customer_id)
                who = (cu.name or cu.phone) if cu else "?"
            lines.append(f"- [{at}] {who}: {(e.question or '')[:160]}")
    else:
        lines.append("AAPKE JAWAB KA INTEZAAR: kuch nahi")

    # STAFF CHAT: last exchange per staff member, so "Superman ne jawab
    # diya?" has a real answer instead of a dashboard deflection.
    # Workers only: the owner asking "kisne kya bola" means his staff, not
    # his own thread with the agent.
    from app.models import StaffRole

    staff_rows = (
        await db.execute(
            select(Staff).where(Staff.is_active, Staff.role != StaffRole.ADMIN)
        )
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


# Their tap comes back as "[button:✅ Haan, ho gaya]"; DotPe sends the id.
# Same buttons serve pickup AND delivery ("ho gaya" / "ho gayi").
_PICKUP_YES_RE = re.compile(r"pickup_yes|job_yes|haan,?\s*ho\s*ga(ya|yi)", re.I)
_PICKUP_NO_RE = re.compile(r"pickup_no|job_no|abhi\s*nahi", re.I)
# a plain "kal 11 baje" / "sham tak" is an ETA, not chatter
_ETA_HINT_RE = re.compile(
    r"\b(sham|shaam|subah|dopahar|raat|kal|parso|aaj|abhi|ghante|ghanta|min|baje|"
    r"tak|nikal|jaa?\s*raha|pahunch)\b|\d{1,2}\s*(baje|bje|am|pm)",
    re.I,
)


def _pickup_reference_date(text: str, today: date) -> date | None:
    """Resolve explicit relative days in a staff pickup ETA."""
    low = (text or "").casefold()
    if re.search(r"\b(aaj|today)\b", low):
        return today
    if re.search(r"\b(kal|tomorrow)\b", low):
        return today + timedelta(days=1)
    if re.search(r"\b(parso|day after tomorrow)\b", low):
        return today + timedelta(days=2)
    return None


async def _handle_pickup_exchange(
    db: AsyncSession, sender_phone: str, sender_label: str, text: str
) -> str | None:
    """Drive the pickup mini-conversation. None = not part of it."""
    from app.services import tasks as task_service

    staff = (
        await db.execute(select(Staff).where(Staff.phone == sender_phone))
    ).scalar_one_or_none()
    if staff is None:
        return None

    btn = re.match(r"^\s*\[button:([^\]]+)\]", text)
    payload = btn.group(1) if btn else text

    if btn and (_PICKUP_YES_RE.search(payload) or _PICKUP_NO_RE.search(payload)):
        m_code = re.search(r"(T-\d+)", payload)
        if m_code:
            task = await task_service.get_by_code(db, m_code.group(1))
        else:
            # button titles carry no code — use their newest open pickup
            task = await task_service.open_pickup_awaiting_confirm(db, staff.id)
        if task is None:
            return None
        return await task_service.confirm_pickup(
            db, task, done=bool(_PICKUP_YES_RE.search(payload)), by=staff.name or sender_label
        )

    # not a button: is this the answer to "kab tak pickup karoge?"
    if text and not text.startswith("["):
        pending = await task_service.open_pickup_awaiting_eta(db, staff.id)
        if pending is not None and _ETA_HINT_RE.search(text):
            order = await db.get(Order, pending.order_id) if pending.order_id else None
            if order is not None and order.pickup_date is not None:
                today_ist = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()
                mentioned = _pickup_reference_date(text, today_ist)
                if mentioned is not None and mentioned != order.pickup_date:
                    target = order.pickup_date.strftime("%d %b %Y")
                    return (
                        f"⚠️ Pickup date mismatch for {order.order_number}. "
                        f"The customer requested pickup on {target}. "
                        f"Please give a pickup time for {target} (for example: 12 PM / 1 PM / evening)."
                    )
            return await task_service.record_pickup_eta(db, pending, text)
    return None


async def _close_task_by_code(
    db: AsyncSession, sender_phone: str, sender_label: str, code: str
) -> str:
    from app.services import tasks as task_service, team

    task = await task_service.get_by_code(db, code)
    if task is None:
        return get_message("task_unknown_code", code=code.upper())
    await task_service.complete_task(db, task, by=sender_label)
    # keep the owner in the loop without him having to ask
    try:
        staff = await db.get(Staff, task.assigned_staff_id) if task.assigned_staff_id else None
        await send_message(
            db, to_phone=await team.primary_admin_phone(db),
            text=f"✅ {staff.name if staff else sender_label} ne {task.code} kar diya: {task.title}",
        )
    except SendError:
        log.info("task_done_owner_notify_failed", code=task.code)
    return get_message("task_done_ack", code=task.code)


_TASK_REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "is_task_reply": {"type": "boolean"},
        "status": {"type": "string", "enum": ["UPDATE", "DONE", "BLOCKED", "QUESTION", "NOT_RELATED"]},
        "summary": {"type": "string"},
    },
    "required": ["is_task_reply", "status", "summary"],
    "additionalProperties": False,
}

async def _handle_open_task_reply(
    db: AsyncSession, sender_phone: str, sender_label: str, text: str
) -> str | None:
    """Understand a free-text reply in the context of the staff member's latest open task.

    Staff often replies naturally instead of typing a task code. The task title
    and the latest conversation are supplied to the model so messages such as
    "Aaj RV, Pooja, BHU aur Samneghat pickup" are treated as an update to the
    pending pickup-details task instead of an unrelated command.
    """
    if sender_label == "manager" or not text or text.startswith("["):
        return None
    from app.services import tasks as task_service, team

    staff = (
        await db.execute(select(Staff).where(Staff.phone == sender_phone))
    ).scalar_one_or_none()
    if staff is None or not staff.is_active:
        return None

    open_tasks = await task_service.open_tasks_for_staff(db, staff.id)
    if not open_tasks:
        return None
    task = open_tasks[0]

    try:
        history = await _sender_history(db, sender_phone)
        prompt = (
            f"STAFF NAME: {staff.name}\n"
            f"OPEN TASK: [{task.code}] {task.title}\n"
            f"PREVIOUS TASK REPLY: {task.reply or '(none)'}\n"
            f"RECENT WHATSAPP HISTORY:\n{history[-2500:] if history else '(none)'}\n\n"
            f"NEW STAFF MESSAGE:\n{text[:1500]}"
        )
        with llm_client.track("task_reply"):
            out = await llm_client.ask_json(
                system=(
                    "You are Kwik Klin's staff task interpreter. Decide whether the new "
                    "message is a natural response/update to the staff member's OPEN TASK. "
                    "Use the task title and recent history, not just keywords. Hinglish, "
                    "Hindi, abbreviations, place names and informal wording are normal. "
                    "A message listing pickup places, customers, delivery points, progress "
                    "or a partial result can be a valid UPDATE even without the task code. "
                    "Do not invent missing facts. Mark DONE only when the message clearly "
                    "says the whole task is completed. Mark BLOCKED when the staff says the "
                    "task cannot proceed. If it is unrelated to the task, use NOT_RELATED. "
                    "Return a short factual summary in the staff's language."
                ),
                user_text=prompt,
                schema=_TASK_REPLY_SCHEMA,
                model=llm_client.MODEL_SMART,
                max_tokens=260,
            )
    except LLMError:
        return None

    if not out.get("is_task_reply") or out.get("status") == "NOT_RELATED":
        return None

    summary = str(out.get("summary") or text).strip()[:500]
    await task_service.note_reply(db, staff.id, text)

    status = str(out.get("status") or "UPDATE").upper()
    if status == "DONE":
        await task_service.complete_task(db, task, by=staff.name or sender_label)
        return f"✅ *{task.code} — Task complete!*\nSamajh gaya: {summary}"
    if status == "BLOCKED":
        await team.notify_admins(
            db,
            f"⚠️ *{task.code} — {staff.name} ki dikkat*\n{summary}",
        )
        return f"⚠️ *{task.code}* ka update note kar liya. Owner ko bata diya: {summary}"
    if status == "QUESTION":
        return f"👍 *{task.code}* ka message samajh gaya. {summary}\nAgar task complete hai to *done {task.code}* bhej dena."
    return (
        f"👍 *{task.code} — Update noted*\n"
        f"{summary}\n\n"
        f"Kaam complete hone par *done {task.code}* bhej dena."
    )


async def _order_in_text(db: AsyncSession, text: str) -> Order | None:
    """Pull a KK-... order out of free text so the task links to the bill."""
    m = ORDER_NUMBER_RE.search(text or "")
    if not m:
        return None
    return (
        await db.execute(select(Order).where(Order.order_number == m.group(0).upper()))
    ).scalar_one_or_none()


# Relay ke do pakke niyam — dono code mein, prompt par bharosa nahi:
#
# 1. Jis aadmi ka naam BHEJNE WALE ne likha hi nahi, uske paas message
#    nahi jayega. (09 Aug: owner ne likha "Message bhejo message kyo nhi
#    bheje" — kisi ka naam nahi tha — aur wo Ravi ko chala gaya.)
# 2. Bhejne wale ke apne shabd hi message banenge. Sirf "Ajit ko bhej do"
#    likha ho to model ne pichhla BOT ka jawab utha kar bhej diya tha —
#    Ajit ke paas task bana "Note save ho gaya hai (KK-...)".
_RELAY_NOISE_RE = re.compile(
    r"\b(ko|se|ka|ki|ke|ye|yeh|wo|is|isko|usko|please|plz|zara|abhi|"
    r"bhej|bhejo|bhej\s*do|bheje|bhejna|bol|bolo|bol\s*do|kah|kaho|kah\s*do|"
    r"bata|batao|bata\s*do|puch|pucho|puch\s*lo|forward|message|msg|"
    r"kar|karo|kar\s*do|de|do|dedo|de\s*do)\b",
    re.I,
)


def _sender_named(target: str, text: str) -> bool:
    """Kya bhejne wale ne sach mein ye naam likha tha?"""
    t, low = (target or "").strip().casefold(), (text or "").casefold()
    if not t:
        return False
    if t in low:
        return True
    # "Ajit bhai" / "ajitji" jaise likhawat par pehla hissa bhi kaafi hai
    head = t.split()[0]
    return len(head) >= 3 and head in low


def _leftover_words(target: str, text: str) -> str:
    """Naam aur "ko bhej do" jaise shabd hata kar bachi hui asli baat."""
    low = (text or "")
    for piece in (target or "").split():
        low = re.sub(re.escape(piece), " ", low, flags=re.I)
    low = _RELAY_NOISE_RE.sub(" ", low)
    return re.sub(r"[\s\.,!?]+", " ", low).strip()


async def _relay_target_exists(db: AsyncSession, target: str) -> bool:
    """Ye naam kisi jaante-pehchante aadmi ka hai?"""
    if target.lower() in ("manager", "boss", "malik"):
        return True
    rows = (await db.execute(select(Staff).where(Staff.is_active))).scalars().all()
    low = target.lower()
    return sum(
        1 for s in rows if s.name and (s.name.lower() in low or low in s.name.lower())
    ) == 1


_RELAY_COMPOSE_SCHEMA = {
    "type": "object",
    "properties": {"message": {"type": "string"}},
    "required": ["message"],
    "additionalProperties": False,
}


async def _compose_relay_message(
    raw_message: str, recipient_type: str, recipient_name: str
) -> str:
    """Turn an extracted instruction into one professional WhatsApp message."""
    if not raw_message:
        return ""
    try:
        with llm_client.track("relay_compose"):
            out = await llm_client.ask_json(
                system=(
                    "You are the message writer for Kwik Klin laundry. "
                    "Rewrite the supplied instruction into one concise, professional "
                    "WhatsApp message addressed directly to the recipient. Preserve "
                    "the exact operational meaning. Do not invent names, dates, prices, "
                    "orders or promises. Do not mention AI, prompts, internal rules, "
                    "or that you rewrote anything. For STAFF use polite professional "
                    "Hinglish/English and ask for a clear action/update. For CUSTOMER "
                    "use warm customer-facing language. Return only the final message."
                ),
                user_text=(
                    f"RECIPIENT TYPE: {recipient_type}\n"
                    f"RECIPIENT: {recipient_name or 'unknown'}\n"
                    f"INSTRUCTION MEANING: {raw_message[:1000]}"
                ),
                schema=_RELAY_COMPOSE_SCHEMA,
                model=llm_client.MODEL_CHEAP,
                max_tokens=220,
            )
        message = str(out.get("message") or "").strip()
        return message[:1200] if message else raw_message[:1200]
    except LLMError:
        log.warning("relay_compose_failed")
        return raw_message[:1200]


async def _apply_relay(
    db: AsyncSession, sender_label: str, extracted: dict, sender_text: str = "",
    sender_phone: str = "",
) -> str | None:
    """Route an owner instruction; staff messages must never create new tasks."""
    # A staff member's free-form message can be misclassified by the LLM as
    # a relay instruction. Never allow that path to create a new Task. Staff
    # messages are handled by the existing-task interpreter above instead.
    if sender_label != "manager":
        log.info("staff_relay_ignored_no_task_creation", sender=sender_label)
        return None

    target = (extracted.get("relay_to") or "").strip()
    raw_message = (extracted.get("relay_message") or "").strip()
    recipient_type = (extracted.get("recipient_type") or "UNKNOWN").strip().upper()
    if not raw_message:
        return get_message("staff_cmd_unknown")

    if recipient_type == "UNKNOWN":
        staff_rows = (await db.execute(select(Staff).where(Staff.is_active))).scalars().all()
        matches = [
            s for s in staff_rows
            if s.name and target and (
                s.name.lower() in target.lower() or target.lower() in s.name.lower()
            )
        ]
        if len(matches) == 1:
            recipient_type = "STAFF"
        elif target.lower() in {"manager", "boss", "malik"}:
            recipient_type = "MANAGER"
        elif target.lower() in {"customer", "grahak", "buyer", "client"}:
            recipient_type = "CUSTOMER"
        else:
            return "Kisko bhejna hai? Customer ya staff ka naam bata dijiye. 🙏"

    if recipient_type == "STAFF":
        staff_rows = (await db.execute(select(Staff).where(Staff.is_active))).scalars().all()
        matches = [
            s for s in staff_rows
            if s.name and target and (
                s.name.lower() in target.lower() or target.lower() in s.name.lower()
            )
        ]
        if len(matches) != 1:
            names = ", ".join(s.name for s in staff_rows if s.name) or "-"
            return f"Kis staff member ko bhejna hai? Naam bata dijiye. 🙏\nStaff: {names}"

        staff = matches[0]
        message = await _compose_relay_message(raw_message, "STAFF", staff.name)
        urgent = bool(_URGENT_RE.search(message))
        order = await _order_in_text(
            db, f"{raw_message} {extracted.get('order_number', '')}"
        )
        if order is None and extracted.get("customer_name"):
            order, _err = await _find_order_flex(db, extracted)

        from app.services import tasks as task_service

        task = await task_service.create_task(
            db, title=message, staff=staff, order=order,
            urgent=urgent, created_by=sender_label,
        )
        note = ""
        if order is not None and urgent and order.priority != "urgent":
            order.priority = "urgent"
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            line = f"[{stamp} {sender_label}] URGENT: {message}"
            order.notes = f"{order.notes}\n{line}" if order.notes else line
            await db.commit()
            note = f"\n🔴 {order.order_number} ko URGENT mark kar diya."
        key = "task_assigned" if task.last_ping_at else "task_assigned_undelivered"
        return get_message(key, name=staff.name, code=task.code, message=message) + note

    if recipient_type == "MANAGER":
        from app.services import team
        to_phone, to_name = await team.primary_admin_phone(db), "Manager"
        message = await _compose_relay_message(raw_message, "MANAGER", to_name)
    elif recipient_type == "CUSTOMER":
        cust_matches = []
        if extracted.get("customer_phone"):
            try:
                phone = normalize_phone(extracted["customer_phone"])
                cust = (
                    await db.execute(select(Customer).where(Customer.phone == phone))
                ).scalar_one_or_none()
                if cust is not None:
                    cust_matches = [cust]
            except ValueError:
                pass
        if not cust_matches and extracted.get("customer_name"):
            cust_matches = list((
                await db.execute(
                    select(Customer).where(
                        Customer.name.ilike(f"%{extracted['customer_name'].strip()}%"),
                        Customer.is_active,
                    ).limit(3)
                )
            ).scalars().all())
        if not cust_matches and extracted.get("order_number"):
            order = await _order_in_text(db, extracted["order_number"])
            if order is not None:
                cust = await db.get(Customer, order.customer_id)
                if cust is not None:
                    cust_matches = [cust]
        if len(cust_matches) != 1:
            return "Customer ka naam ya order number bata dijiye, taaki main sahi customer ko message bhej sakun. 🙏"
        to_phone = cust_matches[0].phone
        to_name = cust_matches[0].name or to_phone
        message = await _compose_relay_message(raw_message, "CUSTOMER", to_name)
    elif recipient_type == "SELF":
        to_phone = sender_phone or manager_phone()
        to_name = sender_label
        message = await _compose_relay_message(raw_message, "SELF", to_name)
    else:
        return get_message("staff_cmd_unknown")

    try:
        await send_message(db, to_phone=to_phone, text=message)
    except WindowClosedError:
        if recipient_type == "CUSTOMER":
            return get_message("relay_window_closed", name=to_name)
        try:
            await send_message(
                db, to_phone=to_phone,
                template_name="kk_staff_alert",
                template_params=[" ".join(f"{sender_label}: {message}".split())[:600]],
            )
        except SendError:
            log.warning("relay_template_failed", to=to_phone)
            return get_message("relay_window_closed", name=to_name)
        return get_message("relay_done_template", name=to_name, message=message)
    except SendError:
        log.warning("relay_send_failed", to=to_phone)
        return get_message("relay_failed", name=to_name)

    if recipient_type == "CUSTOMER":
        from app.models import OpenQuestion

        open_rows = (
            (
                await db.execute(
                    select(OpenQuestion)
                    .join(Customer, Customer.id == OpenQuestion.customer_id)
                    .where(Customer.phone == to_phone, OpenQuestion.status == "open")
                )
            ).scalars().all()
        )
        for oq in open_rows:
            oq.status = "answered"
            oq.answer = message[:2000]
            oq.answered_at = datetime.now(timezone.utc)
        if open_rows:
            await db.commit()
    log.info("relay_sent", to=to_phone, by=sender_label, kind=recipient_type.lower())
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
