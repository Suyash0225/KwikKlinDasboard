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
import httpx
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
    Lead,
    CampaignRecipient,
    Rate,
    Order,
    OrderStatus,
    PaymentStatus,
)
from app.routers.orders import require_admin_owner, require_feature
from app.services import app_settings, audit, wa_templates
from app.services.marketing import (
    _campaign_eligibility,
    campaign_stats,
    compute_segments,
    queue_campaign,
    send_campaign,
)
from app.utils.phone import normalize_phone
from app.utils.dates import IST

router = APIRouter(
    prefix="/admin/api", tags=["agent-admin"], dependencies=[Depends(require_admin_owner)]
)
log = structlog.get_logger()


# ---------------------------------------------------------------------------
# AI Business Manager — read-only daily operating brief
# ---------------------------------------------------------------------------


@router.get("/business-manager/briefing")
async def business_manager_briefing(db: AsyncSession = Depends(get_db)) -> dict:
    """Return a deterministic, DB-grounded manager brief; never sends messages.

    Keep this endpoint read-only and avoid exposing Order.notes, phone numbers,
    or internal staff identifiers. Dates use the shop's India-local calendar.
    """
    today = datetime.now(IST).date()
    rows = (
        await db.execute(
            select(Order, Customer)
            .join(Customer, Customer.id == Order.customer_id)
            .order_by(Order.expected_delivery.asc().nullslast(), Order.created_at.asc())
        )
    ).all()

    active_statuses = {OrderStatus.RECEIVED, OrderStatus.PICKUP_ASSIGNED,
                       OrderStatus.PICKED_UP, OrderStatus.IN_WASH,
                       OrderStatus.IN_DRY, OrderStatus.IN_IRON,
                       OrderStatus.READY, OrderStatus.OUT_FOR_DELIVERY,
                       OrderStatus.ON_HOLD}
    overdue = []
    due_today = []
    unpaid = []
    active_count = 0
    overdue_value = Decimal("0")
    unpaid_balance = Decimal("0")

    def order_summary(order: Order, customer: Customer) -> dict:
        total = Decimal(str(order.total_amount or 0))
        paid = Decimal(str(order.amount_paid or 0))
        return {
            "order_number": order.order_number,
            "customer": customer.name or "Customer",
            "status": order.status.name if order.status else "UNKNOWN",
            "expected_delivery": order.expected_delivery.isoformat() if order.expected_delivery else None,
            "amount_due": float(max(Decimal("0"), total - paid)),
            "priority": order.priority or "normal",
        }

    for order, customer in rows:
        status = order.status
        total = Decimal(str(order.total_amount or 0))
        paid = Decimal(str(order.amount_paid or 0))
        balance = max(Decimal("0"), total - paid)
        if status in active_statuses:
            active_count += 1
        if (status in active_statuses and order.expected_delivery
                and order.expected_delivery < today):
            overdue.append(order_summary(order, customer))
            overdue_value += balance
        elif (status in active_statuses and order.expected_delivery == today):
            due_today.append(order_summary(order, customer))
        if order.payment_status in (PaymentStatus.UNPAID, PaymentStatus.PARTIAL) and balance > 0:
            unpaid.append(order_summary(order, customer))
            unpaid_balance += balance

    overdue.sort(key=lambda item: (item["expected_delivery"] or "", item["priority"] != "urgent"))
    unpaid.sort(key=lambda item: item["amount_due"], reverse=True)
    priorities = []
    if overdue:
        priorities.append({
            "key": "overdue_orders",
            "count": len(overdue),
            "message": f"{len(overdue)} order(s) overdue; delivery commitments need review.",
        })
    if unpaid:
        priorities.append({
            "key": "pending_payments",
            "count": len(unpaid),
            "message": f"{len(unpaid)} bill(s) have pending balance.",
        })
    if due_today:
        priorities.append({
            "key": "due_today",
            "count": len(due_today),
            "message": f"{len(due_today)} order(s) are due for delivery today.",
        })
    if not priorities:
        priorities.append({
            "key": "all_clear",
            "count": 0,
            "message": "No overdue orders or pending balances were found.",
        })

    return {
        "date": today.isoformat(),
        "generated_at": datetime.now(IST).isoformat(),
        "summary": {
            "active_orders": active_count,
            "overdue_orders": len(overdue),
            "due_today": len(due_today),
            "pending_payment_orders": len(unpaid),
            "overdue_balance": float(overdue_value),
            "pending_payment_balance": float(unpaid_balance),
        },
        "priorities": priorities,
        "overdue_orders": overdue[:10],
        "due_today_orders": due_today[:10],
        "pending_payments": unpaid[:10],
        "read_only": True,
    }


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
                {
                    "name": m["name"], "phone": m["phone"],
                    "lifetime_paid": float(m["lifetime_paid"]),
                    "last_visit_at": (
                        m["last_visit_at"].isoformat()
                        if hasattr(m.get("last_visit_at"), "isoformat")
                        else m.get("last_visit_at")
                    ),
                    "average_visit_gap_days": m.get("average_visit_gap_days"),
                    "usual_items": m.get("usual_items", []),
                    "seasonal_items": m.get("seasonal_items", {}),
                }
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
        if c.status not in ("draft", "suggested"):
            stats = await campaign_stats(db, c.id)
        out.append(
            {
                "id": str(c.id),
                "name": c.name,
                "segment": c.segment,
                "campaign_type": getattr(c,"campaign_type","promotion"),
                "rule_flags": c.rule_flags or {},
                "frequency_bypassed": not (c.rule_flags or {}).get("respect_frequency",True),
                "complaints_bypassed": not (c.rule_flags or {}).get("skip_complaints",True),
                "scheduled_at": c.scheduled_at.isoformat() if c.scheduled_at else None,
                "status": c.status,
                "message_text": c.message_text,
                "rationale": c.rationale,
                "coupon_code": c.coupon_code,
                "created_by": c.created_by,
                "created_at": c.created_at.isoformat(),
                "sent_at": c.sent_at.isoformat() if c.sent_at else None,
                "stats": stats,
                "creative_file": (c.stats or {}).get("creative_file"),
                "selected_customer_count": len((c.stats or {}).get("selected_customer_ids") or []),
            }
        )
    return out


@router.get("/campaigns/{campaign_id}/recipients", dependencies=[Depends(require_feature("campaigns"))])
async def campaign_recipients(campaign_id:str,db:AsyncSession=Depends(get_db))->dict:
    try:cid=uuid_module.UUID(campaign_id)
    except ValueError as exc:raise HTTPException(status_code=400,detail="invalid campaign id") from exc
    campaign=await db.get(Campaign,cid)
    if campaign is None:raise HTTPException(status_code=404,detail="campaign not found")
    rows=(await db.execute(select(CampaignRecipient,Customer).join(Customer,Customer.id==CampaignRecipient.customer_id).where(CampaignRecipient.campaign_id==cid).order_by(Customer.name.asc()))).all()
    return {"campaign_id":str(cid),"recipients":[{"customer_id":str(recipient.customer_id),"name":customer.name or "Customer","phone":customer.phone,"last_order_date":None,"status":recipient.status,"reason":recipient.detail or ""} for recipient,customer in rows]}

@router.get("/campaigns/overview", dependencies=[Depends(require_feature("campaigns"))])
async def campaigns_overview(db: AsyncSession = Depends(get_db)) -> dict:
    total_campaigns = int((await db.execute(select(func.count()).select_from(Campaign))).scalar_one())
    status_rows = (await db.execute(select(Campaign.status, func.count()).group_by(Campaign.status))).all()
    campaign_status = {str(status): int(count) for status, count in status_rows}
    recipient_rows = (await db.execute(select(CampaignRecipient.status, func.count()).group_by(CampaignRecipient.status))).all()
    counts = {str(status): int(count) for status, count in recipient_rows}
    sent = sum(counts.get(s, 0) for s in ("sent", "delivered", "read", "replied"))
    delivered = sum(counts.get(s, 0) for s in ("delivered", "read", "replied"))
    running_rows = (await db.execute(
        select(Campaign).where(Campaign.status.in_(("approved", "sending")))
        .order_by(Campaign.created_at.desc()).limit(10)
    )).scalars().all()
    running = []
    for campaign in running_rows:
        stats = await campaign_stats(db, campaign.id)
        processed = sum(stats.get(s, 0) for s in ("sent","delivered","read","replied","failed","skipped"))
        total = sum(stats.get(s, 0) for s in ("queued","sent","delivered","read","replied","failed","skipped"))
        running.append({
            "id": str(campaign.id), "name": campaign.name, "status": campaign.status,
            "processed": processed, "total": total,
            "sent": sum(stats.get(s, 0) for s in ("sent","delivered","read","replied")),
            "delivered": sum(stats.get(s, 0) for s in ("delivered","read","replied")),
            "failed": stats.get("failed", 0), "skipped": stats.get("skipped", 0),
            "replies": stats.get("replied", 0),
            "progress": round(processed * 100 / total, 1) if total else 0,
        })
    return {
        "total_campaigns": total_campaigns,
        "messages_sent": sent,
        "messages_delivered": delivered,
        "messages_failed": counts.get("failed", 0),
        "messages_skipped": counts.get("skipped", 0),
        "replies_received": counts.get("replied", 0),
        "currently_sending": len(running_rows),
        "delivery_rate": round(delivered * 100 / sent, 1) if sent else 0,
        "campaign_status": {
            "running": campaign_status.get("sending", 0) + campaign_status.get("approved", 0),
            "completed": campaign_status.get("sent", 0),
            "failed": campaign_status.get("failed", 0),
            "cancelled": campaign_status.get("cancelled", 0),
            "draft": campaign_status.get("draft", 0),
        },
        "running_campaigns": running,
    }


