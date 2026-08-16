"""Per-tenant usage metering + plan-limit enforcement.

Do meters, dono calendar-month ke:
- AI usage        = llm_usage table ke rows (har LLM call ek row likhta hai)
- WhatsApp usage  = conversations mein OUTBOUND rows

Enforcement points:
- llm_client._generate_with_fallback -> check_ai_quota() (LLM call se PEHLE)
- whatsapp.send_message              -> check_wa_quota() (send se PEHLE)

Limit khatam -> QuotaExceeded. LLM path use LLMUnavailable mein badal deta
hai (agent ka degrade path already handle karta hai); WhatsApp path SendError
(permanent) banata hai jo queue mein dead-letter hota hai — kabhi silent
drop nahi. Counting tenant-scoped hai (tenant_context ka effective tenant).
"""

from datetime import datetime, timezone

import structlog
from sqlalchemy import func, select

from app.services import plans, tenant_context

log = structlog.get_logger()


class QuotaExceeded(Exception):
    def __init__(self, meter: str, used: int, limit: int, plan_name: str):
        self.meter, self.used, self.limit, self.plan_name = meter, used, limit, plan_name
        super().__init__(
            f"{meter} limit reached ({used}/{limit} this month on {plan_name} plan) — upgrade karein"
        )


def _month_start() -> datetime:
    return datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )


async def _tenant_and_limits(db):
    """Effective tenant (request ka, warna home) + uske ASLI limits
    (plan + vendor overrides) + plan name."""
    from app.models.tenant import Tenant

    tid = tenant_context.current_tenant_id.get() or await tenant_context.get_home_tenant_id()
    if tid is None:
        p = plans.get(plans.DEFAULT_PLAN)
        return None, plans.effective_limits(None), p.name
    t = await db.get(Tenant, tid)
    return (
        tid,
        plans.effective_limits(t),
        plans.get(t.plan if t else plans.DEFAULT_PLAN).name,
    )


async def ai_calls_this_month(db, tenant_id) -> int:
    from app.models import LlmUsage

    return (
        await db.execute(
            select(func.count())
            .select_from(LlmUsage)
            .where(LlmUsage.at >= _month_start(), LlmUsage.tenant_id == tenant_id)
        )
    ).scalar_one()


async def wa_messages_this_month(db, tenant_id) -> int:
    """BILLABLE messages is mahine ke (utility + marketing templates).

    Customer ke apne message ka jawab (service, 24h window) Meta par FREE
    hai — wo yahan kabhi nahi ginta, warna client us cheez par block hota
    jispe humara ek rupaya bhi kharch nahi hua.
    """
    from app.models import Conversation
    from app.models.enums import Direction

    return (
        await db.execute(
            select(func.count())
            .select_from(Conversation)
            .where(
                Conversation.created_at >= _month_start(),
                Conversation.direction == Direction.OUTBOUND,
                Conversation.tenant_id == tenant_id,
                Conversation.billing_category.in_(("utility", "marketing")),
            )
        )
    ).scalar_one()


async def _spend_credit(db, tenant_id, kind: str) -> bool:
    """Limit ke BAAD ka ek message/AI call: recharge se ek credit kaato.

    Balance 0 -> False (caller QuotaExceeded uthata hai). Atomic UPDATE
    (balance > 0 check SQL mein hi) — do parallel sends balance ko
    minus mein nahi le ja sakte.
    """
    from sqlalchemy import text as _sql

    col = "ai_credits" if kind == "ai" else "wa_credits"
    r = await db.execute(
        _sql(
            f"UPDATE tenants SET {col} = {col} - 1 "
            f"WHERE id = :tid AND {col} > 0 RETURNING {col}"
        ),
        {"tid": str(tenant_id)},
    )
    left = r.scalar_one_or_none()
    if left is None:
        return False
    await db.commit()
    log.info("credit_spent", kind=kind, tenant=str(tenant_id), left=left)
    return True


async def check_ai_quota() -> None:
    """LLM call se pehle. Raises QuotaExceeded. Apna session kholta hai."""
    from app.database import async_session_factory

    async with async_session_factory() as db:
        tid, limits, plan_name = await _tenant_and_limits(db)
        if tid is None or limits["ai_usage_limit"] is None:
            return
        used = await ai_calls_this_month(db, tid)
        if used < limits["ai_usage_limit"]:
            return
        # Limit khatam — recharge bacha ho to usse chalao
        if await _spend_credit(db, tid, "ai"):
            return
    log.warning("ai_quota_exceeded", used=used, limit=limits["ai_usage_limit"])
    raise QuotaExceeded("AI usage", used, limits["ai_usage_limit"], plan_name)


async def check_wa_quota(db) -> None:
    """WhatsApp send se pehle. Raises QuotaExceeded."""
    tid, limits, plan_name = await _tenant_and_limits(db)
    if tid is None or limits["whatsapp_message_limit"] is None:
        return
    used = await wa_messages_this_month(db, tid)
    if used < limits["whatsapp_message_limit"]:
        return
    # Limit khatam — recharge bacha ho to usse chalao
    if await _spend_credit(db, tid, "wa"):
        return
    log.warning(
        "wa_quota_exceeded", used=used, limit=limits["whatsapp_message_limit"]
    )
    raise QuotaExceeded(
        "WhatsApp message", used, limits["whatsapp_message_limit"], plan_name
    )
