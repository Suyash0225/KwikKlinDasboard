"""Safety guard for scripts that write synthetic demo data."""

from app.config import settings


def refuse_production_seed() -> None:
    """Fail before any demo database read/write when running in production."""
    if settings.ENVIRONMENT == "production":
        raise SystemExit(
            "Refusing demo seed operations when ENVIRONMENT=production."
        )
