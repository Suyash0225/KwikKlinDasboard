"""Durability + security hardening tests.

Covers the crash-safety layer: webhook journal (persist-before-process,
dedup, replay), the outbound retry queue, the admin auth throttle, and
secret redaction in settings.
"""

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.routers.webhook as webhook_module
import app.services.whatsapp as whatsapp_module
from app.config import settings
from app.database import async_session_factory
from app.models import Conversation, Customer, OutboundMessage, WebhookEvent
from app.routers.orders import _FAILED_AUTH
from app.services.whatsapp import SendError, send_message
from tests.conftest import (
    TEST_CUSTOMER_PHONE,
    TEST_CUSTOMER_PHONE_RAW,
    meta_payload,
    sign_body,
)


# --- webhook journal ---

async def test_webhook_journals_before_processing(client, sent) -> None:
    body = meta_payload(
        messages=[{
            "from": TEST_CUSTOMER_PHONE_RAW,
            "id": "wamid.TEST-journal1",
            "type": "text",
            "text": {"body": "namaste"},
        }]
    )
    r = await client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)}
    )
    assert r.status_code == 200
    async with async_session_factory() as s:
        ev = (
            await s.execute(
                select(WebhookEvent).where(
                    WebhookEvent.event_key == hashlib.sha256(body).hexdigest()
                )
            )
        ).scalars().first()
    assert ev is not None, "raw payload must be journaled"
    assert ev.status == "processed"
    assert ev.source == "meta"


async def test_webhook_exact_redelivery_deduped_at_journal(client, sent) -> None:
    body = meta_payload(
        messages=[{
            "from": TEST_CUSTOMER_PHONE_RAW,
            "id": "wamid.TEST-journal-dup",
            "type": "text",
            "text": {"body": "dedup check"},
        }]
    )
    headers = {"X-Hub-Signature-256": sign_body(body)}
    r1 = await client.post("/webhook", content=body, headers=headers)
    r2 = await client.post("/webhook", content=body, headers=headers)
    assert r1.json()["status"] == "received"
    assert r2.json()["status"] == "duplicate"
    assert len(sent) == 1, "redelivery must not double-process"


async def test_failed_event_is_replayed(client, sent, monkeypatch) -> None:
    """Crash during processing -> journal row 'failed' -> retry job recovers it."""
    real_process = webhook_module._process_payload

    async def _boom(payload, db):
        raise RuntimeError("simulated crash mid-processing")

    monkeypatch.setattr(webhook_module, "_process_payload", _boom)
    body = meta_payload(
        messages=[{
            "from": TEST_CUSTOMER_PHONE_RAW,
            "id": "wamid.TEST-replay1",
            "type": "text",
            "text": {"body": "replay me"},
        }]
    )
    r = await client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)}
    )
    assert r.status_code == 200, "Meta must still get 200 — journal owns the retry"
    # restore ONLY the crash patch — the fake send_message must stay patched
    monkeypatch.setattr(webhook_module, "_process_payload", real_process)

    async with async_session_factory() as s:
        ev = (
            await s.execute(
                select(WebhookEvent).where(
                    WebhookEvent.event_key == hashlib.sha256(body).hexdigest()
                )
            )
        ).scalars().first()
        assert ev is not None and ev.status == "failed"
        assert "simulated crash" in (ev.error or "")
        # age it past the 5-minute stuck cutoff
        ev.received_at = datetime.now(timezone.utc) - timedelta(minutes=6)
        s.add(ev)
        await s.commit()

    retried = await webhook_module.retry_stuck_webhook_events()
    assert retried >= 1

    async with async_session_factory() as s:
        conv = (
            await s.execute(
                select(Conversation).where(Conversation.wa_message_id == "wamid.TEST-replay1")
            )
        ).scalars().first()
        assert conv is not None, "replay must complete the lost processing"
        assert conv.message_text == "replay me"


# --- outbound retry queue ---

