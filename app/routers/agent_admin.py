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
            }
        )
    return out


class CampaignIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    segment: str
    message_text: str = Field(min_length=5)
    coupon_code: str | None = None


@router.post("/campaigns", dependencies=[Depends(require_feature("campaigns"))], status_code=201)
async def create_campaign(body: CampaignIn, db: AsyncSession = Depends(get_db)) -> dict:
    c = Campaign(
        name=body.name, segment=body.segment, message_text=body.message_text,
        coupon_code=body.coupon_code, status="draft", created_by="owner",
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


@router.post("/coupons/{code}/toggle", dependencies=[Depends(require_feature("campaigns"))])
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
    )

    out = []
    for key in EDITABLE_KEYS:
        default = MESSAGES[key].get(DEFAULT_LANG) or MESSAGES[key]["en"]
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

import re as _re

import httpx as _httpx

from app.config import settings as _settings

_GRAPH = "https://graph.facebook.com/v21.0"


async def _graph(method: str, path: str, **kw):
    """One Graph API call — isolated so tests can fake it."""
    async with _httpx.AsyncClient(timeout=30) as c:
        r = await c.request(
            method, f"{_GRAPH}/{path}",
            headers={"Authorization": f"Bearer {_settings.WHATSAPP_TOKEN}"}, **kw,
        )
    return r.status_code, r.json()


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

    now = datetime.now(timezone.utc)
    out = []
    for t in rows:
        staff = await db.get(_S, t.assigned_staff_id) if t.assigned_staff_id else None
        order = await db.get(_O, t.order_id) if t.order_id else None
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
                # detail card ke liye — kab aakhri baar poocha aur unhone
                # kya samay diya; ye pehle sirf DB mein tha, kahin dikhta nahi
                "last_ping_at": t.last_ping_at.isoformat() if t.last_ping_at else None,
                "eta_text": t.eta_text,
                "created_by": t.created_by,
                "created_at": t.created_at.isoformat(),
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            }
        )
    return out


@router.post("/tasks", status_code=201)
async def create_task_api(body: TaskIn, db: AsyncSession = Depends(get_db)) -> dict:
    from app.models import Order as _O
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

    task = await task_service.create_task(
        db, title=body.title, staff=staff, order=order,
        urgent=body.urgent, created_by="dashboard",
    )
    return {"code": task.code, "id": str(task.id)}


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


@router.post("/jobs/task-followups")
async def trigger_task_followups() -> dict:
    """Run the follow-up sweep now (the button next to the task list)."""
    from app.services.tasks import run_task_followups

    return {"sent": await run_task_followups()}


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
            "phone": l.phone, "name": l.name, "source": l.source, "area": l.area,
            "stage": l.stage, "followup_count": l.followup_count,
            "last_contact_at": l.last_contact_at.isoformat() if l.last_contact_at else None,
            "next_followup_at": l.next_followup_at.isoformat() if l.next_followup_at else None,
            "created_at": l.created_at.isoformat(),
            "notes": l.notes,
        }
        for l in rows
    ]


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
    if not _settings.WHATSAPP_WABA_ID:
        raise HTTPException(status_code=400, detail="WHATSAPP_WABA_ID not configured")
    status, data = await _graph(
        "GET", f"{_settings.WHATSAPP_WABA_ID}/message_templates",
        params={"fields": "name,status,category,language,components,rejected_reason", "limit": 100},
    )
    if status != 200:
        raise HTTPException(status_code=502, detail=str(data)[:300])
    out = []
    for t in data.get("data", []):
        body = next(
            (c.get("text", "") for c in t.get("components", []) if c.get("type") == "BODY"), ""
        )
        buttons = next(
            (c.get("buttons", []) for c in t.get("components", []) if c.get("type") == "BUTTONS"),
            [],
        )
        out.append(
            {
                "name": t["name"], "status": t.get("status"),
                "category": t.get("category"), "language": t.get("language"),
                "body": body, "buttons": buttons,
                "rejected_reason": t.get("rejected_reason"),
            }
        )
        # approved templates become sendable through the single door
        if t.get("status") == "APPROVED":
            from app.services.templates import register_dynamic

            params = len(set(_re.findall(r"\{\{(\d+)\}\}", body)))
            register_dynamic(t["name"], t.get("language", "en_US"), params)
    return out


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
async def create_template(body: TemplateIn) -> dict:
    if not _settings.WHATSAPP_WABA_ID:
        raise HTTPException(status_code=400, detail="WHATSAPP_WABA_ID not configured")
    name = _re.sub(r"[^a-z0-9_]", "_", body.name.strip().lower())
    var_ids = sorted({int(n) for n in _re.findall(r"\{\{(\d+)\}\}", body.body)})
    if var_ids != list(range(1, len(var_ids) + 1)):
        raise HTTPException(
            status_code=400, detail="Variables must be {{1}}, {{2}}… in order, no gaps"
        )
    if var_ids and len(body.samples) < len(var_ids):
        raise HTTPException(
            status_code=400,
            detail=f"Provide a sample value for each of the {len(var_ids)} variables (Meta needs them for review)",
        )
    components: list[dict] = []
    body_comp: dict = {"type": "BODY", "text": body.body}
    if var_ids:
        body_comp["example"] = {"body_text": [body.samples[: len(var_ids)]]}
    components.append(body_comp)
    if body.footer:
        components.append({"type": "FOOTER", "text": body.footer})
    if body.buttons:
        btns = []
        for b in body.buttons:
            if b.type == "QUICK_REPLY":
                btns.append({"type": "QUICK_REPLY", "text": b.text})
            elif b.type == "URL":
                if not b.url:
                    raise HTTPException(status_code=400, detail=f"Button '{b.text}' needs a URL")
                btns.append({"type": "URL", "text": b.text, "url": b.url})
            else:
                if not b.phone_number:
                    raise HTTPException(status_code=400, detail=f"Button '{b.text}' needs a phone number")
                btns.append({"type": "PHONE_NUMBER", "text": b.text, "phone_number": b.phone_number})
        components.append({"type": "BUTTONS", "buttons": btns})

    status, data = await _graph(
        "POST", f"{_settings.WHATSAPP_WABA_ID}/message_templates",
        json={
            "name": name, "language": body.language,
            "category": body.category, "components": components,
        },
    )
    if status != 200:
        err = data.get("error", {})
        raise HTTPException(
            status_code=400,
            detail=err.get("error_user_msg") or err.get("message") or str(data)[:250],
        )
    await audit.record(
        actor_role="admin", actor="dashboard", action="template_submitted",
        args={"name": name, "category": body.category}, result=data.get("status", "PENDING"),
    )
    return {"name": name, "status": data.get("status", "PENDING")}


