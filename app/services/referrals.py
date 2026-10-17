"""Referral rewards on top of the existing Campaign, Coupon, and CustomerReward tables.

A delivered customer can share one reusable referral coupon. A first-time referred
customer receives the coupon discount on their first order; the referrer earns a
customer-locked reward only after that order is delivered. No new tables needed.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Campaign,
    Coupon,
    CouponRedemption,
    Customer,
    CustomerReward,
    Order,
    OrderStatus,
)
from app.services import app_settings, audit
from app.services.whatsapp import SendError, send_message
from app.utils.dates import today_ist

log = structlog.get_logger()


async def referral_campaign_for_coupon(db: AsyncSession, coupon: Coupon | None) -> Campaign | None:
    """Return the campaign registry only when this is a real referral coupon."""
    if coupon is None or coupon.campaign_id is None:
        return None
    campaign = await db.get(Campaign, coupon.campaign_id)
    if (
        campaign is not None
        and isinstance(campaign.stats, dict)
        and campaign.stats.get("referral_program") is True
        and campaign.stats.get("referrer_customer_id")
    ):
        return campaign
    return None


async def _code() -> str:
    return "REF-" + secrets.token_hex(4).upper()


async def _get_or_create_referral_code(
    db: AsyncSession, customer: Customer
) -> tuple[Campaign, Coupon]:
    campaigns = (
        await db.execute(
            select(Campaign).where(Campaign.segment == "referral").order_by(Campaign.created_at.desc())
        )
    ).scalars().all()
    for campaign in campaigns:
        stats = campaign.stats if isinstance(campaign.stats, dict) else {}
        if stats.get("referral_program") is not True or str(stats.get("referrer_customer_id")) != str(customer.id):
            continue
        coupon = (
            await db.execute(
                select(Coupon).where(Coupon.campaign_id == campaign.id, Coupon.active.is_(True))
                .order_by(Coupon.created_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
        if coupon and (coupon.valid_to is None or coupon.valid_to >= today_ist()):
            return campaign, coupon

    friend_discount = Decimal(str(await app_settings.get(db, "referral_friend_discount") or 50))
    min_order = Decimal(str(await app_settings.get(db, "referral_min_order") or 300))
    valid_days = max(1, min(365, int(await app_settings.get(db, "referral_coupon_valid_days") or 90)))
    friend_discount = max(Decimal("1"), min(friend_discount, Decimal("500")))
    min_order = max(Decimal("0"), min(min_order, Decimal("100000")))
    valid_to = today_ist() + timedelta(days=valid_days)

    for _ in range(5):
        code = await _code()
        exists = (
            await db.execute(select(Coupon.id).where(Coupon.code == code).limit(1))
        ).scalar_one_or_none()
        if exists is None:
            break
    else:
        raise RuntimeError("could not generate a unique referral code")

    campaign = Campaign(
        name=f"Referral code for {customer.id}",
        segment="referral",
        message_text="Referral code registry; not a bulk marketing campaign.",
        status="sent",
        created_by="system",
        stats={
            "referral_program": True,
            "referrer_customer_id": str(customer.id),
            "reward_model": "friend_discount_then_referrer_reward",
        },
        referral_owner_customer_id=customer.id,
    )
    try:
        db.add(campaign)
        await db.flush()
        coupon = Coupon(
            code=code,
            discount_type="flat",
            value=friend_discount,
            min_order=min_order,
            valid_from=today_ist(),
            valid_to=valid_to,
            per_customer_limit=1,
            total_limit=1000,
            campaign_id=campaign.id,
            customer_id=None,
            active=True,
        )
        db.add(coupon)
        await db.commit()
    except IntegrityError:
        # A concurrent delivery event for this same customer won the unique
        # referral-owner constraint. Reuse its code instead of issuing a second.
        await db.rollback()
        existing_campaign = (
            await db.execute(
                select(Campaign).where(
                    Campaign.tenant_id == customer.tenant_id,
                    Campaign.referral_owner_customer_id == customer.id,
                )
            )
        ).scalar_one_or_none()
        if existing_campaign is None:
            raise
        existing_coupon = (
            await db.execute(
                select(Coupon).where(
                    Coupon.campaign_id == existing_campaign.id,
                    Coupon.active.is_(True),
                ).order_by(Coupon.created_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
        if existing_coupon is None:
            raise
        return existing_campaign, existing_coupon
    await audit.record(
        actor_role="system",
        actor="referrals",
        action="referral_code_created",
        args={"customer_id": str(customer.id), "campaign_id": str(campaign.id)},
        result="created",
    )
    return campaign, coupon


async def _issue_share_code_after_delivery(db: AsyncSession, order: Order) -> None:
    if not bool(await app_settings.get(db, "referral_program_enabled")):
        return
    autonomy = str(await app_settings.get(db, "marketing_autonomy") or "suggest").lower()
    if autonomy != "auto":
        await audit.record(
            actor_role="system", actor="referrals", action="referral_share_skipped",
            args={"order_id": str(order.id)}, result=f"autonomy_{autonomy}",
        )
        return
    customer = await db.get(Customer, order.customer_id)
    if customer is None or not customer.is_active or customer.opted_out or customer.marketing_opt_out:
        return
    from app.services.marketing import eligible

    ok, reason = await eligible(db, customer.id)
    if not ok:
        await audit.record(
            actor_role="system", actor="referrals", action="referral_share_skipped",
            args={"customer_id": str(customer.id), "order_id": str(order.id)},
            result=reason,
        )
        return

    # Claim before code/coupon creation and before the WhatsApp side effect.
    # The tenant prefix is applied by scheduler._claim; duplicate delivery
    # events and concurrent webhook retries therefore cannot issue/send twice.
    from app.services.scheduler import _claim, _unclaim

    claim_key = f"referral-share:{order.id}"
    if not await _claim(claim_key):
        return
    send_succeeded = False
    try:
        campaign, coupon = await _get_or_create_referral_code(db, customer)
        friend_discount = f"₹{coupon.value:.0f}"
        minimum = f"₹{coupon.min_order:.0f}" if coupon.min_order is not None else "no minimum"
        valid_to = coupon.valid_to.strftime("%d %b %Y") if coupon.valid_to else "until further notice"
        message = (
            f"Namaste {customer.name or 'ji'}! 🙏\n"
            f"Aapka order {order.order_number} deliver ho gaya. Doston ko Kwik Klin recommend karein.\n"
            f"Unke pehle order par {friend_discount} off (minimum {minimum}).\n"
            f"Referral code: {coupon.code}\nValid till: {valid_to}.\n"
            "Aapko bhi unka pehla order deliver hone par referral reward milega. 🧺"
        )
        try:
            await send_message(
                db, to_phone=customer.phone, text=message, sent_by="bot",
                intent="promotional", enqueue_on_fail=False,
            )
            send_succeeded = True
            customer.last_marketing_at = datetime.now(timezone.utc)
            await db.commit()
            await audit.record(
                actor_role="system", actor="referrals", action="referral_share_code_sent",
                args={"customer_id": str(customer.id), "campaign_id": str(campaign.id)},
                result="sent",
            )
        except SendError as exc:
            await _unclaim(claim_key)
            log.info("referral_share_code_send_failed", reason=str(exc)[:100])
            await audit.record(
                actor_role="system", actor="referrals", action="referral_share_code_send_failed",
                args={"customer_id": str(customer.id), "campaign_id": str(campaign.id)},
                result=str(exc)[:160], ok=False,
            )
    except Exception:
        # Release only when no send succeeded; audit/commit failures after a
        # successful transport must not make a duplicate delivery send again.
        if not send_succeeded:
            await _unclaim(claim_key)
        raise


async def _reward_referrer_after_delivery(db: AsyncSession, order: Order) -> None:
    redemption = (
        await db.execute(
            select(CouponRedemption).where(CouponRedemption.order_id == order.id).limit(1)
        )
    ).scalar_one_or_none()
    if redemption is None:
        return
    coupon = (
        await db.execute(select(Coupon).where(Coupon.code == redemption.coupon_code).limit(1))
    ).scalar_one_or_none()
    campaign = await referral_campaign_for_coupon(db, coupon)
    if campaign is None:
        return

    stats = campaign.stats if isinstance(campaign.stats, dict) else {}
    try:
        referrer_id = uuid.UUID(str(stats.get("referrer_customer_id")))
    except (ValueError, TypeError, AttributeError):
        return
    if referrer_id == order.customer_id:
        return

    # Do not pay a referral reward for an existing customer or a repeat order.
    prior_orders = (
        await db.execute(
            select(func.count()).select_from(Order).where(
                Order.customer_id == order.customer_id,
                Order.id != order.id,
                Order.status != OrderStatus.CANCELLED,
                Order.created_at <= order.created_at,
            )
        )
    ).scalar_one()
    if prior_orders:
        return

    marker = f"referral:{order.customer_id}"
    already_rewarded = (
        await db.execute(
            select(CustomerReward.id).where(
                CustomerReward.customer_id == referrer_id,
                CustomerReward.note == marker,
            ).limit(1)
        )
    ).scalar_one_or_none()
    if already_rewarded is not None:
        return

    referrer = await db.get(Customer, referrer_id)
    if referrer is None or not referrer.is_active:
        return
    reward_value = Decimal(str(await app_settings.get(db, "referral_referrer_reward") or 100))
    min_order = Decimal(str(await app_settings.get(db, "referral_min_order") or 300))
    valid_days = max(1, min(365, int(await app_settings.get(db, "referral_reward_valid_days") or 30)))
    reward_value = max(Decimal("1"), min(reward_value, Decimal("1000")))
    min_order = max(Decimal("0"), min(min_order, Decimal("100000")))
    valid_to = today_ist() + timedelta(days=valid_days)

    for _ in range(5):
        reward_code = "RR-" + secrets.token_hex(4).upper()
        exists = (
            await db.execute(select(Coupon.id).where(Coupon.code == reward_code).limit(1))
        ).scalar_one_or_none()
        if exists is None:
            break
    else:
        raise RuntimeError("could not generate a unique referral reward code")

    reward = CustomerReward(
        customer_id=referrer.id,
        rule_id=None,
        coupon_code=reward_code,
        label=f"₹{reward_value:.0f} referral reward",
        status="earned",
        expires_at=datetime.combine(valid_to, datetime.max.time()).replace(tzinfo=timezone.utc),
        trigger_order_id=order.id,
        note=marker,
    )
    db.add(Coupon(
        code=reward_code,
        discount_type="flat",
        value=reward_value,
        min_order=min_order,
        valid_from=today_ist(),
        valid_to=valid_to,
        per_customer_limit=1,
        total_limit=1,
        customer_id=referrer.id,
        active=True,
    ))
    db.add(reward)
    await db.commit()
    await audit.record(
        actor_role="system", actor="referrals", action="referral_reward_earned",
        args={"referrer_id": str(referrer.id), "referred_customer_id": str(order.customer_id),
              "order_id": str(order.id)},
        result=reward_code,
    )

    # Reward notice is sent only if the existing marketing eligibility gate allows it.
    from app.services.marketing import eligible

    ok, reason = await eligible(db, referrer.id)
    if not ok or referrer.opted_out or referrer.marketing_opt_out:
        return
    try:
        await send_message(
            db,
            to_phone=referrer.phone,
            text=(
                f"🎁 Referral reward unlocked! Aapke referral se ek naya customer deliver ho gaya.\n"
                f"Aapka personal reward code: {reward_code}\n"
                f"₹{reward_value:.0f} off on minimum ₹{min_order:.0f}, valid till {valid_to:%d %b %Y}.\n"
                "Kwik Klin ko recommend karne ke liye dhanyavaad!"
            ),
            sent_by="bot",
            intent="promotional",
        )
        referrer.last_marketing_at = datetime.now(timezone.utc)
        await db.commit()
        await audit.record(
            actor_role="system", actor="referrals", action="referral_reward_notice_sent",
            args={"referrer_id": str(referrer.id), "referred_customer_id": str(order.customer_id)},
            result="sent",
        )
    except SendError as exc:
        log.info("referral_reward_notice_failed", reason=str(exc)[:100])
        await audit.record(
            actor_role="system", actor="referrals", action="referral_reward_notice_failed",
            args={"referrer_id": str(referrer.id), "referred_customer_id": str(order.customer_id)},
            result=str(exc)[:160], ok=False,
        )


async def on_order_delivered(db: AsyncSession, order: Order) -> None:
    """Idempotent best-effort hook after the order status has committed."""
    if order.status is not OrderStatus.DELIVERED:
        return
    try:
        await _reward_referrer_after_delivery(db, order)
    except Exception:
        await db.rollback()
        log.exception("referral_reward_after_delivery_failed", order_number=order.order_number)
    try:
        await _issue_share_code_after_delivery(db, order)
    except Exception:
        await db.rollback()
        log.exception("referral_share_code_after_delivery_failed", order_number=order.order_number)
