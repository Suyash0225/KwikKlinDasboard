"""WhatsApp webhook: GET verify + POST receive.

Security model:
- GET  /webhook — Meta's one-time subscription handshake. We only confirm if
  hub.verify_token matches OUR invented WHATSAPP_VERIFY_TOKEN.
- POST /webhook — every request must carry X-Hub-Signature-256, an HMAC of
  the RAW body with WHATSAPP_APP_SECRET. Wrong/missing signature -> 403.
  This is what stops random internet traffic from injecting fake messages.

Behavior rules:
- Always return 200 once the signature is valid — even if processing fails.
  A non-200 makes Meta retry the same payload for days (retry storm). We log
  the exception and move on; the wa_message_id dedup means a genuine retry
  can't double-insert anyway.
- Inbound from a staff phone is recorded against the staff row; anything
  else upserts a customer row. last_message_at is updated — that's what
  opens the 24h window for free-form replies.
- Phase 2 stores messages only. The ack reply is wired in group (c);
  real intent handling arrives in Phases 3/4.
"""

import hashlib
import hmac
import json
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Conversation, Customer, Direction, Staff
from app.services.messages import get_message
from app.services.whatsapp import SendError, send_message
from app.utils.phone import normalize_phone

router = APIRouter()
log = structlog.get_logger()


@router.get("/webhook")
async def verify_webhook(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
) -> PlainTextResponse:
    """Meta's subscription handshake: echo the challenge iff the token matches."""
    if mode == "subscribe" and token == settings.WHATSAPP_VERIFY_TOKEN:
        log.info("webhook_verify_ok")
        return PlainTextResponse(challenge)
    log.warning("webhook_verify_failed", mode=mode)
    return PlainTextResponse("verification failed", status_code=403)


@router.post("/webhook")
async def receive_webhook(
    request: Request, db: AsyncSession = Depends(get_db)
) -> JSONResponse:
    """Receive inbound messages/statuses from Meta."""
    body = await request.body()

    if not _signature_valid(body, request.headers.get("X-Hub-Signature-256")):
        log.warning("webhook_bad_signature", client=request.client.host if request.client else "?")
        return JSONResponse({"error": "invalid signature"}, status_code=403)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        log.warning("webhook_bad_json")
        return JSONResponse({"status": "ignored"})

    try:
        await _process_payload(payload, db)
    except Exception:
        # Still 200 — see module docstring. The failure is logged with stack.
        log.exception("webhook_processing_failed")

    return JSONResponse({"status": "received"})


def _signature_valid(body: bytes, header: str | None) -> bool:
    """Check X-Hub-Signature-256 (sha256 HMAC of raw body with app secret)."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(
        settings.WHATSAPP_APP_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


async def _process_payload(payload: dict, db: AsyncSession) -> None:
    """Walk Meta's entry/changes structure; handle messages and statuses."""
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                await _handle_inbound_message(msg, db)
            for status in value.get("statuses", []):
                # Delivery receipts: log only for now (Phase 3 may act on them).
                log.info(
                    "whatsapp_status",
                    wa_message_id=status.get("id"),
                    status=status.get("status"),
                    recipient=status.get("recipient_id"),
                )


async def _handle_inbound_message(msg: dict, db: AsyncSession) -> None:
    """Store one inbound message; open the sender's 24h window."""
    wa_message_id = msg.get("id")
    raw_from = msg.get("from", "")

    try:
        phone = normalize_phone(raw_from)
    except ValueError:
        log.warning("inbound_unparseable_phone", raw=raw_from, wa_message_id=wa_message_id)
        return

    text = _extract_text(msg)

    # Staff phone? Record against staff. Otherwise upsert customer.
    staff = (
        await db.execute(select(Staff).where(Staff.phone == phone))
    ).scalar_one_or_none()
    customer: Customer | None = None
    if staff is None:
        customer = (
            await db.execute(select(Customer).where(Customer.phone == phone))
        ).scalar_one_or_none()
        if customer is None:
            customer = Customer(phone=phone)
            db.add(customer)
            # flush() runs the INSERT now so customer.id exists — without
            # this, the Conversation below would get customer_id=None and
            # trip the XOR check constraint.
            await db.flush()
            log.info("customer_created_from_inbound", phone=phone)

    now = datetime.now(timezone.utc)
    participant = staff or customer
    assert participant is not None  # one of the two is always set
    participant.last_message_at = now

    db.add(
        Conversation(
            customer_id=customer.id if customer else None,
            staff_id=staff.id if staff else None,
            direction=Direction.INBOUND,
            message_text=text,
            wa_message_id=wa_message_id,
        )
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        # ONLY the wa_message_id unique violation means "Meta retried the
        # same message" — anything else is a real bug and must surface.
        if "uq_conversations_wa_message_id" in str(exc.orig):
            log.info("inbound_duplicate_ignored", wa_message_id=wa_message_id)
            return
        log.exception("inbound_store_integrity_error", wa_message_id=wa_message_id)
        raise

    log.info(
        "inbound_stored",
        wa_message_id=wa_message_id,
        sender="staff" if staff else "customer",
        phone=phone,
    )

    # Placeholder ack — customers only (acking staff on every reply would be
    # noise). Their inbound just opened the 24h window, so free-form is legal.
    # Replaced by real handling in Phases 3/4. A failed ack must never break
    # the webhook: log it and move on (degrade, never crash).
    if customer is not None:
        try:
            await send_message(db, to_phone=phone, text=get_message("ack_received"))
        except SendError:
            log.exception("ack_send_failed", phone=phone)


def _extract_text(msg: dict) -> str:
    """Flatten Meta's message types to one text column.

    Buttons keep their id — '[button:order:<uuid>:done] Done' — so Phase 3.5
    can parse the action without re-asking. Media types are markers for now.
    """
    mtype = msg.get("type", "unknown")
    if mtype == "text":
        return msg.get("text", {}).get("body", "")
    if mtype == "interactive":
        inter = msg.get("interactive", {})
        if inter.get("type") == "button_reply":
            reply = inter.get("button_reply", {})
            return f"[button:{reply.get('id')}] {reply.get('title', '')}"
        return f"[interactive:{inter.get('type')}]"
    if mtype == "button":  # template quick-reply buttons arrive as this type
        return f"[button:{msg.get('button', {}).get('payload')}] {msg.get('button', {}).get('text', '')}"
    return f"[{mtype}]"
