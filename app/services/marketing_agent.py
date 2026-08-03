"""The proactive marketing agent: studies the data, SUGGESTS, never
sends without the owner's approval (autonomy 'suggest' is the default).

Weekly (Mon night, via scheduler): pick the biggest opportunity segment,
draft copy, message the owner with reach + reasoning. The owner replies
'campaign yes' / 'campaign nahi' on WhatsApp (handled in bill_agent) or
approves from the dashboard.
"""

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal

import structlog
from sqlalchemy import select

from app.config import settings
from app.database import async_session_factory
from app.models import Campaign, Coupon, CouponRedemption, Customer, Order
from app.services import app_settings, audit, llm_client
from app.services.llm_client import LLMError
from app.services.marketing import compute_segments, queue_campaign, send_campaign
from app.services.whatsapp import SendError, send_message

log = structlog.get_logger()

# Which segment to chase first, and the default pitch for each.
_PLAYBOOK = [
    ("lapsed", "60+ din se koi order nahi — win-back offer se wapas laao"),
    ("at_risk", "regular customer thande pad rahe hain — yaad dilao"),
    ("outstanding_dues", "udhaar baaki hai — polite reminder bhi business hai"),
    ("high_value", "best customers ko thank-you + priority treatment"),
]

_COPY_SYSTEM = (
    "You write ONE short WhatsApp marketing message (max 3 lines) for Kwik "
    "Klin laundry, Varanasi, in warm Hinglish. Use {name} as the customer "
    "name placeholder. Mention the offer EXACTLY as given — never invent "
    "discounts, prices or dates. End with '— Kwik Klin'. No links."
)


async def weekly_suggestion() -> None:
    """Weekly campaign: suggest to the owner, or (autonomy=auto) just do it.

    Auto mode still obeys every guardrail IN CODE: opted-in only, frequency
    cap, monthly budget, quiet hours — and the owner is told what was sent.
    """
    async with async_session_factory() as db:
        autonomy = await app_settings.get(db, "marketing_autonomy")
        if autonomy == "off":
            return
        segs = await compute_segments(db)
        target_seg, pitch = None, ""
        for seg, why in _PLAYBOOK:
            if len(segs.get(seg, [])) >= 3:  # too small = not worth a campaign
                target_seg, pitch = seg, why
                break
        if target_seg is None:
            log.info("weekly_suggestion_no_opportunity")
            return

        members = segs[target_seg]
        reach = len(members)
        usual_revenue = sum(Decimal(m["lifetime_paid"]) for m in members)
        offer = "10% off agle order par, 7 din valid"
        copy = await _draft_copy(target_seg, offer)

        campaign = Campaign(
            name=f"{target_seg}-{date.today().isoformat()}",
            segment=target_seg,
            message_text=copy,
            status="suggested",
            rationale=(
                f"{pitch}. Reach: {reach} customers, unka ab tak ka business "
                f"₹{usual_revenue:.0f}. Offer: {offer}."
            ),
            created_by="agent",
        )
        db.add(campaign)
        await db.commit()

        if autonomy == "auto":
            # Full autopilot: approve + send now; TELL the owner, don't ask.
            campaign.status = "approved"
            await db.commit()
            queued = await queue_campaign(db, campaign)
            asyncio.create_task(send_campaign(campaign.id))
            text = (
                f"🚀 Maine campaign bhej diya (autopilot ON):\n"
                f"{campaign.rationale}\n\nMessage:\n{copy}\n\n"
                f"{queued} eligible customers ko ja raha hai (opted-out/"
                f"complaint wale auto-skip). Report: dashboard → Campaigns. "
                f"Rokna ho to: 'campaign nahi'."
            )
            await audit.record(
                actor_role="system", actor="marketing", action="campaign_auto_sent",
                args={"segment": target_seg, "queued": queued}, result=campaign.name,
            )
        else:
            text = (
                f"📣 Marketing idea:\n{campaign.rationale}\n\nMessage draft:\n"
                f"{copy}\n\nBhejun? Reply 'campaign yes' → send hoga (sirf "
                f"opted-in customers ko, quiet hours ke bahar). 'campaign nahi' → skip."
            )
            await audit.record(
                actor_role="system", actor="marketing", action="campaign_suggested",
                args={"segment": target_seg, "reach": reach}, result=campaign.name,
            )
        try:
            await send_message(db, to_phone=settings.MANAGER_PHONE, text=text)
        except SendError:
            log.warning("weekly_suggestion_not_sent")  # dashboard still shows it


async def _draft_copy(segment: str, offer: str) -> str:
    fallback = (
        "Namaste {name} ji! Kaafi din ho gaye — kapdon ki dhulai ya dry clean "
        f"ki zaroorat ho to yaad kijiyega. {offer}. — Kwik Klin"
    )
    # owner's style rules from AI training (hot-reloaded, optional)
    extra = ""
    try:
        async with async_session_factory() as db:
            extra = (await app_settings.get(db, "marketing_instructions") or "").strip()
    except Exception:
        pass
    system = _COPY_SYSTEM + (f"\nOwner's style rules (follow them): {extra}" if extra else "")
    try:
        copy = await llm_client.ask(
            system=system,
            user_text=f"Segment: {segment}. Offer: {offer}. Draft the message.",
            model=llm_client.MODEL_SMART,
            max_tokens=200,
        )
        return copy.strip() or fallback
    except LLMError:
        return fallback


