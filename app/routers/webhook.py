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
import re
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
from app.services.ai_agent import build_ai_reply
from app.services.bill_agent import handle_staff_message
from app.services.messages import get_message, status_label
from app.services.order_service import (
    OrderNotFoundError,
    get_active_orders_for_phone,
    get_order,
)
from app.services.whatsapp import SendError, send_message
from app.utils.phone import normalize_phone

router = APIRouter()
log = structlog.get_logger()

# Matches order numbers like KK-20260801-01 anywhere in a message.
ORDER_NUMBER_RE = re.compile(r"\bKK-\d{8}-\d{2,}\b", re.IGNORECASE)

# Opt-out phrases: English STOP + the Hinglish ways our customers say it.
STOP_RE = re.compile(
    r"^\s*(stop|unsubscribe|band karo|band kro|msg mat bhejo|message mat bhejo)\s*$",
    re.IGNORECASE,
)
START_RE = re.compile(r"^\s*(start|shuru karo|shuru kro)\s*$", re.IGNORECASE)

# Rating button replies: interactive ids (window sends) or the template
# quick-reply payload texts (template sends carry the text, not an id).
_RATING_MAP = {
    "rate_good": "good", "⭐ bahut badhiya": "good", "bahut badhiya": "good",
    "rate_mid": "mid", "🙂 theek thi": "mid", "theek thi": "mid",
    "rate_bad": "bad", "😞 sudhar chahiye": "bad", "sudhar chahiye": "bad",
}


def _match_rating(text: str) -> str | None:
    m = re.match(r"^\[button:([^\]]+)\]", text)
    if not m:
        return None
    return _RATING_MAP.get(m.group(1).strip().lower())


async def _handle_rating(db: AsyncSession, customer: Customer, phone: str, kind: str) -> None:
    """Deterministic rating handling: thank/apologise; bad -> admin + pause."""
    from app.services import audit

    reply_key = {"good": "rate_good_reply", "mid": "rate_mid_reply", "bad": "rate_bad_reply"}[kind]
    try:
        await send_message(db, to_phone=phone, text=get_message(reply_key))
    except SendError:
        log.exception("rating_reply_failed", phone=phone)
    if kind == "bad":
        # unhappy customer: humans take over, owner alerted immediately
        customer.agent_paused = True
        await db.commit()
        try:
            await send_message(
                db, to_phone=settings.MANAGER_PHONE,
                text=get_message(
                    "rate_bad_admin_alert",
                    customer_name=customer.name or "naam nahi pata",
                    phone=phone, rating="Sudhar chahiye",
                ),
            )
        except SendError:
            log.exception("rate_bad_alert_failed")
    await audit.record(
        actor_role="customer", actor=phone, action="rating",
        args={"rating": kind}, result="agent paused" if kind == "bad" else "thanked",
    )


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


@router.post("/webhook/dotpe")
async def receive_dotpe_webhook(
    request: Request, db: AsyncSession = Depends(get_db)
) -> JSONResponse:
    """Receive DotPe events (dlr statuses + inbound messages).

    Auth: DotPe's panel lets us attach a custom header to every webhook call.
    We configure header name 'Dotpe-Webhook-Token' with our secret value —
    requests without it are rejected. Their 'verify and save' test event
    passes the same header, so verification succeeds automatically.
    """
    if not settings.DOTPE_WEBHOOK_TOKEN:
        log.warning("dotpe_webhook_disabled_no_token")
        return JSONResponse({"error": "disabled"}, status_code=403)
    if request.headers.get("Dotpe-Webhook-Token") != settings.DOTPE_WEBHOOK_TOKEN:
        log.warning("dotpe_webhook_bad_token")
        return JSONResponse({"error": "invalid token"}, status_code=403)

    try:
        payload = json.loads(await request.body())
    except json.JSONDecodeError:
        # Their verification test may not be JSON — 200 keeps setup working.
        return JSONResponse({"status": "ok"})

    try:
        if "message" in payload:
            await _handle_inbound_message(_dotpe_to_meta_shape(payload), db)
        elif "status" in payload:
            status = payload.get("status", {})
            log.info(
                "whatsapp_status",
                provider="dotpe",
                job_id=status.get("jobid"),
                status=status.get("status"),
                recipient=status.get("recipient"),
                errors=status.get("errors"),
            )
        else:
            log.info("dotpe_webhook_unknown_event", keys=list(payload.keys()))
    except Exception:
        log.exception("dotpe_webhook_processing_failed")

    return JSONResponse({"status": "received"})


