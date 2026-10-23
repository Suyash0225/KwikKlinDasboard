"""Purane bills par rate card ka daam (`list_rate`) bharo — taaki unpar bhi
"Rate ₹250 → ₹230, you saved ₹20" dikhe.

Naye bill bante hi list_rate apne aap likhte hain (services/item_savings.py).
Ye script un bills ke liye hai jo usse pehle bane.

Galat bachat kabhi nahi: rate card ka daam sirf tab bharte hain jab wo row
bill banne ke BAAD badli na ho (updated_at <= bill ka created_at). Rate
baad mein badla ho to us din ka daam pata nahi — wo line khaali chhod dete
hain. Pehle se bhara list_rate kabhi nahi chhoota. Idempotent.

Run:
    python -m scripts.backfill_list_rates           # sirf dikhata hai
    python -m scripts.backfill_list_rates --apply   # sach mein likhta hai
"""

import argparse
import asyncio

import structlog
from sqlalchemy import select

from app.database import async_session_factory
from app.models import Order, Rate
from app.services import tenant_context, urgent
from app.services.item_savings import item_key, line_saving, rate_key
from app.utils.logger import configure_logging

configure_logging()
log = structlog.get_logger()


async def _backfill_tenant(tid, slug: str, apply: bool) -> dict:
    counts = {"bills": 0, "lines": 0, "bills_with_saving": 0, "skipped_rate_changed": 0}
    async with tenant_context.as_tenant(tid):
        async with async_session_factory() as db:
            card = {
                rate_key(s, g): (r, updated)
                for s, g, r, updated in (
                    await db.execute(
                        select(Rate.service, Rate.garment, Rate.rate, Rate.updated_at)
                        .where(Rate.is_active.is_(True))
                    )
                ).all()
            }
            orders = (
                await db.execute(select(Order).where(Order.total_amount.isnot(None)))
            ).scalars().all()
            for order in orders:
                items = [dict(it) if isinstance(it, dict) else it for it in order.items or []]
                touched = False
                for it in items:
                    if not isinstance(it, dict) or it.get("list_rate") is not None:
                        continue
                    if it.get("kind") == urgent.KIND or it.get("rate") is None:
                        continue
                    hit = card.get(item_key(it))
                    if hit is None:
                        continue
                    rate, updated = hit
                    if order.created_at is None or updated is None or updated > order.created_at:
                        counts["skipped_rate_changed"] += 1
                        continue
                    it["list_rate"] = float(rate)
                    counts["lines"] += 1
                    touched = True
                if not touched:
                    continue
                counts["bills"] += 1
                saved = sum(line_saving(it) for it in items if isinstance(it, dict))
                if saved:
                    counts["bills_with_saving"] += 1
                    print(f"  [{slug}] {order.order_number}: ₹{saved:.0f} bachat dikhegi")
                if apply:
                    order.items = items   # naya list — JSONB change SQLAlchemy ko dikhe
            if apply:
                await db.commit()
    log.info("list_rates_backfill", tenant=slug, apply=apply, **counts)
    return counts


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="sach mein likho")
    args = parser.parse_args()

    total = {"bills": 0, "lines": 0, "bills_with_saving": 0, "skipped_rate_changed": 0}
    for tid, slug, _ in await tenant_context.active_tenants():
        for k, n in (await _backfill_tenant(tid, slug, args.apply)).items():
            total[k] += n
    verb = "bhare" if args.apply else "bharenge (--apply se)"
    print(
        f"\n  {total['bills']} bills ki {total['lines']} lines mein rate card ka daam {verb}; "
        f"{total['bills_with_saving']} bills par bachat dikhegi. "
        f"{total['skipped_rate_changed']} lines chhodi (rate baad mein badla tha)."
    )


if __name__ == "__main__":
    asyncio.run(main())
