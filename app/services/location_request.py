"""📍 Grahak ki WhatsApp location — ek baar maango, aaye to pin save karo.

Pata likha hota hai, pin nahi. Pin do raste se aata hai: staff darwaze par
GPS se (staff app), ya grahak khud WhatsApp par "Send location" kare. Ye
module doosra raasta hai:

- ask_once(): pickup book hote hi / kapde ready hote hi, agar pin nahi hai
  aur pehle kabhi nahi poocha — ek chhota message. Sirf EK baar, kabhi
  dobara nahi (location_asked_at). STOP/inactive grahak ko kabhi nahi.
- save_shared_pin(): webhook se — "[location:lat,lng] ..." aaya to pin
  grahak par likho (by="customer"), pata khali ho to wo bhi bhar do.
"""

from datetime import datetime, timezone

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Customer
from app.services import geo
from app.services.messages import get_message
from app.services.whatsapp import SendError, WindowClosedError, send_message

log = structlog.get_logger()


def should_ask(customer: Customer) -> bool:
    return (
        customer is not None
        and customer.is_active
        and not customer.opted_out
        and customer.location_asked_at is None
        and not geo.payload(customer)["has_location"]
    )


async def ask_once(db: AsyncSession, customer: Customer, *, why: str = "") -> bool:
    """Ek baar request bhejo. Bheji to True. Kabhi raise nahi karta."""
    try:
        if not should_ask(customer):
            return False
        # Pehle nishaan, phir send: WhatsApp dheema ho ya fail ho, dobara
        # kabhi na poochein — ek request kaafi hai, do tang karti hain.
        customer.location_asked_at = datetime.now(timezone.utc)
        db.add(customer)
        await db.commit()
        try:
            await send_message(
                db, to_phone=customer.phone, text=get_message("location_request"),
                intent="transactional", outbound_kind="order_status", sent_by="bot",
            )
        except (SendError, WindowClosedError) as exc:
            log.info("location_request_not_sent", phone=customer.phone, error=str(exc)[:120])
            return False
        log.info("location_requested", phone=customer.phone, why=why)
        return True
    except Exception:
        log.exception("location_request_failed")
        return False


async def save_shared_pin(db: AsyncSession, customer: Customer, lat, lng, place: str = "") -> bool:
    """WhatsApp se aayi location grahak par. Pata khali ho to naam/pata bhi."""
    try:
        if not geo.set_location(customer, lat=lat, lng=lng, by="customer"):
            return False
        if place and not (customer.address or "").strip():
            customer.address = place[:500]
        db.add(customer)
        await db.commit()
        log.info("customer_pin_from_whatsapp", phone=customer.phone)
        return True
    except Exception:
        log.exception("customer_pin_save_failed")
        await db.rollback()
        return False
