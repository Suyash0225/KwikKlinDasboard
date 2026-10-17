"""Escalations: record what the bot couldn't handle, alert the humans.

raise_escalation() NEVER raises — an escalation failure must not break the
webhook or an in-flight reply. The DB row is the source of truth; the
WhatsApp alerts are best-effort and go only to admin/owner recipients (see team.py).
"""

import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Customer, Escalation, EscalationStatus
from app.services.messages import get_message
from app.services.whatsapp import SendError, WindowClosedError, send_message

log = structlog.get_logger()


async def _queue_alert(db: AsyncSession, to_phone: str, alert: str) -> bool:
    """Hold an undeliverable alert in the outbound queue. Never raises."""
    try:
        from app.services.whatsapp import _enqueue_outbound

        await _enqueue_outbound(db, to_phone, {"text": alert, "sent_by": "bot"})
        log.info("escalation_alert_queued", to=to_phone)
        return True
    except Exception:
        log.exception("escalation_alert_queue_failed", to=to_phone)
        return False


async def raise_escalation(
    db: AsyncSession,
    *,
    question: str,
    customer: Customer | None = None,
    order_id: uuid.UUID | None = None,
) -> Escalation | None:
    """Store an escalation and alert admin recipients. Returns None on failure."""
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

    if customer is not None:
        # Tell the customer a human has the case; do not leave them wondering
        # whether the complaint reached anyone. This is a best-effort message.
        acknowledgement = (
            f"Hi {customer.name or 'there'}, your message has been passed to the shop owner. "
            f"We aim to reply within {settings.ESCALATION_SLA_HOURS} hours. "
            "The AI assistant is paused while the team handles this."
        )
        try:
            await send_message(db, to_phone=customer.phone, text=acknowledgement, sent_by="bot")
        except WindowClosedError:
            await _queue_alert(db, customer.phone, acknowledgement)
        except SendError:
            log.exception("escalation_customer_ack_failed", customer=customer.phone)
        except Exception:
            log.exception("escalation_customer_ack_unexpected", customer=customer.phone)

    alert = get_message(
        "escalation_alert",
        customer_name=(customer.name if customer and customer.name else "naam nahi pata"),
        phone=customer.phone if customer else "-",
        question=question[:300],
    )
    # Escalation is an owner/admin alert, not a staff task reminder.
    # Keep recipient policy centralized in team.alert_recipients().
    from app.services import team

    recipients = await team.alert_recipients(db)
    log.info("escalation_alert_admin_only", count=len(recipients))
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



async def remind_overdue_escalations(
    db: AsyncSession, *, now: datetime | None = None
) -> int:
    """Send one second owner alert after the configured SLA; leave cases open."""
    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(hours=settings.ESCALATION_SLA_HOURS)
    rows = (
        await db.execute(
            select(Escalation)
            .where(
                Escalation.status == EscalationStatus.OPEN,
                Escalation.created_at <= cutoff,
                Escalation.owner_reminded_at.is_(None),
            )
            .order_by(Escalation.created_at)
            .with_for_update(skip_locked=True)
            .limit(50)
        )
    ).scalars().all()
    if not rows:
        return 0

    from app.services import team

    recipients = await team.alert_recipients(db)
    if not recipients:
        return 0
    sent_count = 0
    for esc in rows:
        customer = await db.get(Customer, esc.customer_id) if esc.customer_id else None
        alert = (
            "🔴 SLA REMINDER — customer complaint still open. "
            f"Waiting {max(0, int((current - esc.created_at).total_seconds() // 3600))} hours. "
            f"Customer: {customer.name if customer and customer.name else (customer.phone if customer else 'unknown')}. "
            f"Case: {esc.question[:240]}. Please reply and resolve the conversation."
        )
        delivered_or_queued = False
        for to_phone, _name in recipients:
            if not to_phone:
                continue
            try:
                await send_message(db, to_phone=to_phone, text=alert)
                delivered_or_queued = True
            except WindowClosedError:
                try:
                    await send_message(
                        db, to_phone=to_phone,
                        template_name="kk_staff_alert",
                        template_params=[" ".join(alert.split())[:600]],
                    )
                    delivered_or_queued = True
                except SendError:
                    delivered_or_queued = await _queue_alert(db, to_phone, alert) or delivered_or_queued
            except SendError:
                delivered_or_queued = await _queue_alert(db, to_phone, alert) or delivered_or_queued
            except Exception:
                log.exception("escalation_sla_reminder_failed", to=to_phone, escalation_id=str(esc.id))
        if delivered_or_queued:
            esc.owner_reminded_at = current
            sent_count += 1
    if sent_count:
        await db.commit()
    return sent_count
