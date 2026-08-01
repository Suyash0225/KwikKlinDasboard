"""Seed the staff table with the real team. Idempotent — safe to re-run.

Run:
    .venv\\Scripts\\python.exe -m scripts.seed_staff

Staff facts (decided 2026-08-01 with the owner):
- Ravi  (+918707093136) — title "Assistant Manager". In bot terms his role is
  WASHER: he runs the wash/dry/iron workflow, gets the daily 10:00 check-in,
  and answers order-status questions. He ALSO receives escalation CCs —
  that part is Phase 3 logic, not a column here.
- Ajit  (+919336393612) — DELIVERY. The bot addresses him as "Superman"
  (owner's instruction), so that is his stored name.
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
    {"phone": "8707093136", "name": "Ravi", "role": StaffRole.WASHER},
    {"phone": "9336393612", "name": "Superman", "role": StaffRole.DELIVERY},
]


async def seed() -> None:
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
