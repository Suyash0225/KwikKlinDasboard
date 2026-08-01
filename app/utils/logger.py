"""structlog configuration — JSON logs with timestamp and level.

Call configure_logging() once at startup (main.py does this), then anywhere:

    import structlog
    log = structlog.get_logger()
    log.info("order_created", order_number="LDY-...", customer_phone="+91...")

Prefer event names + key-value fields over prose sentences — they are
grep-able and machine-parseable.
"""

import logging
import sys

import structlog

from app.config import settings


def configure_logging() -> None:
    """Configure structlog AND the stdlib root logger (uvicorn, sqlalchemy
    etc. log through stdlib — we want one level knob for everything)."""
    level = getattr(logging, settings.LOG_LEVEL)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )
