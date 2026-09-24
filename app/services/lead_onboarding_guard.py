"""Deterministic guard for incomplete unknown-lead onboarding.

The customer agent must collect name first, then address. Three inbound
turns without an address pause the customer-facing agent for human follow-up.
"""
from datetime import datetime, timezone

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, Customer, Lead, Order, OrderStatus
from app.services import audit

log = structlog.get_logger()


async def enforce(
    db: AsyncSession,
    customer: Customer,
    text: str,
    reply: str,
    *,
    sandbox: bool = False,
) -> str:
    if sandbox:
        return reply

    try:
        active = (
            await db.execute(
                select(Order.id).where(
                    Order.customer_id == customer.id,
                    Order.status.notin_((OrderStatus.DELIVERED, OrderStatus.CANCELLED)),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if active is not None:
            return reply

        lead = (
            await db.execute(select(Lead).where(Lead.phone == customer.phone))
        ).scalar_one_or_none()
        if lead is not None and lead.stage not in {"CONTACTED", "INTERESTED"}:
            return reply

        if not (customer.name or "").strip():
            return "Welcome to Kwik Klin! 😊 May I know your name, please?"

        if (customer.address or "").strip():
            await audit.record(
                actor_role="system",
                actor=customer.phone,
                action="lead_address_collected",
                args={"address_present": True},
                result="onboarding address supplied",
            )
            return reply

        latest_collected = (
            await db.execute(
                select(AuditLog.at)
                .where(
                    AuditLog.actor == customer.phone,
                    AuditLog.action == "lead_address_collected",
                )
                .order_by(AuditLog.at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        q = select(func.count(AuditLog.id)).where(
            AuditLog.actor == customer.phone,
            AuditLog.action == "lead_address_attempt",
        )
        if latest_collected is not None:
            q = q.where(AuditLog.at > latest_collected)
        attempts = int((await db.execute(q)).scalar_one() or 0) + 1

        await audit.record(
            actor_role="customer",
            actor=customer.phone,
            action="lead_address_attempt",
            args={"attempt": attempts, "message": text[:300]},
            result="address still missing",
        )

        if attempts < 3:
            return (
                "Thank you! 🙏 Please share your full address with a nearby landmark, "
                "so we can arrange the pickup properly."
            )

        customer.agent_paused = True
        customer.agent_paused_at = datetime.now(timezone.utc)
        await db.commit()
        log.warning(
            "new_lead_address_limit_reached",
            phone=customer.phone,
            attempts=attempts,
        )

        try:
            from app.services.team import primary_admin_phone
            from app.services.whatsapp import send_message

            await send_message(
                db,
                to_phone=await primary_admin_phone(db),
                text=(
                    "*⚠️ AI PAUSED — LEAD ONBOARDING*\n"
                    "━━━━━━━━━━━━━━\n"
                    f"*Name:* {customer.name or 'Unknown'}\n"
                    f"*Phone:* {customer.phone}\n"
                    "*Reason:* Customer did not provide address after 3 AI requests.\n"
                    "*Action:* Human follow-up required.\n"
                    "━━━━━━━━━━━━━━"
                ),
                sent_by="bot",
            )
        except Exception:
            log.exception("new_lead_address_pause_alert_failed", phone=customer.phone)

        await audit.record(
            actor_role="system",
            actor=customer.phone,
            action="lead_address_pause",
            args={"attempts": attempts},
            result="agent paused after 3 missed address requests",
        )
        return ""
    except Exception:
        log.exception("new_lead_onboarding_guard_failed", phone=customer.phone)
        return reply
