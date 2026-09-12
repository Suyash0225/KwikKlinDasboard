"""The proactive marketing agent: studies the data, SUGGESTS, never
sends without the owner's approval (autonomy 'suggest' is the default).

Weekly (Mon night, via scheduler): pick the biggest opportunity segment,
draft copy, message the owner with reach + reasoning. The owner replies
'campaign yes' / 'campaign nahi' on WhatsApp (handled in bill_agent) or
approves from the dashboard.
"""

import asyncio
from datetime import date
from decimal import Decimal

import structlog
from sqlalchemy import case, func, select

from app.config import settings
from app.database import async_session_factory
from app.models import Campaign, CampaignRecipient, Coupon, CouponRedemption, Order
from app.services import app_settings, audit, llm_client
from app.services.llm_client import LLMError
from app.services.marketing import compute_segments, queue_campaign, send_campaign
from app.services.whatsapp import SendError, send_message
from app.services.tenant_context import manager_phone

log = structlog.get_logger()

# Each segment: strategic weight, why we'd chase it, what we'd offer.
#
# The offer is per-segment on purpose. A blanket "10% off" used to go to
# everyone — including people who OWE us money (a discount reads as a
# reward for not paying) and our best customers (who were about to pay
# full price anyway). Discount only where it buys back a lost customer.
_PLAYBOOK: dict[str, tuple[float, str, str]] = {
    "lapsed": (
        1.00,
        "60+ din se koi order nahi — win-back offer se wapas laao",
        "10% off agle order par, 7 din valid",
    ),
    "at_risk": (
        0.90,
        "regular customer thande pad rahe hain — yaad dilao",
        "5% off agle order par, 10 din valid",
    ),
    "outstanding_dues": (
        0.70,
        "udhaar baaki hai — polite reminder bhi business hai",
        "koi discount nahi, sirf vinamra yaad-dehani",
    ),
    "high_value": (
        0.60,
        "best customers ko thank-you + priority treatment",
        "priority service: same-day pickup, koi extra charge nahi",
    ),
}

# Too small to be a campaign — the noise isn't worth the send.
_MIN_SEGMENT_SIZE = 3

# Response rate when a segment has no history yet, plus how much history it
# takes to overrule that guess (Beta-style smoothing). Without the prior,
# one lucky reply out of two sends would read as a 50% segment and the
# agent would chase it forever.
_PRIOR_RESPONSE_RATE = 0.08
_PRIOR_WEIGHT = 20

# The owner's seasonal calendar — copy rides whatever Varanasi is doing.
_SEASONS = {
    9: "Diwali safai: parde, sofa cover, carpet",
    10: "Diwali safai: parde, sofa cover, carpet",
    11: "Kambal-razai + shaadi season (saree, sherwani, lehenga)",
    12: "Kambal-razai + shaadi season",
    1: "Kambal-razai + shaadi season",
    2: "Kambal-razai + shaadi season",
    3: "Holi ke baad daag safai + winter clothes storage",
    4: "Holi ke baad daag safai + storage",
    5: "Garmi: bedsheet, AC cover, curtain",
    6: "Garmi: bedsheet, AC cover, curtain",
    7: "Barish: hum dho kar, sukha kar, press karke denge",
    8: "Barish: sukha ke denge wala angle",
}

_COPY_SYSTEM = (
    "You write ONE short WhatsApp marketing message (max 3 lines) for Kwik "
    "Klin laundry, Varanasi, in warm Hinglish. Use {name} as the customer "
    "name placeholder. Mention the offer EXACTLY as given — never invent "
    "discounts, prices or dates. End with '— Kwik Klin'. No links."
)

# Fire-and-forget sends need a strong reference. asyncio only holds a WEAK
# one, so a bare create_task() can be garbage-collected mid-campaign and
# the send just... stops, with no error anywhere.
_background: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


async def _response_rates(db) -> dict[str, float]:
    """What each segment has ACTUALLY replied at, smoothed by a prior.

    This is the agent learning from its own results: a segment that keeps
    ignoring us loses its turn to one that answers, without anyone editing
    the playbook.
    """
    rows = (
        await db.execute(
            select(
                Campaign.segment,
                func.count(CampaignRecipient.id),
                func.count(case((CampaignRecipient.status == "replied", 1))),
            )
            .join(CampaignRecipient, CampaignRecipient.campaign_id == Campaign.id)
            .where(
                CampaignRecipient.status.in_(("sent", "delivered", "read", "replied"))
            )
            .group_by(Campaign.segment)
        )
    ).all()
    return {
        seg: (replied + _PRIOR_RESPONSE_RATE * _PRIOR_WEIGHT) / (sent + _PRIOR_WEIGHT)
        for seg, sent, replied in rows
    }


