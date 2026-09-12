"""Purane tenant_id=NULL rows ko home tenant par laga do. Idempotent.

Kab chahiye: agar kabhi seed/maintenance script bina home-cache ke chali
thi (scripts/_bootstrap.py ka comment dekho), to us waqt bane rows
tenant_id=NULL ke saath pade hain — RLS ke andar wo kisi ko dikhte nahi.
Ye script unhe home dukaan par stamp kar deti hai.

SIRF single-shop / home data ke liye. Agar DB mein kai dukaanein hain to
pehle dekh lo ki ye NULL rows kiske hain — ye script sabko HOME par daal
degi. Chalane se pehle count dikhata hai aur poochta hai.

Run:
    python -m scripts.fix_unstamped_rows          # sirf report
    python -m scripts.fix_unstamped_rows --apply  # sudhaar
"""

import asyncio
import sys

import structlog
from sqlalchemy import text

from app.database import async_session_factory
from app.utils.logger import configure_logging

configure_logging()
log = structlog.get_logger()


async def main() -> None:
    apply = "--apply" in sys.argv
    from app.services import tenant_context

    home = await tenant_context.get_home_tenant_id()
    if home is None:
        raise SystemExit("Koi home tenant nahi — pehle bootstrap_home_tenant chalao.")

    async with async_session_factory() as db:
        tables = (
            await db.execute(
                text(
                    "SELECT c.table_name FROM information_schema.columns c "
                    "JOIN pg_class p ON p.relname = c.table_name "
                    "JOIN pg_namespace n ON n.oid = p.relnamespace AND n.nspname = 'public' "
                    "WHERE c.column_name = 'tenant_id' ORDER BY 1"
                )
            )
        ).scalars().all()
        total = 0
        for t in tables:
            n = (
                await db.execute(text(f"SELECT count(*) FROM {t} WHERE tenant_id IS NULL"))
            ).scalar_one()
            if not n:
                continue
            total += n
            print(f"{t:24} {n} row(s) bina tenant ke")
            if apply:
                await db.execute(
                    text(f"UPDATE {t} SET tenant_id = :h WHERE tenant_id IS NULL"),
                    {"h": home},
                )
        if apply:
            await db.commit()

    if not total:
        print("Sab rows par tenant hai — kuch karne ko nahi.")
    elif apply:
        print(f"\n{total} row(s) home tenant par laga di gayin.")
    else:
        print(f"\nKul {total} row(s). Sudhaarne ke liye: --apply")


if __name__ == "__main__":
    asyncio.run(main())
