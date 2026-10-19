"""Google Business Profile daily AI posting."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from sqlalchemy import func, select

import httpx
import structlog

from app.config import settings
from app.models import Lead, Order
from app.models.tenant import Tenant
from app.services import app_settings, audit, google_business as gbp, llm_client
from app.services.tenant_context import current_tenant_id

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

_TOPICS = (
    ("stain-care", "a practical stain-removal tip"),
    ("everyday-freshness", "how regular washing helps keep everyday clothes fresh"),
    ("pickup-convenience", "the convenience of doorstep laundry pickup and delivery"),
    ("garment-longevity", "a simple garment-care tip that helps clothes last longer"),
    ("family-laundry", "a useful laundry tip for busy families"),
    ("weekend-care", "a practical weekend laundry-care idea"),
    ("weekly-planning", "planning laundry and garment care for the coming week"),
)

_DEFAULT_SEASONS = [
    {"name": "winter-care", "start": "11-15", "end": "02-15",
     "focus": "winter garment care, blanket care, and safe storage"},
    {"name": "monsoon-care", "start": "06-15", "end": "09-15",
     "focus": "monsoon drying, damp-odor prevention, and fabric care"},
    {"name": "festive-care", "start": "10-01", "end": "11-15",
     "focus": "festive garment care for sarees and occasion wear"},
]

def _campaign_marker(day: str, topic_key: str) -> str:
    safe_topic = re.sub(r"[^a-z0-9-]", "", str(topic_key).lower())[:32] or "laundry-care"
    return f"gbp_daily_{day.replace('-', '')}_{safe_topic}"[:100]

def _cta_url(now_ist: datetime, topic_key: str, source_tag: str = "") -> str:
    base = (getattr(settings, "SITE_URL", "") or "https://kwikklin.online").rstrip("/")
    parts = urlsplit(base)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({
        "utm_source": "google",
        "utm_medium": "gbp",
        "utm_campaign": _campaign_marker(now_ist.strftime("%Y-%m-%d"), topic_key),
        "utm_content": re.sub(r"[^a-z0-9-]", "", str(topic_key).lower())[:40],
    })
    if source_tag:
        query["utm_term"] = re.sub(r"[^a-z0-9-]", "", source_tag.lower())[:40]
    else:
        query.pop("utm_term", None)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

def _topic(day: int) -> str:
    return _TOPICS[day % len(_TOPICS)][1]

def _topic_key(day: int) -> str:
    return _TOPICS[day % len(_TOPICS)][0]

def _calendar_focus(now_ist: datetime, calendar: list) -> tuple[str, str] | None:
    """Return the editable seasonal calendar match as (key, focus)."""
    today = now_ist.strftime("%m-%d")
    for item in calendar if isinstance(calendar, list) else []:
        if not isinstance(item, dict):
            continue
        start, end = str(item.get("start") or ""), str(item.get("end") or "")
        focus = str(item.get("focus") or "").strip()[:180]
        key = re.sub(r"[^a-z0-9-]", "", str(item.get("name") or "seasonal").lower())[:32]
        if not re.fullmatch(r"\d{2}-\d{2}", start) or not re.fullmatch(r"\d{2}-\d{2}", end) or not focus:
            continue
        in_range = start <= today <= end if start <= end else (today >= start or today <= end)
        if in_range:
            return key or "seasonal", focus
    return None

def _choose_topic(performance: dict[str, dict], fallback_key: str, recent_keys: list[str]) -> str:
    """Prefer attributed leads/revenue when evidence exists; avoid immediate repeats."""
    candidates = [key for key, _ in _TOPICS if key not in recent_keys]
    if not candidates:
        candidates = [key for key, _ in _TOPICS]
    measured = {
        key: (int(performance.get(key, {}).get("leads", 0)) * 10
              + int(performance.get(key, {}).get("orders", 0)) * 20
              + float(performance.get(key, {}).get("collected_revenue", 0)) / 1000)
        for key in candidates
        if key in performance and (
            int(performance[key].get("leads", 0)) > 0
            or int(performance[key].get("orders", 0)) > 0
            or float(performance[key].get("collected_revenue", 0)) > 0
        )
    }
    if measured:
        return max(measured, key=lambda key: (measured[key], key))
    return fallback_key if fallback_key in candidates else candidates[0]

def _public_poster_url(value: object) -> str | None:
    """Accept only HTTPS public-looking poster URLs; never attach local URLs."""
    raw = str(value or "").strip()
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower().rstrip(".")
        if parts.scheme != "https" or not host or parts.username or parts.password:
            return None
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return None
        try:
            address = ipaddress.ip_address(host)
            if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
                return None
        except ValueError:
            pass
        return raw if parts.path and parts.path != "/" else raw
    except Exception:
        return None

async def _topic_performance(db, now_ist: datetime) -> dict[str, dict]:
    """Last 28 days of post-attributed leads, orders, and collected revenue."""
    since = now_ist.astimezone(timezone.utc) - timedelta(days=28)
    lead_rows = (await db.execute(
        select(Lead.source_campaign, func.count(Lead.id))
        .where(Lead.created_at >= since, Lead.source_campaign.like("gbp_daily_%"))
        .group_by(Lead.source_campaign)
    )).all()
    order_rows = (await db.execute(
        select(Order.acquisition_campaign, func.count(Order.id), func.coalesce(func.sum(Order.amount_paid), 0))
        .where(Order.created_at >= since, Order.acquisition_campaign.like("gbp_daily_%"))
        .group_by(Order.acquisition_campaign)
    )).all()
    performance: dict[str, dict] = {}
    for campaign, count in lead_rows:
        match = re.fullmatch(r"gbp_daily_\d{8}_([a-z0-9-]+)", str(campaign or ""))
        if match:
            performance.setdefault(match.group(1), {"leads": 0, "orders": 0, "collected_revenue": 0.0})
            performance[match.group(1)]["leads"] += int(count or 0)
    for campaign, count, revenue in order_rows:
        match = re.fullmatch(r"gbp_daily_\d{8}_([a-z0-9-]+)", str(campaign or ""))
        if match:
            performance.setdefault(match.group(1), {"leads": 0, "orders": 0, "collected_revenue": 0.0})
            performance[match.group(1)]["orders"] += int(count or 0)
            performance[match.group(1)]["collected_revenue"] += float(revenue or 0)
    return performance

async def _post_strategy(db, now_ist: datetime) -> dict:
    performance = await _topic_performance(db, now_ist)
    history = list(await app_settings.get(db, _POSTS_KEY) or [])
    recent_keys = [
        str(item.get("topic_key")) for item in history[-2:]
        if item.get("status") == "published" and item.get("topic_key")
    ]
    calendar = await app_settings.get(db, "gbp_seasonal_calendar")
    season = _calendar_focus(now_ist, calendar or _DEFAULT_SEASONS)
    # Compare recent order volume by weekday; PostgreSQL DOW uses Sunday=0.
    since = now_ist.astimezone(timezone.utc) - timedelta(days=28)
    dow_rows = (await db.execute(
        select(func.extract("dow", Order.created_at), func.count(Order.id))
        .where(Order.created_at >= since)
        .group_by(func.extract("dow", Order.created_at))
    )).all()
    weekday_counts = {int(day): int(count) for day, count in dow_rows if day is not None}
    current_dow = (now_ist.weekday() + 1) % 7
    baseline = (sum(weekday_counts.values()) / len(weekday_counts)) if weekday_counts else 0
    slow_day = bool(baseline and weekday_counts.get(current_dow, 0) < baseline)
    if season:
        topic_key, focus = season
        source_tag = "seasonal" + ("-slow-day" if slow_day else "")
    elif slow_day:
        topic_key, focus, source_tag = "pickup-convenience", (
            "a helpful, low-pressure reminder about convenient laundry pickup and delivery"
        ), "slow-day"
    else:
        fallback_key = _topic_key(now_ist.weekday())
        topic_key = _choose_topic(performance, fallback_key, recent_keys)
        focus = dict(_TOPICS).get(topic_key, _topic(now_ist.weekday()))
        source_tag = "data-driven" if topic_key in performance else ""
    return {
        "topic_key": topic_key, "focus": focus, "source_tag": source_tag,
        "performance": performance.get(topic_key, {"leads": 0, "orders": 0, "collected_revenue": 0.0}),
        "performance_window_days": 28,
    }

async def _generate_post(db, now_ist: datetime) -> dict:
    tenant_id = current_tenant_id.get()
    if tenant_id is None:
        raise RuntimeError("GBP auto-post requires explicit tenant context")
    tenant = await db.get(Tenant, tenant_id)
    cfg = await app_settings.get_many(
        db, "shop_address", "shop_contact_phone", "turnaround_days", "sla_normal_days"
    )
    if tenant and tenant.shop_name:
        cfg = {"shop_name": tenant.shop_name, **cfg}
    facts = "\n".join(f"- {k}: {v}" for k, v in cfg.items() if v not in (None, ""))
    strategy = await _post_strategy(db, now_ist)
    prompt = (
        f"FACTS:\n{facts or '- Kwik Klin is an online laundry service in Varanasi.'}\n\n"
        f"TODAY: {now_ist.strftime('%A, %d %B %Y')}\n"
        f"FOCUS: {strategy['focus']}\n"
        f"TOPIC KEY: {strategy['topic_key']}\n"
        f"LAST 28-DAY ATTRIBUTED PERFORMANCE FOR THIS TOPIC: {strategy['performance']}\n"
        "Use the focus and performance as guidance, not as claims for the public post. "
        "Never state performance numbers or invent a deal. Create one post. "
        "If no real offer is present in FACTS, use STANDARD."
    )
    with llm_client.track("gbp_post"):
        out = await llm_client.ask_json(system=_SYSTEM, user_text=prompt, schema=_POST_SCHEMA,
                                         model=llm_client.MODEL_CHEAP, max_tokens=350)
    summary = " ".join(str(out.get("summary") or "").split()).strip()
    # Reuse the existing GBP-specific sanitizer as a deterministic second
    # policy check: strip phone numbers, URLs, hashtags and dangling contact CTAs.
    from app.services import social
    summary = social.enforce_google_rules(summary)
    if not summary:
        raise llm_client.LLMError("AI returned an empty GBP post after policy checks")
    topic_type = out.get("topic_type") if out.get("topic_type") in {"STANDARD", "OFFER"} else "STANDARD"
    if topic_type == "OFFER" and "offer" not in facts.lower():
        topic_type = "STANDARD"
    return {"summary": summary[:700], "topic_type": topic_type, **strategy}


def _poster_copy(topic_key: str) -> tuple[str, str, str]:
    """SEO-aware but policy-safe copy for the image; no phone, price or offer."""
    choices = {
        "stain-care": ("Stain Care", "Care for Every Fabric", "Laundry and dry cleaning in Varanasi"),
        "everyday-freshness": ("Everyday Laundry", "Fresh Clothes, Less Hassle", "Laundry service in Varanasi"),
        "pickup-convenience": ("Laundry Convenience", "Laundry Made Easier", "Explore Kwik Klin online"),
        "garment-longevity": ("Garment Care", "Give Clothes Thoughtful Care", "Laundry and dry cleaning in Varanasi"),
        "family-laundry": ("Family Laundry", "Laundry for Busy Days", "Laundry service in Varanasi"),
        "weekend-care": ("Weekend Care", "Get Clothes Ready", "Laundry and dry cleaning in Varanasi"),
        "weekly-planning": ("Weekly Laundry", "Plan Your Laundry Care", "Explore Kwik Klin online"),
        "winter-care": ("Seasonal Garment Care", "Care for Winter Clothes", "Laundry and dry cleaning in Varanasi"),
        "monsoon-care": ("Monsoon Garment Care", "Keep Fabric Careful", "Laundry service in Varanasi"),
        "festive-care": ("Occasion Wear Care", "Care for Special Outfits", "Laundry and dry cleaning in Varanasi"),
    }
    return choices.get(topic_key, ("Laundry Care", "Thoughtful Garment Care", "Laundry and dry cleaning in Varanasi"))


async def _build_poster_url(db, now_ist: datetime, topic_key: str) -> tuple[str | None, str | None]:
    """Create a policy-safe branded poster, preferring Gemini-generated artwork.

    The exact Kwik Klin logo and text are overlaid locally. If the image model
    is unavailable/quota-limited, use the existing deterministic poster rather
    than blocking a text post. No phone number or direct contact details appear.
    """
    from app.services import google_auth, social, ai_poster

    theme, headline, subline = _poster_copy(topic_key)
    day = now_ist.strftime("%Y-%m-%d")
    filename = f"social-google-{day}.png"
    path = social.MEDIA_DIR / filename
    poster_issue = None
    try:
        social.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        # One bounded image-generation call per daily campaign, shared by the
        # selected GBP locations. Model must create artwork only, not text/logo.
        try:
            import tempfile
            with tempfile.TemporaryDirectory(prefix="kwikklin-gbp-art-") as tmp:
                background = Path(tmp) / "background.png"
                prompt = (
                    "Create a premium square 1:1 commercial lifestyle photograph for an Indian "
                    "laundry and dry-cleaning brand in Varanasi. Show neatly folded fresh clothes, "
                    "soft clean fabric textures and a tasteful laundry-care setting. Modern, "
                    "trustworthy, realistic photography; deep navy and cyan accents; balanced "
                    "natural light; uncluttered composition with generous calm negative space. "
                    "No text, no letters, no numbers, no logo, no watermark, no phone, no signage, "
                    "no prices, no discounts, no offers. This is background artwork only."
                )
                await ai_poster.generate_background(prompt, background)
                logo_path = Path(__file__).resolve().parent.parent / "site" / "assets" / "img" / "logo.png"
                ai_poster.compose_poster(background, logo_path, path, theme, headline, subline)
        except Exception as exc:
            poster_issue = f"ai_artwork_unavailable: {type(exc).__name__}; branded fallback used"
            log.warning("gbp_ai_poster_fallback", error=type(exc).__name__)
            import tempfile
            with tempfile.TemporaryDirectory(prefix="kwikklin-gbp-fallback-") as tmp:
                background = Path(tmp) / "background.png"
                ai_poster.create_fallback_background(background)
                logo_path = Path(__file__).resolve().parent.parent / "site" / "assets" / "img" / "logo.png"
                ai_poster.compose_poster(background, logo_path, path, theme, headline, subline)

        problems = social.check_google_image(path)
        if problems:
            return None, "poster_validation_failed: " + "; ".join(problems)
        base = await google_auth.public_base(db)
        fixed = bool(await app_settings.get(db, "public_url_fixed")) or bool(settings.SITE_URL)
        url, problem = social.google_image_url(base, fixed, filename)
        if problem:
            return None, "poster_url_unavailable: " + problem
        if not _public_poster_url(url):
            return None, "poster_url_unavailable: URL failed HTTPS/public-host validation"
        return url, poster_issue
    except Exception as exc:
        log.warning("gbp_poster_generation_failed", error=type(exc).__name__)
        return None, "poster_generation_failed: " + type(exc).__name__

async def auto_post_daily(db, now_ist: datetime) -> dict:
    """Publish today's AI post to every selected GBP location, independently."""
    if not gbp.enabled():
        return {"ok": False, "skipped": True, "reason": "google_business_not_configured"}
    enabled = await app_settings.get(db, _ENABLED_KEY)
    if enabled is False:
        return {"ok": False, "skipped": True, "reason": "disabled"}
    conn = await gbp.get_connection(db)
    if not (conn.get("refresh_token") and conn.get("account") and conn.get("location")):
        return {"ok": False, "skipped": True, "reason": "google_not_connected"}

    # Reviews still use the single primary location; daily posts target all
    # explicitly selected locations. Existing connections remain single-location
    # until the owner reconnects/selects locations in the Control panel.
    targets = conn.get("post_locations") or [{
        "account": conn["account"], "location": conn["location"],
        "title": conn.get("title", ""), "address": conn.get("address", ""),
    }]
    unique_targets = []
    seen = set()
    for target in targets:
        if not isinstance(target, dict) or not target.get("account") or not target.get("location"):
            continue
        key = (str(target["account"]), str(target["location"]))
        if key not in seen:
            seen.add(key)
            unique_targets.append(target)
    if not unique_targets:
        return {"ok": False, "skipped": True, "reason": "no_post_locations_selected"}

    day = now_ist.strftime("%Y-%m-%d")
    history = list(await app_settings.get(db, _POSTS_KEY) or [])
    pending = []
    for target in unique_targets:
        loc = str(target["location"])
        acc = str(target["account"])
        already = any(
            str(item.get("date")) == day
            and item.get("status") == "published"
            and str(item.get("location") or conn.get("location")) == loc
            and str(item.get("account") or conn.get("account")) == acc
            for item in history
        )
        if not already:
            pending.append(target)
    if not pending:
        return {"ok": True, "skipped": True, "reason": "already_published_all_locations",
                "date": day, "location_count": len(unique_targets), "published_count": 0}

    post = await _generate_post(db, now_ist)
    topic_key = str(post.get("topic_key") or _topic_key(now_ist.weekday()))
    campaign = _campaign_marker(day, topic_key)
    source_tag = str(post.get("source_tag") or "")
    cta_url = _cta_url(now_ist, topic_key, source_tag)
    payload = {
        "languageCode": "en-IN",
        "summary": post["summary"],
        "topicType": "STANDARD",
        "callToAction": {"actionType": "LEARN_MORE", "url": cta_url},
    }
    # Generate today's branded, phone-free poster locally. Google can only
    # attach it when the stable public /social URL is configured and valid.
    poster_url, poster_issue = await _build_poster_url(db, now_ist, topic_key)
    if poster_url:
        payload["media"] = [{"mediaFormat": "PHOTO", "sourceUrl": poster_url}]

    published, failures = [], []
    for target in pending:
        loc, acc = str(target["location"]), str(target["account"])
        title = str(target.get("title") or loc)
        try:
            created = await _api_post(conn["refresh_token"], acc, loc, payload)
        except Exception as exc:
            entry = {
                "date": day, "status": "failed", "error": str(exc)[:300],
                "topic_key": topic_key, "campaign": campaign,
                "account": acc, "location": loc, "location_title": title,
                "attempted_at": datetime.now(timezone.utc).isoformat(),
            }
            history.append(entry)
            failures.append({"location": loc, "title": title, "error": str(exc)[:200]})
            await audit.record(actor_role="system", actor="gbp_auto_post",
                               action="gbp_daily_post_failed",
                               args={"date": day, "topic_key": topic_key, "location": loc},
                               result=str(exc)[:300], ok=False)
            log.warning("gbp_daily_post_location_failed", date=day, location=loc, error=str(exc)[:200])
            continue

        entry = {
            "date": day, "status": "published", "post_name": created.get("name", ""),
            "summary": post["summary"], "topic_key": topic_key, "campaign": campaign,
            "source_tag": source_tag, "cta_url": cta_url,
            "poster_url": poster_url or "", "poster_attached": bool(poster_url),
            "poster_issue": poster_issue or "",
            "performance_28d": post.get("performance", {"leads": 0, "orders": 0, "collected_revenue": 0.0}),
            "account": acc, "location": loc, "location_title": title,
            "published_at": datetime.now(timezone.utc).isoformat(),
        }
        history.append(entry)
        published.append({"location": loc, "title": title, "post_name": entry["post_name"]})
        await audit.record(actor_role="system", actor="gbp_auto_post",
                           action="gbp_daily_post_published",
                           args={"date": day, "topic_key": topic_key, "campaign": campaign,
                                 "location": loc, "post_name": entry["post_name"]},
                           result="published")
        log.info("gbp_daily_post_published", date=day, location=loc,
                 post_name=entry["post_name"], topic_key=topic_key)

    await app_settings.set_value(db, _POSTS_KEY, history[-_MAX_HISTORY:])
    await app_settings.set_value(db, _ENABLED_KEY, True)
    return {
        "ok": bool(published) and not failures,
        "published": bool(published),
        "date": day,
        "location_count": len(unique_targets),
        "published_count": len(published),
        "published_locations": published,
        "failed_locations": failures,
        "topic_key": topic_key,
        "summary": post["summary"],
        "poster_attached": bool(poster_url),
        "poster_issue": poster_issue or "",
    }


async def status(db) -> dict:
    history = list(await app_settings.get(db, _POSTS_KEY) or [])
    enabled = await app_settings.get(db, _ENABLED_KEY)
    return {"enabled": enabled is not False, "last": history[-1] if history else None, "history": history[-20:]}
