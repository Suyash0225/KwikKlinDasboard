"""Google Business Profile daily AI posting."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import httpx
import structlog

from app.config import settings
from app.models.tenant import Tenant
from app.services import app_settings, google_business as gbp, llm_client
from app.services.tenant_context import current_tenant_id, get_home_tenant_id

log = structlog.get_logger()

_POSTS_KEY = "gbp_auto_posts"
_ENABLED_KEY = "gbp_auto_post_enabled"
_MAX_HISTORY = 90

_POST_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "A useful Google Business Profile post, max 700 characters."},
        "topic_type": {"type": "string", "enum": ["STANDARD", "OFFER"]},
    },
    "required": ["summary", "topic_type"],
    "additionalProperties": False,
}

_SYSTEM = """You write daily Google Business Profile posts for Kwik Klin, an online
laundry service in Varanasi, India.
Rules:
- Write natural, useful Hinglish or simple English suitable for Google Search/Maps.
- 1 short post, max 700 characters.
- Never invent prices, discounts, guarantees, timings, phone numbers, addresses,
  certifications, customer counts, ratings, or services not provided in FACTS.
- Never claim an offer exists unless FACTS explicitly provides one.
- Prefer practical laundry tips, stain-care education, pickup/delivery convenience,
  garment-care advice, seasonal/local relevance, or a gentle website CTA.
- At most 3 hashtags.
- Do not mention that AI wrote the post.
- Return only the requested JSON.
"""

async def _api_post(refresh_token: str, account: str, location: str, payload: dict) -> dict:
    tok = await gbp._access_token(refresh_token)
    loc_id = location.rsplit("/", 1)[-1]
    url = f"{gbp._REVIEWS}/{account}/locations/{loc_id}/localPosts"
    async with httpx.AsyncClient(
        timeout=30,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
    ) as c:
        r = await c.post(url, json=payload)
    if r.status_code not in (200, 201):
        raise gbp.GBPError(gbp._explain(r))
    return r.json() or {}

def _cta_url() -> str:
    base = (getattr(settings, "SITE_URL", "") or "https://kwikklin.online").rstrip("/")
    parts = urlsplit(base)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({"utm_source": "google", "utm_medium": "gbp", "utm_campaign": "daily_post"})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

def _topic(day: int) -> str:
    topics = (
        "a practical stain-removal tip",
        "how regular washing helps keep everyday clothes fresh",
        "the convenience of doorstep laundry pickup and delivery",
        "a simple garment-care tip that helps clothes last longer",
        "a useful laundry tip for busy families",
        "a practical weekend laundry-care idea",
        "planning laundry and garment care for the coming week",
    )
    return topics[day % len(topics)]

async def _generate_post(db, now_ist: datetime) -> dict:
    tenant_id = current_tenant_id.get() or await get_home_tenant_id()
    tenant = await db.get(Tenant, tenant_id) if tenant_id else None
    cfg = await app_settings.get_many(
        db, "shop_address", "shop_contact_phone", "turnaround_days", "sla_normal_days"
    )
    if tenant and tenant.shop_name:
        cfg = {"shop_name": tenant.shop_name, **cfg}
    facts = "\n".join(f"- {k}: {v}" for k, v in cfg.items() if v not in (None, ""))
    prompt = (
        f"FACTS:\n{facts or '- Kwik Klin is an online laundry service in Varanasi.'}\n\n"
        f"TODAY: {now_ist.strftime('%A, %d %B %Y')}\n"
        f"FOCUS: {_topic(now_ist.weekday())}\n"
        "Create one post. If no real offer is present in FACTS, use STANDARD."
    )
    with llm_client.track("gbp_post"):
        out = await llm_client.ask_json(system=_SYSTEM, user_text=prompt, schema=_POST_SCHEMA,
                                         model=llm_client.MODEL_CHEAP, max_tokens=350)
    summary = " ".join(str(out.get("summary") or "").split()).strip()
    if not summary:
        raise llm_client.LLMError("AI returned an empty GBP post")
    topic_type = out.get("topic_type") if out.get("topic_type") in {"STANDARD", "OFFER"} else "STANDARD"
    if topic_type == "OFFER" and "offer" not in facts.lower():
        topic_type = "STANDARD"
    return {"summary": summary[:700], "topic_type": topic_type}

async def auto_post_daily(db, now_ist: datetime) -> dict:
    """Publish at most one post per IST calendar day for this tenant."""
    if not gbp.enabled():
        return {"ok": False, "skipped": True, "reason": "google_business_not_configured"}
    enabled = await app_settings.get(db, _ENABLED_KEY)
    if enabled is False:
        return {"ok": False, "skipped": True, "reason": "disabled"}
    conn = await gbp.get_connection(db)
    if not (conn.get("refresh_token") and conn.get("account") and conn.get("location")):
        return {"ok": False, "skipped": True, "reason": "google_not_connected"}

    day = now_ist.strftime("%Y-%m-%d")
    history = list(await app_settings.get(db, _POSTS_KEY) or [])
    if any(str(x.get("date")) == day and x.get("status") == "published" for x in history):
        return {"ok": True, "skipped": True, "reason": "already_published", "date": day}

    post = await _generate_post(db, now_ist)
    payload = {
        "languageCode": "en-IN",
        "summary": post["summary"],
        "topicType": "STANDARD",
        "callToAction": {"actionType": "LEARN_MORE", "url": _cta_url()},
    }
    try:
        created = await _api_post(conn["refresh_token"], conn["account"], conn["location"], payload)
    except Exception as exc:
        history.append({"date": day, "status": "failed", "error": str(exc)[:300],
                        "attempted_at": datetime.now(timezone.utc).isoformat()})
        await app_settings.set_value(db, _POSTS_KEY, history[-_MAX_HISTORY:])
        raise

    entry = {"date": day, "status": "published", "post_name": created.get("name", ""),
             "summary": post["summary"], "published_at": datetime.now(timezone.utc).isoformat()}
    history.append(entry)
    await app_settings.set_value(db, _POSTS_KEY, history[-_MAX_HISTORY:])
    await app_settings.set_value(db, _ENABLED_KEY, True)
    log.info("gbp_daily_post_published", date=day, post_name=entry["post_name"])
    return {"ok": True, "published": True, **entry}

async def status(db) -> dict:
    history = list(await app_settings.get(db, _POSTS_KEY) or [])
    enabled = await app_settings.get(db, _ENABLED_KEY)
    return {"enabled": enabled is not False, "last": history[-1] if history else None, "history": history[-20:]}
