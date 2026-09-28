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
    # Automatic delivery promise: working days only; Sunday/holidays are skipped.
    "delivery_normal_days": 4,
    "delivery_heavy_days": 5,
    "delivery_holidays": [],
    # Legacy setting kept for compatibility with old tenants; new bill/order
    # flows use the delivery_* settings above.
    "turnaround_days": 4,
    "default_washer_phone": "",     # unassigned orders go to this staff phone
    # marketing compliance
    "marketing_freq_cap_per_month": 2,
    "marketing_monthly_msg_budget": 300,
    "marketing_max_discount_percent": 15,
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
    "communication_language": "en",
    "agent_btn_done": "✅ Done",
    "agent_btn_later": "⏳ Need more time",
    "agent_btn_problem": "⚠️ Problem",
    "agent_list_button": "Select task",
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
    # After a human replies from the WhatsApp phone, keep the Service
    # Agent paused for this many minutes, then resume automatically.
    "human_handoff_grace_minutes": 15,
    "customer_instructions": "",      # owner's extra instructions, hot-loaded
    "staff_instructions": "",
    "marketing_instructions": "",     # tone/style rules for campaign copy
    # daily social posts (Instagram auto-publish + GMB ready-to-post)
    "social_daily_enabled": True,
    "social_post_hour": 11,           # IST hour the daily poster goes out
    "ig_user_id": "",                 # Instagram Business user id (empty = off)
    "ig_access_token": "",            # token with instagram_content_publish
    "public_base_url": "",            # current tunnel URL (IG fetches images from here)
    # Pichhli Google Business posts [{date, text}] — AI inhe dohraata nahi
    # (Google repeat text ko spam maanta hai). Aakhri 30 rakhe jaate hain.
    "google_post_history": [],
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
    # Bill ke neeche ki sharten — ek line, ek shart. Laundry ka aam chalan
    # (check at delivery, colour/shrink, jeb khali, 10x muavza, 30 din).
    # Owner Settings se badal sakta hai; khali = koi shart nahi chhapti.
    # Chhoti rakhi hain jaan-boojh kar: 58mm kagaz par har shabd kagaz hai.
    "invoice_terms": (
        "Check clothes at delivery. Complaints within 24 hours.\n"
        "Not responsible for colour bleed, shrinkage or damage to weak/delicate fabric, buttons or work.\n"
        "Empty all pockets. Not responsible for items left inside.\n"
        "Compensation is limited to 10x the service charge of the item.\n"
        "Clothes not collected in 30 days are not our responsibility.\n"
        "Please bring this bill at collection."
    ),
    # Chhote Bluetooth/thermal printer ki chaudai — 58mm (32 akshar) ya 80mm (48)
    "receipt_paper_mm": 58,
    # Grahak ke statement page (/b/c/) par pichhle kitne din ke bill dikhein
    "bill_history_days": 90,
    "upi_vpa": "",                    # scan-to-pay on bills when set
    "upi_payee": "",
    "gst_percent": 18,
    "gst_default_on": False,          # New Bill GST checkbox default
    "default_delivery_phone": "",
    # Ops agent (services/ops_agent.py): bill bante hi washerman/delivery boy
    # chunkar order-linked kaam banata hai, sabse kam load wale ko
    "agent_auto_assign": True,
    # Turnaround (services/turnaround.py): har stage ki hadd GHANTON mein —
    # isse zyada ruka to stage reminder/warning tight hota hai. Customer-facing
    # "Delayed" dashboard count sirf expected delivery date cross hone par hota hai.
    "stage_limit_hours": {
        "RECEIVED": 6, "PICKUP_ASSIGNED": 6, "PICKED_UP": 12, "IN_WASH": 24,
        "IN_DRY": 12, "IN_IRON": 12, "READY": 24, "OUT_FOR_DELIVERY": 6, "ON_HOLD": 48,
    },
    # Staff panel mein grahak ka pata + "Route" (Google Maps) button. Abhi
    # band: pata sirf haath se likha text hai, WhatsApp pin save nahi hota —
    # galat raasta dikhane se behtar hai na dikhana. Zaroorat pade to
    # Settings -> Operations se chalu.
    "staff_show_route": False,
    # Urgent kapde: bill banate waqt "⚡ Urgent" — extra charge (items ka %
    # ya fixed ₹) jo har bill par badla/hataya ja sakta hai, aur jaldi delivery.
    "urgent_charge_type": "percent",   # percent | flat
    "urgent_charge_value": 50,
    "urgent_delivery_days": 1,
    # named discount presets for New Bill [{name, type: percent|flat, value}]
    "discount_presets": [],
    # owner ki apni expense categories (built-in list services/expenses.py
    # mein; ye usme JUDTI hain, uski jagah nahi)
    "expense_categories": [],
    # Order Agent SLA (owner's spec): pickup se ginke
    "sla_normal_days": 4,
    "sla_heavy_days": 7,
    "heavy_items": "blanket,kambal,razai,quilt,curtain,parda,saree,carpet,sofa,jacket,coat,sherwani,lehenga",
    # Google Business Profile connection (services/google_business.py):
    # {refresh_token (encrypted), account, location, title, email, choices}
    # Credential hai — settings API isse browser ko kabhi nahi bhejti.
    "gbp_connection": {},
    # Aakhri sync ke reviews: {rating, count, reviews[], synced_at, error}
    "gbp_reviews": {},
    "google_review_link": "",    # bheja jata hai sirf 4-5 star par
    "google_review_link_2": "",  # doosri listing — customers me rotate hota hai
    # --- live-conversation follow-ups (app/services/engage.py) ---
    # A customer wrote, the thread went quiet, no order came of it: nudge
    # them warmly while their 24h window is still open. Capped on purpose —
    # pinging a silent person all day earns blocks, and blocks kill the
    # WhatsApp number. Set engage_max_followups to 0 to switch it off.
    "engage_followups_enabled": False,
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
    return value


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
