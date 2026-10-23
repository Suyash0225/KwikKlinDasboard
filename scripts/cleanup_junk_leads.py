"""Purani faltu leads saaf karo — jo bina service interest ke ban gayi thin.

Pehle har WhatsApp message bhejne wala lead ban jaata tha ("hi", "kaise ho"
bhi). Ab sirf asli enquiry lead banti hai; ye script pichhla kachra hatata hai.

Faltu lead = sab sach hon:
  - source "whatsapp" (website/QR/poster/manual leads ko haath nahi lagate)
  - stage CONTACTED ya INTERESTED (CONVERTED/LOST/NEW wahi rehte hain)
  - customer ka ek bhi order nahi
  - customer ke kisi bhi incoming message mein service interest nahi
    (wahi classifier jo webhook naye messages par chalata hai)

Classifier fail ho to lead RAKHI jaati hai — shak mein kuch nahi hatata.
Lead delete nahi hoti: stage LOST, follow-up band, notes mein wajah. Galti
ho to stage wapas badal do.

Run:
    python -m scripts.cleanup_junk_leads           # sirf dikhata hai
    python -m scripts.cleanup_junk_leads --apply   # sach mein LOST karta hai
"""

import argparse
import asyncio
from datetime import date

import structlog
from sqlalchemy import select

from app.database import async_session_factory
from app.models import Conversation, Customer, Direction, Lead, Order
from app.services import tenant_context
from app.services.inbound_guard import classify_inbound_message, is_customer_lead
from app.utils.logger import configure_logging

configure_logging()
log = structlog.get_logger()

_OPEN_STAGES = ("CONTACTED", "INTERESTED")


def _classifier_failed(result: dict) -> bool:
    return str(result.get("reason") or "").startswith(
        ("classifier unavailable", "invalid classifier result")
    )


async def _verdict(db, lead: Lead) -> str:
    """'junk', 'keep' ya 'unsure' (classifier nahi chala)."""
    customer = (
        await db.execute(select(Customer).where(Customer.phone == lead.phone))
    ).scalar_one_or_none()
    if customer is None:
        return "unsure"
    has_order = (
        await db.execute(select(Order.id).where(Order.customer_id == customer.id).limit(1))
    ).scalar_one_or_none()
    if has_order is not None:
        return "keep"
    texts = (
        await db.execute(
            select(Conversation.message_text)
            .where(
                Conversation.customer_id == customer.id,
                Conversation.direction == Direction.INBOUND,
            )
            .order_by(Conversation.created_at.asc())
            .limit(30)
        )
    ).scalars().all()
    texts = [t for t in texts if (t or "").strip()] or [lead.items_text or ""]
    unsure = False
    for text in texts:
        result = await classify_inbound_message(text)
        if is_customer_lead(result):
            return "keep"
        unsure = unsure or _classifier_failed(result)
    return "unsure" if unsure else "junk"


async def _clean_tenant(tid, slug: str, apply: bool) -> dict:
    counts = {"junk": 0, "keep": 0, "unsure": 0}
    note = f"[{date.today().isoformat()} cleanup] bina service interest ke lead bani thi"
    async with tenant_context.as_tenant(tid):
        async with async_session_factory() as db:
            leads = (
                await db.execute(
                    select(Lead).where(
                        Lead.source == "whatsapp", Lead.stage.in_(_OPEN_STAGES)
                    )
                )
            ).scalars().all()
            for lead in leads:
                verdict = await _verdict(db, lead)
                counts[verdict] += 1
                if verdict != "junk":
                    continue
                print(f"  [{slug}] faltu: {lead.name or '-'} {lead.phone} — {(lead.items_text or '')[:60]!r}")
                if apply:
                    lead.stage = "LOST"
                    lead.next_followup_at = None
                    lead.notes = f"{lead.notes}\n{note}" if lead.notes else note
            if apply:
                await db.commit()
    log.info("junk_leads_cleanup", tenant=slug, apply=apply, **counts)
    return counts


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="sach mein LOST mark karo")
    args = parser.parse_args()

    total = {"junk": 0, "keep": 0, "unsure": 0}
    for tid, slug, _ in await tenant_context.active_tenants():
        for k, n in (await _clean_tenant(tid, slug, args.apply)).items():
            total[k] += n
    action = "LOST mark ki" if args.apply else "LOST hongi (--apply se)"
    print(
        f"\n  faltu {total['junk']} ({action}), asli {total['keep']} rakhi, "
        f"shak wali {total['unsure']} rakhi"
    )


if __name__ == "__main__":
    asyncio.run(main())
