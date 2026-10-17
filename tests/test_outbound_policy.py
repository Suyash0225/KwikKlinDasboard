"""Outbound policy regression tests; all transport calls are mocked."""
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import delete, select

from app.database import async_session_factory

async def _test_tenant_id():
    from app.services import tenant_context

    tenant_id = await tenant_context.get_home_tenant_id()
    if tenant_id is None:
        raise RuntimeError("outbound policy tests require a seeded tenant")
    return tenant_id

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

    tenant_id = await _test_tenant_id()

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
async def test_restart_retry_job_emits_mock_message_once() -> None:
    """A scheduler retry repeats the job key; the mocked send remains one-shot."""
    from app.models import Tenant
    from app.services import tenant_context
    from app.services.scheduler import _claim

    tenant_id = await _test_tenant_id()

    sends = []
    key = f"restart-send-regression:{uuid.uuid4().hex}"
    async def job():
        async with tenant_context.as_tenant(tenant_id):
            if await _claim(key):
                sends.append("mocked WhatsApp send")

    await job()
    await job()
    assert sends == ["mocked WhatsApp send"]


@pytest.mark.requires_independent_sessions
async def test_concurrent_referral_code_issuance_reuses_one_coupon() -> None:
    """Two delivery workers racing for one customer must converge on one code."""
    import asyncio
    from sqlalchemy import delete, text
    from app.models import Campaign, Coupon, Customer, Tenant
    from app.services import tenant_context
    from app.services.referrals import _get_or_create_referral_code

    tenant_id = await _test_tenant_id()

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

    tenant_id = await _test_tenant_id()

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


async def test_promotional_daily_cap_is_shared_per_tenant(monkeypatch) -> None:
    from app.models import Customer, OutboundThrottle
    from app.services import tenant_context, app_settings, outbound_policy
    from app.services.whatsapp import SendError

    tenant_id = await _test_tenant_id()

    async def setting(db, key):
        if key == "outbound_daily_msg_cap":
            return 1
        if key == "marketing_freq_cap_per_month":
            return 2
        return 100

    async def eligible(db, customer_id):
        return True, ""

    monkeypatch.setattr(app_settings, "get", setting)
    monkeypatch.setattr(outbound_policy, "_quiet_now", lambda: False)
    monkeypatch.setattr("app.services.marketing.eligible", eligible)

    async with tenant_context.as_tenant(tenant_id):
        async with async_session_factory() as db:
            # Start from a clean tenant limiter row; other test cases may have
            # exercised promotional sends for the shared home tenant.
            await db.execute(delete(OutboundThrottle).where(OutboundThrottle.tenant_id == tenant_id))
            await db.commit()
            customer = Customer(phone=f"+91996{uuid.uuid4().int % 10**7:07d}", name="Cap Test")
            db.add(customer)
            await db.flush()
            await outbound_policy.enforce_outbound_policy(
                db, customer=customer, phone=customer.phone, intent="promotional"
            )
            with pytest.raises(SendError, match="daily cap reached"):
                await outbound_policy.enforce_outbound_policy(
                    db, customer=customer, phone=customer.phone, intent="promotional"
                )


async def test_transactional_policy_honors_stop_and_urgent_quiet_exception(monkeypatch) -> None:
    from types import SimpleNamespace
    from app.services import tenant_context, outbound_policy
    from app.services.whatsapp import SendError

    tenant_id = uuid.uuid4()
    token = tenant_context.current_tenant_id.set(tenant_id)
    monkeypatch.setattr(outbound_policy, "_quiet_now", lambda: True)
    try:
        customer = SimpleNamespace(
            tenant_id=tenant_id, is_active=False, opted_out=False,
            payment_reminders_enabled=True,
        )
        # Transactional sends are not blocked merely because a customer is inactive.
        with pytest.raises(SendError, match="quiet hours"):
            await outbound_policy.enforce_outbound_policy(
                None, customer=customer, phone="+919999000012",
                intent="transactional", kind="order_status",
            )
        await outbound_policy.enforce_outbound_policy(
            None, customer=customer, phone="+919999000012",
            intent="transactional", kind="order_status", urgent=True,
        )
        customer.opted_out = True
        with pytest.raises(SendError, match="customer opted out"):
            await outbound_policy.enforce_outbound_policy(
                None, customer=customer, phone="+919999000012",
                intent="transactional", kind="order_status", urgent=True,
            )
    finally:
        tenant_context.current_tenant_id.reset(token)


