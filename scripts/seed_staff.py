"""Seed the staff table with the real team. Idempotent — safe to re-run.

Run:
    .venv\\Scripts\\python.exe -m scripts.seed_staff

Staff facts (decided 2026-08-01, updated 2026-08-06 with the owner):
- Suyash (+918933871103) — ADMIN, the owner. Same number as MANAGER_PHONE,
  but he is a PERSON in the system too: the agent reports every order,
  payment and problem to him, and his WhatsApp messages carry manager
  powers (see app/services/team.py).
- Ravi  (+918707093136) — title "Assistant Manager". In bot terms his role is
  WASHER: he runs the wash/dry/iron workflow, gets the daily 10:00 check-in,
  and answers order-status questions.
- Ajit  (+919336393612) — DELIVERY. Every pickup and every delivery is asked
  of him by name; his answer ("sham tak") is stored on the task.

All three receive customer escalations (owner's rule, 06 Aug).
"""

import asyncio

import structlog
from sqlalchemy import select

from app.database import async_session_factory, engine
from app.models import Staff, StaffRole
from app.utils.logger import configure_logging
from app.utils.phone import normalize_phone

configure_logging()
log = structlog.get_logger()

STAFF: list[dict[str, str | StaffRole]] = [
    {"phone": "8933871103", "name": "Suyash", "role": StaffRole.ADMIN},
    {"phone": "8707093136", "name": "Ravi", "role": StaffRole.WASHER},
    {"phone": "9336393612", "name": "Ajit", "role": StaffRole.DELIVERY},
]


async def seed() -> None:
    # Pehle home tenant ka cache — warna naye rows tenant_id=NULL ke saath
    # jaate hain aur RLS ke andar dukaan ko dikhte hi nahi (scripts/_bootstrap.py).
    from scripts._bootstrap import prime

    await prime()
    async with async_session_factory() as session:
        for entry in STAFF:
            phone = normalize_phone(str(entry["phone"]))
            existing = (
                await session.execute(select(Staff).where(Staff.phone == phone))
            ).scalar_one_or_none()
            if existing:
                existing.name = str(entry["name"])
                existing.role = entry["role"]  # type: ignore[assignment]
                existing.is_active = True
                log.info("staff_updated", phone=phone, name=entry["name"])
            else:
                session.add(
                    Staff(phone=phone, name=str(entry["name"]), role=entry["role"])
                )
                log.info("staff_created", phone=phone, name=entry["name"], role=entry["role"].name)
        await session.commit()

        rows = (await session.execute(select(Staff).order_by(Staff.name))).scalars().all()
        for s in rows:
            log.info("staff_row", name=s.name, phone=s.phone, role=s.role.name, active=s.is_active)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