@router.delete("/templates/{name}")
async def delete_template(name: str) -> dict:
    status, data = await _graph(
        "DELETE", f"{_settings.WHATSAPP_WABA_ID}/message_templates",
        params={"name": name},
    )
    if status != 200:
        raise HTTPException(status_code=400, detail=str(data)[:250])
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
_SECRET_SETTINGS = {"ig_access_token"}
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


@router.put("/settings")
async def put_setting(body: SettingIn, db: AsyncSession = Depends(get_db)) -> dict:
    # Saving the mask back would overwrite the real secret with dots.
    if body.key in _SECRET_SETTINGS and body.value == _SECRET_MASK:
        return {"ok": True, "unchanged": True}
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
    await db.commit()
    await audit.record(
        actor_role="admin", actor="dashboard",
        action="agent_paused" if body.paused else "agent_resumed",
        args={"phone": phone}, result="",
    )
    return {"phone": phone, "agent_paused": cust.agent_paused}


@router.get("/whatsapp/stats", dependencies=[Depends(require_feature("reports"))])
async def whatsapp_stats(db: AsyncSession = Depends(get_db)) -> dict:
    """Today's WhatsApp traffic (our DB) + live Meta template/quality data."""
    from zoneinfo import ZoneInfo

    from app.models import Conversation, Direction

    ist = ZoneInfo("Asia/Kolkata")
    today_start = (
        datetime.now(ist).replace(hour=0, minute=0, second=0, microsecond=0)
    ).astimezone(timezone.utc)
    sent_n = (
        await db.execute(
            select(func.count()).select_from(Conversation).where(
                Conversation.direction == Direction.OUTBOUND,
                Conversation.created_at >= today_start,
            )
        )
    ).scalar_one()
    recv_n = (
        await db.execute(
            select(func.count()).select_from(Conversation).where(
                Conversation.direction == Direction.INBOUND,
                Conversation.created_at >= today_start,
            )
        )
    ).scalar_one()
    talked = (
        await db.execute(
            select(func.count(func.distinct(Conversation.customer_id))).where(
                Conversation.created_at >= today_start,
                Conversation.customer_id.isnot(None),
            )
        )
    ).scalar_one()

    tpl = {"approved": 0, "pending": 0, "rejected": 0}
    quality, meta_ok = None, False
    try:
        status, data = await _graph(
            "GET", f"{_settings.WHATSAPP_WABA_ID}/message_templates",
            params={"fields": "name,status", "limit": 100},
        )
        if status == 200:
            meta_ok = True
            for t in data.get("data", []):
                k = (t.get("status") or "").lower()
                if k in tpl:
                    tpl[k] += 1
        s2, d2 = await _graph(
            "GET", f"{_settings.WHATSAPP_PHONE_NUMBER_ID}",
            params={"fields": "quality_rating"},
        )
        if s2 == 200:
            quality = d2.get("quality_rating")
    except Exception:
        log.exception("wa_stats_meta_failed")
    return {
        "today": {"sent": sent_n, "received": recv_n, "customers_talked": talked},
        "templates": tpl,
        "quality": quality,
        "meta_ok": meta_ok,
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