async def preview_suggestion(db) -> str:
    """'test marketing' on WhatsApp: show what the agent WOULD send. No sends."""
    segs = await compute_segments(db)
    counts = ", ".join(f"{k}: {len(v)}" for k, v in segs.items() if v)
    target_seg, pitch = None, ""
    for seg, why in _PLAYBOOK:
        if len(segs.get(seg, [])) >= 3:
            target_seg, pitch = seg, why
            break
    if target_seg is None:
        return (
            f"🧪 Marketing preview:\nSegments abhi: {counts or 'sab khali'}\n"
            "Koi segment 3+ customers ka nahi — is hafte campaign nahi banta. "
            "Jaise hi customers badhenge, Monday ko khud bhejunga."
        )
    offer = "10% off agle order par, 7 din valid"
    copy = await _draft_copy(target_seg, offer)
    return (
        f"🧪 Marketing preview (kuch bheja NAHI gaya):\n"
        f"Segments: {counts}\n\n"
        f"Agla campaign hoga → {target_seg} ({len(segs[target_seg])} customers)\n"
        f"Wajah: {pitch}\n\nMessage draft:\n{copy}\n\n"
        "Style badalna ho: Dashboard → AI training → Marketing message style."
    )


async def approve_latest_suggestion(db, approved: bool) -> str:
    """Owner replied 'campaign yes/nahi' on WhatsApp. Returns reply text."""
    campaign = (
        await db.execute(
            select(Campaign)
            .where(Campaign.status == "suggested")
            .order_by(Campaign.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if campaign is None and not approved:
        # 'campaign nahi' also works as an emergency brake on a running send
        campaign = (
            await db.execute(
                select(Campaign)
                .where(Campaign.status.in_(("approved", "sending")))
                .order_by(Campaign.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if campaign is not None:
            campaign.status = "cancelled"
            await db.commit()
            return f"🛑 '{campaign.name}' rok diya — jo bache the unko nahi jayega."
    if campaign is None:
        return "Koi campaign suggestion pending nahi hai."
    if not approved:
        campaign.status = "cancelled"
        await db.commit()
        return f"Theek hai, '{campaign.name}' skip kar diya."
    campaign.status = "approved"
    await db.commit()
    queued = await queue_campaign(db, campaign)
    asyncio.create_task(send_campaign(campaign.id))
    await audit.record(
        actor_role="admin", actor="manager", action="campaign_approved",
        args={"campaign": campaign.name}, result=f"queued {queued}",
    )
    return (
        f"✅ '{campaign.name}' approved — {queued} eligible customers ko "
        f"bheja ja raha hai (opted-out/complaint wale auto-skip). "
        f"Report: dashboard → Campaigns."
    )


# ---------------------------------------------------------------------------
# Coupons
# ---------------------------------------------------------------------------


async def validate_coupon(
    db, code: str, customer_id, order_total: Decimal
) -> tuple[Coupon | None, Decimal, str]:
    """Returns (coupon, discount, error). error='' when valid."""
    coupon = await db.get(Coupon, code.strip().upper())
    if coupon is None or not coupon.active:
        return None, Decimal("0"), "coupon nahi mila ya band hai"
    today = date.today()
    if coupon.valid_from and today < coupon.valid_from:
        return None, Decimal("0"), "coupon abhi shuru nahi hua"
    if coupon.valid_to and today > coupon.valid_to:
        return None, Decimal("0"), "coupon expire ho gaya"
    if coupon.min_order and order_total < coupon.min_order:
        return None, Decimal("0"), f"minimum order ₹{coupon.min_order} chahiye"
    from sqlalchemy import func as f

    used_by_cust = (
        await db.execute(
            select(f.count())
            .select_from(CouponRedemption)
            .where(
                CouponRedemption.coupon_code == coupon.code,
                CouponRedemption.customer_id == customer_id,
            )
        )
    ).scalar_one()
    if used_by_cust >= coupon.per_customer_limit:
        return None, Decimal("0"), "aap ye coupon use kar chuke hain"
    if coupon.total_limit is not None:
        used_total = (
            await db.execute(
                select(f.count())
                .select_from(CouponRedemption)
                .where(CouponRedemption.coupon_code == coupon.code)
            )
        ).scalar_one()
        if used_total >= coupon.total_limit:
            return None, Decimal("0"), "coupon ki limit khatam"
    if coupon.discount_type == "percent":
        discount = (order_total * coupon.value / 100).quantize(Decimal("0.01"))
    else:
        discount = min(coupon.value, order_total)
    return coupon, discount, ""


async def redeem_coupon(db, coupon: Coupon, order: Order, discount: Decimal) -> None:
    db.add(
        CouponRedemption(
            coupon_code=coupon.code,
            order_id=order.id,
            customer_id=order.customer_id,
            discount_applied=discount,
        )
    )
    await db.commit()
    log.info("coupon_redeemed", code=coupon.code, order=order.order_number)
