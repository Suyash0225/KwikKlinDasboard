"""Send WhatsApp messages — the ONLY outbound door in the entire system.

NON-NEGOTIABLE RULES (from PROJECT_SPEC.md):
- Business logic never calls the Graph API directly. Everything outbound goes
  through send_message() below.
- 24h customer service window: free-form text/buttons are allowed only if the
  recipient messaged us within the last 24 hours (their last_message_at in
  the DB). Outside the window, only pre-approved templates. We NEVER guess —
  if the DB says the window is closed, free-form raises WindowClosedError.
- Every send is logged (structlog) and recorded in `conversations`
  (except recipients that are neither customer nor staff — e.g. the manager —
  which are log-only, because conversations requires a participant row).

Usage:
    from app.services.whatsapp import send_message, Button

    # free-form (only inside 24h window):
    await send_message(db, to_phone="+91...", text="...")

    # with buttons (also free-form category):
    await send_message(db, to_phone="+91...", text="Order LDY-...?",
                       buttons=[Button("order:<id>:done", "Done ✅")])

    # template (always allowed):
    await send_message(db, to_phone="+91...", template_name="hello_world")
"""

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import httpx
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Conversation, Customer, Direction, Staff
from app.services import dotpe
from app.services.templates import TEMPLATES, build_template

log = structlog.get_logger()

GRAPH_URL = (
    f"https://graph.facebook.com/v21.0/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
)
SERVICE_WINDOW = timedelta(hours=24)

# Every media type WhatsApp can send, mapped to a real extension. Anything
# missing still saves (see download_media) — this just keeps the common
# ones openable by name.
MEDIA_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif",
    "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
    "audio/aac": ".aac", "audio/amr": ".amr", "audio/wav": ".wav",
    "video/mp4": ".mp4", "video/3gpp": ".3gp", "video/quicktime": ".mov",
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/plain": ".txt", "text/csv": ".csv", "application/zip": ".zip",
}
MAX_BUTTONS = 3
MAX_BUTTON_TITLE = 20  # WhatsApp hard limit


class Button(NamedTuple):
    """One interactive reply button. id carries context, e.g. 'order:<uuid>:done'."""

    id: str
    title: str


class SendError(Exception):
    """A message could not be sent. Caller decides how to degrade.

    transient=True means network/5xx/429 — worth retrying later. Transient
    failures are auto-queued in outbound_queue and retried by the scheduler.
    """

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class WindowClosedError(SendError):
    """Free-form send attempted outside the 24h window. Use a template."""


# Owner ka niyam: customer ko jaane wala HAR automated free-form message
# neeche bold sign + ek halki italic line ke saath jaye — customer ko pata
# rahe ki AI bol raha hai, aur AI se galti bhi ho sakti hai. Note italic
# hai taaki message mein chamke nahi.
# - sirf customers ko (staff/owner ke internal messages par nahi)
# - manual Inbox replies (sent_by="manager") insaan ke hain — unpar nahi
# - pre-approved templates ka body Meta fix karta hai, wahan runtime par
#   kuch nahi juda sakta — uske liye Template Studio ka footer hai
AI_SIGNATURE = "*Thank you – Kwik Klin AI*"
AI_NOTE = "_AI hai — chhoti-moti galti mumkin hai_"


def sign_ai(text: str) -> str:
    if not text or AI_SIGNATURE in text:
        return text
    return f"{text}\n\n{AI_SIGNATURE}\n{AI_NOTE}"


def _template_log(name: str, params: list[str] | None) -> str:
    """What we store for a template send, so the Inbox stays readable.

    "[template:kk_staff_alert]" on its own told the owner nothing — the
    PARAMS are the message (Meta fills them on delivery), so they belong in
    the log line too. The dashboard substitutes them back into the approved
    body; the marker keeps the 'Template' badge working.
    """
    joined = " | ".join((p or "").strip() for p in (params or []))
    return f"[template:{name}] {joined}".strip()


