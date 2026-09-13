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
    # --- AI cost tracking ---
    # Rate card in USD per MILLION tokens, per model. Gemini's free tier is
    # 0 by design; put your real numbers here the day you start paying, and
    # the whole usage history re-prices itself.
    "llm_rates": {
        "gemini-3.5-flash": {"in": 0.0, "out": 0.0},
        "gemini-3.5-flash-lite": {"in": 0.0, "out": 0.0},
        "claude-opus-5": {"in": 5.0, "out": 25.0},
        "claude-sonnet-5": {"in": 3.0, "out": 15.0},
        "claude-haiku-4-5": {"in": 1.0, "out": 5.0},
    },
    # --- billing (vendor-global, not per-tenant) ---
    # past_due -> locked se pehle kitne din ka grace (dunning window)
    "grace_days": 30,
    # Vendor ka UPI/GPay — client ke billing page par yahi dikhta hai
    # (recharge ka paisa isi par aata hai, phir panel se approve).
    "vendor_upi_id": "",
    "vendor_upi_name": "KwikKlin",
    # WA overage billing: plan limit ke UPAR har outbound msg ka daam
    # (paise). 0 = overage billing off. Panel profile mein estimate dikhta
    # hai; charge abhi manual hai (+extend / Razorpay addon aage).
    "wa_overage_paise_per_msg": 0,
    # past_due shuru hone ke kaun se dinon par owner ko WhatsApp reminder
    "dunning_reminder_days": [1, 3, 7],
    # Razorpay Plan ids, auto-created+cached by billing._ensure_rzp_plan:
    # {"pro:monthly": "plan_...", ...}. Test/live keys alag ids banayenge.
    "rzp_plan_ids": {},
    # Free-tier ceiling for the ACTIVE provider, requests per day.
    # 0 = unknown/none, and the dashboard then shows usage without a bar.
    "llm_daily_request_cap": 0,
    # What you're willing to spend per month on AI (USD). 0 = no budget set.
    "llm_monthly_budget_usd": 0.0,
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
    # Holdout ("control group"): itne % eligible customers ko JAAN-BOOJH kar
    # message NAHI bheja jata, taaki baad mein pata chale ki campaign se
    # kitna EXTRA business aaya. Bina holdout ke attribution jhoothi hai —
    # jo customer waise bhi aata, wo bhi campaign ke khaate mein chadh jata.
    # Chhote campaigns par apne aap off (neeche MIN_REACH_FOR_HOLDOUT).
    "marketing_holdout_percent": 10,
    # --- agent ke shabd (har shop apne hisaab se) ---
    # Staff ko jaane wale button aur list ke labels. Ye pehle code mein
    # gade the, isliye har client ke yahan ek jaise dikhte — koi apni
    # bhasha ya apne shabd nahi rakh sakta tha. Ab ye per-tenant hain:
    # ek client "✅ Ho gaya" rakhe, doosra "Done", teesra Bangla mein.
    # WhatsApp button ka title 20 akshar tak hi ja sakta hai — lamba
    # rakhne par apne aap chhota kar diya jata hai (message girta nahi).
    "agent_btn_done": "✅ Ho gaya",
    "agent_btn_later": "⏳ Time lagega",
    "agent_btn_problem": "❓ Dikkat hai",
    "agent_list_button": "Kaam chuniye",
    # Staff kin shabdon se dikkat batata hai — shop ki apni bol-chaal ke
    # shabd yahan jud sakte hain (code ke default ke UPAR, uski jagah nahi).
    "agent_trouble_words": [],
    # agent behaviour (AI Training)
    "agent_enabled": True,            # global kill switch
    # Complaint/bura-rating par bot us thread par chup ho jaata hai (insaan
    # sambhale). Itne ghante baad wo khud resume kar leta hai — warna
    # customer ka agla normal sawal bhi hamesha ke liye anjaana reh jaata.
    # 0 = kabhi auto-resume mat karo (purana behaviour).
    "agent_pause_hours": 24,
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
    # Free text, not open/close times: real shops say "11 se 6, Sunday band"
    # and pickup runs all day. Customers get this verbatim when they ask.
    "shop_hours": "",
    "shop_address": "",
    "shop_gstin": "",
    "shop_contact_phone": "",
    # Neutral default: ye har NAYE tenant ko milta hai. Pehle yahan
    # "Kwik Klin" likha tha, yaani koi bhi nayi dukaan sign-up karti
    # aur uske bill ke neeche kisi aur ka naam chhapta.
    "invoice_footer": "Thank you for your business! 🙏",
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
    # --- live-conversation follow-ups (app/services/engage.py) ---
    # A customer wrote, the thread went quiet, no order came of it: nudge
    # them warmly while their 24h window is still open. Capped on purpose —
    # pinging a silent person all day earns blocks, and blocks kill the
    # WhatsApp number. Set engage_max_followups to 0 to switch it off.
    "engage_followups_enabled": True,
    "engage_gap_hours": 6,            # min hours of silence before a nudge
    "engage_max_followups": 2,        # per conversation, resets when they reply
    # --- kis dukaan ka deployment hai ---
    # Ek instance ek shop ka data rakhta hai. Sirf ISI tenant ke users ko
    # /admin dashboard ka data milta hai; baaki (naye signup) ko welcome page.
    # Khali = sabse purana tenant home maana jayega.
    "home_tenant_slug": "",
    # Public URL sthir hai (Tailscale Funnel / apna domain / VM)?
    # True hone par tunnel-guard cloudflare tunnel banana BAND kar deta hai —
    # warna wo har blip par naya trycloudflare URL bana kar aapka sthir URL
    # overwrite kar deta, aur Meta ka webhook bhi wahin mod deta.
    "public_url_fixed": False,
}


