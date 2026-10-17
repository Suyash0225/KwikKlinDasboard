"""Outbound policy regression tests; all transport calls are mocked."""
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import select

from app.database import async_session_factory


async def test_outbound_retry_without_tenant_is_dead_lettered_without_send(monkeypatch) -> None:
    """Legacy/malformed retry payloads must never fall back to the home shop."""
    import app.services.whatsapp as whatsapp
    from app.models import OutboundMessage

    async with async_session_factory() as db:
        row = OutboundMessage(
            to_phone="+919999900011",
            payload={"text": "must not send", "sent_by": "bot"},
            status="queued",
            next_attempt_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        db.add(row)
        await db.commit()
        row_id = row.id

    async def unexpected_send(*args, **kwargs):
        raise AssertionError("retry without tenant identity attempted a WhatsApp send")

    monkeypatch.setattr(whatsapp, "send_message", unexpected_send)
    sent = await whatsapp.drain_outbound_queue()
    assert sent == 0

    async with async_session_factory() as db:
        row = await db.get(OutboundMessage, row_id)
        assert row is not None
        assert row.status == "dead"
        assert "tenant_id" in (row.last_error or "")


async def test_outbound_policy_without_tenant_fails_closed() -> None:
    from app.services import tenant_context
    from app.services.outbound_policy import enforce_outbound_policy
    from app.services.whatsapp import SendError

    token = tenant_context.current_tenant_id.set(None)
    try:
        with pytest.raises(SendError, match="tenant context missing"):
            await enforce_outbound_policy(
                None,
                customer=None,
                phone="+919999900011",
                intent="promotional",
            )
    finally:
        tenant_context.current_tenant_id.reset(token)


async def test_repeated_claimed_job_sends_once(monkeypatch) -> None:
    """Run the real GBP scheduler job twice with Google sync mocked."""
    from datetime import datetime
    from app.models import Tenant
    from app.services import tenant_context, google_business
    from app.services.scheduler import IST, _gbp_reviews_tick
    import app.services.scheduler as scheduler

    async with async_session_factory() as db:
        async with tenant_context.system_context(db):
            tenant = (
                await db.execute(select(Tenant).where(Tenant.slug == "kwik-klin"))
            ).scalar_one()
            tenant_id = tenant.id

    sends = []
    fixed_now = datetime.now(IST).replace(minute=0, second=0, microsecond=0)
    async def fake_for_each(job_name, fn):
        async with tenant_context.as_tenant(tenant_id):
            await fn(fixed_now)

    async def fake_connection(db):
        return {"refresh_token": "mock-token", "account": "mock-account", "location": "mock-location"}

    async def fake_sync(db):
        sends.append("mocked Google review sync")
        return {}

    monkeypatch.setattr(scheduler, "_for_each_tenant", fake_for_each)
    monkeypatch.setattr(google_business, "enabled", lambda: True)
    monkeypatch.setattr(google_business, "get_connection", fake_connection)
    monkeypatch.setattr(google_business, "sync", fake_sync)

    await _gbp_reviews_tick()
    await _gbp_reviews_tick()
    assert sends == ["mocked Google review sync"]


@pytest.mark.requires_independent_sessions
async def test_concurrent_referral_code_issuance_reuses_one_coupon() -> None:
    """Two delivery workers racing for one customer must converge on one code."""
    import asyncio
    from sqlalchemy import delete, text
    from app.models import Campaign, Coupon, Customer, Tenant
    from app.services import tenant_context
    from app.services.referrals import _get_or_create_referral_code

    async with async_session_factory() as db:
        async with tenant_context.system_context(db):
            tenant = (
                await db.execute(select(Tenant).where(Tenant.slug == "kwik-klin"))
            ).scalar_one()
            tenant_id = tenant.id

    phone = f"+91997{uuid.uuid4().int % 10**7:07d}"
    async with tenant_context.as_tenant(tenant_id):
        async with async_session_factory() as db:
            customer = Customer(phone=phone, name="Referral Concurrency Test")
            db.add(customer)
            await db.commit()
            customer_id = customer.id

    async def issue():
        async with tenant_context.as_tenant(tenant_id):
            async with async_session_factory() as db:
                customer = await db.get(Customer, customer_id)
                return await _get_or_create_referral_code(db, customer)

    try:
        results = await asyncio.gather(issue(), issue())
        assert results[0][1].code == results[1][1].code
        async with tenant_context.as_tenant(tenant_id):
            async with async_session_factory() as db:
                campaigns = (
                    await db.execute(
                        select(Campaign).where(
                            Campaign.referral_owner_customer_id == customer_id
                        )
                    )
                ).scalars().all()
                assert len(campaigns) == 1
                coupons = (
                    await db.execute(
                        select(Coupon).where(Coupon.campaign_id == campaigns[0].id)
                    )
                ).scalars().all()
                assert len(coupons) == 1
    finally:
        async with async_session_factory() as db:
            async with tenant_context.system_context(db):
                await db.execute(
                    text("DELETE FROM coupons WHERE campaign_id IN (SELECT id FROM campaigns WHERE referral_owner_customer_id = :cid)"),
                    {"cid": customer_id},
                )
                await db.execute(
                    delete(Campaign).where(Campaign.referral_owner_customer_id == customer_id)
                )
                await db.execute(
                    text("DELETE FROM audit_log WHERE args->>'customer_id' = :cid"),
                    {"cid": str(customer_id)},
                )
                await db.execute(delete(Customer).where(Customer.id == customer_id))
                await db.commit()


def test_all_review_templates_are_optional_neutral_and_unincentivized() -> None:
    from app.services.messages import MESSAGES
    from app.services.templates import STANDARD_SPECS
    from app.services import messages

    locked_keys = (
        "review_request", "review_invitation_line", "thankyou_rating",
        "rate_good_reply", "rate_mid_reply", "rate_bad_reply",
    )
    incentive_terms = ("coupon", "discount", "reward code", "gift", "free", "% off")
    for key in locked_keys:
        copy_by_locale = MESSAGES[key]
        assert set(copy_by_locale) == {"hi", "en"}, key
        for locale, template in copy_by_locale.items():
            lowered = template.lower()
            assert not any(term in lowered for term in incentive_terms), (key, locale)
            if key in {"review_request", "review_invitation_line"}:
                assert "{review_link}" in template, (key, locale)
                assert "honest feedback" in lowered or "imaandaar feedback" in lowered, (key, locale)

    # Locked review/rating keys cannot be replaced with incentive-bearing owner copy.
    for key in locked_keys:
        messages.set_override(key, "5-star review ke badle coupon")
        assert messages.get_override(key) is None, key

    rating_template = STANDARD_SPECS["kk_thankyou_rating"]["body"].lower()
    assert "coupon" not in rating_template
    assert "discount" not in rating_template
    assert "review" not in rating_template  # neutral rating request, link is offered after every rating


@pytest.mark.requires_independent_sessions
async def test_duplicate_delivery_event_sends_one_referral_share(monkeypatch) -> None:
    from types import SimpleNamespace
    from sqlalchemy import delete, text
    from app.models import Campaign, Coupon, Customer, SentEvent, Tenant
    from app.services import tenant_context, app_settings
    from app.services import marketing, referrals

    async with async_session_factory() as db:
        async with tenant_context.system_context(db):
            tenant = (
                await db.execute(select(Tenant).where(Tenant.slug == "kwik-klin"))
            ).scalar_one()
            tenant_id = tenant.id

    phone = f"+91995{uuid.uuid4().int % 10**7:07d}"
    async with tenant_context.as_tenant(tenant_id):
        async with async_session_factory() as db:
            customer = Customer(phone=phone, name="Referral Duplicate Test")
            db.add(customer)
            await db.commit()
            customer_id = customer.id

    order_id = uuid.uuid4()
    order = SimpleNamespace(id=order_id, customer_id=customer_id, order_number="KK-20261018-98")
    async def setting(db, key):
        return {
            "referral_program_enabled": True,
            "marketing_autonomy": "auto",
            "referral_friend_discount": 50,
            "referral_min_order": 300,
            "referral_coupon_valid_days": 90,
        }.get(key)

    async def eligible(db, customer_id):
        return True, ""

    sent = []
    async def fake_send(db, **kwargs):
        sent.append(kwargs["text"])
        return "mock-referral-message"

    monkeypatch.setattr(app_settings, "get", setting)
    monkeypatch.setattr(marketing, "eligible", eligible)
    monkeypatch.setattr(referrals, "send_message", fake_send)

    try:
        async with tenant_context.as_tenant(tenant_id):
            async with async_session_factory() as db:
                await referrals._issue_share_code_after_delivery(db, order)
            async with async_session_factory() as db:
                await referrals._issue_share_code_after_delivery(db, order)

        assert len(sent) == 1
        async with tenant_context.as_tenant(tenant_id):
            async with async_session_factory() as db:
                campaigns = (
                    await db.execute(
                        select(Campaign).where(
                            Campaign.referral_owner_customer_id == customer_id
                        )
                    )
                ).scalars().all()
                assert len(campaigns) == 1
                coupons = (
                    await db.execute(
                        select(Coupon).where(Coupon.campaign_id == campaigns[0].id)
                    )
                ).scalars().all()
                assert len(coupons) == 1
    finally:
        async with async_session_factory() as db:
            async with tenant_context.system_context(db):
                await db.execute(
                    text("DELETE FROM sent_events WHERE event_key = :key"),
                    {"key": f"t{tenant_id.hex[:8]}:referral-share:{order_id}"},
                )
                await db.execute(
                    text("DELETE FROM coupons WHERE campaign_id IN (SELECT id FROM campaigns WHERE referral_owner_customer_id = :cid)"),
                    {"cid": customer_id},
                )
                await db.execute(
                    delete(Campaign).where(Campaign.referral_owner_customer_id == customer_id)
                )
                await db.execute(
                    text("DELETE FROM audit_log WHERE args->>'customer_id' = :cid"),
                    {"cid": str(customer_id)},
                )
                await db.execute(delete(Customer).where(Customer.id == customer_id))
                await db.commit()