async def send_message(
    db: AsyncSession,
    *,
    to_phone: str,
    text: str | None = None,
    buttons: list[Button] | None = None,
    template_name: str | None = None,
    template_params: list[str] | None = None,
    sent_by: str = "bot",
    enqueue_on_fail: bool = True,
    reply_to: str | None = None,
) -> str:
    """Send one WhatsApp message. Returns Meta's wa_message_id.

    Exactly one mode:
      text                      -> free-form text        (window required)
      text + buttons            -> interactive buttons    (window required)
      template_name [+ params]  -> template               (always allowed)

    reply_to: a wamid to quote, so the recipient sees which message this
    answers — the same as swiping to reply in WhatsApp.

    Raises WindowClosedError / SendError. Never returns silently on failure.
    """
    # --- validate the mode ---
    if template_name and (text or buttons):
        raise ValueError("template cannot be combined with text/buttons")
    if buttons and not text:
        raise ValueError("buttons need a text body")
    if not template_name and not text:
        raise ValueError("nothing to send: give text or template_name")
    if buttons:
        if len(buttons) > MAX_BUTTONS:
            raise ValueError(f"WhatsApp allows max {MAX_BUTTONS} buttons")
        for b in buttons:
            if len(b.title) > MAX_BUTTON_TITLE:
                raise ValueError(f"button title too long (max {MAX_BUTTON_TITLE}): {b.title!r}")

    customer, staff = await _find_recipient(db, to_phone)

    # --- 24h window check for free-form ---
    is_free_form = template_name is None
    if is_free_form:
        last_inbound = None
        participant = customer or staff
        if participant is not None:
            last_inbound = participant.last_message_at
        if last_inbound is None or _now() - last_inbound > SERVICE_WINDOW:
            log.warning(
                "window_closed_freeform_refused",
                to=to_phone,
                last_inbound=str(last_inbound),
            )
            raise WindowClosedError(
                f"24h window closed for {to_phone} — send a template instead"
            )

    if text:
        text = _wa_format(text)
        if customer is not None and sent_by != "manager":
            text = sign_ai(text)

    # --- DotPe provider: delegate the actual send, keep everything else ---
    if settings.WHATSAPP_PROVIDER == "dotpe":
        if buttons:
            # DotPe's API has no interactive reply buttons (their docs:
            # text/media/location only). Callers must use numbered text
            # options on this provider.
            raise SendError("DotPe provider does not support interactive buttons")
        try:
            if template_name:
                build_template(template_name, template_params)  # validates
                wa_message_id = await dotpe.send_template(
                    to_phone,
                    template_name,
                    TEMPLATES[template_name]["language"],
                    template_params,
                )
                logged_text = _template_log(template_name, template_params)
            else:
                assert text is not None
                wa_message_id = await dotpe.send_text(to_phone, text)
                logged_text = text
        except dotpe.DotpeError as exc:
            raise SendError(str(exc)) from exc
        log.info("whatsapp_sent", to=to_phone, provider="dotpe", wa_message_id=wa_message_id)
        await _record_outbound(
        db, customer, staff, logged_text, wa_message_id, to_phone, sent_by, reply_to
    )
        return wa_message_id

    # --- build payload (Meta direct) ---
    payload: dict = {"messaging_product": "whatsapp", "to": to_phone.lstrip("+")}
    if reply_to:
        # Meta shows this message quoting the one being answered.
        payload["context"] = {"message_id": reply_to}
    if template_name:
        payload["type"] = "template"
        payload["template"] = build_template(template_name, template_params)
        logged_text = _template_log(template_name, template_params)
    elif buttons:
        payload["type"] = "interactive"
        payload["interactive"] = {
            "type": "button",
            "body": {"text": text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b.id, "title": b.title}}
                    for b in buttons
                ]
            },
        }
        logged_text = f"{text} [buttons: {', '.join(b.title for b in buttons)}]"
    else:
        payload["type"] = "text"
        payload["text"] = {"body": text, "preview_url": False}
        logged_text = text or ""

    # --- send (with one retry on transient failure) ---
    try:
        data = await _post_with_retry(payload, to_phone)
    except SendError as exc:
        if exc.transient and enqueue_on_fail:
            await _enqueue_outbound(
                db,
                to_phone,
                {
                    "text": text,
                    "buttons": [[b.id, b.title] for b in buttons] if buttons else None,
                    "template_name": template_name,
                    "template_params": template_params,
                    "sent_by": sent_by,
                },
            )
        raise
    wa_message_id: str = data["messages"][0]["id"]
    log.info(
        "whatsapp_sent",
        to=to_phone,
        kind=payload["type"],
        wa_message_id=wa_message_id,
    )

    await _record_outbound(
        db, customer, staff, logged_text, wa_message_id, to_phone, sent_by, reply_to
    )
    return wa_message_id


