"""Verified owner WhatsApp flow for immediate Google Business Profile posts.

A poster image and caption sent from the configured owner number publishes immediately
to the selected GBP locations. The daily scheduler separately reuses the latest owner poster.
"""
from __future__ import annotations

import io
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
import structlog
from PIL import Image, ImageOps
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services import app_settings, audit, google_business as gbp, google_auth, social
from app.services.tenant_context import manager_phone
from app.utils.phone import normalize_phone

log = structlog.get_logger()
_POSTS_KEY = "gbp_auto_posts"
_MAX_HISTORY = 90
_MAX_IMAGE_BYTES = 5 * 1024 * 1024


def _same_phone(left: str, right: str) -> bool:
    # WAHA identifies senders as JIDs (for example 919876543210@c.us).
    # Strip the JID suffix before applying the application's E.164 normalizer.
    try:
        left_number = str(left or "").split("@", 1)[0]
        right_number = str(right or "").split("@", 1)[0]
        return normalize_phone(left_number) == normalize_phone(right_number)
    except (ValueError, TypeError):
        return False


def _caption_and_cta(raw: str) -> tuple[str, str]:
    """Support a single GBP CTA per post: CALL NOW or WEBSITE/VISIT WEBSITE."""
    text = " ".join(str(raw or "").split()).strip()
    action = "LEARN_MORE"
    match = re.match(r"^(?:CTA\s*:\s*)?(CALL(?:\s+NOW)?|WEBSITE|VISIT\s+WEBSITE)\s*[-:|]?\s*", text, re.I)
    if match:
        action = "CALL" if match.group(1).upper().startswith("CALL") else "LEARN_MORE"
        text = text[match.end():].strip()
    return social.enforce_google_rules(text)[:700], action


OWNER_CAPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "A polished Google Business Profile caption, maximum 700 characters."},
    },
    "required": ["summary"],
    "additionalProperties": False,
}


async def _interpret_owner_caption(raw_caption: str) -> str:
    """Understand the owner's instruction and polish copy without inventing claims."""
    source = social.enforce_google_rules(" ".join(str(raw_caption or "").split()))[:1200]
    if not source:
        raise ValueError("Caption is empty after Google Business Profile policy checks")
    try:
        from app.services import llm_client
        result = await llm_client.ask_json(
            system=(
                "You are Kwik Klin's Google Business Profile copy editor. "
                "Interpret the owner's instruction and return one concise, natural "
                "Hinglish or English caption suitable for Google Search/Maps. "
                "Use only claims explicitly present in the owner's message. "
                "Never invent a discount, price, date, guarantee, phone number, "
                "address, certification, rating, or service. Do not add hashtags. "
                "Do not include a CTA button instruction in the caption. Return JSON only."
            ),
            user_text=(
                "Owner's WhatsApp caption/instruction (business content only):\n" + source
            ),
            schema=OWNER_CAPTION_SCHEMA,
            model=llm_client.MODEL_SMART,
            max_tokens=500,
        )
        polished = social.enforce_google_rules(
            " ".join(str(result.get("summary") or "").split())
        )[:700]
        return polished or source[:700]
    except Exception as exc:
        log.warning("gbp_owner_caption_ai_fallback", error=str(exc)[:180])
        return source[:700]


async def _download_owner_image(payload: dict) -> bytes:
    media = payload.get("media") or {}
    if not isinstance(media, dict):
        raise ValueError("WhatsApp image attachment is missing media metadata")
    raw_url = str(media.get("url") or "").strip()
    if not raw_url:
        raise ValueError("WAHA did not include a downloadable image URL; resend the image as a document/photo")
    base = str(settings.WAHA_BASE_URL or "").rstrip("/") + "/"
    target = urljoin(base, raw_url)
    base_parts, target_parts = urlsplit(base), urlsplit(target)
    # The webhook payload is external input. Only fetch media from the configured WAHA host.
    if not base_parts.hostname or target_parts.hostname != base_parts.hostname or target_parts.port != base_parts.port:
        raise ValueError("WhatsApp media URL did not match the configured WAHA host")
    headers = {"Accept": "image/*"}
    if settings.WAHA_API_KEY:
        headers["X-Api-Key"] = settings.WAHA_API_KEY
    async with httpx.AsyncClient(timeout=25, follow_redirects=False, headers=headers) as client:
        response = await client.get(target)
    if response.status_code != 200:
        raise ValueError(f"WAHA image download failed (HTTP {response.status_code})")
    blob = response.content
    if not blob or len(blob) > _MAX_IMAGE_BYTES:
        raise ValueError("Poster must be a non-empty image no larger than 5 MB")
    return blob


async def _save_public_poster(db: AsyncSession, blob: bytes) -> str:
    """Validate/re-encode an owner image and return a stable public HTTPS URL."""
    social.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    digest = __import__("hashlib").sha256(blob).hexdigest()[:16]
    filename = f"social-google-owner-{digest}.png"
    path = social.MEDIA_DIR / filename
    try:
        with Image.open(io.BytesIO(blob)) as original:
            original.verify()
        with Image.open(io.BytesIO(blob)) as original:
            image = ImageOps.exif_transpose(original).convert("RGB")
            image.save(path, "PNG", optimize=True)
    except Exception as exc:
        raise ValueError("The attachment is not a valid image") from exc
    issues = social.check_google_image(path)
    if issues:
        path.unlink(missing_ok=True)
        raise ValueError("Poster failed Google image checks: " + "; ".join(issues))
    base = await google_auth.public_base(db)
    fixed = bool(await app_settings.get(db, "public_url_fixed")) or bool(settings.SITE_URL)
    url, problem = social.google_image_url(base, fixed, filename)
    if problem or not url.startswith("https://"):
        path.unlink(missing_ok=True)
        raise ValueError("Public poster URL is not ready: " + (problem or "HTTPS required"))
    return url


