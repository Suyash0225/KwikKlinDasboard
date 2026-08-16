"""Set-password invite links — onboarding bina kisi plaintext password ke.

Rule jo kabhi nahi tootta: PASSWORD NA HUM BANATE HAIN, NA STORE KARTE
HAIN, NA BHEJTE HAIN. Sirf ek one-time link jaata hai; password user khud
apne browser mein set karta hai (scrypt-hashed, hamesha ki tarah).

Flow:
  create_invite() -> user row (sentinel hash, login-impossible) + invite row
                     (token ka sha256) -> raw link EK baar caller ko
  GET /invite/<token>  -> set-password page (static/invite.html)
  POST /api/invite/accept {token, password} -> hash set, invite used,
                     session cookie — user seedha andar
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Invite, User

log = structlog.get_logger()

INVITE_DAYS = 7
# verify_password() ise KABHI pass nahi karega (scrypt format hi nahi hai) —
# jab tak invite accept nahi hota, is user se login ho hi nahi sakta.
PENDING_SENTINEL = "invite$pending"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_invite(
    db: AsyncSession,
    *,
    tenant_id,
    email: str,
    name: str,
    role: str,
    invited_by: str,
    phone: str | None = None,
    reset: bool = False,
) -> tuple[User, str]:
    """User (agar nahi hai) + invite banao. Returns (user, invite_path).

    invite_path = "/invite/<raw-token>" — caller isse base URL ke saath
    jodkar dikhata/bhejta hai. Raw token DB mein nahi hota.
    Dobara bulaya to purana pending user reuse hota hai (naya token,
    purana invite bekaar — yahi resend hai).
    """
    email = email.strip().lower()
    user = (
        await db.execute(
            select(User).where(User.tenant_id == tenant_id, User.email == email)
        )
    ).scalar_one_or_none()
    if user is None:
        user = User(
            tenant_id=tenant_id,
            name=name.strip() or email,
            email=email,
            phone=(phone or "").strip() or None,
            password_hash=PENDING_SENTINEL,
            role=role,
        )
        db.add(user)
        await db.flush()
    elif user.password_hash != PENDING_SENTINEL and not reset:
        # Already set password — invite ka matlab nahi (reset=True chhoot hai:
        # password-reset link, jo accept par purana hash overwrite karta hai)
        raise ValueError("Ye user pehle se active hai (password set ho chuka hai)")

    token = secrets.token_urlsafe(32)
    db.add(
        Invite(
            tenant_id=tenant_id,
            email=email,
            role=role,
            token_hash=_hash(token),
            invited_by=invited_by[:80],
            expires_at=datetime.now(timezone.utc) + timedelta(days=INVITE_DAYS),
        )
    )
    await db.commit()
    log.info("invite_created", email=email, role=role, by=invited_by)
    return user, f"/invite/{token}"


async def accept_invite(db: AsyncSession, *, token: str, password: str) -> User:
    """Token + naya password -> user active. Raises ValueError with reason."""
    from app.services import auth

    inv = (
        await db.execute(select(Invite).where(Invite.token_hash == _hash(token or "")))
    ).scalar_one_or_none()
    if inv is None:
        raise ValueError("Ye invite link galat hai")
    if inv.used_at is not None:
        raise ValueError("Ye invite pehle hi use ho chuka hai — login karein")
    if inv.expires_at < datetime.now(timezone.utc):
        raise ValueError("Invite expire ho gaya — nayi invite mangwayein")

    user = (
        await db.execute(
            select(User).where(User.tenant_id == inv.tenant_id, User.email == inv.email)
        )
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise ValueError("Ye account ab maujood nahi hai")

    user.password_hash = auth.hash_password(password)  # min-length yahin check hoti hai
    user.must_change_password = False
    inv.used_at = datetime.now(timezone.utc)
    await db.commit()
    log.info("invite_accepted", email=user.email, role=user.role)
    from app.services import audit

    await audit.record(
        actor_role="user", actor=user.email, action="invite_accepted",
        args={"role": user.role},
    )
    return user
