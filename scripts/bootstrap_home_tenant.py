"""Is deployment ki apni dukaan ko 'home tenant' banao. Idempotent.

Kyun zaroori hai: `/admin` dashboard sirf HOME tenant ke users ko data deta
hai. Agar home tay nahi hai, to code sabse purane tenant ko home maan leta
hai — aur agar pehla tenant koi naya signup nikla, to uske paas is dukaan ka
data chala jaayega. Isliye ise ek baar chalana zaroori hai.

Run:
    .venv\\Scripts\\python.exe -m scripts.bootstrap_home_tenant

Owner ka email aur password argument se ya prompt se:
    python -m scripts.bootstrap_home_tenant owner@email.com mypassword123
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select

from app.config import settings
from app.database import async_session_factory, engine
from app.models import ROLE_OWNER, TENANT_ACTIVE, Tenant, User
from app.services import app_settings, auth, tenant_context
from app.utils.logger import configure_logging
from app.utils.phone import normalize_phone

configure_logging()
log = structlog.get_logger()

HOME_SLUG = "kwik-klin"


async def bootstrap(email: str, password: str) -> None:
    async with async_session_factory() as db:
        tenant = (
            await db.execute(select(Tenant).where(Tenant.slug == HOME_SLUG))
        ).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(
                slug=HOME_SLUG,
                shop_name=settings.SHOP_NAME,
                owner_name="Suyash",
                owner_phone=normalize_phone(settings.MANAGER_PHONE),
                owner_email=email,
                city="Varanasi",
                plan="growth",              # apni dukaan par koi limit nahi
                status=TENANT_ACTIVE,
                current_period_end=datetime.now(timezone.utc) + timedelta(days=3650),
                setup_fee_paid=True,
                onboarding_done=True,
                notes="Ye deployment isi dukaan ka hai (home tenant).",
            )
            db.add(tenant)
            await db.flush()
            log.info("home_tenant_created", slug=HOME_SLUG)
        else:
            # Row pehle se hai — par uska matlab "set ho chuka" nahi. Ye row
            # multi_tenant_foundation migration ne banayi hoti hai, aur wahan
            # plan column apni default 'starter' par rehta hai. Bootstrap sirf
            # CREATE par entitlements deta tha, isliye apni hi dukaan starter
            # par atki rehti thi: reports aur CSV export 402, order limit
            # dashboard par lagti hui. Isliye ye har baar pakka karo.
            tenant.plan = "growth"
            tenant.status = TENANT_ACTIVE
            tenant.setup_fee_paid = True
            tenant.onboarding_done = True
            if (
                tenant.current_period_end is None
                or tenant.current_period_end < datetime.now(timezone.utc)
            ):
                tenant.current_period_end = datetime.now(timezone.utc) + timedelta(days=3650)
            log.info("home_tenant_exists", slug=HOME_SLUG, entitlements="ensured")

        user = (
            await db.execute(
                select(User).where(User.tenant_id == tenant.id, User.email == email)
            )
        ).scalar_one_or_none()
        if user is None:
            db.add(
                User(
                    tenant_id=tenant.id,
                    name="Suyash",
                    email=email,
                    phone=tenant.owner_phone,
                    password_hash=auth.hash_password(password),
                    role=ROLE_OWNER,
                )
            )
            log.info("home_owner_created", email=email)
        else:
            user.password_hash = auth.hash_password(password)
            user.is_active = True
            user.role = ROLE_OWNER
            log.info("home_owner_password_reset", email=email)
        await db.commit()

        # Ab home pakka — koi naya signup 'sabse purana tenant' bankar
        # is dukaan ka data nahi le sakta.
        #
        # Ye likhna context ke ANDAR hona zaroori hai. settings_kv.tenant_id
        # ab NOT NULL hai, aur naye row par wo id `before_flush` event
        # `cached_home_tenant_id()` se leta hai — jo abhi khali hai, kyunki
        # home ko tay karne wali setting yahi hai jo likhi ja rahi hai.
        # Bina iske script anda-murgi par girti thi:
        #   null value in column "tenant_id" of relation "settings_kv"
        # yaani ek bilkul nayi DB bootstrap ho hi nahi sakti thi.
        async with tenant_context.as_tenant(tenant.id, tenant.owner_phone):
            await app_settings.set_value(db, "home_tenant_slug", HOME_SLUG)
        log.info("home_tenant_locked", slug=HOME_SLUG)

    await engine.dispose()
    print(f"\n  Home tenant : {HOME_SLUG} ({settings.SHOP_NAME})")
    print(f"  Login email : {email}")
    print("  Login yahan : http://127.0.0.1:8000/#login\n")


if __name__ == "__main__":
    email = sys.argv[1] if len(sys.argv) > 1 else input("Owner email: ").strip()
    password = sys.argv[2] if len(sys.argv) > 2 else input("Password (8+): ").strip()
    if len(password) < 8:
        raise SystemExit("Password kam se kam 8 characters ka hona chahiye")
    asyncio.run(bootstrap(email.lower(), password))