async def test_dotpe_webhook_retry_without_tenant_is_dead_lettered(monkeypatch) -> None:
    from app.models import WebhookEvent
    from app.routers import webhook

    async with async_session_factory() as db:
        event = WebhookEvent(
            source="dotpe",
            event_key=uuid.uuid4().hex + uuid.uuid4().hex,
            payload={"message": {"from": "+919999000012", "text": "hello"}},
            status="received",
            attempts=0,
            received_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        db.add(event)
        await db.commit()
        event_key = event.event_key

    async def unexpected_handler(*args, **kwargs):
        raise AssertionError("DotPe retry without tenant identity must not process/send")

    monkeypatch.setattr(webhook, "_handle_inbound_message", unexpected_handler)
    await webhook.retry_stuck_webhook_events()

    async with async_session_factory() as db:
        row = (
            await db.execute(select(WebhookEvent).where(WebhookEvent.event_key == event_key))
        ).scalar_one()
        assert row.status == "dead"
        assert "tenant_id" in (row.error or "")


async def test_promotional_daily_cap_zero_blocks_all_sends(monkeypatch) -> None:
    from app.models import Customer, OutboundThrottle
    from app.services import tenant_context, app_settings, outbound_policy
    from app.services.whatsapp import SendError

    tenant_id = await _test_tenant_id()

    async def setting(db, key):
        if key == "outbound_daily_msg_cap":
            return 0
        if key == "marketing_freq_cap_per_month":
            return 2
        return 100

    async def eligible(db, customer_id):
        return True, ""

    monkeypatch.setattr(app_settings, "get", setting)
    monkeypatch.setattr(outbound_policy, "_quiet_now", lambda: False)
    monkeypatch.setattr("app.services.marketing.eligible", eligible)

    phone = f"+91994{uuid.uuid4().int % 10**7:07d}"
    async with tenant_context.as_tenant(tenant_id):
        async with async_session_factory() as db:
            await db.execute(delete(OutboundThrottle).where(OutboundThrottle.tenant_id == tenant_id))
            customer = Customer(phone=phone, name="Zero Cap Test")
            db.add(customer)
            await db.flush()
            with pytest.raises(SendError, match=r"daily cap reached \(0\)"):
                await outbound_policy.enforce_outbound_policy(
                    db, customer=customer, phone=phone, intent="promotional"
                )
            customer_id = customer.id
            await db.rollback()

    async with async_session_factory() as db:
        from app.services import tenant_context
        async with tenant_context.system_context(db):
            await db.execute(delete(OutboundThrottle).where(OutboundThrottle.tenant_id == tenant_id))
            await db.execute(delete(Customer).where(Customer.id == customer_id))
            await db.commit()


@pytest.mark.requires_independent_sessions
async def test_outbound_retry_claim_prevents_duplicate_after_restart(monkeypatch) -> None:
    """A persisted claim for the same queue attempt suppresses a second send."""
    import app.services.whatsapp as whatsapp
    from app.models import OutboundMessage
    from app.services import tenant_context
    from app.services.scheduler import _claim

    tenant_id = await _test_tenant_id()
    async with tenant_context.as_tenant(tenant_id):
        async with async_session_factory() as db:
            row = OutboundMessage(
                to_phone=f"+91993{uuid.uuid4().int % 10**7:07d}",
                payload={"tenant_id": str(tenant_id), "text": "send once", "sent_by": "bot"},
                status="queued",
                next_attempt_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
            db.add(row)
            await db.commit()
            row_id = row.id
        assert await _claim(f"outbound-retry:{row_id}:attempt:0")

    async def unexpected_send(*args, **kwargs):
        raise AssertionError("a previously claimed retry attempt must not send again")

    monkeypatch.setattr(whatsapp, "send_message", unexpected_send)
    try:
        assert await whatsapp.drain_outbound_queue() == 0
        async with async_session_factory() as db:
            row = await db.get(OutboundMessage, row_id)
            assert row is not None and row.status == "dead"
            assert "duplicate retry claim" in (row.last_error or "")
    finally:
        async with async_session_factory() as db:
            from app.services import tenant_context
            async with tenant_context.system_context(db):
                await db.execute(delete(OutboundMessage).where(OutboundMessage.id == row_id))
                await db.execute(
                    delete(__import__("app.models", fromlist=["SentEvent"]).SentEvent).where(
                        __import__("app.models", fromlist=["SentEvent"]).SentEvent.event_key
                        == f"t{tenant_id.hex[:8]}:outbound-retry:{row_id}:attempt:0"
                    )
                )
                await db.commit()


@pytest.mark.requires_independent_sessions
async def test_gbp_daily_post_tick_claims_before_publish_and_runs_once(monkeypatch) -> None:
    """The daily publish side effect is mocked; two scheduler invocations publish once."""
    from datetime import datetime
    from sqlalchemy import delete
    from app.models import SentEvent
    from app.services import tenant_context, gbp_auto_post
    from app.services.scheduler import IST, _gbp_daily_post_tick
    import app.services.scheduler as scheduler

    tenant_id = await _test_tenant_id()
    now_ist = datetime.now(IST).replace(hour=10, minute=0, second=0, microsecond=0)
    claim_key = f"t{tenant_id.hex[:8]}:gbp-daily-post:{now_ist.strftime('%Y-%m-%d')}"

    async with async_session_factory() as db:
        async with tenant_context.system_context(db):
            await db.execute(delete(SentEvent).where(SentEvent.event_key == claim_key))
            await db.commit()

    publishes = []

    async def fake_for_each(job_name, fn):
        async with tenant_context.as_tenant(tenant_id):
            await fn(now_ist)

    async def fake_publish(db, now):
        publishes.append(now.date().isoformat())
        return {"ok": True, "published": True}

    monkeypatch.setattr(scheduler, "_for_each_tenant", fake_for_each)
    monkeypatch.setattr(gbp_auto_post.gbp, "enabled", lambda: True)
    monkeypatch.setattr(gbp_auto_post, "auto_post_daily", fake_publish)

    try:
        await _gbp_daily_post_tick()
        await _gbp_daily_post_tick()
        assert publishes == [now_ist.date().isoformat()]
    finally:
        async with async_session_factory() as db:
            async with tenant_context.system_context(db):
                await db.execute(delete(SentEvent).where(SentEvent.event_key == claim_key))
                await db.commit()


@pytest.mark.requires_independent_sessions
async def test_campaign_recipient_claim_suppresses_duplicate_after_restart(monkeypatch) -> None:
    """Simulate provider acceptance followed by a worker crash before DB status commit."""
    from sqlalchemy import delete, text
    from app.models import Campaign, CampaignRecipient, Customer
    from app.services import tenant_context, app_settings, marketing, plans

    tenant_id = await _test_tenant_id()
    phone = f"+91992{uuid.uuid4().int % 10**7:07d}"
    async with tenant_context.as_tenant(tenant_id):
        async with async_session_factory() as db:
            customer = Customer(phone=phone, name="Campaign Idempotency Test")
            campaign = Campaign(
                name="Campaign idempotency test",
                segment="all_active",
                message_text="Hello {name}",
                status="approved",
                created_by="test",
                stats={},
            )
            db.add_all([customer, campaign])
            await db.flush()
            recipient = CampaignRecipient(campaign_id=campaign.id, customer_id=customer.id)
            db.add(recipient)
            await db.commit()
            campaign_id, customer_id, recipient_id = campaign.id, customer.id, recipient.id

    async def setting(db, key):
        return 100

    async def eligible(db, customer_id):
        return True, ""

    async def bulk_eligible(db, customer_ids, **kwargs):
        return {customer_id: (True, "") for customer_id in customer_ids}

    async def no_month_count(db):
        return 0

    sends = []
    async def fake_send(db, **kwargs):
        sends.append(kwargs["to_phone"])
        raise RuntimeError("simulated crash after provider acceptance")

    monkeypatch.setattr(marketing, "_in_quiet_hours_now", lambda: False)
    monkeypatch.setattr(app_settings, "get", setting)
    monkeypatch.setattr(marketing, "eligible", eligible)
    monkeypatch.setattr(marketing, "month_send_count", no_month_count)
    monkeypatch.setattr(marketing, "send_message", fake_send)
    monkeypatch.setattr("app.services.leads.check_marketing_eligible_bulk", bulk_eligible)
    monkeypatch.setattr(plans, "effective_limits", lambda tenant: {"max_campaign_msgs_month": None})

    claim_key = f"t{tenant_id.hex[:8]}:campaign-send:{campaign_id}:{customer_id}"
    try:
        async with tenant_context.as_tenant(tenant_id):
            with pytest.raises(RuntimeError, match="simulated crash"):
                await marketing.send_campaign(campaign_id)
            await marketing.send_campaign(campaign_id)

        assert sends == [phone]
        async with tenant_context.as_tenant(tenant_id):
            async with async_session_factory() as db:
                recipient = await db.get(CampaignRecipient, recipient_id)
                assert recipient is not None
                assert recipient.status == "skipped"
                assert recipient.detail == "duplicate_send_claim_suppressed"
    finally:
        async with async_session_factory() as db:
            async with tenant_context.system_context(db):
                await db.execute(
                    delete(CampaignRecipient).where(CampaignRecipient.id == recipient_id)
                )
                await db.execute(delete(Campaign).where(Campaign.id == campaign_id))
                await db.execute(delete(Customer).where(Customer.id == customer_id))
                await db.execute(
                    text("DELETE FROM sent_events WHERE event_key = :key"),
                    {"key": claim_key},
                )
                await db.commit()


async def test_scheduled_marketing_jobs_do_not_fall_back_to_home_tenant(monkeypatch) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.services import app_settings, tenant_context, marketing

    async def setting(db, key):
        return "auto" if key == "marketing_autonomy" else 100

    def forbidden_home_fallback():
        raise AssertionError("scheduler marketing must not resolve a home tenant")

    monkeypatch.setattr(app_settings, "get", setting)
    monkeypatch.setattr(tenant_context, "cached_home_tenant_id", forbidden_home_fallback)
    token = tenant_context.current_tenant_id.set(None)
    try:
        now = datetime(2026, 10, 18, 12, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        assert await marketing.run_customer_cycle_nudges(None, now=now) == 0
    finally:
        tenant_context.current_tenant_id.reset(token)


async def test_gbp_generation_requires_explicit_tenant_context() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.services import tenant_context, gbp_auto_post

    token = tenant_context.current_tenant_id.set(None)
    try:
        with pytest.raises(RuntimeError, match="explicit tenant context"):
            await gbp_auto_post._generate_post(
                None, datetime(2026, 10, 18, 12, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
            )
    finally:
        tenant_context.current_tenant_id.reset(token)