async def _record_outbound(
    db: AsyncSession,
    customer: Customer | None,
    staff: Staff | None,
    logged_text: str,
    wa_message_id: str,
    to_phone: str,
    sent_by: str,
    reply_to: str | None = None,
) -> None:
    """Record an outbound message in conversations (needs a participant row).

    status starts at "sent" — Meta's status webhook moves it to delivered
    and read, which is what the Inbox ticks show.
    """
    if customer or staff:
        db.add(
            Conversation(
                customer_id=customer.id if customer else None,
                staff_id=staff.id if staff else None,
                direction=Direction.OUTBOUND,
                message_text=logged_text,
                wa_message_id=wa_message_id,
                sent_by=sent_by,
                status="sent",
                reply_to_wamid=reply_to,
            )
        )
        await db.commit()
    else:
        # e.g. the manager's number — no customer/staff row exists.
        log.info("outbound_not_recorded_no_participant", to=to_phone)


async def send_image(
    db: AsyncSession,
    *,
    to_phone: str,
    file_path: str,
    mime_type: str,
    caption: str | None = None,
    local_url: str,
    sent_by: str = "manager",
) -> str:
    """Upload an image to Meta and send it. Free-form -> window required.

    local_url is our own serving path, stored in the conversation text as
    '[image:<local_url>] <caption>' so the Inbox can render it.
    """
    customer, staff = await _find_recipient(db, to_phone)
    participant = customer or staff
    last_inbound = participant.last_message_at if participant else None
    if last_inbound is None or _now() - last_inbound > SERVICE_WINDOW:
        raise WindowClosedError(f"24h window closed for {to_phone} — media needs an open window")

    headers = {"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"}
    media_url = f"https://graph.facebook.com/v21.0/{settings.WHATSAPP_PHONE_NUMBER_ID}/media"
    try:
        with open(file_path, "rb") as fh:
            async with httpx.AsyncClient(timeout=60) as client:
                up = await client.post(
                    media_url,
                    headers=headers,
                    data={"messaging_product": "whatsapp", "type": mime_type},
                    files={"file": (file_path.rsplit("\\", 1)[-1], fh, mime_type)},
                )
    except (OSError, httpx.TransportError) as exc:
        raise SendError(f"media upload failed: {exc}") from exc
    if up.status_code >= 400:
        raise SendError(f"media upload rejected: {up.status_code} {up.text[:150]}")
    media_id = up.json().get("id")
    if not media_id:
        raise SendError("media upload: no id in response")

    payload: dict = {
        "messaging_product": "whatsapp",
        "to": to_phone.lstrip("+"),
        "type": "image",
        "image": {"id": media_id, **({"caption": caption} if caption else {})},
    }
    data = await _post_with_retry(payload, to_phone)
    wa_message_id: str = data["messages"][0]["id"]
    log.info("whatsapp_image_sent", to=to_phone, wa_message_id=wa_message_id)
    logged = f"[image:{local_url}]" + (f" {caption}" if caption else "")
    await _record_outbound(db, customer, staff, logged, wa_message_id, to_phone, sent_by)
    return wa_message_id


