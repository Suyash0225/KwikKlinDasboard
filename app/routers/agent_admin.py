"""Admin APIs for the agent era: campaigns, activity log, AI training,
hot settings, coupons, manual job triggers, per-thread agent pause.

Same auth as everything else: X-API-Key (require_admin_key).
"""

import asyncio
import re
import time as _time
import uuid as uuid_module
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import (
    AuditLog,
    Campaign,
    Correction,
    Coupon,
    Customer,
    FaqEntry,
    OpenQuestion,
    CampaignRecipient,
    Rate,
)
from app.routers.orders import require_admin_owner, require_feature
from app.services import app_settings, audit
from app.services.marketing import (
    campaign_stats,
    compute_segments,
    queue_campaign,
    send_campaign,
)
from app.utils.phone import normalize_phone

router = APIRouter(
    prefix="/admin/api", tags=["agent-admin"], dependencies=[Depends(require_admin_owner)]
)
log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Segments + campaigns
# ---------------------------------------------------------------------------


@router.get("/segments", dependencies=[Depends(require_feature("campaigns"))])
async def segments(db: AsyncSession = Depends(get_db)) -> dict:
    segs = await compute_segments(db)
    return {
        "counts": {k: len(v) for k, v in segs.items()},
        "members": {
            k: [
                {"name": m["name"], "phone": m["phone"], "lifetime_paid": float(m["lifetime_paid"])}
                for m in v[:50]
            ]
            for k, v in segs.items()
        },
    }