def _avg_order_value(members: list[dict]) -> Decimal:
    orders = sum(m["order_count"] for m in members)
    if not orders:
        return Decimal("0")
    return sum(Decimal(m["lifetime_paid"]) for m in members) / orders


async def _pick_segment(db) -> dict | None:
    """The one campaign worth running this week, or None.

    Scores by EXPECTED RUPEES, not playbook order:
        reach x average order value x learned response rate x weight
    The old code took the first playbook entry with 3+ members, so a
    4-person lapsed segment beat a 200-person at_risk one every time.
    """
    segs = await compute_segments(db)
    rates = await _response_rates(db)

    ranked = []
    for seg, (weight, pitch, offer) in _PLAYBOOK.items():
        members = segs.get(seg, [])
        if len(members) < _MIN_SEGMENT_SIZE:
            continue
        aov = _avg_order_value(members)
        rate = rates.get(seg, _PRIOR_RESPONSE_RATE)
        ranked.append(
            {
                "segment": seg,
                "pitch": pitch,
                "offer": offer,
                "members": members,
                "reach": len(members),
                "aov": aov,
                "rate": rate,
                "score": len(members) * float(aov) * rate * weight,
            }
        )
    if not ranked:
        return None
    ranked.sort(key=lambda c: c["score"], reverse=True)
    best = ranked[0]
    log.info(
        "campaign_segment_picked",
        segment=best["segment"], reach=best["reach"],
        expected_value=round(best["score"]),
        runners_up={c["segment"]: round(c["score"]) for c in ranked[1:]},
    )
    return best


async def weekly_suggestion() -> None:
    """Weekly campaign: suggest to the owner, or (autonomy=auto) just do it.

    Auto mode still obeys every guardrail IN CODE: opted-in only, frequency
    cap, monthly budget, quiet hours — and the owner is told what was sent.
    """
    async with async_session_factory() as db:
        autonomy = await app_settings.get(db, "marketing_autonomy")
        if autonomy == "off":
            return
        pick = await _pick_segment(db)
        if pick is None:
            log.info("weekly_suggestion_no_opportunity")
            return

        target_seg, offer = pick["segment"], pick["offer"]
        season = _SEASONS.get(date.today().month, "")
        copy = await _draft_copy(f"{target_seg} ({season})" if season else target_seg, offer)

        campaign = Campaign(
            name=f"{target_seg}-{date.today().isoformat()}",
            segment=target_seg,
            message_text=copy,
            status="suggested",
            rationale=_rationale(pick),
            created_by="agent",
        )
        db.add(campaign)
        await db.commit()

        reach = pick["reach"]
        if autonomy == "auto":
            # Full autopilot: approve + send now; TELL the owner, don't ask.
            campaign.status = "approved"
            await db.commit()
            queued = await queue_campaign(db, campaign)
            _spawn(send_campaign(campaign.id))
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
            await send_message(db, to_phone=manager_phone(), text=text)
        except SendError:
            log.warning("weekly_suggestion_not_sent")  # dashboard still shows it


def _rationale(pick: dict) -> str:
    """Why THIS segment — in the owner's terms, with the maths shown.

    He is the one clicking approve; "the model suggested it" is not a
    reason he can check.
    """
    return (
        f"{pick['pitch']}. Reach: {pick['reach']} customers, average order "
        f"₹{pick['aov']:.0f}, is segment ka reply rate ab tak "
        f"{pick['rate'] * 100:.0f}% — expected business ₹{pick['score']:.0f}. "
        f"Offer: {pick['offer']}."
    )


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
        with llm_client.track("marketing"):
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
    pick = await _pick_segment(db)
    if pick is None:
        return (
            f"🧪 Marketing preview:\nSegments abhi: {counts or 'sab khali'}\n"
            f"Koi segment {_MIN_SEGMENT_SIZE}+ customers ka nahi — is hafte "
            "campaign nahi banta. Jaise hi customers badhenge, Monday ko khud bhejunga."
        )
    copy = await _draft_copy(pick["segment"], pick["offer"])
    return (
        f"🧪 Marketing preview (kuch bheja NAHI gaya):\n"
        f"Segments: {counts}\n\n"
        f"Agla campaign hoga → {pick['segment']} ({pick['reach']} customers)\n"
        f"Wajah: {_rationale(pick)}\n\nMessage draft:\n{copy}\n\n"
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
    _spawn(send_campaign(campaign.id))
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