async def _targets(conn: dict) -> list[dict]:
    raw = conn.get("post_locations") or [{
        "account": conn.get("account"), "location": conn.get("location"),
        "title": conn.get("title") or conn.get("location"),
    }]
    result, seen = [], set()
    for item in raw:
        if not isinstance(item, dict) or not item.get("account") or not item.get("location"):
            continue
        key = (str(item["account"]), str(item["location"]))
        if key not in seen:
            seen.add(key)
            result.append({**item, "account": key[0], "location": key[1]})
    return result


async def publish_owner_post(db: AsyncSession, summary: str, poster_url: str, action: str) -> dict:
    if not gbp.enabled():
        raise ValueError("Google Business Profile API is not configured")
    conn = await gbp.get_connection(db)
    if not (conn.get("refresh_token") and conn.get("account") and conn.get("location")):
        raise ValueError("Google Business Profile is not connected")
    targets = await _targets(conn)
    if not targets:
        raise ValueError("No Google Business Profile locations are selected")
    from app.services import gbp_auto_post
    now = datetime.now(timezone.utc)
    ist_day = now.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    history = list(await app_settings.get(db, _POSTS_KEY) or [])
    topic_key = "owner-supplied"
    payload = {
        "languageCode": "en-IN",
        "summary": summary,
        "topicType": "STANDARD",
        "media": [{"mediaFormat": "PHOTO", "sourceUrl": poster_url}],
        "callToAction": ({"actionType": "CALL"} if action == "CALL" else {
            "actionType": "LEARN_MORE", "url": gbp_auto_post._cta_url(
                now.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata")), topic_key, "owner"
            )
        }),
    }
    published, failed = [], []
    for target in targets:
        acc, loc = target["account"], target["location"]
        title = str(target.get("title") or loc)
        try:
            created = await gbp_auto_post._api_post(conn["refresh_token"], acc, loc, payload)
        except Exception as exc:
            error = str(exc)[:300]
            history.append({
                "date": ist_day, "status": "failed", "error": error, "topic_key": topic_key,
                "account": acc, "location": loc, "location_title": title,
                "source": "owner_whatsapp", "attempted_at": datetime.now(timezone.utc).isoformat(),
                "poster_url": poster_url, "summary": summary,
            })
            failed.append(f"{title}: {error}")
            await audit.record(actor_role="owner", actor="verified_whatsapp",
                action="gbp_owner_post_failed", args={"location": loc}, result=error, ok=False)
            continue
        entry = {
            "date": ist_day, "status": "published", "post_name": created.get("name", ""),
            "summary": summary, "topic_key": topic_key, "source": "owner_whatsapp",
            "account": acc, "location": loc, "location_title": title,
            "poster_url": poster_url, "poster_attached": True, "cta_action": action,
            "published_at": datetime.now(timezone.utc).isoformat(),
        }
        history.append(entry)
        published.append(title)
        await audit.record(actor_role="owner", actor="verified_whatsapp",
            action="gbp_owner_post_published", args={"location": loc, "cta_action": action},
            result=created.get("name", "published"))
    await app_settings.set_value(db, _POSTS_KEY, history[-_MAX_HISTORY:])
    return {"published": published, "failed": failed, "cta_action": action}


async def handle_owner_image_message(db: AsyncSession, sender: str, payload: dict) -> bool:
    """Consume only an image sent by the verified owner number; other messages remain untouched."""
    if not _same_phone(sender, manager_phone()):
        return False
    if not (payload.get("hasMedia") and isinstance(payload.get("media"), dict)
            and str((payload.get("media") or {}).get("mimetype") or "").lower().startswith("image/")):
        return False
    body = str(payload.get("body") or "").strip()
    caption, action = _caption_and_cta(body)
    try:
        if not caption:
            raise ValueError("Image mil gayi, lekin caption missing hai. Image ke saath caption bhejein. CTA ke liye 'CALL NOW: caption' ya 'WEBSITE: caption' likhein.")
        caption = await _interpret_owner_caption(caption)
        blob = await _download_owner_image(payload)
        poster_url = await _save_public_poster(db, blob)
        # Persist before publishing so the 10 AM daily job can reuse the latest owner poster.
        await app_settings.set_value(db, "gbp_owner_poster_url", poster_url)
        await app_settings.set_value(db, "gbp_owner_poster_uploaded_at", datetime.now(timezone.utc).isoformat())
        result = await publish_owner_post(db, caption, poster_url, action)
        success = result["published"]
        lines = ["GBP post result:", f"CTA: {'Call now' if action == 'CALL' else 'Visit website'}"]
        if success:
            lines.append("Published: " + ", ".join(success))
        if result["failed"]:
            lines.append("Failed: " + " | ".join(result["failed"]))
        await __import__("app.services.whatsapp", fromlist=["send_message"]).send_message(
            db, to_phone=manager_phone(), text="\n".join(lines),
            intent="transactional", outbound_kind="admin_alert",
        )
    except Exception as exc:
        log.warning("gbp_owner_post_rejected", error=str(exc)[:240])
        try:
            await __import__("app.services.whatsapp", fromlist=["send_message"]).send_message(
                db, to_phone=manager_phone(), text="GBP post nahi hua: " + str(exc)[:500],
                intent="transactional", outbound_kind="admin_alert",
            )
        except Exception:
            log.exception("gbp_owner_post_error_reply_failed")
    return True
