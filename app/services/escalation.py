"""Escalations: record what the bot couldn't handle, alert the humans.

raise_escalation() NEVER raises — an escalation failure must not break the
webhook or an in-flight reply. The DB row is the source of truth; the
WhatsApp alerts to manager (+ optional CC, Ravi) are best-effort.
"""

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Customer, Escalation
from app.services.messages import get_message
from app.services.whatsapp import SendError, WindowClosedError, send_message

log = structlog.get_logger()


async def raise_escalation(
    db: AsyncSession,
    *,
    question: str,
    customer: Customer | None = None,
    order_id: uuid.UUID | None = None,
) -> Escalation | None:
    """Store an escalation and ping the manager. Returns None on failure."""
    try:
        esc = Escalation(
            customer_id=customer.id if customer else None,
            order_id=order_id,
            question=question[:2000],
        )
        db.add(esc)
        await db.commit()
    except Exception:
        log.exception("escalation_store_failed")
        return None

    log.info(
        "escalation_raised",
        escalation_id=str(esc.id),
        customer=customer.phone if customer else None,
    )

    alert = get_message(
        "escalation_alert",
        customer_name=(customer.name if customer and customer.name else "naam nahi pata"),
        phone=customer.phone if customer else "-",
        question=question[:300],
    )
    for to_phone in (settings.MANAGER_PHONE, settings.ESCALATION_CC_PHONE):
        if not to_phone:
            continue
        try:
            await send_message(db, to_phone=to_phone, text=alert)
        except WindowClosedError:
            # Window shut -> pre-approved template (params must be one line).
            try:
                await send_message(
                    db, to_phone=to_phone,
                    template_name="kk_staff_alert",
                    template_params=[" ".join(alert.split())[:600]],
                )
            except SendError:
                log.warning("escalation_alert_not_sent", to=to_phone)
        except SendError:
            # Send failed — the dashboard still shows the row.
            log.warning("escalation_alert_not_sent", to=to_phone)
        except Exception:
            log.exception("escalation_alert_failed", to=to_phone)
    return esc
