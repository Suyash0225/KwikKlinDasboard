"""Who the shop's people are, and who must hear about what.

One place answers three questions the whole app kept answering differently:
- who is an ADMIN (owner side)? -> admins(), is_admin_phone()
- who must hear when a CUSTOMER has a problem? -> alert_recipients()
  (owner's rule, 06 Aug: escalations reach Suyash, Ravi AND Ajit — not just
  the manager and one CC number)
- who does pickups/deliveries? -> delivery_staff()

Every function degrades instead of raising: settings.MANAGER_PHONE is always
in the admin list even if the staff table is empty or the DB hiccups, so a
problem can never end up with nobody.
"""

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Staff, StaffRole
from app.services.whatsapp import SendError, WindowClosedError, send_message
from app.utils.phone import normalize_phone

log = structlog.get_logger()


def _norm(phone: str) -> str:
    try:
        return normalize_phone(phone)
    except ValueError:
        return (phone or "").strip()


async def active_staff(db: AsyncSession) -> list[Staff]:
    """Everyone on the payroll, admins included. [] if the query fails."""
    try:
        return list(
            (await db.execute(select(Staff).where(Staff.is_active))).scalars().all()
        )
    except Exception:
        log.exception("active_staff_failed")
        return []


async def admins(db: AsyncSession) -> list[Staff]:
    """Staff rows with the ADMIN role (the owner and anyone he adds)."""
    return [s for s in await active_staff(db) if s.role is StaffRole.ADMIN]


async def admin_phones(db: AsyncSession) -> list[str]:
    """Admin numbers, manager first. Never empty — MANAGER_PHONE is always in."""
    out = [_norm(settings.MANAGER_PHONE)]
    for st in await admins(db):
        p = _norm(st.phone)
        if p and p not in out:
            out.append(p)
    return [p for p in out if p]


async def is_admin_phone(db: AsyncSession, phone: str) -> bool:
    """Does this number carry owner-level powers?"""
    return _norm(phone) in await admin_phones(db)


async def alert_recipients(db: AsyncSession) -> list[tuple[str, str]]:
    """(phone, name) for the people a CUSTOMER PROBLEM must reach.

    Owner's rule (06 Aug, revised): the admins and the washerman (Ravi) —
    the people who can actually answer a customer. The delivery boy is NOT
    on this list: he was getting every "urgent delivery?" escalation as a
    template alert he could do nothing about. He is told when the owner
    tells him ("Ajit ko bol do ..."), and by his own pickup/delivery jobs.
    """
    seen: dict[str, str] = {}
    for st in await active_staff(db):
        if st.role is StaffRole.DELIVERY:
            continue
        p = _norm(st.phone)
        if p:
            seen.setdefault(p, st.name or p)
    for phone, label in (
        (settings.MANAGER_PHONE, "Manager"),
        (settings.ESCALATION_CC_PHONE, "CC"),
    ):
        p = _norm(phone)
        if p:
            seen.setdefault(p, label)
    return list(seen.items())


async def notify_admins(db: AsyncSession, text: str, *, skip_phone: str = "") -> int:
    """Tell the owner side something. Returns how many messages went out.

    skip_phone: don't echo an event back to the person who just caused it.
    Never raises — an FYI must not break the action it is reporting.
    """
    skip = _norm(skip_phone) if skip_phone else ""
    sent = 0
    for phone in await admin_phones(db):
        if phone == skip:
            continue
        try:
            await send_message(db, to_phone=phone, text=text[:1500])
            sent += 1
        except WindowClosedError:
            try:
                await send_message(
                    db, to_phone=phone,
                    template_name="kk_staff_alert",
                    template_params=[" ".join(text.split())[:600]],
                )
                sent += 1
            except SendError:
                log.info("admin_notify_window_closed", to=phone)
        except SendError:
            log.info("admin_notify_failed", to=phone)
        except Exception:
            # An FYI must never poison the caller's transaction: a failed
            # flush here would make the order/payment work that follows blow
            # up on a session that is already dead.
            log.exception("admin_notify_error", to=phone)
            try:
                await db.rollback()
            except Exception:
                log.exception("admin_notify_rollback_failed")
    return sent


async def delivery_staff(db: AsyncSession) -> Staff | None:
    """Who collects and delivers: the configured default, else the only
    active DELIVERY person. None when the shop has not named anyone."""
    from app.services import app_settings

    try:
        phone = (await app_settings.get(db, "default_delivery_phone") or "").strip()
    except Exception:
        phone = ""
    rows = await active_staff(db)
    if phone:
        st = next((s for s in rows if s.phone == phone), None)
        if st is not None:
            return st
    boys = [s for s in rows if s.role is StaffRole.DELIVERY]
    return boys[0] if len(boys) == 1 else None