@router.get("/campaigns/failed", dependencies=[Depends(require_feature("campaigns"))])
async def campaign_failed_messages(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(
        select(CampaignRecipient, Campaign, Customer)
        .join(Campaign, Campaign.id == CampaignRecipient.campaign_id)
        .join(Customer, Customer.id == CampaignRecipient.customer_id)
        .where(CampaignRecipient.status == "failed")
        .order_by(CampaignRecipient.updated_at.desc())
        .limit(200)
    )).all()
    return [{
        "customer": cust.name or "Customer",
        "phone": cust.phone,
        "error": rec.detail or "Send failed",
        "time": rec.updated_at.isoformat() if rec.updated_at else None,
        "campaign": camp.name,
        "campaign_id": str(camp.id),
    } for rec, camp, cust in rows]


@router.get("/campaigns/{campaign_id}", dependencies=[Depends(require_feature("campaigns"))])
async def campaign_detail(campaign_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        cid = uuid_module.UUID(campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid campaign id") from exc
    campaign = await db.get(Campaign, cid)
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    stats = await campaign_stats(db, campaign.id)
    rows = (await db.execute(
        select(CampaignRecipient, Customer)
        .join(Customer, Customer.id == CampaignRecipient.customer_id)
        .where(CampaignRecipient.campaign_id == campaign.id)
        .order_by(CampaignRecipient.updated_at.desc())
    )).all()
    wamids = [rec.wa_message_id for rec, _ in rows if rec.wa_message_id]
    reply_rows = []
    if wamids:
        reply_rows = (await db.execute(
            select(Conversation.reply_to_wamid, Conversation.message_text, Conversation.created_at)
            .where(
                Conversation.direction == Direction.INBOUND,
                Conversation.reply_to_wamid.in_(wamids),
            )
            .order_by(Conversation.created_at.asc())
        )).all()
    replies = {wamid: {"text": text, "at": at} for wamid, text, at in reply_rows}
    recipients = [{
        "id": str(rec.id), "customer_id": str(cust.id),
        "customer": cust.name or "Customer", "phone": cust.phone,
        "status": rec.status, "detail": rec.detail,
        "wa_message_id": rec.wa_message_id,
        "status_at": rec.updated_at.isoformat() if rec.updated_at else None,
        "replied": rec.status == "replied",
        "reply": replies.get(rec.wa_message_id),
    } for rec, cust in rows]
    return {
        "id": str(campaign.id), "name": campaign.name, "segment": campaign.segment,
        "campaign_type": getattr(campaign,"campaign_type","promotion"), "rule_flags": campaign.rule_flags or {},
        "status": campaign.status, "message_text": campaign.message_text,
        "created_by": campaign.created_by,
        "created_at": campaign.created_at.isoformat() if campaign.created_at else None,
        "scheduled_at": campaign.scheduled_at.isoformat() if campaign.scheduled_at else None,
        "sent_at": campaign.sent_at.isoformat() if campaign.sent_at else None,
        "creative_file": (campaign.stats or {}).get("creative_file"),
        "selected_customer_count": len((campaign.stats or {}).get("selected_customer_ids") or []),
        "stats": stats, "recipients": recipients,
    }


@router.delete("/campaigns/{campaign_id}", dependencies=[Depends(require_admin_owner), Depends(require_feature("campaigns"))])
async def delete_campaign(campaign_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        cid = uuid_module.UUID(campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid campaign id") from exc
    campaign = await db.get(Campaign, cid)
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    if campaign.status in ("approved", "sending"):
        raise HTTPException(status_code=409, detail="cancel the running campaign before deleting it")
    creative_file = (campaign.stats or {}).get("creative_file")
    await db.execute(delete(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign.id))
    await db.execute(update(Coupon).where(Coupon.campaign_id == campaign.id).values(campaign_id=None))
    campaign_name = campaign.name
    await db.delete(campaign)
    await db.commit()
    if creative_file:
        try:
            (Path(__file__).resolve().parent.parent / "media" / creative_file).unlink(missing_ok=True)
        except Exception:
            log.warning("campaign_creative_delete_failed", campaign=str(cid))
    await audit.record(
        actor_role="admin", actor="dashboard", action="campaign_deleted",
        args={"campaign": str(cid), "name": campaign_name}, result="deleted",
    )
    return {"ok": True, "id": campaign_id}


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
    message_text: str = Field(default="", max_length=4000)
    selected_customer_ids: list[str] = Field(default_factory=list, max_length=500)
    coupon_code: str | None = None
    creative_file: str | None = None
    campaign_type: str = Field(default="promotion", pattern="^(wish|promotion|payment_reminder)$")
    respect_frequency: bool = True
    skip_complaints: bool = True
    skip_blocked: bool = True
    include_opted_out: bool = False
    scheduled_at: datetime | None = None


@router.post("/campaigns/preview", dependencies=[Depends(require_feature("campaigns"))])
async def preview_campaign(body: CampaignIn, db: AsyncSession = Depends(get_db)) -> dict:
    """Read-only audience estimate; it never writes recipient rows or sends messages."""
    if not body.message_text.strip() and not body.creative_file:
        raise HTTPException(status_code=400, detail="Add message text or a supported image before previewing.")
    if body.creative_file:
        if not re.fullmatch(r"campaign-[a-f0-9]{32}\.(?:png|jpg|jpeg)",body.creative_file):
            raise HTTPException(status_code=400,detail="invalid campaign creative")
        from pathlib import Path
        if not (Path(__file__).resolve().parent.parent/"media"/body.creative_file).is_file():
            raise HTTPException(status_code=400,detail="Campaign image is missing; upload it again.")
    if body.include_opted_out:
        raise HTTPException(status_code=400, detail="Opted-out customers cannot be included in campaigns.")
    segment="all_active" if body.campaign_type=="wish" else ("outstanding_dues" if body.campaign_type=="payment_reminder" else body.segment)
    temp=Campaign(name=body.name,segment=segment,message_text=body.message_text,campaign_type=body.campaign_type,rule_flags={"respect_frequency":False if body.campaign_type in ("wish","payment_reminder") else body.respect_frequency,"skip_complaints":body.skip_complaints,"skip_blocked":True})
    if segment=="selected":
        try: ids=[uuid_module.UUID(x) for x in body.selected_customer_ids]
        except (ValueError,TypeError): raise HTTPException(status_code=400,detail="invalid selected customer")
        rows=(await db.execute(select(Customer).where(Customer.id.in_(ids),Customer.is_active))).scalars().all()
        if len(rows)!=len(set(ids)): raise HTTPException(status_code=400,detail="selected customers are invalid or inactive")
        targets=[{"id":x.id,"name":x.name or "Customer","phone":x.phone,"last_order_at":None} for x in rows]
    else:
        segs=await compute_segments(db); targets=segs.get(segment,[])
    allow_active=segment in ("selected","all_active") or body.campaign_type=="wish"
    recipients=[]; skipped={}; skipped_recipients=[]
    for row in targets:
        ok,reason=await _campaign_eligibility(db,row["id"],temp,allow_active_orders=allow_active)
        item={"customer_id":str(row["id"]),"name":row.get("name") or "Customer","phone":row.get("phone") or "","last_order_date":row.get("last_order_at").isoformat() if row.get("last_order_at") else None,"reason":reason}
        if ok: recipients.append(item)
        else:
            skipped[reason]=skipped.get(reason,0)+1
            skipped_recipients.append(item)
    return {"total_in_audience":len(targets),"will_receive":len(recipients),"skipped":skipped,"recipients":recipients,"skipped_recipients":skipped_recipients}


@router.post("/campaigns/test-send", dependencies=[Depends(require_feature("campaigns"))])
async def test_campaign_send(body: CampaignIn, db: AsyncSession = Depends(get_db)) -> dict:
    """Send the reviewed creative only to the configured manager/admin number."""
    creative=body.creative_file or ""
    if not body.message_text.strip() and not creative:
        raise HTTPException(status_code=400,detail="Add message text or a supported image before test sending.")
    if creative and not re.fullmatch(r"campaign-[a-f0-9]{32}\.(?:png|jpg|jpeg)",creative):
        raise HTTPException(status_code=400,detail="invalid campaign creative")
    from pathlib import Path
    from app.services.tenant_context import manager_phone
    from app.services.whatsapp import SendError, send_image, send_message
    raw_phone=manager_phone()
    if not raw_phone: raise HTTPException(status_code=400,detail="No saved admin WhatsApp number is configured.")
    phone=normalize_phone(raw_phone)
    if not phone: raise HTTPException(status_code=400,detail="No saved admin WhatsApp number is configured.")
    try:
        if creative:
            media=Path(__file__).resolve().parent.parent/"media"/creative
            if not media.is_file(): raise HTTPException(status_code=400,detail="Campaign image is no longer available; upload it again.")
            wamid=await send_image(db,to_phone=phone,file_path=str(media),mime_type="image/jpeg" if media.suffix.lower() in (".jpg",".jpeg") else "image/png",caption=body.message_text,local_url=f"/admin/media/{creative}",sent_by="manager",intent="promotional")
        else:
            wamid=await send_message(db,to_phone=phone,text=body.message_text,category="marketing",sent_by="manager")
    except SendError as exc:
        raise HTTPException(status_code=502,detail="Test message could not be accepted. Review the number/provider status and retry safely.") from exc
    await audit.record(actor_role="admin",actor="dashboard",action="campaign_test_send",args={"campaign_name":body.name,"destination":phone},result="provider accepted; delivery not confirmed")
    return {"status":"accepted","message_id":wamid,"destination":phone,"note":"Provider accepted the request; delivery is not confirmed."}


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
        "copy <= 3 short lines. For Google Business local discovery, naturally mention "
        "a relevant laundry/dry-cleaning service and Varanasi where it fits; avoid keyword "
        "stuffing. Google Business copy must not include a phone number, fake address, "
        "fake review, or fabricated claim. Return only JSON matching the schema."
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
            "gmb_body": "Book laundry and dry cleaning with Kwik Klin in Varanasi this week.",
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
    # Run the same deterministic phone/link/hashtag sanitizer used by daily GBP
    # posts. AI instructions alone are not a policy gate.
    from app.services.social import check_google_image, draw_poster, enforce_google_rules
    draft["gmb_body"] = enforce_google_rules(draft["gmb_body"])[:700]
    # Add lightweight local-search context when the model omits it. Keep this
    # to one natural service/location phrase; never repeat keywords or fabricate
    # a street address, ranking claim, review, or service not in our rate card.
    seo_body = draft["gmb_body"]
    lowered = seo_body.casefold()
    has_service = any(term in lowered for term in ("laundry", "dry clean", "dry-clean", "wash and fold", "garment care"))
    has_location = "varanasi" in lowered
    if not has_service or not has_location:
        if not has_service and not has_location:
            suffix = " Laundry and dry cleaning services in Varanasi."
        elif not has_service:
            suffix = " Laundry and dry cleaning services."
        else:
            suffix = " Serving Varanasi."
        if suffix.strip().casefold() not in lowered:
            seo_body = (seo_body.rstrip(" .") + suffix) if seo_body else suffix.strip()
    draft["gmb_body"] = enforce_google_rules(seo_body)[:700]
    draft["gmb_title"] = enforce_google_rules(draft["gmb_title"])[:120]
    draft["gmb_cta"] = enforce_google_rules(draft["gmb_cta"])[:60]
    # Generate a branded phone-free creative and validate it before exposing it
    # as a reusable campaign image. A failed image check falls back to text.
    creative_file = None
    try:
        from pathlib import Path
        media_dir = Path(__file__).resolve().parent.parent / "media"
        media_dir.mkdir(exist_ok=True)
        candidate = f"campaign-{uuid_module.uuid4().hex}.png"
        image_path = media_dir / candidate
        draw_poster(
            str(draft.get("gmb_title") or "Kwik Klin"),
            str(draft.get("gmb_title") or "Fresh clothes, less hassle"),
            str(draft.get("gmb_body") or "Book your next laundry service."),
            image_path,
            show_phone=False,
        )
        image_problems = check_google_image(image_path)
        if image_problems:
            log.warning("campaign_creative_validation_failed", problems=image_problems)
            image_path.unlink(missing_ok=True)
        else:
            creative_file = candidate
    except Exception:
        log.exception("campaign_creative_generation_failed")
    draft["creative_file"] = creative_file
    return draft


@router.post("/campaigns", dependencies=[Depends(require_feature("campaigns"))], status_code=201)
async def create_campaign(body: CampaignIn, db: AsyncSession = Depends(get_db)) -> dict:
    creative = body.creative_file or ""
    if not body.message_text.strip() and not creative: raise HTTPException(status_code=400, detail="Add message text or upload a supported campaign image.")
    if creative:
        if not re.fullmatch(r"campaign-[a-f0-9]{32}\.(?:png|jpg|jpeg)",creative): raise HTTPException(status_code=400,detail="invalid campaign creative")
        from pathlib import Path
        if not (Path(__file__).resolve().parent.parent/"media"/creative).is_file(): raise HTTPException(status_code=400,detail="Campaign image is missing; upload it again.")
    if body.include_opted_out: raise HTTPException(status_code=400, detail="Opted-out customers cannot be included in campaigns.")
    segment="all_active" if body.campaign_type=="wish" else ("outstanding_dues" if body.campaign_type=="payment_reminder" else body.segment)
    rule_flags={"respect_frequency":False if body.campaign_type in ("wish","payment_reminder") else bool(body.respect_frequency),"skip_complaints":bool(body.skip_complaints),"skip_blocked":True,"include_opted_out":False}
    if segment == "selected":
        if not body.selected_customer_ids:
            raise HTTPException(status_code=400, detail="select at least one customer")
        try:
            selected_ids = [uuid_module.UUID(str(x)) for x in body.selected_customer_ids]
        except (ValueError, AttributeError, TypeError):
            raise HTTPException(status_code=400, detail="invalid selected customer")

        # Resolve the selection against this tenant before persisting it. The
        # campaign may only reference real, active customers visible to the
        # authenticated tenant; never trust client-supplied IDs blindly.
        selected_rows = (
            await db.execute(
                select(Customer.id).where(
                    Customer.id.in_(selected_ids),
                    Customer.is_active,
                )
            )
        ).scalars().all()
        if len(selected_rows) != len(set(selected_ids)):
            raise HTTPException(status_code=400, detail="one or more selected customers are invalid or inactive")

        stats = {"selected_customer_ids": [str(x) for x in selected_ids]}
        if creative:
            stats["creative_file"] = creative
    else:
        stats = {"creative_file": creative} if creative else None

    scheduled_at=body.scheduled_at
    if scheduled_at and scheduled_at.tzinfo is None:scheduled_at=scheduled_at.replace(tzinfo=timezone.utc)
    scheduled=bool(scheduled_at and scheduled_at>datetime.now(timezone.utc))
    c=Campaign(name=body.name,segment=segment,message_text=body.message_text,campaign_type=body.campaign_type,rule_flags=rule_flags,scheduled_at=scheduled_at,coupon_code=body.coupon_code,status="scheduled" if scheduled else "draft",created_by="owner",stats=stats)
    db.add(c)
    await db.commit()
    return {"id":str(c.id),"status":c.status,"scheduled_at":c.scheduled_at.isoformat() if c.scheduled_at else None}


@router.post("/campaigns/{campaign_id}/approve", dependencies=[Depends(require_feature("campaigns"))])
async def approve_campaign(campaign_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    c = await db.get(Campaign, uuid_module.UUID(campaign_id))
    if c is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    if c.status=="scheduled" and c.scheduled_at and c.scheduled_at>datetime.now(timezone.utc):return {"queued":0,"status":"scheduled","scheduled_at":c.scheduled_at.isoformat()}
    if c.status not in ("draft", "suggested"):
        raise HTTPException(status_code=409, detail=f"campaign is {c.status}")
    if c.segment == "selected" and not (c.stats or {}).get("selected_customer_ids"):
        raise HTTPException(status_code=400, detail="selected-customer campaign has no recipients")
    c.status = "approved"
    await db.commit()
    queued = await queue_campaign(db, c)
    if queued <= 0:
        # Do not start a worker that can mark an empty campaign as sent.
        # The dashboard gets an actionable result instead of a false success.
        c.status = "failed"
        await db.commit()
        await audit.record(
            actor_role="admin", actor="dashboard", action="campaign_approval_blocked",
            args={"campaign": c.name}, result="no eligible recipients",
        )
        return {
            "queued": 0,
            "status": "failed",
            "message": "No eligible recipients. Check segment, opt-outs, frequency caps, and active orders.",
        }
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
                    intent="transactional", outbound_kind="service_reply",
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
# ---------------------------------------------------------------------------
# WhatsApp (WAHA/NOWEB only)
# ---------------------------------------------------------------------------

# WAHA is the only active WhatsApp transport for Kwik Klin. Meta/Graph is not
# queried by the admin dashboard or message flow.
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
                    func.count().filter(LlmUsage.ok.is_(False)).label("failed"),
                )
                .where(LlmUsage.at >= since)
                .group_by(LlmUsage.model)
            )
        ).all()
        by_model, calls, tin, tout, cost = [], 0, 0, 0, 0.0
        for model, n, i, o, failed in rows:
            c = _cost(model, i, o)
            by_model.append({
                "model": model, "calls": n, "input_tokens": int(i),
                "output_tokens": int(o), "cost_usd": round(c, 4),
                "failed": int(failed or 0),
                "priced": bool((rates.get(model) or {}).get("in") or (rates.get(model) or {}).get("out")),
            })
            calls += n; tin += int(i); tout += int(o); cost += c
        by_model.sort(key=lambda m: (-m["cost_usd"], -m["calls"]))
        return {
            "calls": calls, "input_tokens": tin, "output_tokens": tout,
            "failed_calls": sum(int(m.get("failed", 0)) for m in by_model),
            "successful_calls": max(calls - sum(int(m.get("failed", 0)) for m in by_model), 0),
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

    # Daily series is the accounting basis for INR. Every day's USD
    # usage is converted using that day's USD/INR historical mid-market rate,
    # rather than today's FX rate.
    series_rows = (
        await db.execute(
            select(
                func.date_trunc("day", func.timezone("Asia/Kolkata", LlmUsage.at)).label("d"),
                func.count(),
                func.coalesce(func.sum(LlmUsage.input_tokens), 0),
                func.coalesce(func.sum(LlmUsage.output_tokens), 0),
            )
            .where(LlmUsage.at >= window_start)
            .group_by("d").order_by("d")
        )
    ).all()

    fx_by_date: dict[str, float] = {}
    fx_source = "Frankfurter USD/INR historical mid-market"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            fx_resp = await client.get(
                "https://api.frankfurter.dev/v2/rates",
                params={
                    "from": window_start.date().isoformat(),
                    "to": now_ist.date().isoformat(),
                    "base": "USD",
                    "quotes": "INR",
                },
            )
            fx_resp.raise_for_status()
            for item in fx_resp.json():
                if item.get("date") and item.get("rate"):
                    fx_by_date[str(item["date"])] = float(item["rate"])
    except Exception:
        log.warning("llm_usage_fx_unavailable", exc_info=True)
        fx_source = "unavailable"

    def _fx_for_date(day: str) -> float | None:
        if day in fx_by_date:
            return fx_by_date[day]
        earlier = [d for d in fx_by_date if d <= day]
        return fx_by_date[max(earlier)] if earlier else None

    purpose_daily_rows = (
        await db.execute(
            select(
                func.date_trunc("day", func.timezone("Asia/Kolkata", LlmUsage.at)).label("d"),
                LlmUsage.purpose,
                LlmUsage.model,
                func.coalesce(func.sum(LlmUsage.input_tokens), 0),
                func.coalesce(func.sum(LlmUsage.output_tokens), 0),
            )
            .where(LlmUsage.at >= month_start)
            .group_by("d", LlmUsage.purpose, LlmUsage.model)
        )
    ).all()
    purpose_cost_inr: dict[str, float] = {}
    for d, purpose, model, i, o in purpose_daily_rows:
        fx = _fx_for_date(d.date().isoformat())
        if fx is not None:
            purpose_cost_inr[purpose] = purpose_cost_inr.get(purpose, 0.0) + _cost(model, int(i), int(o)) * fx

    by_purpose = sorted(acc.values(), key=lambda x: -x["tokens"])
    for e in by_purpose:
        e["cost_usd"] = round(e["cost_usd"], 4)
        e["cost_inr"] = round(purpose_cost_inr.get(e["purpose"], 0.0), 4)
    series = []
    for d, n, i, o in series_rows:
        day = d.date().isoformat()
        fx = _fx_for_date(day)
        series.append({
            "date": day,
            "calls": int(n),
            "input_tokens": int(i),
            "output_tokens": int(o),
            "tokens": int(i) + int(o),
            "fx_usd_inr": fx,
            "cost_usd": 0.0,
            "cost_inr": None,
        })

    # Price each day/model separately so mixed-model usage remains exact.
    daily_model_rows = (
        await db.execute(
            select(
                func.date_trunc("day", func.timezone("Asia/Kolkata", LlmUsage.at)).label("d"),
                LlmUsage.model,
                func.coalesce(func.sum(LlmUsage.input_tokens), 0),
                func.coalesce(func.sum(LlmUsage.output_tokens), 0),
            )
            .where(LlmUsage.at >= window_start)
            .group_by("d", LlmUsage.model)
        )
    ).all()
    daily_map = {row["date"]: row for row in series}
    for d, model, i, o in daily_model_rows:
        day = d.date().isoformat()
        row = daily_map.get(day)
        if row:
            row["cost_usd"] += _cost(model, int(i), int(o))
    for row in series:
        row["cost_usd"] = round(row["cost_usd"], 6)
        if row["fx_usd_inr"] is not None:
            row["cost_inr"] = round(row["cost_usd"] * row["fx_usd_inr"], 4)

    model_cost_inr: dict[str, float] = {}
    for d, model, i, o in daily_model_rows:
        day = d.date().isoformat()
        fx = _fx_for_date(day)
        if fx is not None:
            model_cost_inr[model] = model_cost_inr.get(model, 0.0) + _cost(model, int(i), int(o)) * fx
    for m in month["by_model"]:
        m["cost_inr"] = round(model_cost_inr.get(m["model"], 0.0), 4)

    month_cost_inr = round(
        sum((row["cost_inr"] or 0.0) for row in series
            if row["date"] >= month_start.date().isoformat()), 4
    )
    today_cost_inr = round(
        sum((row["cost_inr"] or 0.0) for row in series
            if row["date"] == now_ist.date().isoformat()), 4
    )

    # Attribution: customer-facing calls are linked to the customer and,
    # when exactly one active order existed at call time, that order.
    # Cost is always calculated per model before grouping, so mixed-model rows
    # cannot accidentally inherit a zero/free rate.
    from app.models import Customer as _Customer, Order as _Order

    attr_rows = (
        await db.execute(
            select(
                func.date_trunc("day", func.timezone("Asia/Kolkata", LlmUsage.at)).label("d"),
                LlmUsage.order_id,
                _Order.order_number,
                _Order.customer_id,
                _Customer.name,
                LlmUsage.model,
                func.count(),
                func.coalesce(func.sum(LlmUsage.input_tokens), 0),
                func.coalesce(func.sum(LlmUsage.output_tokens), 0),
                func.count().filter(LlmUsage.ok.is_(False)),
            )
            .outerjoin(_Order, _Order.id == LlmUsage.order_id)
            .outerjoin(_Customer, _Customer.id == LlmUsage.customer_id)
            .where(LlmUsage.at >= month_start)
            .group_by("d", LlmUsage.order_id, _Order.order_number, _Order.customer_id,
                      _Customer.name, LlmUsage.model)
        )
    ).all()

    order_acc: dict[str, dict] = {}
    customer_acc: dict[str, dict] = {}
    unattributed_calls = unattributed_tokens = 0
    unattributed_cost = 0.0
    unattributed_cost_inr = 0.0
    for d, oid, order_number, customer_id, customer_name, model, n, i, o, failed in attr_rows:
        i, o, n, failed = int(i), int(o), int(n), int(failed or 0)
        cost = _cost(model, i, o)
        fx = _fx_for_date(d.date().isoformat())
        cost_inr = cost * fx if fx is not None else 0.0
        if oid and order_number:
            key = str(oid)
            e = order_acc.setdefault(key, {
                "order_id": key, "order_number": order_number,
                "customer_name": customer_name or "Unknown customer",
                "calls": 0, "failed_calls": 0, "tokens": 0, "cost_usd": 0.0, "cost_inr": 0.0,
            })
            e["calls"] += n; e["failed_calls"] += failed
            e["tokens"] += i + o; e["cost_usd"] += cost; e["cost_inr"] += cost_inr
        elif customer_id:
            key = str(customer_id)
            e = customer_acc.setdefault(key, {
                "customer_id": key, "customer_name": customer_name or "Unknown customer",
                "calls": 0, "failed_calls": 0, "tokens": 0, "cost_usd": 0.0,
            })
            e["calls"] += n; e["failed_calls"] += failed
            e["tokens"] += i + o; e["cost_usd"] += cost
        else:
            unattributed_calls += n
            unattributed_tokens += i + o
            unattributed_cost += cost
            unattributed_cost_inr += cost_inr

    by_order = sorted(order_acc.values(), key=lambda x: (-x["cost_usd"], -x["calls"]))
    by_customer = sorted(customer_acc.values(), key=lambda x: (-x["cost_usd"], -x["calls"]))
    for e in by_order + by_customer:
        e["cost_usd"] = round(e["cost_usd"], 4)
        e["cost_inr"] = round(e.get("cost_inr", 0.0), 4)

    priced_models = any(
        bool((rates.get(model) or {}).get("in") or (rates.get(model) or {}).get("out"))
        for model in {m["model"] for m in month["by_model"]}
    )

    # simple run-rate projection for the rest of the month
    day_of_month = now_ist.day
    projected = round(month["cost_usd"] / day_of_month * 30, 4) if day_of_month else 0.0

    from app.services.llm_client import MODEL_CHEAP, MODEL_SMART, PROVIDER

    return {
        "provider": PROVIDER,
        "models": {"cheap": MODEL_CHEAP, "smart": MODEL_SMART},
        "today": {**today, "cost_inr": today_cost_inr},
        "month": {**month, "cost_inr": month_cost_inr},
        "by_purpose": by_purpose,
        "series": series,
        "fx": {"base": "USD", "quote": "INR", "source": fx_source},
        "daily_request_cap": cap,
        "calls_left_today": max(cap - today["calls"], 0) if cap else None,
        "monthly_budget_usd": budget,
        "projected_month_usd": projected,
        # honest flag: free-tier models price at 0, so a 0 total is not a bug
        "all_free": not priced_models,
        "attribution": {
            "by_order": by_order[:50],
            "by_customer": by_customer[:50],
            "unattributed": {
                "calls": unattributed_calls,
                "tokens": unattributed_tokens,
                "cost_usd": round(unattributed_cost, 4),
                "cost_inr": round(unattributed_cost_inr, 4),
            },
        },
    }


class TaskIn(BaseModel):
    title: str = Field(min_length=2, max_length=2000)
    staff: str | None = None
    order_number: str | None = None
    customer_id: str | None = None
    kind: str = Field(default="general", pattern="^(general|pickup|wash|dry|iron|delivery|packing|custom)$")
    due_at: datetime | None = None
    urgent: bool = False


@router.get("/tasks/customers/{customer_id}/orders")
async def task_customer_orders(customer_id: str, db: AsyncSession = Depends(get_db)) -> list[dict]:
    """Return active/running orders for the selected customer."""
    from app.models import Customer as _C, Order as _O
    try:
        cid = uuid_module.UUID(customer_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid customer")
    customer = await db.get(_C, cid)
    if customer is None or not customer.is_active:
        raise HTTPException(status_code=404, detail="Customer not found")
    rows = (
        await db.execute(
            select(_O)
            .where(_O.customer_id == customer.id)
            .where(_O.status.not_in(["DELIVERED", "CANCELLED"]))
            .order_by(_O.created_at.desc())
            .limit(30)
        )
    ).scalars().all()
    return [
        {
            "id": str(o.id),
            "order_number": o.order_number,
            "status": o.status,
            "created_at": o.created_at.isoformat() if o.created_at else None,
            "expected_delivery": o.expected_delivery.isoformat() if o.expected_delivery else None,
            "total_amount": float(o.total_amount or 0),
        }
        for o in rows
    ]


@router.get("/tasks/customers")
async def task_customer_search(
    q: str = Query(default="", max_length=80),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Search existing customers in the live tenant database."""
    term = q.strip()
    # New-task picker: blank search means "show recent customers"; typed
    # search narrows by name/phone. This makes the field behave like a real
    # dropdown instead of requiring the owner to guess that typing 2 chars
    # is necessary.
    if not term:
        rows = (
            await db.execute(
                select(Customer)
                .where(Customer.is_active.is_(True))
                .order_by(Customer.last_message_at.desc().nulls_last(), Customer.created_at.desc())
                .limit(20)
            )
        ).scalars().all()
    else:
        digits = re.sub(r"\D", "", term)
        clauses = [Customer.name.ilike(f"%{term}%")]
        if len(digits) >= 3:
            clauses.append(Customer.phone.ilike(f"%{digits}%"))
        from sqlalchemy import or_ as _or
        rows = (
            await db.execute(
                select(Customer)
                .where(Customer.is_active.is_(True), _or(*clauses))
                .order_by(Customer.last_message_at.desc().nulls_last(), Customer.created_at.desc())
                .limit(20)
            )
        ).scalars().all()
    return [{"id": str(c.id), "name": c.name or "Customer", "phone": c.phone, "address": c.address or ""} for c in rows]


class TaskCustomerIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=8, max_length=20)
    address: str | None = Field(default=None, max_length=1000)


@router.post("/tasks/customers", status_code=201)
async def create_task_customer(body: TaskCustomerIn, db: AsyncSession = Depends(get_db)) -> dict:
    phone = normalize_phone(body.phone)
    existing = (await db.execute(select(Customer).where(Customer.phone == phone))).scalar_one_or_none()
    if existing:
        if body.name.strip() and not existing.name:
            existing.name = body.name.strip()
        if body.address and not existing.address:
            existing.address = body.address.strip()
        await db.commit()
        return {"id": str(existing.id), "name": existing.name or body.name.strip(), "phone": existing.phone, "existing": True}
    customer = Customer(phone=phone, name=body.name.strip(), address=body.address.strip() if body.address else None)
    db.add(customer)
    await db.commit()
    await db.refresh(customer)
    return {"id": str(customer.id), "name": customer.name, "phone": customer.phone, "existing": False}


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
    # kram dekhte hain: owner ne jawab de diya aur staff ne phir se poocha,
    # to wo dobara bina jawab ka hai.
    from app.models import TaskMessage

    threads: dict = {}
    if rows:
        msgs = (
            await db.execute(
                select(TaskMessage)
                .where(TaskMessage.task_id.in_([t.id for t in rows]))
                .order_by(TaskMessage.at)
            )
        ).scalars().all()
        for m in msgs:
            threads.setdefault(m.task_id, []).append(m)

    now = datetime.now(timezone.utc)
    out = []
    for t in rows:
        staff = await db.get(_S, t.assigned_staff_id) if t.assigned_staff_id else None
        order = await db.get(_O, t.order_id) if t.order_id else None
        thread = threads.get(t.id, [])
        waiting = bool(thread) and thread[-1].author_kind == "staff"
        out.append(
            {
                "id": str(t.id), "code": t.code, "title": t.title,
                "status": t.status, "urgent": t.urgent,
                "staff": staff.name if staff else None,
                "staff_phone": staff.phone if staff else None,
                "order_number": order.order_number if order else None,
                "reply": t.reply,
                "ping_count": t.ping_count,
                "escalated": t.escalated_at is not None,
                "age_hours": int((now - t.created_at).total_seconds() // 3600),
                # Sawaal ka hisaab — card par badge, sheet mein poora thread
                "msg_count": len(thread),
                "awaiting_reply": waiting,
                "last_question": thread[-1].text[:160] if waiting else None,
                # detail card ke liye — kab aakhri baar poocha aur unhone
                # kya samay diya; ye pehle sirf DB mein tha, kahin dikhta nahi
                "last_ping_at": t.last_ping_at.isoformat() if t.last_ping_at else None,
                "eta_text": t.eta_text,
                "due_at": t.due_at.isoformat() if t.due_at else None,
                "kind": t.kind,
                "customer_id": str(t.customer_id) if t.customer_id else (str(order.customer_id) if order else None),
                "customer_name": (
                    (await db.get(Customer, t.customer_id)).name
                    if t.customer_id else
                    ((await db.get(Customer, order.customer_id)).name if order and order.customer_id else None)
                ),
                "customer_phone": (
                    (await db.get(Customer, t.customer_id)).phone
                    if t.customer_id else
                    ((await db.get(Customer, order.customer_id)).phone if order and order.customer_id else None)
                ),
                "created_by": t.created_by,
                "created_at": t.created_at.isoformat(),
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            }
        )
    return out


@router.post("/tasks", status_code=201)
async def create_task_api(body: TaskIn, db: AsyncSession = Depends(get_db)) -> dict:
    from app.models import Order as _O, Customer as _C
    from app.services import tasks as task_service

    staff = await task_service.find_staff(db, body.staff or "") if body.staff else None
    if body.staff and staff is None:
        raise HTTPException(status_code=400, detail=f"'{body.staff}' staff list mein nahi mila")

    order = None
    if body.order_number:
        order = (
            await db.execute(select(_O).where(_O.order_number == body.order_number.upper()))
        ).scalar_one_or_none()
        if order is None:
            raise HTTPException(status_code=404, detail=f"{body.order_number} nahi mila")

    customer = None
    if body.customer_id:
        try:
            customer = await db.get(_C, uuid_module.UUID(body.customer_id))
        except (ValueError, AttributeError):
            customer = None
        if customer is None or not customer.is_active:
            raise HTTPException(status_code=404, detail="Customer not found")
    elif order is not None:
        customer = await db.get(_C, order.customer_id)

    if order is not None and customer is not None and order.customer_id != customer.id:
        raise HTTPException(status_code=400, detail="Selected customer does not match this order")

    task = await task_service.create_task(
        db, title=body.title, staff=staff, order=order,
        urgent=body.urgent, created_by="dashboard", kind=body.kind,
        due_at=body.due_at, customer=customer,
    )
    return {
        "code": task.code, "id": str(task.id), "kind": task.kind,
        "customer_id": str(customer.id) if customer else None,
        "due_at": task.due_at.isoformat() if task.due_at else None,
    }

class TaskEditIn(BaseModel):
    title: str = Field(min_length=2, max_length=2000)
    staff: str | None = None
    order_number: str | None = None
    kind: str = Field(default="general", pattern="^(general|pickup|wash|dry|iron|delivery|packing|custom)$")
    due_at: datetime | None = None
    priority: str = Field(default="normal", pattern="^(normal|urgent)$")
    customer_name: str | None = Field(default=None, max_length=120)
    customer_phone: str | None = Field(default=None, max_length=20)


@router.put("/tasks/{code}")
async def edit_task_api(code: str, body: TaskEditIn, db: AsyncSession = Depends(get_db)) -> dict:
    """Edit an assigned task and persist every editable field."""
    from app.models import Customer as _C, Order as _O, Staff as _S, Task
    from app.services import tasks as task_service

    task = await task_service.get_by_code(db, code)
    if task is None:
        raise HTTPException(status_code=404, detail=f"{code} nahi mila")
    previous_staff_id = task.assigned_staff_id

    staff = await task_service.find_staff(db, body.staff or "") if body.staff else None
    if body.staff and staff is None:
        raise HTTPException(status_code=400, detail="Selected staff not found")

    order = None
    if body.order_number:
        order = (await db.execute(select(_O).where(_O.order_number == body.order_number.upper()))).scalar_one_or_none()
        if order is None:
            raise HTTPException(status_code=404, detail="Selected order not found")

    customer = await db.get(_C, task.customer_id) if task.customer_id else None
    if customer is None and task.order_id:
        existing_order = await db.get(_O, task.order_id)
        if existing_order is not None:
            customer = await db.get(_C, existing_order.customer_id)
    if order is not None:
        if customer is not None and order.customer_id != customer.id:
            raise HTTPException(status_code=400, detail="Order must belong to the selected customer")
        task.order_id = order.id
        customer = await db.get(_C, order.customer_id)
        task.customer_id = customer.id if customer else None
    else:
        task.order_id = None

    if customer is not None:
        if body.customer_name is not None:
            customer.name = body.customer_name.strip() or None
        if body.customer_phone is not None:
            phone = normalize_phone(body.customer_phone)
            clash = (await db.execute(select(_C).where(_C.phone == phone, _C.id != customer.id))).scalar_one_or_none()
            if clash:
                raise HTTPException(status_code=409, detail="This phone number already belongs to another customer")
            customer.phone = phone

    task.title = body.title.strip()
    task.assigned_staff_id = staff.id if staff else None
    task.kind = body.kind
    task.due_at = body.due_at
    task.urgent = body.priority == "urgent"
    db.add(task)
    await db.commit()
    await db.refresh(task)

    if previous_staff_id != task.assigned_staff_id:
        notified = False
        if staff is not None and task.status == "OPEN":
            notified = await task_service.send_task_to(db, task, staff)
        previous_staff = await db.get(_S, previous_staff_id) if previous_staff_id else None
        await audit.record(
            actor_role="admin",
            actor="dashboard",
            action="task_reassigned",
            args={
                "code": task.code,
                "from_staff": previous_staff.name if previous_staff else None,
                "to_staff": staff.name if staff else None,
            },
            result="notification sent" if notified else "assignment saved; notification not delivered",
        )
    return {"ok": True, "code": task.code}


@router.post("/tasks/{code}/done")
async def complete_task_api(code: str, db: AsyncSession = Depends(get_db)) -> dict:
    from app.services import tasks as task_service

    task = await task_service.get_by_code(db, code)
    if task is None:
        raise HTTPException(status_code=404, detail=f"{code} nahi mila")
    await task_service.complete_task(db, task, by="dashboard")
    return {"ok": True}


@router.post("/tasks/{code}/cancel")
async def cancel_task_api(code: str, db: AsyncSession = Depends(get_db)) -> dict:
    from app.services import tasks as task_service

    task = await task_service.get_by_code(db, code)
    if task is None:
        raise HTTPException(status_code=404, detail=f"{code} nahi mila")
    await task_service.cancel_task(db, task, by="dashboard")
    return {"ok": True}


@router.post("/tasks/{code}/ping")
async def ping_task_api(code: str, db: AsyncSession = Depends(get_db)) -> dict:
    """'Abhi pooch lo' — nudge the assignee without waiting for the clock."""
    from datetime import datetime, timezone

    from app.models import Staff as _S
    from app.services import tasks as task_service

    task = await task_service.get_by_code(db, code)
    if task is None:
        raise HTTPException(status_code=404, detail=f"{code} nahi mila")
    staff = await db.get(_S, task.assigned_staff_id) if task.assigned_staff_id else None
    if staff is None:
        raise HTTPException(status_code=400, detail="Ye kaam kisi ko assign nahi hai")
    ok = await task_service._send_to_assignee(db, task, staff, first=False)
    task.ping_count += 1
    task.last_ping_at = datetime.now(timezone.utc)
    db.add(task)
    await db.commit()
    return {"ok": ok, "detail": "bhej diya" if ok else "window band hai — nahi ja paya"}


class TaskReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


@router.get("/tasks/{code}/messages")
async def task_thread_admin(code: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Is kaam par staff ne kya poocha aur kya jawab gaya — ek jagah."""
    from app.models import TaskMessage
    from app.services import tasks as task_service

    task = await task_service.get_by_code(db, code)
    if task is None:
        raise HTTPException(status_code=404, detail=f"{code} nahi mila")
    rows = (
        await db.execute(
            select(TaskMessage).where(TaskMessage.task_id == task.id).order_by(TaskMessage.at)
        )
    ).scalars().all()
    return {
        "code": task.code,
        "title": task.title,
        "messages": [
            {"who": m.author_kind, "name": m.author_name, "text": m.text,
             "at": m.at.isoformat(), "read": m.read_by_staff_at is not None}
            for m in rows
        ],
    }


@router.post("/tasks/{code}/reply")
async def reply_to_task(
    code: str, body: TaskReplyIn, db: AsyncSession = Depends(get_db)
) -> dict:
    """Staff ke sawaal ka jawab — thread mein bhi, uske WhatsApp par bhi.

    Pehle jawab dene ka koi rasta hi nahi tha: sawaal owner ke WhatsApp par
    aata tha aur wo wahin se reply karta tha, jo kisi record mein nahi
    jaata tha. Ab dono taraf ek hi thread dikhta hai, aur panel mein
    staff ko unread badge milta hai.
    """
    from app.models import Staff as _S
    from app.models import TaskMessage
    from app.services import tasks as task_service
    from app.services.whatsapp import SendError, send_message

    task = await task_service.get_by_code(db, code)
    if task is None:
        raise HTTPException(status_code=404, detail=f"{code} nahi mila")
    text = body.text.strip()
    db.add(
        TaskMessage(
            task_id=task.id, author_kind="owner", author_name="Owner", text=text[:1000]
        )
    )
    await db.commit()

    delivered = False
    staff = await db.get(_S, task.assigned_staff_id) if task.assigned_staff_id else None
    wa_text = f"[{task.code}] {task.title}\n\nJawab: {text[:500]}"
    if staff is not None:
        try:
            await send_message(db, to_phone=staff.phone, text=wa_text)
            delivered = True
        except SendError as exc:
            # Panel mein to dikh hi jayega — WhatsApp fail hona jawab ko
            # rokta nahi.
            log.warning("task_reply_wa_failed", code=task.code, error=str(exc))
    await audit.record(
        actor_role="admin", actor="dashboard", action="task_replied",
        args={"code": task.code}, result=text[:150],
    )
    # API se na gaya? To owner ke apne phone ka WhatsApp hai. Number aur
    # bana-banaya text wapas bhejte hain taaki dashboard ek wa.me link de
    # sake — wahi rasta jo staff panel bill share karne ke liye use karta
    # hai. Bina iske jawab sirf panel mein baithta hai aur staff ko tab
    # tak pata nahi chalta jab tak wo khud khol kar na dekhe.
    return {
        "ok": True,
        "whatsapp": delivered,
        "staff_phone": (staff.phone if staff is not None and not delivered else None),
        "staff_name": (staff.name if staff is not None else None),
        "wa_text": (wa_text if not delivered else None),
    }


@router.post("/jobs/task-followups")
async def trigger_task_followups() -> dict:
    """Run the follow-up sweep now (the button next to the task list)."""
    from app.services.tasks import run_task_followups

    return {"sent": await run_task_followups()}


class ManualLeadIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=8, max_length=24)
    need: str = Field(min_length=2, max_length=300)
    call_notes: str | None = Field(default=None, max_length=2000)
    whatsapp_consent_confirmed: bool = False
    send_whatsapp: bool = False
    message: str | None = Field(default=None, max_length=1200)


@router.post("/leads/manual", status_code=201, dependencies=[Depends(require_feature("marketing_agent"))])
async def create_manual_lead(
    body: ManualLeadIn, db: AsyncSession = Depends(get_db)
) -> dict:
    """Save a phone-call lead and optionally send an owner-approved WhatsApp opener.

    Sending is an explicit choice and requires the owner to confirm the lead
    agreed to WhatsApp follow-up. Inbound replies use the existing AI webhook;
    saving alone never schedules an automatic message.
    """
    if body.send_whatsapp and not body.whatsapp_consent_confirmed:
        raise HTTPException(
            status_code=400,
            detail="Confirm that the customer agreed to WhatsApp follow-up before sending.",
        )
    if body.send_whatsapp and not (body.message or "").strip():
        raise HTTPException(status_code=422, detail="Prepare a WhatsApp message before sending.")

    try:
        phone = normalize_phone(body.phone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    name = body.name.strip()
    need = body.need.strip()
    if not name or not need:
        raise HTTPException(status_code=422, detail="Name and interested service cannot be blank.")
    now = datetime.now(timezone.utc)
    customer = (
        await db.execute(select(Customer).where(Customer.phone == phone))
    ).scalar_one_or_none()
    if customer is None:
        customer = Customer(phone=phone, name=name)
        db.add(customer)
        await db.flush()
    elif name and not customer.name:
        customer.name = name

    lead = (
        await db.execute(select(Lead).where(Lead.phone == phone))
    ).scalar_one_or_none()
    if lead is None:
        lead = Lead(
            phone=phone, name=name, source="phone_call", items_text=need,
            stage="NEW", followup_count=0, next_followup_at=None,
        )
        db.add(lead)
        await db.flush()
    else:
        lead.name = name or lead.name
        if not lead.source:
            lead.source = "phone_call"
        lead.items_text = need
        if body.send_whatsapp and lead.stage != "CONVERTED":
            # Do not leave an older due follow-up queued if this new manual
            # outreach fails. A fresh ladder starts only after a successful send.
            # A converted lead must never be moved backwards into the funnel.
            lead.stage = "NEW"
            lead.followup_count = 0
            lead.next_followup_at = None

    note_parts = [f"Phone call lead ({now.isoformat()}): need={need}"]
    if body.call_notes and body.call_notes.strip():
        note_parts.append("Call notes: " + body.call_notes.strip())
    if body.whatsapp_consent_confirmed:
        note_parts.append(
            "WhatsApp follow-up consent confirmed by owner at " + now.isoformat()
        )
    existing_notes = (lead.notes or "").strip()
    lead.notes = "\n\n".join(part for part in [existing_notes, *note_parts] if part)
    await db.commit()
    await db.refresh(lead)

    if not body.send_whatsapp:
        return {
            "id": str(lead.id), "name": lead.name, "phone": lead.phone,
            "stage": lead.stage, "whatsapp_status": "not_sent",
            "message": "Lead saved. No WhatsApp message was sent.",
        }

    from app.services.whatsapp import SendError, send_message

    try:
        await send_message(
            db, to_phone=phone, text=body.message.strip(), sent_by="bot",
            intent="promotional", enqueue_on_fail=False,
        )
    except SendError as exc:
        log.warning("manual_lead_whatsapp_not_sent", lead_id=str(lead.id), reason=str(exc))
        return {
            "id": str(lead.id), "name": lead.name, "phone": lead.phone,
            "stage": lead.stage, "whatsapp_status": "not_sent",
            "message": "Lead saved, but WhatsApp did not send: " + str(exc),
        }

    sent_at = datetime.now(timezone.utc)
    lead.last_contact_at = sent_at
    next_followup_at = None
    if lead.stage != "CONVERTED":
        lead.stage = "CONTACTED"
        next_followup_at = sent_at + timedelta(hours=2)
        lead.next_followup_at = next_followup_at
    else:
        # Existing customers with a converted lead can receive the owner-approved
        # message, but must not be pushed back into the lead follow-up ladder.
        lead.next_followup_at = None
    await db.commit()
    return {
        "id": str(lead.id), "name": lead.name, "phone": lead.phone,
        "stage": lead.stage, "whatsapp_status": "sent",
        "next_followup_at": next_followup_at.isoformat() if next_followup_at else None,
        "message": "WhatsApp sent. Replies will be handled by the existing AI inbox flow.",
    }


@router.get("/leads", dependencies=[Depends(require_feature("marketing_agent"))])
async def list_leads(
    db: AsyncSession = Depends(get_db),
    stage: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    """Lead pipeline for the dashboard/CRM view. Newest activity first."""
    from app.models import Lead

    q = select(Lead).order_by(Lead.created_at.desc()).limit(limit)
    if stage:
        q = q.where(Lead.stage == stage.upper())
    rows = (await db.execute(q)).scalars().all()
    return [
        {
            "id": str(l.id), "phone": l.phone, "name": l.name, "source": l.source, "area": l.area,
            "need": l.items_text, "stage": l.stage, "followup_count": l.followup_count,
            "last_contact_at": l.last_contact_at.isoformat() if l.last_contact_at else None,
            "next_followup_at": l.next_followup_at.isoformat() if l.next_followup_at else None,
            "created_at": l.created_at.isoformat(),
            "notes": l.notes,
        }
        for l in rows
    ]


@router.get("/leads/attribution", dependencies=[Depends(require_feature("marketing_agent"))])
async def lead_attribution_summary(db: AsyncSession = Depends(get_db)) -> dict:
    """Confirmed orders and collected payments grouped by original acquisition source."""
    from app.models import Order

    rows = (
        await db.execute(
            select(
                Order.acquisition_source,
                Order.acquisition_campaign,
                func.count(Order.id).label("orders"),
                func.coalesce(func.sum(Order.amount_paid), 0).label("collected_revenue"),
                func.coalesce(func.sum(Order.total_amount), 0).label("billed_revenue"),
            )
            .group_by(Order.acquisition_source, Order.acquisition_campaign)
            .order_by(func.sum(Order.amount_paid).desc())
        )
    ).all()
    return {
        "items": [
            {
                "source": source or "unattributed",
                "campaign": campaign,
                "confirmed_orders": int(count),
                "collected_revenue": float(collected or 0),
                "billed_revenue": float(billed or 0),
            }
            for source, campaign, count, collected, billed in rows
        ],
        "revenue_basis": "collected_revenue is sum of Order.amount_paid; billed_revenue is sum of Order.total_amount",
    }


@router.get("/templates/registry")
async def templates_registry() -> list[dict]:
    """Local template registry — works even when Meta's API is down."""
    from app.services.templates import _DYNAMIC, TEMPLATES

    merged = {**TEMPLATES, **_DYNAMIC}
    return [
        {
            "name": name, "status": "UNKNOWN", "category": "UTILITY",
            "body": " ".join("{{%d}}" % i for i in range(1, spec["param_count"] + 1))
            or "(no variables)",
            "param_count": spec["param_count"],
        }
        for name, spec in merged.items()
        if name != "hello_world"
    ]


@router.get("/templates")
async def list_templates() -> list[dict]:
    """Return the local template registry for the WAHA-only WhatsApp setup."""
    return await templates_registry()


class TplButtonIn(BaseModel):
    type: str = Field(pattern="^(QUICK_REPLY|URL|PHONE_NUMBER)$")
    text: str = Field(min_length=1, max_length=25)
    url: str | None = None
    phone_number: str | None = None


class TemplateIn(BaseModel):
    name: str = Field(min_length=3, max_length=60)
    category: str = Field(pattern="^(UTILITY|MARKETING)$")
    language: str = "en_US"
    body: str = Field(min_length=5, max_length=1024)
    footer: str | None = Field(default=None, max_length=60)
    buttons: list[TplButtonIn] = Field(default_factory=list, max_length=3)
    samples: list[str] = Field(default_factory=list)  # one per {{n}}


@router.post("/templates", status_code=201)
async def create_template(body: TemplateIn, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        out = await wa_templates.create(
            await wa_templates.creds_for_current(db), **body.model_dump(exclude={"buttons"}),
            buttons=[b.model_dump() for b in body.buttons],
        )
    except wa_templates.TemplateError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    await audit.record(
        actor_role="admin", actor="dashboard", action="template_submitted",
        args={"name": out["name"], "category": body.category}, result=out["status"],
    )
    return out


@router.delete("/templates/{name}")
async def delete_template(name: str, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        await wa_templates.delete(await wa_templates.creds_for_current(db), name)
    except wa_templates.TemplateError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    return {"deleted": name}


# ---------------------------------------------------------------------------
# Agents overview (control room)
# ---------------------------------------------------------------------------


@router.get("/agents/overview", dependencies=[Depends(require_feature("service_agent"))])
async def agents_overview(db: AsyncSession = Depends(get_db)) -> dict:
    """One call powering the Agents control-room page."""
    from zoneinfo import ZoneInfo

    from app.models import Campaign, Correction, DocChunk, FaqEntry
    from app.services.marketing import compute_segments, month_send_count

    ist = ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(ist)
    today_start = now_ist.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
        timezone.utc
    )
    month_start = now_ist.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)

    counts_rows = (
        await db.execute(
            select(AuditLog.action, func.count())
            .where(AuditLog.at >= today_start)
            .group_by(AuditLog.action)
        )
    ).all()
    today_actions = {a: c for a, c in counts_rows}

    faq_n = (await db.execute(select(func.count()).select_from(FaqEntry))).scalar_one()
    corr_n = (await db.execute(select(func.count()).select_from(Correction))).scalar_one()
    docs_n = (
        await db.execute(select(func.count(func.distinct(DocChunk.document))))
    ).scalar_one()
    teachme_n = (
        await db.execute(
            select(func.count())
            .select_from(OpenQuestion)
            .where(OpenQuestion.status == "open")
        )
    ).scalar_one()

    month_campaigns = (
        (
            await db.execute(
                select(Campaign).where(
                    Campaign.status == "sent", Campaign.sent_at >= month_start
                )
            )
        )
        .scalars()
        .all()
    )
    orders_attr = sum((c.stats or {}).get("orders_attributed", 0) for c in month_campaigns)
    revenue_attr = sum((c.stats or {}).get("revenue_attributed", 0) for c in month_campaigns)

    s = await app_settings.all_settings(db)
    segs = await compute_segments(db)

    return {
        "service": {
            "enabled": s["agent_enabled"],
            "today": {
                "replies": today_actions.get("ai_reply", 0),
                "escalations": today_actions.get("escalated", 0)
                + today_actions.get("complaint_escalated", 0),
                "fyis": today_actions.get("admin_fyi", 0),
                "commands": sum(
                    today_actions.get(a, 0)
                    for a in (
                        "create_bill", "status_update", "delay_update", "relay",
                        "set_priority", "assign_staff", "add_note", "record_payment",
                    )
                ),
                "taught": today_actions.get("taught_via_whatsapp", 0),
            },
            "knowledge": {"faqs": faq_n, "corrections": corr_n, "docs": docs_n},
            "teachme_open": teachme_n,
        },
        "marketing": {
            "autonomy": s["marketing_autonomy"],
            "segments": {k: len(v) for k, v in segs.items()},
            "month": {
                "campaigns_sent": len(month_campaigns),
                "orders_attributed": orders_attr,
                "revenue_attributed": revenue_attr,
                "messages_used": await month_send_count(db),
                "budget": s["marketing_monthly_msg_budget"],
            },
            "social": {
                "enabled": s["social_daily_enabled"],
                "hour": s["social_post_hour"],
                "instagram_linked": bool(s["ig_user_id"] and s["ig_access_token"]),
            },
        },
        "health": {
            "llm_provider": __import__("app.services.llm_client", fromlist=["PROVIDER"]).PROVIDER,
            "public_url_set": bool(s["public_base_url"]),
            "standup_hour": s["standup_hour"],
            "turnaround_days": s["turnaround_days"],
        },
    }


# ---------------------------------------------------------------------------
# Settings + agent controls
# ---------------------------------------------------------------------------


# Settings whose values are credentials — never sent back to the browser.
_SECRET_SETTINGS = {"ig_access_token", "gbp_connection"}
# Sirf vendor Control panel likhta hai (routers/control.py) — dukaan ke
# dashboard ke generic settings PUT se nahi, warna koi token/listing badal de.
_READONLY_SETTINGS = {
    "gbp_connection", "gbp_reviews", "ig_user_id", "ig_access_token",
    "home_tenant_slug", "public_url_fixed",
}
_SECRET_MASK = "••••••••"


def _redact_settings(s: dict) -> dict:
    return {
        k: (_SECRET_MASK if k in _SECRET_SETTINGS and v else v) for k, v in s.items()
    }


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db)) -> dict:
    return _redact_settings(await app_settings.all_settings(db))


class SettingIn(BaseModel):
    key: str
    value: object



# ---------------------------------------------------------------------------
@router.put("/settings")
async def put_setting(body: SettingIn, db: AsyncSession = Depends(get_db)) -> dict:
    # Saving the mask back would overwrite the real secret with dots.
    if body.key in _SECRET_SETTINGS and body.value == _SECRET_MASK:
        return {"ok": True, "unchanged": True}
    if body.key in _READONLY_SETTINGS:
        raise HTTPException(status_code=400, detail=f"{body.key} is managed by the Kwik Klin team (Control panel)")
    try:
        await app_settings.set_value(db, body.key, body.value)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


class AgentToggleIn(BaseModel):
    phone: str
    paused: bool


@router.post("/inbox/toggle-agent", dependencies=[Depends(require_feature("service_agent"))])
async def toggle_agent(body: AgentToggleIn, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        phone = normalize_phone(body.phone)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad phone")
    cust = (
        await db.execute(select(Customer).where(Customer.phone == phone))
    ).scalar_one_or_none()
    if cust is None:
        raise HTTPException(status_code=404, detail="customer not found")
    cust.agent_paused = body.paused
    # Manual owner pause is PERMANENT until the owner presses Resume.
    # Auto-pauses created by complaint/rating keep agent_paused_at set and
    # may expire after agent_pause_hours. A manual pause uses NULL timestamp
    # so the inbound webhook can never auto-resume it.
    cust.agent_paused_at = None if body.paused else None
    await db.commit()
    await audit.record(
        actor_role="admin", actor="dashboard",
        action="agent_paused" if body.paused else "agent_resumed",
        args={"phone": phone, "permanent": bool(body.paused)}, result="",
    )
    return {"phone": phone, "agent_paused": cust.agent_paused}


@router.get("/growth-analytics", dependencies=[Depends(require_feature("reports"))])
async def growth_analytics(db: AsyncSession = Depends(get_db)) -> dict:
    """Website realtime + GBP daily performance + local campaign KPIs."""
    from app.services.analytics import growth_snapshot
    snap = await growth_snapshot(db)
    from app.services.marketing import month_send_count
    from app.models import Campaign, CampaignRecipient
    month = await month_send_count(db)
    campaigns = (
        await db.execute(select(Campaign).order_by(Campaign.created_at.desc()).limit(20))
    ).scalars().all()
    return {
        **snap,
        "campaign": {
            "messages_sent_this_month": month,
            "recent": [
                {
                    "name": c.name,
                    "status": c.status,
                    "segment": c.segment,
                    "stats": c.stats or {},
                }
                for c in campaigns
            ],
        },
    }


@router.get("/whatsapp/stats", dependencies=[Depends(require_feature("reports"))])
async def whatsapp_stats(db: AsyncSession = Depends(get_db)) -> dict:
    """Today's WhatsApp traffic from our DB. Transport is WAHA/NOWEB only."""
    from zoneinfo import ZoneInfo
    from app.models import Conversation, Direction

    ist = ZoneInfo("Asia/Kolkata")
    today_start = datetime.now(ist).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    sent_n = (await db.execute(select(func.count()).select_from(Conversation).where(
        Conversation.direction == Direction.OUTBOUND, Conversation.created_at >= today_start
    ))).scalar_one()
    recv_n = (await db.execute(select(func.count()).select_from(Conversation).where(
        Conversation.direction == Direction.INBOUND, Conversation.created_at >= today_start
    ))).scalar_one()
    talked = (await db.execute(select(func.count(func.distinct(Conversation.customer_id))).where(
        Conversation.created_at >= today_start, Conversation.customer_id.isnot(None)
    ))).scalar_one()

    return {
        "today": {"sent": sent_n, "received": recv_n, "customers_talked": talked},
        "templates": {"approved": 0, "pending": 0, "rejected": 0},
        "quality": None,
        "meta_ok": None,
        "meta_state": "disabled",
        "provider": "waha",
    }


@router.get("/backup.json")
async def backup_json(db: AsyncSession = Depends(get_db)) -> dict:
    """One-click business backup: every business table as plain JSON.

    (Full binary-safe backups: pg_dump. This is the owner-friendly export.)
    """
    from app.models import (
        Correction as _Cr,
        Coupon as _Cp,
        Customer as _C,
        DocChunk as _D,
        Expense as _E,
        FaqEntry as _F,
        Order as _O,
        Payment as _P,
        Rate as _R,
    )

    import enum as _enum

    def _row(obj, cols):
        out = {}
        for col in cols:
            v = getattr(obj, col)
            if isinstance(v, _enum.Enum):
                v = v.name
            elif v is not None and not isinstance(v, (int, float, bool, str, list, dict)):
                v = str(v)
            out[col] = v
        return out

    customers = (await db.execute(select(_C))).scalars().all()
    orders = (await db.execute(select(_O))).scalars().all()
    payments = (await db.execute(select(_P))).scalars().all()
    rates = (await db.execute(select(_R))).scalars().all()
    expenses = (await db.execute(select(_E))).scalars().all()
    coupons = (await db.execute(select(_Cp))).scalars().all()
    faqs = (await db.execute(select(_F))).scalars().all()
    # everything the owner TAUGHT the agent — without these the export
    # restores the business but loses the agent's learning
    corrections = (await db.execute(select(_Cr))).scalars().all()
    doc_chunks = (
        (await db.execute(select(_D).order_by(_D.document, _D.chunk_index)))
        .scalars()
        .all()
    )
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "customers": [_row(c, ["phone", "name", "address", "opted_out", "created_at"]) for c in customers],
        "orders": [
            _row(o, ["order_number", "status", "items", "total_amount", "discount_amount",
                     "gst_amount", "amount_paid", "payment_status", "expected_delivery",
                     "priority", "notes", "created_at"])
            | {"customer_phone": next((c.phone for c in customers if c.id == o.customer_id), None)}
            for o in orders
        ],
        "payments": [_row(p, ["amount", "method", "recorded_by", "received_at"]) for p in payments],
        "rates": [_row(r, ["service", "garment", "unit", "rate", "is_active"]) for r in rates],
        "expenses": [_row(e, ["category", "amount", "spent_on", "description"]) for e in expenses],
        "coupons": [_row(c, ["code", "discount_type", "value", "active"]) for c in coupons],
        "faq": [_row(f, ["question", "answer", "audience", "enabled"]) for f in faqs],
        "corrections": [
            _row(c, ["question", "correct_reply", "audience", "enabled", "created_at"])
            for c in corrections
        ],
        "doc_chunks": [
            _row(d, ["document", "chunk_index", "content", "enabled"]) for d in doc_chunks
        ],
        "settings": _redact_settings(await app_settings.all_settings(db)),
    }


@router.post("/jobs/standup")
async def trigger_standup() -> dict:
    """Manual standup trigger — for testing and 'bhej do abhi' moments."""
    from app.services.scheduler import run_standup

    sends = await run_standup(force=True)
    return {"sent_to": sends}

# ---------------------------------------------------------------------------
# Task Control Room — explicit reassignment
# ---------------------------------------------------------------------------

class TaskAssignIn(BaseModel):
    staff: str = Field(min_length=1, max_length=120)


@router.post("/tasks/{code}/assign")
async def assign_task_api(
    code: str, body: TaskAssignIn, db: AsyncSession = Depends(get_db)
) -> dict:
    """Reassign an open task to an active staff member and notify the new owner."""
    from app.models import TASK_OPEN, Staff as _Staff
    from app.services import tasks as task_service

    task = await task_service.get_by_code(db, code)
    if task is None:
        raise HTTPException(status_code=404, detail=f"{code} nahi mila")
    if task.status != TASK_OPEN:
        raise HTTPException(status_code=409, detail="Only open tasks can be reassigned")

    staff = await task_service.find_staff(db, body.staff)
    if staff is None or not staff.is_active:
        raise HTTPException(status_code=404, detail="Active staff member not found")

    previous_staff_id = task.assigned_staff_id
    if previous_staff_id == staff.id:
        return {"ok": True, "code": task.code, "assigned_staff_id": str(staff.id), "unchanged": True}

    task.assigned_staff_id = staff.id
    db.add(task)
    await db.commit()
    await db.refresh(task)

    # Persist assignment first; notification failure must not roll back ownership.
    notified = await task_service.send_task_to(db, task, staff)
    previous_staff = await db.get(_Staff, previous_staff_id) if previous_staff_id else None
    await audit.record(
        actor_role="admin",
        actor="dashboard",
        action="task_reassigned",
        args={
            "code": task.code,
            "from_staff": previous_staff.name if previous_staff else None,
            "to_staff": staff.name,
        },
        result="notification sent" if notified else "assignment saved; notification not delivered",
    )
    return {
        "ok": True,
        "code": task.code,
        "assigned_staff_id": str(staff.id),
        "assigned_staff": staff.name,
        "notified": notified,
    }
