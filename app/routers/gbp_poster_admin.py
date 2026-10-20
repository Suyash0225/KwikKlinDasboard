"""Owner-controlled Google Business Profile poster upload and publishing."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.routers.orders import require_admin_owner
from app.services import app_settings, gbp_owner_post, google_business as gbp

router = APIRouter(
    prefix="/admin/api/google-business/poster",
    tags=["google-business"],
    dependencies=[Depends(require_admin_owner)],
)
MAX_BYTES = 5 * 1024 * 1024


@router.post("/upload")
async def upload_poster(
    image: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Validate and save an owner-supplied poster; does not publish it yet."""
    content_type = (image.content_type or "").lower()
    if not content_type.startswith("image/"):
        raise HTTPException(415, "Please upload an image file.")
    blob = await image.read(MAX_BYTES + 1)
    if not blob or len(blob) > MAX_BYTES:
        raise HTTPException(413, "Poster must be between 1 byte and 5 MB.")
    try:
        poster_url = await gbp_owner_post._save_public_poster(db, blob)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await app_settings.set_value(db, "gbp_owner_poster_url", poster_url)
    await app_settings.set_value(db, "gbp_owner_poster_uploaded_at", datetime.now(timezone.utc).isoformat())
    return {"ok": True, "poster_url": poster_url, "uploaded_at": await app_settings.get(db, "gbp_owner_poster_uploaded_at")}


@router.post("/publish")
async def publish_poster(
    caption: str = Form(..., min_length=3, max_length=1200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Publish the saved poster to every configured GBP location."""
    poster_url = str(await app_settings.get(db, "gbp_owner_poster_url") or "").strip()
    if not poster_url.startswith("https://"):
        raise HTTPException(409, "Upload a poster before publishing.")
    if not gbp.enabled():
        raise HTTPException(503, "Google Business Profile is not configured.")
    try:
        polished, action = gbp_owner_post._caption_and_cta(caption)
        polished = await gbp_owner_post._interpret_owner_caption(polished)
        if not polished:
            raise ValueError("Caption is empty after Google Business Profile policy checks.")
        result = await gbp_owner_post.publish_owner_post(db, polished, poster_url, action)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": not result.get("failed"), **result}
