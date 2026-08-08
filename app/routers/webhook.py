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
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Conversation, Customer, Direction, Staff, WebhookEvent
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

# AI signature ab whatsapp.send_message ke andar centrally lagta hai
# (app/services/whatsapp.py: sign_ai) — har automated customer message
# par, sirf yahan ke conversational replies par nahi.

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
    reply_text = get_message(reply_key)
    if kind == "good":
        # Google review link ONLY on happy ratings (owner's spec). Two
        # listings — rotate by phone so both profiles grow.
        from app.services import app_settings

        l1 = (await app_settings.get(db, "google_review_link") or "").strip()
        l2 = (await app_settings.get(db, "google_review_link_2") or "").strip()
        links = [l for l in (l1, l2) if l]
        if links:
            link = links[sum(ord(c) for c in phone) % len(links)]
            reply_text += (
                "\n\nAap jaise pyare customers ki wajah se hi hum chal rahe hain 🥰 "
                "Bas 30 second — yahan tap karke Google par 2 shabd likh dijiye, "
                "aapka ek review hamari dukaan ke liye diwali ka bonus jaisa hai! 🎁\n"
                f"{link}"
            )
    try:
        await send_message(db, to_phone=phone, text=reply_text)
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
    if mode == "subscribe" and hmac.compare_digest(token, settings.WHATSAPP_VERIFY_TOKEN):
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

    # Journal FIRST: once this commit lands, the event can never be lost —
    # a crash mid-processing is replayed by the scheduler from this row.
    event = await _journal_event(db, "meta", body, payload)
    if event is None:
        return JSONResponse({"status": "duplicate"})
    event_key = event.event_key

    try:
        await _process_payload(payload, db)
    except Exception as exc:
        # Still 200 — see module docstring. The failure is logged with stack
        # and the journal row stays 'failed' for the retry job.
        log.exception("webhook_processing_failed")
        await _mark_event(db, event_key, "failed", error=repr(exc))
    else:
        await _mark_event(db, event_key, "processed")

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
    if not hmac.compare_digest(
        request.headers.get("Dotpe-Webhook-Token", ""), settings.DOTPE_WEBHOOK_TOKEN
    ):
        log.warning("dotpe_webhook_bad_token")
        return JSONResponse({"error": "invalid token"}, status_code=403)

    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        # Their verification test may not be JSON — 200 keeps setup working.
        return JSONResponse({"status": "ok"})

    event_key = None
    if "message" in payload:
        event = await _journal_event(db, "dotpe", body, payload)
        if event is None:
            return JSONResponse({"status": "duplicate"})
        event_key = event.event_key

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
    except Exception as exc:
        log.exception("dotpe_webhook_processing_failed")
        if event_key is not None:
            await _mark_event(db, event_key, "failed", error=repr(exc))
    else:
        if event_key is not None:
            await _mark_event(db, event_key, "processed")

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


async def _journal_event(
    db: AsyncSession, source: str, body: bytes, payload: dict
) -> WebhookEvent | None:
    """Persist the raw event before any processing. None = already journaled
    (an exact redelivery), so the caller must skip processing."""
    key = hashlib.sha256(body).hexdigest()
    event = WebhookEvent(source=source, event_key=key, payload=payload)
    db.add(event)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        log.info("webhook_event_duplicate", source=source, event_key=key)
        return None
    return event