@pytest.fixture
async def _window_open_customer():
    async with async_session_factory() as s:
        s.add(
            Customer(
                phone=TEST_CUSTOMER_PHONE,
                name="Test",
                last_message_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()
    yield


async def test_transient_send_failure_lands_in_queue(_window_open_customer, monkeypatch) -> None:
    async def _network_down(payload, to_phone):
        raise SendError(f"send to {to_phone} failed: network error", transient=True)

    monkeypatch.setattr(whatsapp_module, "_post_with_retry", _network_down)
    async with async_session_factory() as db:
        with pytest.raises(SendError):
            await send_message(db, to_phone=TEST_CUSTOMER_PHONE, text="kal ready hoga")

    async with async_session_factory() as s:
        row = (
            await s.execute(
                select(OutboundMessage).where(OutboundMessage.to_phone == TEST_CUSTOMER_PHONE)
            )
        ).scalar_one()
    assert row.status == "queued"
    assert row.payload["text"] == "kal ready hoga"


async def test_drain_sends_queued_message(_window_open_customer, monkeypatch) -> None:
    async with async_session_factory() as s:
        s.add(
            OutboundMessage(
                to_phone=TEST_CUSTOMER_PHONE,
                payload={"text": "queued msg", "sent_by": "bot"},
            )
        )
        await s.commit()

    async def _post_ok(payload, to_phone):
        return {"messages": [{"id": "wamid.TESTQ-drained"}]}

    monkeypatch.setattr(whatsapp_module, "_post_with_retry", _post_ok)
    sent_count = await whatsapp_module.drain_outbound_queue()
    assert sent_count == 1

    async with async_session_factory() as s:
        row = (
            await s.execute(
                select(OutboundMessage).where(OutboundMessage.to_phone == TEST_CUSTOMER_PHONE)
            )
        ).scalar_one()
        assert row.status == "sent"
        conv = (
            await s.execute(
                select(Conversation).where(Conversation.wa_message_id == "wamid.TESTQ-drained")
            )
        ).scalars().first()
        assert conv is not None, "drained send must be recorded in conversations"


async def test_permanent_send_failure_not_queued(_window_open_customer, monkeypatch) -> None:
    async def _rejected(payload, to_phone):
        raise SendError("send failed: 400 code=131047 window closed", transient=False)

    monkeypatch.setattr(whatsapp_module, "_post_with_retry", _rejected)
    async with async_session_factory() as db:
        with pytest.raises(SendError):
            await send_message(db, to_phone=TEST_CUSTOMER_PHONE, text="reject me")

    async with async_session_factory() as s:
        rows = (
            await s.execute(
                select(OutboundMessage).where(OutboundMessage.to_phone == TEST_CUSTOMER_PHONE)
            )
        ).scalars().all()
    assert rows == [], "permanent rejections must not retry forever"


# --- admin auth hardening ---

async def test_auth_throttle_after_repeated_bad_keys(client) -> None:
    _FAILED_AUTH.clear()  # other tests' deliberate bad keys must not count
    try:
        for _ in range(10):
            r = await client.get("/orders", headers={"X-API-Key": "wrong-key"})
            assert r.status_code == 401
        r = await client.get("/orders", headers={"X-API-Key": "wrong-key"})
        assert r.status_code == 429, "11th bad attempt must be throttled"
        # even the right key is refused while throttled — brute force gains nothing
        r = await client.get("/orders", headers={"X-API-Key": settings.ADMIN_API_KEY})
        assert r.status_code == 429
    finally:
        _FAILED_AUTH.clear()  # never leak throttle state into other tests


async def test_valid_key_still_works(client) -> None:
    r = await client.get("/orders", headers={"X-API-Key": settings.ADMIN_API_KEY})
    assert r.status_code == 200


# --- payment ledger behavior (owner's rule: advance/extra is fine) ---

async def test_payment_flow_and_overpay_allowed(client, sent) -> None:
    from tests.conftest import purge_phones

    try:
        r = await client.post(
            "/orders", headers={"X-API-Key": settings.ADMIN_API_KEY},
            json={
                "customer_phone": TEST_CUSTOMER_PHONE, "customer_name": "QA Pay",
                "items": [{"type": "shirt", "qty": 1, "service": "wash"}],
                "total_amount": 100,
            },
        )
        assert r.status_code in (200, 201)
        num = r.json()["order_number"]
        H = {"X-API-Key": settings.ADMIN_API_KEY}

        r = await client.post(f"/orders/{num}/payment", headers=H, json={"amount": 60, "method": "CASH"})
        assert r.status_code == 200 and r.json()["payment_status"] == "PARTIAL"

        # customer pays extra / advance — accepted, order goes PAID and the
        # full amount stays in the ledger
        r = await client.post(f"/orders/{num}/payment", headers=H, json={"amount": 60, "method": "CASH"})
        assert r.status_code == 200
        assert r.json()["payment_status"] == "PAID"
        assert float(r.json()["amount_paid"]) == 120.0

        # zero/negative still rejected
        r = await client.post(f"/orders/{num}/payment", headers=H, json={"amount": 0, "method": "CASH"})
        assert 400 <= r.status_code < 500
    finally:
        await purge_phones(TEST_CUSTOMER_PHONE)


# --- secret redaction ---

async def test_settings_secret_redacted(client) -> None:
    from app.services import app_settings

    async with async_session_factory() as db:
        await app_settings.set_value(db, "ig_access_token", "IGQVJreal-secret-token")
    try:
        r = await client.get(
            "/admin/api/settings", headers={"X-API-Key": settings.ADMIN_API_KEY}
        )
        assert r.status_code == 200
        assert r.json()["ig_access_token"] == "••••••••"

        # writing the mask back must NOT clobber the real token
        r = await client.put(
            "/admin/api/settings",
            headers={"X-API-Key": settings.ADMIN_API_KEY},
            json={"key": "ig_access_token", "value": "••••••••"},
        )
        assert r.status_code == 200
        async with async_session_factory() as db:
            assert await app_settings.get(db, "ig_access_token") == "IGQVJreal-secret-token"
    finally:
        async with async_session_factory() as db:
            await app_settings.set_value(db, "ig_access_token", "")
