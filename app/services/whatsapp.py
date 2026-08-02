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
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import httpx
import structlog
from sqlalchemy import select
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
MAX_BUTTONS = 3
MAX_BUTTON_TITLE = 20  # WhatsApp hard limit


class Button(NamedTuple):
    """One interactive reply button. id carries context, e.g. 'order:<uuid>:done'."""

    id: str
    title: str


class SendError(Exception):
    """A message could not be sent. Caller decides how to degrade."""


class WindowClosedError(SendError):
    """Free-form send attempted outside the 24h window. Use a template."""


async def send_message(
    db: AsyncSession,
    *,
    to_phone: str,
    text: str | None = None,
    buttons: list[Button] | None = None,
    template_name: str | None = None,
    template_params: list[str] | None = None,
    sent_by: str = "bot",
) -> str:
    """Send one WhatsApp message. Returns Meta's wa_message_id.

    Exactly one mode:
      text                      -> free-form text        (window required)
      text + buttons            -> interactive buttons    (window required)
      template_name [+ params]  -> template               (always allowed)

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
                logged_text = f"[template:{template_name}]"
            else:
                assert text is not None
                wa_message_id = await dotpe.send_text(to_phone, text)
                logged_text = text
        except dotpe.DotpeError as exc:
            raise SendError(str(exc)) from exc
        log.info("whatsapp_sent", to=to_phone, provider="dotpe", wa_message_id=wa_message_id)
        await _record_outbound(db, customer, staff, logged_text, wa_message_id, to_phone, sent_by)
        return wa_message_id

    # --- build payload (Meta direct) ---
    payload: dict = {"messaging_product": "whatsapp", "to": to_phone.lstrip("+")}
    if template_name:
        payload["type"] = "template"
        payload["template"] = build_template(template_name, template_params)
        logged_text = f"[template:{template_name}]"
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
    data = await _post_with_retry(payload, to_phone)
    wa_message_id: str = data["messages"][0]["id"]
    log.info(
        "whatsapp_sent",
        to=to_phone,
        kind=payload["type"],
        wa_message_id=wa_message_id,
    )

    await _record_outbound(db, customer, staff, logged_text, wa_message_id, to_phone, sent_by)
    return wa_message_id


async def _record_outbound(
    db: AsyncSession,
    customer: Customer | None,
    staff: Staff | None,
    logged_text: str,
    wa_message_id: str,
    to_phone: str,
    sent_by: str,
) -> None:
    """Record an outbound message in conversations (needs a participant row)."""
    if customer or staff:
        db.add(
            Conversation(
                customer_id=customer.id if customer else None,
                staff_id=staff.id if staff else None,
                direction=Direction.OUTBOUND,
                message_text=logged_text,
                wa_message_id=wa_message_id,
                sent_by=sent_by,
            )
        )
        await db.commit()
    else:
        # e.g. the manager's number — no customer/staff row exists.
        log.info("outbound_not_recorded_no_participant", to=to_phone)


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
    """POST to the Graph API. One retry on network error / 5xx, then raise."""
    headers = {"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"}
    last_error: str = "unknown"

    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(GRAPH_URL, headers=headers, json=payload)
        except httpx.TransportError as exc:
            last_error = f"network error: {exc}"
            log.warning("whatsapp_send_network_error", to=to_phone, attempt=attempt, error=str(exc))
            if attempt == 1:
                await asyncio.sleep(1.0)
                continue
            break

        if r.status_code < 400:
            return r.json()

        err = r.json().get("error", {})
        last_error = f"{r.status_code} code={err.get('code')} {err.get('message')}"
        if r.status_code >= 500:
            log.warning("whatsapp_send_5xx", to=to_phone, attempt=attempt, error=last_error)
            if attempt == 1:
                await asyncio.sleep(1.0)
                continue
            break
        # 4xx: our bug or Meta policy — retrying won't help.
        log.error("whatsapp_send_rejected", to=to_phone, error=last_error)
        break

    raise SendError(f"send to {to_phone} failed: {last_error}")


def _now() -> datetime:
    return datetime.now(timezone.utc)
