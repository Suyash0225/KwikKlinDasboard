"""Seed a starter rate card (idempotent — skips rows that already exist).

Run:  .venv\\Scripts\\python.exe -m scripts.seed_rates
Rates are STARTING points — owner edits them in Settings.
"""

import asyncio
from decimal import Decimal

import structlog
from sqlalchemy import select

from app.database import async_session_factory, engine
from app.models import Rate
from app.utils.logger import configure_logging

configure_logging()
log = structlog.get_logger()

DEFAULT_RATES: list[tuple[str, str, str, str]] = [
    # (service, garment, unit, rate)
    ("Dry Clean", "Shirt", "pc", "80"),
    ("Dry Clean", "Pant", "pc", "90"),
    ("Dry Clean", "Kurta", "pc", "80"),
    ("Dry Clean", "Saree", "pc", "150"),
    ("Dry Clean", "Suit 2pc", "pc", "220"),
    ("Dry Clean", "Suit 3pc", "pc", "250"),
    ("Dry Clean", "Blazer", "pc", "150"),
    ("Dry Clean", "Sherwani", "pc", "300"),
    ("Dry Clean", "Lehenga", "pc", "350"),
    ("Wash & Iron", "Shirt", "pc", "25"),
    ("Wash & Iron", "Pant", "pc", "30"),
    ("Wash & Iron", "Kurta", "pc", "25"),
    ("Wash & Iron", "T-shirt", "pc", "20"),
    ("Wash & Iron", "Jeans", "pc", "35"),
    ("Sirf Iron", "Shirt", "pc", "12"),
    ("Sirf Iron", "Pant", "pc", "12"),
    ("Sirf Iron", "Saree", "pc", "30"),
    ("Dry Clean", "Razai/Blanket", "pc", "250"),
    ("Dry Clean", "Curtain", "pc", "100"),
    ("Wash & Fold (kg)", "", "kg", "60"),
    ("Wash & Iron (kg)", "", "kg", "80"),
]


async def seed() -> None:
    async with async_session_factory() as s:
        existing = {
            (r.service, r.garment)
            for r in (await s.execute(select(Rate))).scalars().all()
        }
        added = 0
        for service, garment, unit, rate in DEFAULT_RATES:
            if (service, garment) in existing:
                continue
            s.add(Rate(service=service, garment=garment, unit=unit, rate=Decimal(rate)))
            added += 1
        await s.commit()
        log.info("rates_seeded", added=added, skipped=len(DEFAULT_RATES) - added)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
