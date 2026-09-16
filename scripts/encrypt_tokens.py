"""Purane plaintext tokens ko ek baar mein encrypt karo. Idempotent.

Naye writes apne aap encrypted hote hain. Ye script un rows ke liye hai jo
key set hone se PEHLE likhi gayi thin:
  - tenants.wa_token                      (WhatsApp)
  - settings_kv ig_access_token           (Instagram)
  - settings_kv gbp_connection.refresh_token (Google Business)

Run (TOKEN_ENCRYPTION_KEY .env mein set hone ke baad):
    python -m scripts.encrypt_tokens
"""

import asyncio
import json

import structlog
from sqlalchemy import text

from app.database import async_session_factory
from app.services import secrets
from app.utils.logger import configure_logging

configure_logging()
log = structlog.get_logger()


def _plain(v) -> bool:
    return isinstance(v, str) and v != "" and not v.startswith("enc:v1:")


async def main() -> None:
    if not secrets.is_enabled():
        raise SystemExit("TOKEN_ENCRYPTION_KEY is not set — nothing to do (and nothing would be safe).")
    counts = {"whatsapp": 0, "instagram": 0, "google": 0}
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
        counts["whatsapp"] = len(rows)

        rows = (
            await db.execute(
                text("SELECT id, key, value FROM settings_kv "
                     "WHERE key IN ('ig_access_token', 'gbp_connection')")
            )
        ).all()
        for sid, key, value in rows:
            value = dict(value or {})
            if key == "ig_access_token" and _plain(value.get("v")):
                value["v"] = secrets.encrypt(value["v"])
                counts["instagram"] += 1
            elif key == "gbp_connection" and isinstance(value.get("v"), dict) and _plain(value["v"].get("refresh_token")):
                value["v"] = {**value["v"], "refresh_token": secrets.encrypt(value["v"]["refresh_token"])}
                counts["google"] += 1
            else:
                continue
            await db.execute(
                text("UPDATE settings_kv SET value = CAST(:v AS jsonb) WHERE id = :id"),
                {"v": json.dumps(value), "id": sid},
            )
        await db.commit()
    log.info("tokens_encrypted", **counts)
    print("  encrypted: " + ", ".join(f"{k} {n}" for k, n in counts.items()))


if __name__ == "__main__":
    asyncio.run(main())