async def download_media(media_id: str, dest_dir: str) -> str | None:
    """Fetch an inbound media file from Meta; returns saved filename or None.

    Never raises — inbound processing must survive a failed download.
    """
    import os
    import uuid as _uuid

    headers = {"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            meta = await client.get(
                f"https://graph.facebook.com/v21.0/{media_id}", headers=headers
            )
            if meta.status_code >= 400:
                log.warning("media_meta_failed", media_id=media_id, status=meta.status_code)
                return None
            info = meta.json()
            url = info.get("url")
            mime = info.get("mime_type", "image/jpeg")
            if not url:
                return None
            blob = await client.get(url, headers=headers)
            if blob.status_code >= 400:
                log.warning("media_download_failed", media_id=media_id, status=blob.status_code)
                return None
        ext = MEDIA_EXT.get(mime.split(";")[0].strip())
        if not ext:
            # unknown type: keep the subtype so the file is still openable
            ext = "." + (mime.split("/")[-1].split(";")[0].strip() or "bin")[:8]
        name = f"in-{_uuid.uuid4().hex}{ext}"
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, name), "wb") as fh:
            fh.write(blob.content)
        log.info("media_downloaded", media_id=media_id, file=name, bytes=len(blob.content))
        return name
    except Exception:
        log.exception("media_download_error", media_id=media_id)
        return None


async def _find_recipient(
    db: AsyncSession, phone: str
) -> tuple[Customer | None, Staff | None]:
    """Look up the phone in customers and staff. Staff wins if both match."""
    try:
        staff = (
            await db.execute(select(Staff).where(Staff.phone == phone))
        ).scalar_one_or_none()
        if staff:
            return None, staff
        customer = (
            await db.execute(select(Customer).where(Customer.phone == phone))
        ).scalar_one_or_none()
        return customer, None
    except Exception:
        log.exception("recipient_lookup_failed", phone=phone)
        raise


async def _post_with_retry(payload: dict, to_phone: str) -> dict:
    """POST to the Graph API. One retry on network error / 5xx / 429, then
    raise SendError — transient=True for anything worth retrying later."""
    headers = {"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"}
    last_error: str = "unknown"
    transient = False

    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(GRAPH_URL, headers=headers, json=payload)
        except httpx.TransportError as exc:
            last_error = f"network error: {exc}"
            transient = True
            log.warning("whatsapp_send_network_error", to=to_phone, attempt=attempt, error=str(exc))
            if attempt == 1:
                await asyncio.sleep(1.0)
                continue
            break

        if r.status_code < 400:
            return r.json()

        err = r.json().get("error", {})
        last_error = f"{r.status_code} code={err.get('code')} {err.get('message')}"
        if r.status_code == 429:
            transient = True
            retry_after = _parse_retry_after(r.headers.get("Retry-After"))
            log.warning("whatsapp_send_rate_limited", to=to_phone, attempt=attempt, retry_after=retry_after)
            if attempt == 1:
                await asyncio.sleep(min(retry_after, 5.0))
                continue
            break
        if r.status_code >= 500:
            transient = True
            log.warning("whatsapp_send_5xx", to=to_phone, attempt=attempt, error=last_error)
            if attempt == 1:
                await asyncio.sleep(1.0)
                continue
            break
        # other 4xx: our bug or Meta policy — retrying won't help.
        log.error("whatsapp_send_rejected", to=to_phone, error=last_error)
        break

    raise SendError(f"send to {to_phone} failed: {last_error}", transient=transient)


def _parse_retry_after(value: str | None) -> float:
    try:
        return max(float(value), 1.0) if value else 2.0
    except ValueError:
        return 2.0


_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_MD_BULLET_RE = re.compile(r"^(\s*)[-*]\s+", re.M)


