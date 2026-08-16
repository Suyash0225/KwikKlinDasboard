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
    tenant_id=None,
) -> None:
    """Fire-and-forget audit row. Swallows every failure (logged only).

    tenant_id: KIS tenant ke baare mein action hai. Vendor/control actions
    ke liye zaroori — unka request-context home hota hai, isliye auto-stamp
    galat tenant par chala jaata (explicit value ko before_flush kabhi
    overwrite nahi karta)."""
    try:
        # Explicit (doosre tenant ka) tenant_id request-ctx ke RLS WITH CHECK
        # par girta — vendor actions system context mein likhte hain.
        from app.services import tenant_context

        ctx = (
            tenant_context.current_tenant_id.set(None)
            if tenant_id is not None else None
        )
        try:
            async with async_session_factory() as s:
                row = AuditLog(
                    actor_role=actor_role,
                    actor=(actor or "")[:80] or None,
                    action=action[:60],
                    args=args,
                    result=(result or "")[:2000] or None,
                    ok=ok,
                )
                if tenant_id is not None:
                    row.tenant_id = tenant_id
                s.add(row)
                await s.commit()
        finally:
            if ctx is not None:
                tenant_context.current_tenant_id.reset(ctx)
    except Exception:
        log.exception("audit_write_failed", action=action)
