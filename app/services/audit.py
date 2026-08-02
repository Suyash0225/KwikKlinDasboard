"""Agent audit trail — every action the agent/scheduler takes, in the DB.

Uses its OWN short session so a logging failure (or a caller rollback) can
never interfere with business writes. NEVER raises.
"""

import structlog

from app.database import async_session_factory
from app.models import AuditLog

log = structlog.get_logger()


async def record(
    *,
    actor_role: str,
    actor: str | None,
    action: str,
    args: dict | None = None,
    result: str | None = None,
    ok: bool = True,
) -> None:
    """Fire-and-forget audit row. Swallows every failure (logged only)."""
    try:
        async with async_session_factory() as s:
            s.add(
                AuditLog(
                    actor_role=actor_role,
                    actor=(actor or "")[:80] or None,
                    action=action[:60],
                    args=args,
                    result=(result or "")[:2000] or None,
                    ok=ok,
                )
            )
            await s.commit()
    except Exception:
        log.exception("audit_write_failed", action=action)
