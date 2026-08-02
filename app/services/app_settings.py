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
