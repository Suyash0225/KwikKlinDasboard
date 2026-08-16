"""Recharge credits ki validity — "X din tak" wale top-ups ka hisaab.

Balance tenants.ai_credits/wa_credits par rehta hai (quota hot-path fast).
Ye module sirf EXPIRY sambhalta hai: nightly sweep un lots ko dekhta hai
jinki validity guzar chuki hai aur jo abhi tak process nahi hue, aur unka
bacha hua hissa balance se kaat deta hai.

Hisaab ka niyam (jaan-boojh kar simple, aur client ke haq mein):
- Kharch hamesha PURANE (jaldi expire hone wale) lot se maana jaata hai.
- Expiry par kaata utna hi jaata hai jitna us lot ka abhi tak istemal
  NAHI hua — aur balance kabhi 0 se neeche nahi jaata.
- Har cut ledger mein minus entry banata hai (reason: "expired"), taaki
  "mere credits kahan gaye" ka jawab hamesha record mein ho.
"""

from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CreditLedger
from app.models.tenant import Tenant

log = structlog.get_logger()


async def expire_due_credits(db: AsyncSession) -> dict:
    """Guzar chuki validity wale lots ko band karo. Returns {ai, wa} cut."""
    now = datetime.now(timezone.utc)
    lots = (
        await db.execute(
            select(CreditLedger).where(
                CreditLedger.amount > 0,
                CreditLedger.expires_at.isnot(None),
                CreditLedger.expires_at <= now,
                CreditLedger.expired_at.is_(None),
            ).order_by(CreditLedger.expires_at)
        )
    ).scalars().all()
    cut = {"ai": 0, "wa": 0}
    for lot in lots:
        tenant = await db.get(Tenant, lot.tenant_id)
        if tenant is None:
            lot.expired_at = now
            continue
        col = "ai_credits" if lot.kind == "ai" else "wa_credits"
        balance = getattr(tenant, col)
        # Bacha hua hissa = is lot ka amount, par balance se zyada kabhi nahi
        # (agar client pehle hi kharch kar chuka hai to kaatne ko kuch nahi).
        take = min(lot.amount, balance)
        lot.expired_at = now
        if take <= 0:
            continue
        setattr(tenant, col, balance - take)
        db.add(
            CreditLedger(
                tenant_id=tenant.id, kind=lot.kind, amount=-take,
                balance_after=balance - take,
                reason=f"expired (validity {lot.expires_at:%d %b %Y})",
                created_by="system",
            )
        )
        cut[lot.kind] += take
        log.info("credits_expired", tenant=tenant.slug, kind=lot.kind, amount=take)
    if lots:
        await db.commit()
    return cut