def _wa_format(text: str) -> str:
    """LLM markdown -> WhatsApp formatting.

    WhatsApp has no markdown: '**bold**' renders literally as asterisks and
    '## heading' as hashes. Bold there is *single* asterisks.
    """
    text = _MD_BOLD_RE.sub(r"*\1*", text)
    text = _MD_HEADING_RE.sub("", text)
    text = _MD_BULLET_RE.sub(r"\1• ", text)
    return text


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- outbound retry queue -------------------------------------------------
# A transient send failure must never lose a customer message: it lands in
# outbound_queue and the scheduler drains it with exponential backoff.

MAX_OUTBOUND_ATTEMPTS = 8


async def _enqueue_outbound(db: AsyncSession, to_phone: str, payload: dict) -> None:
    """Queue a failed send for retry. Never raises — the original SendError
    is the caller's signal; this is purely a safety net on top."""
    from app.models import OutboundMessage

    try:
        await db.rollback()  # the failed send may have left the session dirty
        db.add(OutboundMessage(to_phone=to_phone, payload=payload))
        await db.commit()
        log.info("outbound_queued_for_retry", to=to_phone)
    except Exception:
        log.exception("outbound_enqueue_failed", to=to_phone)


async def drain_outbound_queue() -> int:
    """Scheduler entry: retry queued sends that are due. Returns sends made.

    Backoff doubles from 5 min up to 6 h; MAX_OUTBOUND_ATTEMPTS transient
    failures (or any permanent failure) dead-letters the row — visible in
    the table for forensics, never silently dropped.
    """
    from app.database import async_session_factory
    from app.models import OutboundMessage

    now = _now()
    sent = 0
    async with async_session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(OutboundMessage)
                    .where(
                        OutboundMessage.status == "queued",
                        # compare against the DB clock: next_attempt_at is
                        # written by the DB, and its clock can read a few ms
                        # ahead of ours — a just-queued row would then look
                        # "not due yet" and wait a whole tick for nothing
                        OutboundMessage.next_attempt_at <= func.now(),
                    )
                    .order_by(OutboundMessage.created_at)
                    .limit(30)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            p = row.payload or {}
            raw_buttons = p.get("buttons") or []
            buttons = [Button(b[0], b[1]) for b in raw_buttons] or None
            try:
                await send_message(
                    db,
                    to_phone=row.to_phone,
                    text=p.get("text"),
                    buttons=buttons,
                    template_name=p.get("template_name"),
                    template_params=p.get("template_params"),
                    sent_by=p.get("sent_by") or "bot",
                    enqueue_on_fail=False,
                )
            except WindowClosedError as exc:
                # A 24h window REOPENS the moment they message again, so a
                # closed window is a wait, not a failure. Keep retrying on
                # the normal backoff until the attempt cap.
                await db.rollback()
                row.attempts += 1
                row.last_error = f"window closed: {exc}"[:500]
                if row.attempts >= MAX_OUTBOUND_ATTEMPTS:
                    row.status = "dead"
                    log.info("outbound_dead_window_never_opened", to=row.to_phone)
                else:
                    row.next_attempt_at = now + timedelta(seconds=min(900 * row.attempts, 21_600))
            except SendError as exc:
                await db.rollback()
                row.attempts += 1
                row.last_error = str(exc)[:500]
                if not exc.transient or row.attempts >= MAX_OUTBOUND_ATTEMPTS:
                    row.status = "dead"
                    log.error("outbound_dead_lettered", to=row.to_phone, error=row.last_error)
                else:
                    backoff = min(300 * (2 ** row.attempts), 21_600)
                    row.next_attempt_at = now + timedelta(seconds=backoff)
            except Exception:
                await db.rollback()
                log.exception("outbound_drain_unexpected", to=row.to_phone)
                row.attempts += 1
                row.status = "dead" if row.attempts >= MAX_OUTBOUND_ATTEMPTS else "queued"
                row.next_attempt_at = now + timedelta(seconds=600)
            else:
                row.status = "sent"
                row.sent_at = now
                sent += 1
            db.add(row)
            await db.commit()
    return sent