@router.get("/campaigns", dependencies=[Depends(require_feature("campaigns"))])
async def list_campaigns(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (
        (await db.execute(select(Campaign).order_by(Campaign.created_at.desc()).limit(50)))
        .scalars()
        .all()
    )
    out = []
    for c in rows:
        stats = c.stats or {}
        if c.status in ("sending", "sent", "approved"):
            stats = await campaign_stats(db, c.id)
        out.append(
            {
                "id": str(c.id),
                "name": c.name,
                "segment": c.segment,
                "status": c.status,
                "message_text": c.message_text,
                "rationale": c.rationale,
                "coupon_code": c.coupon_code,
                "created_by": c.created_by,
                "created_at": c.created_at.isoformat(),
                "sent_at": c.sent_at.isoformat() if c.sent_at else None,
                "stats": stats,
                "creative_file": (c.stats or {}).get("creative_file"),
            }
        )
    return out


@router.post("/campaigns/upload-image", dependencies=[Depends(require_feature("campaigns"))])
async def upload_campaign_image(
    file: UploadFile = File(...),
) -> dict:
    """Store an owner-supplied campaign image as a safe JPEG.

    The upload is not sent anywhere. It becomes the draft's reusable creative
    and can be replaced before approval.
    """
    from pathlib import Path
    from PIL import Image, ImageOps
    import io, uuid

    allowed = {"image/jpeg", "image/png", "image/webp"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="Upload JPG, PNG or WEBP image only")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Image is empty")
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image is too large (8MB max)")

    try:
        image = Image.open(io.BytesIO(raw))
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((1600, 1600))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid image file") from exc

    media_dir = Path(__file__).resolve().parent.parent / "media"
    media_dir.mkdir(exist_ok=True)
    name = f"campaign-{uuid.uuid4().hex}.jpg"
    image.save(media_dir / name, "JPEG", quality=90, optimize=True)
    return {"creative_file": name, "preview_url": f"/admin/media/{name}"}


class CampaignIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    segment: str
    message_text: str = Field(min_length=5)
    coupon_code: str | None = None
    creative_file: str | None = None


class CampaignAIDraftIn(BaseModel):
    segment: str
    goal: str = Field(default="increase repeat orders", max_length=120)


_CAMPAIGN_AI_SCHEMA = {
    "type": "object",
    "properties": {
        "campaign_name": {"type": "string"},
        "segment": {"type": "string"},
        "offer_type": {"type": "string", "enum": ["percent", "flat", "service_bonus", "no_discount"]},
        "discount_value": {"type": "number"},
        "min_order": {"type": "number"},
        "validity_days": {"type": "integer"},
        "coupon_code": {"type": "string"},
        "message": {"type": "string"},
        "gmb_title": {"type": "string"},
        "gmb_body": {"type": "string"},
        "gmb_cta": {"type": "string"},
        "creative_brief": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": [
        "campaign_name", "segment", "offer_type", "discount_value", "min_order",
        "validity_days", "coupon_code", "message", "gmb_title", "gmb_body",
        "gmb_cta", "creative_brief", "rationale",
    ],
}


@router.post("/campaigns/ai-draft", dependencies=[Depends(require_feature("campaigns"))])
async def ai_campaign_draft(
    body: CampaignAIDraftIn, db: AsyncSession = Depends(get_db)
) -> dict:
    """Use AI for offer strategy/copy, then clamp the result with code.

    AI never creates a coupon or sends a message. The owner still saves the
    returned draft and explicitly approves the campaign.
    """
    segs = await compute_segments(db)
    members = segs.get(body.segment, [])
    if not members:
        raise HTTPException(status_code=400, detail="No customers in this segment")

    now = datetime.now(timezone.utc)
    aov = (
        sum(Decimal(str(m["lifetime_paid"])) for m in members)
        / max(sum(int(m["order_count"]) for m in members), 1)
    )
    prev = (
        await db.execute(
            select(
                CampaignRecipient.status,
                func.count(CampaignRecipient.id),
            )
            .join(Campaign, Campaign.id == CampaignRecipient.campaign_id)
            .where(Campaign.segment == body.segment)
            .group_by(CampaignRecipient.status)
        )
    ).all()
    history = {str(k): int(v) for k, v in prev}
    rates = (
        await db.execute(
            select(Rate.service, Rate.garment, Rate.rate, Rate.unit)
            .where(Rate.is_active.is_(True))
            .order_by(Rate.service, Rate.garment)
            .limit(80)
        )
    ).all()
    rate_card = [
        {"service": s, "garment": g, "rate": float(r), "unit": u}
        for s, g, r, u in rates
    ]
    max_discount = float(await app_settings.get(db, "marketing_max_discount_percent"))
    month = now.month
    season = {
        9: "festival/Diwali preparation and home linen cleaning",
        10: "Diwali preparation",
        11: "winter blankets plus wedding garments",
        12: "winter blankets plus wedding garments",
        1: "winter blankets plus wedding garments",
        2: "winter blankets plus wedding garments",
        3: "post-Holi stain cleaning",
        4: "summer storage and linen",
        5: "summer linen and curtains",
        6: "summer linen and curtains",
        7: "monsoon care",
        8: "monsoon care",
    }.get(month, "general laundry demand")

    system = (
        "You are Kwik Klin's marketing strategist. Recommend an offer using the "
        "actual segment, customer value, previous campaign outcomes, season and rate card. "
        "Do not invent services, prices, margins or customer facts. Prefer non-discount "
        "value when it can work. Never exceed the supplied max discount. Keep WhatsApp "
        "copy <= 3 short lines. Google Business copy must not include a phone number, "
        "fake address, fake review, or fabricated claim. Return only JSON matching the schema."
    )
    user = {
        "segment": body.segment,
        "goal": body.goal,
        "audience_size": len(members),
        "average_order_value": round(float(aov), 2),
        "season": season,
        "previous_campaign_status_counts": history,
        "max_discount_percent": max_discount,
        "rate_card": rate_card,
        "shop_name": "Kwik Klin",
    }
    try:
        from app.services.llm_client import ask_json, MODEL_SMART, track
        with track("marketing_strategy"):
            draft = await ask_json(
                system=system,
                user_text=str(user),
                schema=_CAMPAIGN_AI_SCHEMA,
                model=MODEL_SMART,
                max_tokens=900,
            )
    except Exception:
        # Deterministic fallback: safe and cheap, but clearly not pretending
        # that the model succeeded.
        value = min(10.0 if body.segment == "lapsed" else 5.0, max_discount)
        draft = {
            "campaign_name": f"{body.segment}-{now.date().isoformat()}",
            "segment": body.segment,
            "offer_type": "percent" if value else "no_discount",
            "discount_value": value,
            "min_order": round(float(aov), 0) if aov else 0,
            "validity_days": 7,
            "coupon_code": f"KK{body.segment[:5].upper()}{now.strftime('%d%m')}",
            "message": f"Hi {{name}}! Get {value:g}% off your next laundry order this week. — Kwik Klin",
            "gmb_title": f"{value:g}% off your next laundry order" if value else "Fresh clothes, less hassle",
            "gmb_body": "Book your next laundry service with Kwik Klin this week.",
            "gmb_cta": "Book now",
            "creative_brief": "Kwik Klin logo + clean laundry visual + offer badge; no phone number.",
            "rationale": "Safe deterministic fallback because the marketing model was unavailable.",
        }

    # Hard safety/business clamps. AI is advisory; code owns money and dates.
    draft["segment"] = body.segment
    draft["discount_value"] = max(0.0, min(float(draft.get("discount_value") or 0), max_discount))
    if draft.get("offer_type") == "percent" and draft["discount_value"] > max_discount:
        draft["discount_value"] = max_discount
    draft["validity_days"] = max(1, min(int(draft.get("validity_days") or 7), 30))
    draft["min_order"] = max(0.0, float(draft.get("min_order") or 0))
    draft["coupon_code"] = str(draft.get("coupon_code") or f"KK{body.segment[:5].upper()}{now.strftime('%d%m')}").upper()[:30]
    for key in ("message", "gmb_title", "gmb_body", "gmb_cta", "creative_brief", "rationale"):
        draft[key] = str(draft.get(key) or "").strip()
    draft["gmb_body"] = draft["gmb_body"].replace("\\n", " ")
    # Phone-free Google copy is a hard rule, not an AI instruction.
    import re as _re
    draft["gmb_body"] = _re.sub(r"(?:\+?91[-\s]?)?[6-9]\d{9}", "", draft["gmb_body"]).strip()
    draft["gmb_title"] = _re.sub(r"(?:\+?91[-\s]?)?[6-9]\d{9}", "", draft["gmb_title"]).strip()
    # Generate the actual branded Google-safe creative now. This is deterministic
    # PIL rendering (logo/brand/offer layout), not a fake AI-generated shop photo.
    creative_file = None
    try:
        from pathlib import Path
        from app.services.social import draw_poster
        media_dir = Path(__file__).resolve().parent.parent / "media"
        media_dir.mkdir(exist_ok=True)
        creative_file = f"campaign-{uuid_module.uuid4().hex}.png"
        draw_poster(
            str(draft.get("gmb_title") or "Kwik Klin"),
            str(draft.get("gmb_title") or "Fresh clothes, less hassle"),
            str(draft.get("gmb_body") or "Book your next laundry service."),
            media_dir / creative_file,
            show_phone=False,
        )
    except Exception:
        log.exception("campaign_creative_generation_failed")
    draft["creative_file"] = creative_file
    return draft


@router.post("/campaigns", dependencies=[Depends(require_feature("campaigns"))], status_code=201)
async def create_campaign(body: CampaignIn, db: AsyncSession = Depends(get_db)) -> dict:
    creative = body.creative_file or ""
    if creative and not re.fullmatch(r"campaign-[a-f0-9]{32}\\.(?:png|jpg|jpeg)", creative):
        raise HTTPException(status_code=400, detail="invalid campaign creative")
    c = Campaign(
        name=body.name, segment=body.segment, message_text=body.message_text,
        coupon_code=body.coupon_code, status="draft", created_by="owner",
        stats={"creative_file": creative} if creative else None,
    )
    db.add(c)
    await db.commit()
    return {"id": str(c.id), "status": c.status}


@router.post("/campaigns/{campaign_id}/approve", dependencies=[Depends(require_feature("campaigns"))])
async def approve_campaign(campaign_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    c = await db.get(Campaign, uuid_module.UUID(campaign_id))
    if c is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    if c.status not in ("draft", "suggested"):
        raise HTTPException(status_code=409, detail=f"campaign is {c.status}")
    c.status = "approved"
    await db.commit()
    queued = await queue_campaign(db, c)
    asyncio.create_task(send_campaign(c.id))
    await audit.record(
        actor_role="admin", actor="dashboard", action="campaign_approved",
        args={"campaign": c.name}, result=f"queued {queued}",
    )
    return {"queued": queued, "status": "approved"}


@router.post("/campaigns/{campaign_id}/cancel", dependencies=[Depends(require_feature("campaigns"))])
async def cancel_campaign(campaign_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    c = await db.get(Campaign, uuid_module.UUID(campaign_id))
    if c is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    c.status = "cancelled"
    await db.commit()
    return {"status": "cancelled"}


# ---------------------------------------------------------------------------
# Coupons
# ---------------------------------------------------------------------------


class CouponIn(BaseModel):
    code: str = Field(min_length=3, max_length=30)
    discount_type: str = Field(pattern="^(percent|flat)$")
    value: float = Field(gt=0)
    min_order: float | None = None
    valid_to: str | None = None  # ISO date
    per_customer_limit: int = 1
    total_limit: int | None = None


@router.get("/coupons", dependencies=[Depends(require_feature("campaigns"))])
async def list_coupons(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (
        (await db.execute(select(Coupon).order_by(Coupon.created_at.desc()).limit(100)))
        .scalars()
        .all()
    )
    from app.models import CouponRedemption

    out = []
    for c in rows:
        used = (
            await db.execute(
                select(func.count())
                .select_from(CouponRedemption)
                .where(CouponRedemption.coupon_code == c.code)
            )
        ).scalar_one()
        out.append(
            {
                "code": c.code, "discount_type": c.discount_type, "value": float(c.value),
                "min_order": float(c.min_order) if c.min_order else None,
                "valid_to": c.valid_to.isoformat() if c.valid_to else None,
                "per_customer_limit": c.per_customer_limit,
                "total_limit": c.total_limit, "active": c.active, "used": used,
            }
        )
    return out


@router.post("/coupons", dependencies=[Depends(require_feature("campaigns"))], status_code=201)
async def create_coupon(body: CouponIn, db: AsyncSession = Depends(get_db)) -> dict:
    from datetime import date as _date
    from decimal import Decimal as D

    code = body.code.strip().upper()
    from app.services.marketing import get_coupon

    if await get_coupon(db, code):
        raise HTTPException(status_code=409, detail="coupon code already exists")
    db.add(
        Coupon(
            code=code, discount_type=body.discount_type, value=D(str(body.value)),
            min_order=D(str(body.min_order)) if body.min_order else None,
            valid_to=_date.fromisoformat(body.valid_to) if body.valid_to else None,
            per_customer_limit=body.per_customer_limit, total_limit=body.total_limit,
        )
    )
    await db.commit()
    return {"code": code}


@router.post("/coupons/{code}/toggle", dependencies=[Depends(require_feature("campaigns"))])
async def toggle_coupon(code: str, db: AsyncSession = Depends(get_db)) -> dict:
    from app.services.marketing import get_coupon

    c = await get_coupon(db, code)
    if c is None:
        raise HTTPException(status_code=404, detail="coupon not found")
    c.active = not c.active
    await db.commit()
    return {"code": c.code, "active": c.active}


# ---------------------------------------------------------------------------
# Activity log (agent observability)
# ---------------------------------------------------------------------------


@router.get("/activity")
async def activity(
    db: AsyncSession = Depends(get_db),
    role: str | None = Query(default=None),
    action: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    q = select(AuditLog).order_by(AuditLog.at.desc()).limit(limit)
    if role:
        q = q.where(AuditLog.actor_role == role)
    if action:
        q = q.where(AuditLog.action == action)
    rows = (await db.execute(q)).scalars().all()
    return [
        {
            "at": r.at.isoformat(), "role": r.actor_role, "actor": r.actor,
            "action": r.action, "args": r.args, "result": r.result, "ok": r.ok,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# AI Training: FAQ, corrections, teach-me queue
# ---------------------------------------------------------------------------


class FaqIn(BaseModel):
    question: str = Field(min_length=3)
    answer: str = Field(min_length=2)
    audience: str = Field(default="customer", pattern="^(customer|staff|all)$")


@router.get("/training/faq", dependencies=[Depends(require_feature("service_agent"))])
async def list_faq(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (
        (await db.execute(select(FaqEntry).order_by(FaqEntry.created_at.desc()).limit(500)))
        .scalars()
        .all()
    )
    return [
        {
            "id": str(r.id), "question": r.question, "answer": r.answer,
            "audience": r.audience, "enabled": r.enabled,
        }
        for r in rows
    ]


@router.post("/training/faq", dependencies=[Depends(require_feature("service_agent"))], status_code=201)
async def add_faq(body: FaqIn, db: AsyncSession = Depends(get_db)) -> dict:
    row = FaqEntry(question=body.question, answer=body.answer, audience=body.audience)
    db.add(row)
    await db.commit()
    return {"id": str(row.id)}


@router.delete("/training/faq/{faq_id}", dependencies=[Depends(require_feature("service_agent"))])
async def delete_faq(faq_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    row = await db.get(FaqEntry, uuid_module.UUID(faq_id))
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    await db.delete(row)
    await db.commit()
    return {"deleted": True}


class CorrectionIn(BaseModel):
    question: str = Field(min_length=3)
    correct_reply: str = Field(min_length=2)
    audience: str = Field(default="customer", pattern="^(customer|staff|all)$")


@router.get("/training/corrections", dependencies=[Depends(require_feature("service_agent"))])
async def list_corrections(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (
        (
            await db.execute(
                select(Correction).order_by(Correction.created_at.desc()).limit(500)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(r.id), "question": r.question, "correct_reply": r.correct_reply,
            "audience": r.audience, "enabled": r.enabled,
        }
        for r in rows
    ]


@router.post("/training/corrections", dependencies=[Depends(require_feature("service_agent"))], status_code=201)
async def add_correction(body: CorrectionIn, db: AsyncSession = Depends(get_db)) -> dict:
    row = Correction(
        question=body.question, correct_reply=body.correct_reply, audience=body.audience
    )
    db.add(row)
    await db.commit()
    return {"id": str(row.id)}


@router.delete("/training/corrections/{cid}", dependencies=[Depends(require_feature("service_agent"))])
async def delete_correction(cid: str, db: AsyncSession = Depends(get_db)) -> dict:
    row = await db.get(Correction, uuid_module.UUID(cid))
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    await db.delete(row)
    await db.commit()
    return {"deleted": True}


from fastapi import File, UploadFile

_ALLOWED_DOC_TYPES = {".pdf", ".txt", ".csv", ".md"}
_MAX_DOC_BYTES = 8 * 1024 * 1024  # 8 MB
_CHUNK_CHARS = 700


def _chunk_text(text: str) -> list[str]:
    """Paragraph-packed ~700-char chunks; tiny fragments merge forward."""
    paras = [p.strip() for p in text.replace("\r", "").split("\n\n") if p.strip()]
    if not paras:  # fall back to line-based packing (CSVs, PDFs without paras)
        paras = [l.strip() for l in text.split("\n") if l.strip()]
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) + 1 > _CHUNK_CHARS and cur:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks[:200]  # sanity cap per document


@router.post("/training/upload", dependencies=[Depends(require_feature("service_agent"))], status_code=201)
async def upload_training_doc(
    file: UploadFile = File(...), db: AsyncSession = Depends(get_db)
) -> dict:
    """PDF/TXT/CSV/MD -> text -> chunks the agent retrieves at answer time."""
    from pathlib import PurePosixPath

    from app.models import DocChunk

    name = PurePosixPath(file.filename or "document").name[:160]
    suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if suffix not in _ALLOWED_DOC_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Only PDF, TXT, CSV and MD files are supported (convert DOCX to PDF first)",
        )
    blob = await file.read()
    if len(blob) > _MAX_DOC_BYTES:
        raise HTTPException(status_code=400, detail="File is too large (max 8 MB)")

    if suffix == ".pdf":
        import io as _io

        from pypdf import PdfReader

        try:
            reader = PdfReader(_io.BytesIO(blob))
            text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception:
            raise HTTPException(status_code=400, detail="Could not read this PDF")
    else:
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            text = blob.decode("latin-1", errors="replace")

    chunks = _chunk_text(text)
    if not chunks:
        raise HTTPException(status_code=400, detail="No readable text found in the file")

    # replace any previous upload of the same filename
    from sqlalchemy import delete as sqldelete

    await db.execute(sqldelete(DocChunk).where(DocChunk.document == name))
    for i, c in enumerate(chunks):
        db.add(DocChunk(document=name, chunk_index=i, content=c[:4000]))
    await db.commit()
    await audit.record(
        actor_role="admin", actor="dashboard", action="training_doc_uploaded",
        args={"document": name}, result=f"{len(chunks)} chunks",
    )
    return {"document": name, "chunks": len(chunks)}


@router.get("/training/docs", dependencies=[Depends(require_feature("service_agent"))])
async def list_training_docs(db: AsyncSession = Depends(get_db)) -> list[dict]:
    from app.models import DocChunk

    rows = (
        await db.execute(
            select(DocChunk.document, func.count(), func.max(DocChunk.created_at))
            .group_by(DocChunk.document)
            .order_by(func.max(DocChunk.created_at).desc())
        )
    ).all()
    return [
        {"document": d, "chunks": n, "uploaded_at": at.isoformat()} for d, n, at in rows
    ]


@router.delete("/training/docs/{document}", dependencies=[Depends(require_feature("service_agent"))])
async def delete_training_doc(document: str, db: AsyncSession = Depends(get_db)) -> dict:
    from sqlalchemy import delete as sqldelete

    from app.models import DocChunk

    r = await db.execute(sqldelete(DocChunk).where(DocChunk.document == document))
    await db.commit()
    # bulk DELETE skips the ORM unit of work, so the cache event never fires
    from app.services.knowledge import invalidate as _drop_knowledge_cache

    _drop_knowledge_cache()
    if r.rowcount == 0:
        raise HTTPException(status_code=404, detail="document not found")
    return {"deleted": document, "chunks": r.rowcount}


@router.get("/training/teachme", dependencies=[Depends(require_feature("service_agent"))])
async def teachme_queue(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (
        await db.execute(
            select(OpenQuestion, Customer)
            .join(Customer, Customer.id == OpenQuestion.customer_id)
            .where(OpenQuestion.status == "open")
            .order_by(OpenQuestion.asked_at.desc())
            .limit(200)
        )
    ).all()
    return [
        {
            "id": str(oq.id), "question": oq.question,
            "customer": cust.name or cust.phone, "phone": cust.phone,
            "asked_at": oq.asked_at.isoformat(),
        }
        for oq, cust in rows
    ]


class TeachIn(BaseModel):
    answer: str = Field(min_length=2)
    save_as_faq: bool = True
    send_to_customer: bool = True


@router.post("/training/teachme/{qid}/answer", dependencies=[Depends(require_feature("service_agent"))])
async def answer_teachme(qid: str, body: TeachIn, db: AsyncSession = Depends(get_db)) -> dict:
    oq = await db.get(OpenQuestion, uuid_module.UUID(qid))
    if oq is None:
        raise HTTPException(status_code=404, detail="not found")
    oq.status = "answered"
    oq.answer = body.answer
    oq.answered_at = datetime.now(timezone.utc)
    if body.save_as_faq:
        db.add(FaqEntry(question=oq.question[:2000], answer=body.answer))
    await db.commit()

    sent = False
    if body.send_to_customer:
        cust = await db.get(Customer, oq.customer_id)
        if cust:
            from app.services.messages import get_message
            from app.services.whatsapp import SendError, send_message

            try:
                await send_message(
                    db, to_phone=cust.phone,
                    text=get_message("relay_message_customer", message=body.answer),
                )
                sent = True
            except SendError:
                log.info("teachme_answer_not_sent", phone=cust.phone)
    await audit.record(
        actor_role="admin", actor="dashboard", action="teachme_answered",
        args={"question": oq.question[:150]}, result=f"faq={body.save_as_faq} sent={sent}",
    )
    return {"answered": True, "sent_to_customer": sent}


# ---------------------------------------------------------------------------
# Message formats (owner-edited copy for free-form sends, hot-reloaded)
# ---------------------------------------------------------------------------

_MSG_LABELS = {
    "order_confirmed_bill": "New order — bill details",
    "thankyou_rating": "After delivery — thank you + rating",
    "order_ready": "Order ready",
    "order_out_for_delivery": "Out for delivery",
    "delay_notice": "Delivery date changed",
    "payment_reminder": "Payment reminder (polite)",
    "payment_reminder_firm": "Payment reminder (firm)",
    "payment_thanks": "Payment received — thank you",
    "partial_delivery": "Part of the order delivered",
    "service_thanks": "Thank you for the service",
    "review_request": "Please review us on Google",
    "ack_received": "Fallback acknowledgement",
    "complaint_ack": "Complaint apology",
    "escalated_ack": "Escalated to manager",
    "rate_good_reply": "Rating reply — great",
    "rate_mid_reply": "Rating reply — okay",
    "rate_bad_reply": "Rating reply — bad",
    "stop_confirmed": "STOP confirmation",
    "start_confirmed": "START welcome back",
}


@router.get("/message-formats")
async def message_formats() -> list[dict]:
    from app.services.messages import (
        DEFAULT_LANG,
        EDITABLE_KEYS,
        MESSAGES,
        allowed_placeholders,
        get_override,
        lang_for,
    )

    out = []
    for key in EDITABLE_KEYS:
        default = MESSAGES[key].get(lang_for(key)) or MESSAGES[key].get(DEFAULT_LANG) or MESSAGES[key]["en"]
        out.append(
            {
                "key": key,
                "label": _MSG_LABELS.get(key, key),
                "default": default,
                "current": get_override(key) or default,
                "overridden": get_override(key) is not None,
                "placeholders": sorted(allowed_placeholders(key)),
            }
        )
    return out


class MsgFormatIn(BaseModel):
    key: str
    text: str = ""  # empty = reset to default


@router.put("/message-formats")
async def put_message_format(body: MsgFormatIn, db: AsyncSession = Depends(get_db)) -> dict:
    import string

    from app.services.messages import EDITABLE_KEYS, allowed_placeholders, set_override

    if body.key not in EDITABLE_KEYS:
        raise HTTPException(status_code=400, detail="This message is not editable")
    text = body.text.strip()
    if text:
        allowed = allowed_placeholders(body.key)
        used = {
            f for _, f, _, _ in string.Formatter().parse(text) if f
        }
        bad = used - allowed
        if bad:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown placeholder(s): {', '.join(sorted(bad))}. "
                f"Allowed: {', '.join(sorted('{' + a + '}' for a in allowed))}",
            )
    # persist + hot-apply
    overrides = dict(await app_settings.get(db, "message_overrides") or {})
    if text:
        overrides[body.key] = text
    else:
        overrides.pop(body.key, None)
    await app_settings.set_value(db, "message_overrides", overrides)
    set_override(body.key, text or None)
    await audit.record(
        actor_role="admin", actor="dashboard",
        action="message_format_edited" if text else "message_format_reset",
        args={"key": body.key}, result=text[:150],
    )
    return {"key": body.key, "overridden": bool(text)}


# ---------------------------------------------------------------------------
# WhatsApp template studio (create -> submit to Meta -> track approval)
# ---------------------------------------------------------------------------

from app.services import wa_templates


# Template counts and quality rating move on Meta's timescale (hours), not
# ours. Without this every dashboard load — every manager, every refresh —
# was two more Graph calls.
_WA_STATS_TTL = 60.0
_wa_stats_cache: dict[str, tuple[float, dict]] = {}


def _wa_stats_cached(key: str) -> dict | None:
    hit = _wa_stats_cache.get(key)
    if hit is None:
        return None
    at, payload = hit
    if _time.monotonic() - at > _WA_STATS_TTL:
        _wa_stats_cache.pop(key, None)
        return None
    return payload


@router.get("/usage", dependencies=[Depends(require_feature("reports"))])
async def llm_usage(db: AsyncSession = Depends(get_db), days: int = Query(default=30, ge=1, le=180)) -> dict:
    """AI usage and cost: today, this month, per model, and where it's going.

    Money is derived here from the settings rate card, so correcting a rate
    re-prices the whole history instead of leaving stale numbers behind.
    """
    from datetime import datetime, timedelta, timezone

    from app.models import LlmUsage

    IST = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(IST)
    day_start = now_ist.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    month_start = now_ist.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    window_start = (now_ist - timedelta(days=days)).astimezone(timezone.utc)

    rates = await app_settings.get(db, "llm_rates") or {}
    cap = int(await app_settings.get(db, "llm_daily_request_cap") or 0)
    budget = float(await app_settings.get(db, "llm_monthly_budget_usd") or 0)

    def _cost(model: str, tin: int, tout: int) -> float:
        r = rates.get(model) or {}
        return (tin / 1_000_000) * float(r.get("in", 0)) + (tout / 1_000_000) * float(r.get("out", 0))

    async def _totals(since) -> dict:
        rows = (
            await db.execute(
                select(
                    LlmUsage.model,
                    func.count().label("calls"),
                    func.coalesce(func.sum(LlmUsage.input_tokens), 0),
                    func.coalesce(func.sum(LlmUsage.output_tokens), 0),
                )
                .where(LlmUsage.at >= since)
                .group_by(LlmUsage.model)
            )
        ).all()
        by_model, calls, tin, tout, cost = [], 0, 0, 0, 0.0
        for model, n, i, o in rows:
            c = _cost(model, i, o)
            by_model.append({
                "model": model, "calls": n, "input_tokens": int(i),
                "output_tokens": int(o), "cost_usd": round(c, 4),
                "priced": bool((rates.get(model) or {}).get("in") or (rates.get(model) or {}).get("out")),
            })
            calls += n; tin += int(i); tout += int(o); cost += c
        by_model.sort(key=lambda m: (-m["cost_usd"], -m["calls"]))
        return {
            "calls": calls, "input_tokens": tin, "output_tokens": tout,
            "cost_usd": round(cost, 4), "by_model": by_model,
        }

    today, month = await _totals(day_start), await _totals(month_start)

    # where the spend goes — useful for deciding what to trim
    # grouped by purpose AND model — cost is per-model, so collapsing the
    # model away first would price everything at zero
    purpose_rows = (
        await db.execute(
            select(
                LlmUsage.purpose, LlmUsage.model, func.count(),
                func.coalesce(func.sum(LlmUsage.input_tokens), 0),
                func.coalesce(func.sum(LlmUsage.output_tokens), 0),
            )
            .where(LlmUsage.at >= month_start)
            .group_by(LlmUsage.purpose, LlmUsage.model)
        )
    ).all()
    acc: dict[str, dict] = {}
    for p, model, n, i, o in purpose_rows:
        e = acc.setdefault(p, {"purpose": p, "calls": 0, "tokens": 0, "cost_usd": 0.0})
        e["calls"] += n
        e["tokens"] += int(i) + int(o)
        e["cost_usd"] += _cost(model, int(i), int(o))
    by_purpose = sorted(acc.values(), key=lambda x: -x["tokens"])
    for e in by_purpose:
        e["cost_usd"] = round(e["cost_usd"], 4)

    # daily series for the chart
    series_rows = (
        await db.execute(
            select(
                func.date_trunc("day", LlmUsage.at).label("d"),
                func.count(),
                func.coalesce(func.sum(LlmUsage.input_tokens), 0),
                func.coalesce(func.sum(LlmUsage.output_tokens), 0),
            )
            .where(LlmUsage.at >= window_start)
            .group_by("d").order_by("d")
        )
    ).all()
    series = [
        {"date": d.date().isoformat(), "calls": n, "tokens": int(i) + int(o)}
        for d, n, i, o in series_rows
    ]

    # simple run-rate projection for the rest of the month
    day_of_month = now_ist.day
    projected = round(month["cost_usd"] / day_of_month * 30, 4) if day_of_month else 0.0

    from app.services.llm_client import MODEL_CHEAP, MODEL_SMART, PROVIDER

    return {
        "provider": PROVIDER,
        "models": {"cheap": MODEL_CHEAP, "smart": MODEL_SMART},
        "today": today,
        "month": month,
        "by_purpose": by_purpose,
        "series": series,
        "daily_request_cap": cap,
        "calls_left_today": max(cap - today["calls"], 0) if cap else None,
        "monthly_budget_usd": budget,
        "projected_month_usd": projected,
        # honest flag: free-tier models price at 0, so a 0 total is not a bug
        "all_free": month["cost_usd"] == 0,
    }


class TaskIn(BaseModel):
    title: str = Field(min_length=2, max_length=2000)
    staff: str | None = None          # name or phone
    order_number: str | None = None
    urgent: bool = False


@router.get("/tasks")
async def list_tasks(
    db: AsyncSession = Depends(get_db),
    status: str = Query(default="OPEN", pattern="^(OPEN|DONE|CANCELLED|ALL)$"),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    """Every assigned job with who owns it and how long it has been sitting."""
    from datetime import datetime, timezone

    from app.models import Order as _O, Staff as _S, Task

    q = select(Task).order_by(Task.status, Task.urgent.desc(), Task.created_at.desc()).limit(limit)
    if status != "ALL":
        q = q.where(Task.status == status)
    rows = (await db.execute(q)).scalars().all()

    # Staff ke sawaal. Ab tak ye DB mein likhe jaate the aur kahin dikhte
    # nahi the — owner ko khabar sirf WhatsApp par jaati thi, aur API juda
    # na ho to kahin nahi. Ek hi query mein sabke liye, warna sau task par
    # sau round-trip.
    #
    # "Bina jawab ka" = us task ka aakhri sandesh staff ka hai. Ginti nahi,