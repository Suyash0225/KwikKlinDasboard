"""Escalations: record what the bot couldn't handle, alert the humans.

raise_escalation() NEVER raises — an escalation failure must not break the
webhook or an in-flight reply. The DB row is the source of truth; the
WhatsApp alerts are best-effort and go to the whole team (see team.py).
"""

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Customer, Escalation
from app.services.messages import get_message
from app.services.whatsapp import SendError, WindowClosedError, send_message

log = structlog.get_logger()


async def _queue_alert(db: AsyncSession, to_phone: str, alert: str) -> None:
    """Hold an undeliverable alert in the outbound queue. Never raises."""
    try:
        from app.services.whatsapp import _enqueue_outbound

        await _enqueue_outbound(db, to_phone, {"text": alert, "sent_by": "bot"})
        log.info("escalation_alert_queued", to=to_phone)
    except Exception:
        log.exception("escalation_alert_queue_failed", to=to_phone)


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
    # Owner's rule (06 Aug): a customer problem goes to the WHOLE team —
    # the admins and every active staff member — not just two numbers in
    # .env. Whoever is free answers first; nobody can say "mujhe pata nahi".
    from app.services import team

    recipients = await team.alert_recipients(db)
    log.info("escalation_alert_fanout", count=len(recipients))
    for to_phone, _name in recipients:
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
                # Template unapproved too — park it so it goes out the
                # moment their window reopens. An alert the owner never
                # sees is how a real inquiry got lost.
                await _queue_alert(db, to_phone, alert)
        except SendError:
            await _queue_alert(db, to_phone, alert)
        except Exception:
            log.exception("escalation_alert_failed", to=to_phone)
    return esc