async def _mark_event(
    db: AsyncSession, event_key: str, status: str, error: str | None = None
) -> None:
    """Update the journal row after processing. Never raises — a failure here
    only means the retry job reprocesses an already-handled event, which the
    wa_message_id dedup absorbs. Plain UPDATE by key: after a rollback the
    ORM instance is expired and unusable in async code."""
    from sqlalchemy import update

    try:
        await db.rollback()  # processing may have left the session dirty
        await db.execute(
            update(WebhookEvent)
            .where(WebhookEvent.event_key == event_key)
            .values(
                status=status,
                error=error[:2000] if error else None,
                attempts=WebhookEvent.attempts + 1,
                processed_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()
    except Exception:
        log.exception("webhook_event_mark_failed", event_key=event_key)


_MAX_EVENT_ATTEMPTS = 5


async def retry_stuck_webhook_events() -> int:
    """Scheduler entry: replay journaled events that failed or got stuck.

    'failed'   = processing raised; retry with fresh state.
    'received' = journaled but never marked — the process crashed mid-handling.
    After _MAX_EVENT_ATTEMPTS the row is marked 'dead' (visible for forensics).
    Returns the number of events retried.
    """
    from app.database import async_session_factory

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    retried = 0
    async with async_session_factory() as db:
        # plain tuples, not ORM instances — the rollback inside _mark_event
        # would expire instances and async lazy-refresh blows up
        rows = (
            await db.execute(
                select(
                    WebhookEvent.event_key,
                    WebhookEvent.attempts,
                    WebhookEvent.source,
                    WebhookEvent.payload,
                    WebhookEvent.error,
                )
                .where(
                    WebhookEvent.status.in_(("failed", "received")),
                    WebhookEvent.received_at <= cutoff,
                )
                .order_by(WebhookEvent.received_at)
                .limit(50)
            )
        ).all()
        for key, attempts, source, payload, error in rows:
            if (attempts or 0) >= _MAX_EVENT_ATTEMPTS:
                await _mark_event(db, key, "dead", error=error)
                continue
            retried += 1
            try:
                if source == "dotpe":
                    if "message" in payload:
                        await _handle_inbound_message(_dotpe_to_meta_shape(payload), db)
                else:
                    await _process_payload(payload, db)
            except Exception as exc:
                log.exception("webhook_event_retry_failed", event_key=key)
                await _mark_event(db, key, "failed", error=repr(exc))
            else:
                await _mark_event(db, key, "processed")
    return retried


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
            # Meta names the sender for us: every inbound batch carries the
            # WhatsApp profile name next to the number. Without this an
            # unknown number stayed "+9198..." forever and the owner had to
            # ask "aap kaun?" to a person WhatsApp had already introduced.
            profiles = {
                str(c.get("wa_id") or ""): ((c.get("profile") or {}).get("name") or "")
                for c in value.get("contacts", []) or []
            }
            for msg in value.get("messages", []):
                await _handle_inbound_message(
                    msg, db, profile_name=profiles.get(str(msg.get("from") or ""), "")
                )
            for status in value.get("statuses", []):
                log.info(
                    "whatsapp_status",
                    wa_message_id=status.get("id"),
                    status=status.get("status"),
                    recipient=status.get("recipient_id"),
                )
                await _record_delivery_status(
                    db, status.get("id", ""), status.get("status", "")
                )
                # campaign delivered/read tracking — never breaks the webhook
                try:
                    from app.services.marketing import track_status_update

                    await track_status_update(
                        db, status.get("id", ""), status.get("status", "")
                    )
                except Exception:
                    log.exception("campaign_status_track_failed")


# WhatsApp's ladder. A status may arrive out of order (read before
# delivered on a fast phone) — never walk a message backwards.
_STATUS_RANK = {"sent": 1, "delivered": 2, "read": 3, "failed": 4}


async def _record_delivery_status(db: AsyncSession, wamid: str, status: str) -> None:
    """Move an outbound message's tick forward. Never breaks the webhook."""
    status = (status or "").strip().lower()
    if not wamid or status not in _STATUS_RANK:
        return
    try:
        row = (
            await db.execute(
                select(Conversation).where(Conversation.wa_message_id == wamid)
            )
        ).scalar_one_or_none()
        if row is None:
            return  # a campaign send or an old row — nothing to tick
        if _STATUS_RANK.get(row.status or "", 0) >= _STATUS_RANK[status]:
            return
        row.status = status
        await db.commit()
    except Exception:
        log.exception("delivery_status_record_failed", wa_message_id=wamid)


# A WhatsApp profile name is whatever the person typed for themselves —
# emojis, shop slogans, an empty string. Keep it human and bounded, and
# never accept something that is just their own number back.
def _clean_profile_name(raw: str) -> str:
    """Make a WhatsApp display name safe to store, or "" if it is useless.

    People put emojis, shop slogans or their own number in that field.
    Strip the invisible junk, cap the length, and refuse a 'name' that is
    just digits — saving a number as a name helps nobody.
    """
    name = "".join(ch for ch in str(raw or "") if ch.isprintable())
    name = " ".join(name.split())[:120]
    if len(re.sub(r"\D", "", name)) >= 8 and not re.search(r"[^\W\d_]", name):
        return ""
    return name


async def _handle_inbound_message(
    msg: dict, db: AsyncSession, profile_name: str = ""
) -> None:
    """Store one inbound message; open the sender's 24h window.

    profile_name is the sender's WhatsApp display name, which Meta sends
    with every inbound batch. We use it ONLY to fill a blank — a name the
    shop typed itself always wins.
    """
    wa_message_id = msg.get("id")
    raw_from = msg.get("from", "")

    try:
        phone = normalize_phone(raw_from)
    except ValueError:
        log.warning("inbound_unparseable_phone", raw=raw_from, wa_message_id=wa_message_id)
        return

    text = _extract_text(msg)

    # Anything with a file — photo, voice note, PDF, video, sticker — is
    # pulled from Meta so the Inbox can show or play it, exactly like
    # WhatsApp does. Only text and taps have no file.
    mtype = msg.get("type", "")
    if mtype in _MEDIA_TYPES:
        from pathlib import Path

        from app.services.whatsapp import download_media

        media_dir = str(Path(__file__).resolve().parent.parent / "media")
        part = msg.get(mtype, {}) or {}
        media_id = part.get("id")
        caption = part.get("caption") or ""
        # documents carry the sender's own file name — keep it, it's the
        # only human-readable label a PDF gets
        label = part.get("filename") or ""
        if media_id:
            fname = await download_media(media_id, media_dir)
            if fname:
                marker = "image" if mtype in ("image", "sticker") else mtype
                text = f"[{marker}:/admin/media/{fname}]"
                extra = " ".join(x for x in (label, caption) if x)
                # A voice note is a message, not an attachment — transcribe it
                # so the agent can act on what was actually said.
                if mtype in ("audio", "voice"):
                    said = await _transcribe(Path(media_dir) / fname, part)
                    if said:
                        extra = said
                if extra:
                    text += f" {extra}"

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
            customer = Customer(phone=phone, name=_clean_profile_name(profile_name) or None)
            db.add(customer)
            # flush() runs the INSERT now so customer.id exists — without
            # this, the Conversation below would get customer_id=None and
            # trip the XOR check constraint.
            await db.flush()
            log.info(
                "customer_created_from_inbound", phone=phone,
                named=bool(customer.name),
            )
        elif not (customer.name or "").strip():
            # we met them before but never learned a name — WhatsApp has one
            got = _clean_profile_name(profile_name)
            if got:
                customer.name = got
                log.info("customer_named_from_whatsapp", phone=phone, name=got)

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
            # they swiped-to-reply on one of our messages: keep the link so
            # the Inbox shows WHICH message they are answering
            reply_to_wamid=(msg.get("context") or {}).get("id"),
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
    # A transcribed voice note is a MESSAGE: everything downstream should see
    # the words. The stored row keeps the marker so the Inbox can still play
    # the audio alongside what was said.
    from app.services.ai_agent import _voice_transcript

    spoken = _voice_transcript(text) or text

    # An ADMIN staff row (the owner, Suyash) carries manager powers even
    # though he is in the staff table — otherwise adding him as a person
    # would quietly demote him to a washerman.
    from app.models import StaffRole

    is_manager = phone == normalize_phone(settings.MANAGER_PHONE) or (
        staff is not None and staff.role is StaffRole.ADMIN
    )
    if staff is not None or is_manager:
        try:
            command_reply = await handle_staff_message(
                db,
                sender_phone=phone,
                sender_label="manager" if is_manager else staff.name,
                text=spoken,
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
            from app.services import audit as _audit

            await _audit.record(
                actor_role="customer", actor=phone, action="stop_optout",
                args={}, result="opted out",
            )
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
            if ORDER_NUMBER_RE.search(spoken):
                reply = await _build_customer_reply(db, customer, spoken)
            else:
                reply = await build_ai_reply(db, customer, spoken)
                if reply is None:
                    reply = await _build_customer_reply(db, customer, spoken)
        except Exception:
            log.exception("reply_build_failed", phone=phone)
            reply = get_message("error_fallback")
        try:
            await send_message(db, to_phone=phone, text=reply)
        except SendError:
            log.exception("reply_send_failed", phone=phone)
        # first-contact numbers with no orders -> lead pipeline (never raises)
        try:
            from app.services.leads import note_inquiry

            await note_inquiry(db, customer, text or "")
        except Exception:
            log.exception("lead_capture_failed")


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


# WhatsApp message types that carry a downloadable file
_MEDIA_TYPES = ("image", "sticker", "audio", "voice", "video", "document")


async def _transcribe(path, part: dict) -> str | None:
    """Read a downloaded voice note and turn it into text. Never raises —
    an unreadable note falls back to the plain acknowledgement."""
    try:
        from app.services.llm_client import transcribe_audio

        blob = path.read_bytes()
        if not blob or len(blob) > 15_000_000:  # Gemini inline-data ceiling
            return None
        mime = (part.get("mime_type") or "audio/ogg").split(";")[0].strip()
        return await transcribe_audio(blob, mime)
    except Exception:
        log.exception("voice_transcribe_error")
        return None


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
    if mtype == "location":
        # A shared pin IS the pickup address — keep it readable and mappable
        loc = msg.get("location", {}) or {}
        lat, lon = loc.get("latitude"), loc.get("longitude")
        bits = [b for b in (loc.get("name"), loc.get("address")) if b]
        where = " · ".join(bits) if bits else "Location"
        return f"[location:{lat},{lon}] {where}"
    if mtype == "contacts":
        people = []
        for c in msg.get("contacts", []) or []:
            name = (c.get("name") or {}).get("formatted_name", "")
            nums = ", ".join(p.get("phone", "") for p in (c.get("phones") or []))
            people.append(" ".join(x for x in (name, nums) if x))
        return "[contact] " + "; ".join(p for p in people if p)
    if mtype == "reaction":
        r = msg.get("reaction", {}) or {}
        return f"[reaction] {r.get('emoji', '')}"
    return f"[{mtype}]"