def _effective_tenant_id():
    """Kis tenant ki settings — request ctx ka tenant, warna home.

    Explicit filter zaroori hai: system context (scheduler) mein ORM ka
    auto-filter off hota hai, aur multi-tenant mein ek key ke kai rows
    hain — bina filter ke scalar_one_or_none() phat jaata."""
    from app.services import tenant_context

    return (
        tenant_context.current_tenant_id.get()
        or tenant_context.cached_home_tenant_id()
    )


async def get(db: AsyncSession, key: str) -> Any:
    """Read one setting (effective tenant ki); falls back to DEFAULTS."""
    default = DEFAULTS[key]  # raises for typo'd keys — that's a bug we want loud
    row = (
        await db.execute(
            select(SettingKV).where(
                SettingKV.key == key,
                SettingKV.tenant_id == _effective_tenant_id(),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return default
    return row.value.get("v", default)


async def get_many(db: AsyncSession, *keys: str) -> dict[str, Any]:
    """Read several settings in ONE round trip.

    A loop of get() is one query per key — on the agent's hot path (facts
    building, campaign gating) that was 4-6 sequential round trips before a
    single customer message could be answered. Same semantics as get():
    unknown keys raise, missing rows fall back to DEFAULTS.
    """
    out = {k: DEFAULTS[k] for k in keys}  # raises for typo'd keys, like get()
    if not out:
        return out
    rows = (
        await db.execute(
            select(SettingKV).where(
                SettingKV.key.in_(tuple(out)),
                SettingKV.tenant_id == _effective_tenant_id(),
            )
        )
    ).scalars().all()
    for r in rows:
        if r.key in out:
            out[r.key] = r.value.get("v", out[r.key])
    return out


async def set_value(db: AsyncSession, key: str, value: Any) -> None:
    """Upsert one setting (effective tenant ki). Commits."""
    if key not in DEFAULTS:
        raise KeyError(f"unknown setting {key!r}")
    tid = _effective_tenant_id()
    row = (
        await db.execute(
            select(SettingKV).where(
                SettingKV.key == key, SettingKV.tenant_id == tid
            )
        )
    ).scalar_one_or_none()
    if row is None:
        db.add(SettingKV(key=key, value={"v": value}, tenant_id=tid))
    else:
        row.value = {"v": value}
    await db.commit()
    log.info("setting_updated", key=key)


async def all_settings(db: AsyncSession) -> dict[str, Any]:
    """Everything, defaults merged — for the Settings page (effective tenant)."""
    rows = (
        await db.execute(
            select(SettingKV).where(SettingKV.tenant_id == _effective_tenant_id())
        )
    ).scalars().all()
    merged = dict(DEFAULTS)
    for r in rows:
        if r.key in merged:
            merged[r.key] = r.value.get("v", merged[r.key])
    return merged