def _dotpe_to_meta_shape(payload: dict) -> dict:
    """Convert DotPe's inbound event to the Meta-style dict our handler eats.

    DotPe events carry no per-message id, so we synthesize a stable one from
    timestamp+sender+content — a retried delivery of the same event hashes
    identically and dedups via the existing unique constraint.
    """
    msg = payload.get("message", {})
    ts = payload.get("timestamp", 0)
    body = msg.get("body", "")
    button = msg.get("button") or {}
    fingerprint = f"{ts}:{msg.get('from', '')}:{body}:{button.get('payload', '')}"
    synth_id = "dotpe:" + hashlib.sha256(fingerprint.encode()).hexdigest()[:40]

    if msg.get("type") == "button":
        return {
            "from": msg.get("from", ""),
            "id": synth_id,
            "type": "button",
            "button": {"payload": button.get("payload"), "text": button.get("text", "")},
        }
    return {
        "from": msg.get("from", ""),
        "id": synth_id,
        "type": "text",
        "text": {"body": body},
    }


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
                log.info(
                    "whatsapp_status",
                    wa_message_id=status.get("id"),
                    status=status.get("status"),
                    recipient=status.get("recipient_id"),
                )
                # campaign delivered/read tracking — never breaks the webhook
                try:
                    from app.services.marketing import track_status_update

                    await track_status_update(
                        db, status.get("id", ""), status.get("status", "")
                    )
                except Exception:
                    log.exception("campaign_status_track_failed")


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

    # Inbound photo: pull the file from Meta so the Inbox can show it.
    if msg.get("type") == "image":
        from pathlib import Path

        from app.services.whatsapp import download_media

        media_dir = str(Path(__file__).resolve().parent.parent / "media")
        media_id = msg.get("image", {}).get("id")
        caption = msg.get("image", {}).get("caption", "")
        if media_id:
            fname = await download_media(media_id, media_dir)
            if fname:
                text = f"[image:/admin/media/{fname}]" + (f" {caption}" if caption else "")

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

    # Staff and the manager (whose number has a customer row from his own
    # tests) get the command agent: bill-by-text, delay + status updates.
    # A silent None means "not a command" — same as pre-Phase-4 behavior.
    is_manager = phone == normalize_phone(settings.MANAGER_PHONE)
    if staff is not None or is_manager:
        try:
            command_reply = await handle_staff_message(
                db,
                sender_phone=phone,
                sender_label=staff.name if staff else "manager",
                text=text,
            )
        except Exception:
            log.exception("staff_command_failed", phone=phone)
            command_reply = None
        if command_reply:
            try:
                await send_message(db, to_phone=phone, text=command_reply)
            except SendError:
                log.exception("staff_reply_send_failed", phone=phone)
        return

    # Customer replies, in strict priority order:
    # 0. STOP -> opt out instantly; takeover-paused threads stay silent.
    # 1. Message names an order number -> deterministic status reply (no AI).
    # 2. AI agent (Phase 4) -> may answer or escalate; returns None if the
    #    LLM is down/unsure.
    # 3. Fallback: the same rule-based replies Phase 3 shipped with.
    # A failed reply must never break the webhook: log it and move on.
    if customer is not None:
        # campaign reply tracking (best-effort, before any reply logic)
        try:
            from app.services.marketing import track_reply

            await track_reply(db, customer.id)
        except Exception:
            log.exception("campaign_reply_track_failed")
        rating = _match_rating(text or "")
        if rating:
            await _handle_rating(db, customer, phone, rating)
            return
        if STOP_RE.search(text or ""):
            customer.opted_out = True
            customer.marketing_opt_out = True
            await db.commit()
            log.info("customer_opted_out", phone=phone)
            try:
                await send_message(db, to_phone=phone, text=get_message("stop_confirmed"))
            except SendError:
                log.exception("stop_confirm_send_failed", phone=phone)
            return
        if START_RE.search(text or "") and customer.opted_out:
            customer.opted_out = False
            customer.marketing_opt_out = False
            await db.commit()
            log.info("customer_opted_in", phone=phone)
            try:
                await send_message(db, to_phone=phone, text=get_message("start_confirmed"))
            except SendError:
                log.exception("start_confirm_send_failed", phone=phone)
            return
        if customer.agent_paused:
            # Owner pressed 'Take over' in the Inbox — humans only here.
            log.info("agent_paused_thread", phone=phone)
            return
        try:
            if ORDER_NUMBER_RE.search(text):
                reply = await _build_customer_reply(db, customer, text)
            else:
                reply = await build_ai_reply(db, customer, text)
                if reply is None:
                    reply = await _build_customer_reply(db, customer, text)
        except Exception:
            log.exception("reply_build_failed", phone=phone)
            reply = get_message("error_fallback")
        try:
            await send_message(db, to_phone=phone, text=reply)
        except SendError:
            log.exception("reply_send_failed", phone=phone)


async def _build_customer_reply(db: AsyncSession, customer: Customer, text: str) -> str:
    """Deterministic reply logic (Phase 3 — Phase 4's agent replaces this):

    1. Message contains an order number -> that order's status
       (only the customer's OWN orders — no leaking others' orders).
    2. Customer has exactly one active order -> its status.
    3. Several active orders -> a short list.
    4. Nothing to say -> the generic ack.
    """
    match = ORDER_NUMBER_RE.search(text)
    if match:
        number = match.group(0).upper()
        try:
            order = await get_order(db, number)
        except OrderNotFoundError:
            return get_message("order_not_found")
        if order.customer_id != customer.id:
            log.warning(
                "order_lookup_denied_wrong_customer",
                order_number=number,
                asking_customer=str(customer.id),
            )
            return get_message("order_not_found")
        return _status_reply(order)

    active = await get_active_orders_for_phone(db, customer.phone)
    if len(active) == 1:
        return _status_reply(active[0])
    if len(active) > 1:
        lines = [get_message("orders_list_header", count=str(len(active)))]
        lines += [f"{o.order_number} — {status_label(o.status)}" for o in active]
        return "\n".join(lines)
    return get_message("ack_received")


def _status_reply(order) -> str:
    label = status_label(order.status)
    if order.expected_delivery:
        return get_message(
            "status_reply_with_date",
            order_number=order.order_number,
            status_label=label,
            date=order.expected_delivery.strftime("%d %b %Y"),
        )
    return get_message("status_reply", order_number=order.order_number, status_label=label)


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
