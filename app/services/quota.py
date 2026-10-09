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

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal

import structlog
from sqlalchemy import func, select, text

from app.services import plans, tenant_context
from app.utils.dates import IST

log = structlog.get_logger()


class QuotaExceeded(Exception):
    def __init__(self, meter: str, used: int | float, limit: int | float, plan_name: str):
        self.meter, self.used, self.limit, self.plan_name = meter, used, limit, plan_name
        super().__init__(
            f"{meter} limit reached ({used}/{limit} this month on {plan_name} plan) — upgrade karein"
        )


def _month_start() -> datetime:
    now_ist = datetime.now(IST)
    return now_ist.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)


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


def estimated_cost_usd(model: str, input_tokens: int, output_tokens: int, rates: dict) -> Decimal:
    """Estimated USD using the same per-million-token rate card as the dashboard."""
    rate = rates.get(model) or {}
    if not rate:
        available = [r for r in rates.values() if isinstance(r, dict)]
        rate = {
            "in": max((float(r.get("in", 0) or 0) for r in available), default=0),
            "out": max((float(r.get("out", 0) or 0) for r in available), default=0),
        }
    incoming = Decimal(str(rate.get("in", 0) or 0))
    outgoing = Decimal(str(rate.get("out", 0) or 0))
    return (
        Decimal(max(0, int(input_tokens))) * incoming
        + Decimal(max(0, int(output_tokens))) * outgoing
    ) / Decimal(1_000_000)


async def check_ai_quota(
    *, model: str | None = None, input_text: str = "",
    max_output_tokens: int = 0, image: bool = False,
) -> None:
    """Enforce per-tenant call limits and configured monthly USD budget before provider I/O."""
    from app.database import async_session_factory
    from app.models import LlmUsage
    from app.services import app_settings

    async with async_session_factory() as db:
        tid, limits, plan_name = await _tenant_and_limits(db)
        if tid is None:
            return

        # Check the monetary budget first so a rejected request never burns
        # an AI usage credit as a side effect.
        monthly_budget = Decimal(str(await app_settings.get(db, "llm_monthly_budget_usd") or 0))
        if monthly_budget > 0:
            rates = await app_settings.get(db, "llm_rates") or {}
            usage_rows = (
                await db.execute(
                    select(LlmUsage.model, LlmUsage.input_tokens, LlmUsage.output_tokens)
                    .where(LlmUsage.at >= _month_start(), LlmUsage.tenant_id == tid)
                )
            ).all()
            spent = sum(
                (estimated_cost_usd(row.model, row.input_tokens, row.output_tokens, rates)
                 for row in usage_rows),
                start=Decimal("0"),
            )
            # Reserve a conservative upper bound for this request before provider
            # I/O. Image input gets an additional allowance. Zero disables the cap.
            estimated_input_tokens = (len(input_text) + 3) // 4 + (1000 if image else 0)
            reserve = estimated_cost_usd(
                model or "", estimated_input_tokens, max(0, max_output_tokens), rates
            )
            if spent + reserve > monthly_budget:
                log.warning(
                    "ai_monthly_budget_exceeded", spent_usd=round(float(spent), 6),
                    reserve_usd=round(float(reserve), 6), budget_usd=float(monthly_budget),
                )
                raise QuotaExceeded(
                    "AI monthly budget USD",
                    round(float(spent), 4),
                    round(float(monthly_budget), 4),
                    plan_name,
                )

        call_limit = limits["ai_usage_limit"]
        if call_limit is not None:
            used = await ai_calls_this_month(db, tid)
            if used >= call_limit and not await _spend_credit(db, tid, "ai"):
                log.warning("ai_quota_exceeded", used=used, limit=call_limit)
                raise QuotaExceeded("AI usage", used, call_limit, plan_name)
        usage_rows = (
            await db.execute(
                select(LlmUsage.model, LlmUsage.input_tokens, LlmUsage.output_tokens)
                .where(LlmUsage.at >= _month_start(), LlmUsage.tenant_id == tid)
            )
        ).all()
        spent = sum(
            (estimated_cost_usd(row.model, row.input_tokens, row.output_tokens, rates)
             for row in usage_rows),
            start=Decimal("0"),
        )

        # Reserve a conservative upper bound for this request before calling
        # the provider. Image input gets an additional allowance. The setting
        # is opt-in (0 disables it); the dashboard rate card is the source of
        # truth, so pricing changes do not require a migration.
        estimated_input_tokens = (len(input_text) + 3) // 4 + (1000 if image else 0)
        reserve = estimated_cost_usd(
            model or "", estimated_input_tokens, max(0, max_output_tokens), rates
        )
        if spent + reserve > monthly_budget:
            log.warning(
                "ai_monthly_budget_exceeded", spent_usd=round(float(spent), 6),
                reserve_usd=round(float(reserve), 6), budget_usd=float(monthly_budget),
            )
            raise QuotaExceeded(
                "AI monthly budget USD",
                round(float(spent), 4),
                round(float(monthly_budget), 4),
                plan_name,
            )


@asynccontextmanager
async def ai_quota_guard(
    *, model: str | None = None, input_text: str = "",
    max_output_tokens: int = 0, image: bool = False,
):
    """Serialize budgeted LLM calls per tenant until token usage is recorded.

    PostgreSQL session-level advisory locks are held across provider I/O.
    Usage recording commits in its own session before this lock is released,
    so a concurrent request rechecks the updated monthly spend instead of
    spending against the same stale total.
    """
    from app.database import async_session_factory
    from app.services import app_settings

    async with async_session_factory() as lock_db:
        tid, _, _ = await _tenant_and_limits(lock_db)
        budget = Decimal(str(await app_settings.get(lock_db, "llm_monthly_budget_usd") or 0))
        if tid is None or budget <= 0:
            await check_ai_quota(
                model=model, input_text=input_text,
                max_output_tokens=max_output_tokens, image=image,
            )
            yield
            return

        lock_key = f"kwikklin-ai-budget:{tid}"
        await lock_db.execute(
            text("SELECT pg_advisory_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )
        await lock_db.commit()
        try:
            await check_ai_quota(
                model=model, input_text=input_text,
                max_output_tokens=max_output_tokens, image=image,
            )
            yield
        finally:
            await lock_db.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:lock_key, 0))"),
                {"lock_key": lock_key},
            )
            await lock_db.commit()


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
