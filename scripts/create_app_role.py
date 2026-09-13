"""App ke liye ek aisa DB role jo RLS ko bypass na kar sake.

KYUN:

Har tenant table par `ENABLE + FORCE ROW LEVEL SECURITY` laga hai. Schema
dekh kar sab theek lagta hai — par FORCE sirf TABLE OWNER ko baandhta hai.
SUPERUSER par RLS laagu hoti hi nahi. Aur docker-compose ka POSTGRES_USER
(`laundry`) Postgres ka bootstrap superuser hai, isliye default setup mein
defence-in-depth ki teesri parat maujood hi nahi hoti — isolation akele
ORM ke filter par tik jaati hai.

Naapa hua farq, ek hi row set aur ek hi GUC par:

    superuser      SET app.tenant_id = A  ->  "A ka grahak", "B ka grahak"
    NOSUPERUSER    SET app.tenant_id = A  ->  "A ka grahak"

KAAM KAISE BAANTA HAI:

    laundry (owner, superuser)  ->  migrations. DDL ke liye chahiye.
    kk_app  (NOSUPERUSER)       ->  app. RLS iske upar sach mein lagti hai.

Isliye ye MIGRATION nahi, script hai: role cluster-level cheez hai
(database-level nahi), aur uska password migration history mein likha
jaana galat hoga.

CHALAO:

    python -m scripts.create_app_role                 # password apne aap
    APP_DB_PASSWORD=... python -m scripts.create_app_role

Idempotent hai. Dobara chalane par sirf grants taaza hote hain.

BAAD MEIN: chhapa hua APP_DATABASE_URL .env mein daalo. Wo set ho to app
usse judegi; na ho to DATABASE_URL se, yaani kuch todta nahi.
"""

import asyncio
import os
import re
import secrets
import sys
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

ROLE = "kk_app"


async def main() -> None:
    password = os.environ.get("APP_DB_PASSWORD") or secrets.token_urlsafe(24)
    owner_url = settings.DATABASE_URL

    # Owner se judo — role banane ke liye superuser chahiye.
    engine = create_async_engine(owner_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            me = (await conn.execute(text("SELECT current_user"))).scalar_one()
            if me == ROLE:
                raise SystemExit(
                    f"DATABASE_URL pehle se {ROLE} par hai. Ye script OWNER se "
                    "chalni chahiye (docker-compose wala 'laundry')."
                )

            exists = (
                await conn.execute(
                    text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": ROLE}
                )
            ).scalar_one_or_none()

            if exists:
                print(f"  role {ROLE} pehle se hai — password waisa hi, grants taaza kar raha hoon")
                password = None
            else:
                # CREATE ROLE DDL hai — bind parameters leta hi nahi,
                # password inline karna padta hai. Yahi wo jagah hai jahan
                # SQL injection ghusti hai, isliye charset par pehra:
                # token_urlsafe se bana password hamesha safe hai, par env
                # se aaya hua kuch bhi ho sakta hai.
                if not re.fullmatch(r"[A-Za-z0-9_\-.~]{12,128}", password):
                    raise SystemExit(
                        "APP_DB_PASSWORD mein sirf A-Z a-z 0-9 _ - . ~ chalega "
                        "(12-128 akshar). Quote/backslash wala password yahan "
                        "inline karna padta, jo surakshit nahi."
                    )
                await conn.execute(
                    text(f"CREATE ROLE {ROLE} LOGIN NOSUPERUSER NOCREATEDB "
                         f"NOCREATEROLE PASSWORD '{password}'")
                )
                print(f"  role {ROLE} bana (NOSUPERUSER)")

            db = urlsplit(owner_url).path.lstrip("/")
            for stmt in (
                f"GRANT CONNECT ON DATABASE {db} TO {ROLE}",
                f"GRANT USAGE ON SCHEMA public TO {ROLE}",
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {ROLE}",
                f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {ROLE}",
                # Aane wali tables ke liye BHI. Iske bina agli migration ki
                # nayi table par app ko permission denied milega — aur wo
                # deploy ke baad, chalti hui app par phatega.
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {ROLE}",
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                f"GRANT USAGE, SELECT ON SEQUENCES TO {ROLE}",
            ):
                await conn.execute(text(stmt))
            print("  grants lag gaye (aane wali tables ke liye bhi)")

            # Sabse zaroori jaanch: ye role sach mein RLS ke neeche hai?
            bypass = (
                await conn.execute(
                    text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = :r"),
                    {"r": ROLE},
                )
            ).scalar_one()
            if bypass:
                raise SystemExit(f"  {ROLE} abhi bhi RLS bypass karta hai — ruk raha hoon")
    finally:
        await engine.dispose()

    if password is None:
        print("\n  Password pehle se hai. Bhool gaye ho to:")
        print(f"    ALTER ROLE {ROLE} PASSWORD '<naya>';\n")
        return

    parts = urlsplit(owner_url)
    host = parts.netloc.split("@", 1)[-1]
    app_url = urlunsplit((parts.scheme, f"{ROLE}:{password}@{host}", parts.path,
                          parts.query, parts.fragment))
    print("\n  Ye line .env mein daalo (DATABASE_URL waisa hi rehne do —")
    print("  migrations usi owner se chalti hain):\n")
    print(f"APP_DATABASE_URL={app_url}\n")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
