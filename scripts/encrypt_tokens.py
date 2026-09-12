"""Purane plaintext WhatsApp tokens ko ek baar mein encrypt karo. Idempotent.

Naye writes apne aap encrypted hote hain (EncryptedText column). Ye script
un rows ke liye hai jo is feature se PEHLE likhi gayi thin.

Run (TOKEN_ENCRYPTION_KEY .env mein set hone ke baad):
    python -m scripts.encrypt_tokens
"""

import asyncio

import structlog
from sqlalchemy import text

from app.database import async_session_factory
from app.services import secrets
from app.utils.logger import configure_logging

configure_logging()
log = structlog.get_logger()


async def main() -> None:
    if not secrets.is_enabled():
        raise SystemExit("TOKEN_ENCRYPTION_KEY is not set — nothing to do (and nothing would be safe).")
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                text("SELECT id, wa_token FROM tenants WHERE wa_token IS NOT NULL "
                     "AND wa_token <> '' AND wa_token NOT LIKE 'enc:v1:%'")
            )
        ).all()
        for tid, raw in rows:
            await db.execute(
                text("UPDATE tenants SET wa_token = :v WHERE id = :id"),
                {"v": secrets.encrypt(raw), "id": tid},
            )
        await db.commit()
    log.info("tokens_encrypted", count=len(rows))
    print(f"encrypted {len(rows)} token(s)")


if __name__ == "__main__":
    asyncio.run(main())
