"""Hot-reloadable settings stored in the settings_kv table.

The owner edits these from the dashboard; every read hits the DB (tiny
table, primary-key lookup) so changes take effect immediately — no
restart, no cache invalidation to get wrong.

Value column shape is always {'v': <value>} so plain scalars survive JSONB.
"""

from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SettingKV

log = structlog.get_logger()

# Single source of defaults — also drives the Settings UI.
DEFAULTS: dict[str, Any] = {
    # operations
    "standup_hour": 10,             # daily staff standup (Asia/Kolkata hour)
    "turnaround_days": 2,           # default delivery = today + this
    "default_washer_phone": "",     # unassigned orders go to this staff phone
    "staff_reply_window_hours": 2,  # no standup reply -> remind, then admin
    # marketing compliance
    "marketing_freq_cap_per_month": 2,
    "marketing_monthly_msg_budget": 300,
    "marketing_autonomy": "suggest",  # suggest | auto | off
    "attribution_window_days": 7,
    # agent behaviour (AI Training)
    "agent_enabled": True,            # global kill switch
    "customer_instructions": "",      # owner's extra instructions, hot-loaded
    "staff_instructions": "",
    "marketing_instructions": "",     # tone/style rules for campaign copy
    # daily social posts (Instagram auto-publish + GMB ready-to-post)
    "social_daily_enabled": True,
    "social_post_hour": 11,           # IST hour the daily poster goes out
    "ig_user_id": "",                 # Instagram Business user id (empty = off)
    "ig_access_token": "",            # token with instagram_content_publish
    "public_base_url": "",            # current tunnel URL (IG fetches images from here)
    # owner-edited customer message formats {message_key: text}
    "message_overrides": {},
    # business profile (Settings -> Business Profile & Invoices)
    "shop_address": "",
    "shop_gstin": "",
    "shop_contact_phone": "",
    "invoice_footer": "Thank you for choosing Kwik Klin! 🙏",
    "upi_vpa": "",                    # scan-to-pay on bills when set
    "upi_payee": "",
    "gst_percent": 18,
    "gst_default_on": False,          # New Bill GST checkbox default
    "default_delivery_phone": "",
    # named discount presets for New Bill [{name, type: percent|flat, value}]
    "discount_presets": [],
    # Order Agent SLA (owner's spec): pickup se ginke
    "sla_normal_days": 4,
    "sla_heavy_days": 7,
    "heavy_items": "blanket,kambal,razai,quilt,curtain,parda,saree,carpet,sofa,jacket,coat,sherwani,lehenga",
    "google_review_link": "",    # bheja jata hai sirf 4-5 star par
    "google_review_link_2": "",  # doosri listing — customers me rotate hota hai
    "tone": "friendly",               # formal | professional | friendly | casual
    "emoji_level": "minimal",         # off | minimal | expressive
}


async def get(db: AsyncSession, key: str) -> Any:
    """Read one setting; falls back to DEFAULTS. Unknown key -> KeyError."""
    default = DEFAULTS[key]  # raises for typo'd keys — that's a bug we want loud
    row = (
        await db.execute(select(SettingKV).where(SettingKV.key == key))
    ).scalar_one_or_none()
    if row is None:
        return default
    return row.value.get("v", default)


async def set_value(db: AsyncSession, key: str, value: Any) -> None:
    """Upsert one setting. Commits."""
    if key not in DEFAULTS:
        raise KeyError(f"unknown setting {key!r}")
    row = (
        await db.execute(select(SettingKV).where(SettingKV.key == key))
    ).scalar_one_or_none()
    if row is None:
        db.add(SettingKV(key=key, value={"v": value}))
    else:
        row.value = {"v": value}
    await db.commit()
    log.info("setting_updated", key=key)


async def all_settings(db: AsyncSession) -> dict[str, Any]:
    """Everything, defaults merged — for the Settings page."""
    rows = (await db.execute(select(SettingKV))).scalars().all()
    merged = dict(DEFAULTS)
    for r in rows:
        if r.key in merged:
            merged[r.key] = r.value.get("v", merged[r.key])
    return merged
