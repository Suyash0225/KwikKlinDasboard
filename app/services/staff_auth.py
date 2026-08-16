"""Staff panel ka login aur uske pehre.

Teen pehre, teenon SERVER par — UI par kuch chhupana bharosa nahi hai:

1. **Tenant**: kaun si dukaan, ye sirf TOKEN se aata hai. Request body,
   query ya header se kabhi nahi — warna koi bhi doosri dukaan ka
   business_id bhej kar uska data maang leta.
2. **Role**: washerman ko sirf uska kaam, delivery wale ko uska. Manager
   ko poori dukaan, par settings/billing nahi.
3. **Plan**: kaunsi suvidha khuli hai ye plans.py tay karta hai — wahi ek
   jagah jise backend aur UI dono padhte hain.

Password owner/manager deta hai; staff khud account nahi bana sakta.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import Cookie, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Staff, StaffRole, StaffSession
from app.models.tenant import Tenant
from app.services import plans
from app.services.auth import hash_password, note_failure, throttled, verify_password

log = structlog.get_logger()

STAFF_COOKIE = "kk_staff"
SESSION_HOURS = 12          # ek shift; roz login = kho gaye phone ka ilaaj

# Kaunsa role kya dekhta hai. Ye ek hi jagah likha hai — API aur UI dono
# yahi padhte hain, isliye "panel par dikh raha tha par API ne mana kar
# diya" wali gadbad nahi ho sakti.
MANAGER_ROLES = (StaffRole.MANAGER, StaffRole.SUPERVISOR, StaffRole.ADMIN)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def set_password(db: AsyncSession, staff: Staff, password: str, *, temp: bool) -> None:
    """Password set/reset. Plain kabhi store ya log nahi hota."""
    if len(password) < 6:
        raise ValueError("password must be at least 6 characters")
    staff.password_hash = hash_password(password)
    staff.must_change_password = temp
    db.add(staff)
    await db.commit()
    log.info("staff_password_set", staff=staff.name, temp=temp)


async def login(
    db: AsyncSession, *, phone: str, password: str, ip: str, user_agent: str = ""
) -> tuple[str, Staff]:
    """Phone+password se login. Galat par hamesha ek jaisa 401 — kaunsa
    number maujood hai ye batana bhi ek leak hai."""
    if throttled(ip):
        raise HTTPException(status_code=429, detail="Too many attempts — try again shortly")
    rows = (
        await db.execute(select(Staff).where(Staff.phone == phone, Staff.is_active))
    ).scalars().all()
    staff = next((s for s in rows if s.password_hash and verify_password(password, s.password_hash)), None)
    if staff is None:
        note_failure(ip)
        log.info("staff_login_failed", phone=phone[-4:])
        raise HTTPException(status_code=401, detail="Wrong number or password")

    token = secrets.token_urlsafe(32)
    db.add(
        StaffSession(
            staff_id=staff.id,
            # Tenant SAAF-SAAF likha jata hai. Login ke waqt context system
            # hota hai (abhi pata hi nahi tha kis dukaan ka aadmi hai), aur
            # us haalat mein naya row HOME tenant par stamp ho jata — phir
            # doosri dukaan ka staff apne hi session se pehchana na jaata.
            tenant_id=staff.tenant_id,
            token_hash=_token_hash(token),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS),
            user_agent=(user_agent or "")[:200],
        )
    )
    staff.last_login_at = datetime.now(timezone.utc)
    db.add(staff)
    await db.commit()
    log.info("staff_login", staff=staff.name, role=staff.role.name)
    return token, staff


async def logout(db: AsyncSession, token: str) -> None:
    if not token:
        return
    row = (
        await db.execute(
            select(StaffSession).where(StaffSession.token_hash == _token_hash(token))
        )
    ).scalar_one_or_none()
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        db.add(row)
        await db.commit()


async def revoke_all(db: AsyncSession, staff_id) -> int:
    """Us aadmi ke saare session khatam — phone kho jaye ya nikal diya jaye."""
    rows = (
        await db.execute(
            select(StaffSession).where(
                StaffSession.staff_id == staff_id, StaffSession.revoked_at.is_(None)
            )
        )
    ).scalars().all()
    now = datetime.now(timezone.utc)
    for r in rows:
        r.revoked_at = now
        db.add(r)
    await db.commit()
    return len(rows)


async def staff_for_token(db: AsyncSession, token: str) -> Staff | None:
    """Token -> staff. Expired/revoked/band aadmi = None."""
    if not token:
        return None
    row = (
        await db.execute(
            select(StaffSession).where(StaffSession.token_hash == _token_hash(token))
        )
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    if row.expires_at <= datetime.now(timezone.utc):
        return None
    staff = await db.get(Staff, row.staff_id)
    if staff is None or not staff.is_active:
        return None
    return staff


class StaffPrincipal:
    """Ek request ka staff: kaun, kis dukaan ka, kis plan par."""

    def __init__(self, staff: Staff, tenant: Tenant | None):
        self.staff = staff
        self.tenant = tenant
        self.plan = (tenant.plan if tenant else plans.DEFAULT_PLAN)

    @property
    def is_manager(self) -> bool:
        """Manager ki taakat Premium se shuru hoti hai.

        Basic mein sab ek jaise "staff" hain (plan ki shart) — wahan koi
        bhi doosre ka kaam nahi dekh sakta, chahe DB mein role kuch bhi
        likha ho. Isse downgrade bhi apne aap sahi behave karta hai.
        """
        return self.staff.role in MANAGER_ROLES and self.has("staff_roles")

    def has(self, feature: str) -> bool:
        return plans.feature_on(self.plan, feature)

    @property
    def features(self) -> list[str]:
        return sorted(plans.get(self.plan).features)


async def current_staff(
    request: Request,
    kk_staff: str = Cookie(default=""),
    db: AsyncSession = Depends(get_db),
) -> StaffPrincipal:
    """Har staff-API ka darwaza. Tenant TOKEN se aata hai, aur kahin se nahi."""
    staff = await staff_for_token(db, kk_staff)
    if staff is None:
        raise HTTPException(status_code=401, detail="Please log in")
    tenant = await db.get(Tenant, staff.tenant_id) if staff.tenant_id else None
    # Plan hi tay karta hai ki panel khula hai ya nahi. Basic mein bhi
    # khula hai; band sirf tab jab vendor ne feature hata diya ho.
    if not plans.feature_on(tenant.plan if tenant else plans.DEFAULT_PLAN, "staff_panel"):
        raise HTTPException(status_code=402, detail="The staff panel is not included in this plan")
    # Band/locked account: panel bhi band. Kaam ka data phir bhi safe hai.
    if tenant is not None and tenant.status in ("locked", "suspended", "cancelled"):
        raise HTTPException(status_code=402, detail="This account is closed — please speak to the owner")
    request.state.staff_id = str(staff.id)
    return StaffPrincipal(staff, tenant)


def require_manager(p: StaffPrincipal = Depends(current_staff)) -> StaffPrincipal:
    """Sirf manager/owner. Delivery boy ko poori dukaan kabhi nahi dikhti."""
    if not p.is_manager:
        raise HTTPException(status_code=403, detail="Only a manager can do this")
    return p


def require_staff_feature(feature: str):
    """Plan ka pehra — wahi keys jo plans.py mein hain.

    Feature band ho to 402 aur "kis plan mein milega" — panel isi se
    upgrade ka ishara dikhata hai.
    """

    def _dep(p: StaffPrincipal = Depends(current_staff)) -> StaffPrincipal:
        if p.has(feature):
            return p
        need = plans.plan_with_feature(feature)
        name = plans.get(need).name if need else "upar wale plan"
        raise HTTPException(
            status_code=402,
            detail=f"This is available on the {name} plan — ask your owner",
        )

    _dep.__name__ = f"require_staff_feature_{feature}"
    return _dep
