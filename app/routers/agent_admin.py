"""Admin APIs for the agent era: campaigns, activity log, AI training,
hot settings, coupons, manual job triggers, per-thread agent pause.

Same auth as everything else: X-API-Key (require_admin_key).
"""

import asyncio
import uuid as uuid_module
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import (
    AuditLog,
    Campaign,
    CampaignRecipient,
    Correction,
    Coupon,
    Customer,
    FaqEntry,
    OpenQuestion,
)
from app.routers.orders import require_admin_key
from app.services import app_settings, audit
from app.services.marketing import (
    campaign_stats,
    compute_segments,
    queue_campaign,
    send_campaign,
)
from app.utils.phone import normalize_phone

router = APIRouter(
    prefix="/admin/api", tags=["agent-admin"], dependencies=[Depends(require_admin_key)]
)
log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Segments + campaigns
# ---------------------------------------------------------------------------


@router.get("/segments")
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


@router.get("/campaigns")
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
            }
        )
    return out


class CampaignIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    segment: str
    message_text: str = Field(min_length=5)
    coupon_code: str | None = None


@router.post("/campaigns", status_code=201)
async def create_campaign(body: CampaignIn, db: AsyncSession = Depends(get_db)) -> dict:
    c = Campaign(
        name=body.name, segment=body.segment, message_text=body.message_text,
        coupon_code=body.coupon_code, status="draft", created_by="owner",
    )
    db.add(c)
    await db.commit()
    return {"id": str(c.id), "status": c.status}


@router.post("/campaigns/{campaign_id}/approve")
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


@router.post("/campaigns/{campaign_id}/cancel")
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


@router.get("/coupons")
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


@router.post("/coupons", status_code=201)
async def create_coupon(body: CouponIn, db: AsyncSession = Depends(get_db)) -> dict:
    from datetime import date as _date
    from decimal import Decimal as D

    code = body.code.strip().upper()
    if await db.get(Coupon, code):
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


@router.post("/coupons/{code}/toggle")
async def toggle_coupon(code: str, db: AsyncSession = Depends(get_db)) -> dict:
    c = await db.get(Coupon, code.upper())
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


@router.get("/training/faq")
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


@router.post("/training/faq", status_code=201)
async def add_faq(body: FaqIn, db: AsyncSession = Depends(get_db)) -> dict:
    row = FaqEntry(question=body.question, answer=body.answer, audience=body.audience)
    db.add(row)
    await db.commit()
    return {"id": str(row.id)}


@router.delete("/training/faq/{faq_id}")
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


@router.get("/training/corrections")
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


@router.post("/training/corrections", status_code=201)
async def add_correction(body: CorrectionIn, db: AsyncSession = Depends(get_db)) -> dict:
    row = Correction(
        question=body.question, correct_reply=body.correct_reply, audience=body.audience
    )
    db.add(row)
    await db.commit()
    return {"id": str(row.id)}


@router.delete("/training/corrections/{cid}")
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


@router.post("/training/upload", status_code=201)
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


@router.get("/training/docs")
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


@router.delete("/training/docs/{document}")
async def delete_training_doc(document: str, db: AsyncSession = Depends(get_db)) -> dict:
    from sqlalchemy import delete as sqldelete

    from app.models import DocChunk

    r = await db.execute(sqldelete(DocChunk).where(DocChunk.document == document))
    await db.commit()
    if r.rowcount == 0:
        raise HTTPException(status_code=404, detail="document not found")
    return {"deleted": document, "chunks": r.rowcount}


@router.get("/training/teachme")
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


@router.post("/training/teachme/{qid}/answer")
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
# Agents overview (control room)
# ---------------------------------------------------------------------------


@router.get("/agents/overview")
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


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db)) -> dict:
    return await app_settings.all_settings(db)


class SettingIn(BaseModel):
    key: str
    value: object


@router.put("/settings")
async def put_setting(body: SettingIn, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        await app_settings.set_value(db, body.key, body.value)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


class AgentToggleIn(BaseModel):
    phone: str
    paused: bool


@router.post("/inbox/toggle-agent")
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
    await db.commit()
    await audit.record(
        actor_role="admin", actor="dashboard",
        action="agent_paused" if body.paused else "agent_resumed",
        args={"phone": phone}, result="",
    )
    return {"phone": phone, "agent_paused": cust.agent_paused}


@router.post("/jobs/standup")
async def trigger_standup() -> dict:
    """Manual standup trigger — for testing and 'bhej do abhi' moments."""
    from app.services.scheduler import run_standup

    sends = await run_standup(force=True)
    return {"sent_to": sends}
