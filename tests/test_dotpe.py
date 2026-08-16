"""DotPe provider tests: webhook receiver + provider switch in the send door.

DotPe's real HTTP API is never called — adapter internals are patched. The
live HTTP path gets verified once the owner's API key arrives.
"""

import json

import pytest
from sqlalchemy import select, text as sqltext

import app.services.whatsapp as whatsapp_module
from app.config import settings
from app.database import async_session_factory
from app.models import Conversation, Customer
from app.services import dotpe
from app.services.whatsapp import AI_SIGNATURE, Button, SendError, send_message

PHONE = "+919999900055"
PHONE_RAW = "919999900055"


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    async with async_session_factory() as s:
        await s.execute(
            sqltext(
                "DELETE FROM conversations WHERE customer_id IN "
                f"(SELECT id FROM customers WHERE phone = '{PHONE}')"
            )
        )
        await s.execute(sqltext(f"DELETE FROM customers WHERE phone = '{PHONE}'"))
        # fixed-timestamp payloads hash identically across runs — stale
        # journal rows would dedup away the next run's webhook POST
        await s.execute(
            sqltext(f"DELETE FROM webhook_events WHERE payload::text LIKE '%{PHONE_RAW}%'")
        )
        await s.commit()


def dotpe_inbound(body_text: str, ts: int = 1700000001) -> bytes:
    return json.dumps(
        {
            "timestamp": ts,
            "metadata": {"wabaId": "x", "wabaDisplayPhone": settings.DOTPE_WABA_NUMBER},
            "message": {
                "from": PHONE_RAW,
                "profileName": "Test",
                "body": body_text,
                "button": {},
                "type": "text",
            },
        }
    ).encode()


AUTH = {"Dotpe-Webhook-Token": settings.DOTPE_WEBHOOK_TOKEN}


# --- webhook auth ---

async def test_dotpe_webhook_requires_token(client, sent) -> None:
    body = dotpe_inbound("hello")
    assert (await client.post("/webhook/dotpe", content=body)).status_code == 403
    assert (
        await client.post(
            "/webhook/dotpe", content=body, headers={"Dotpe-Webhook-Token": "galat"}
        )
    ).status_code == 403
    assert sent == []


# --- inbound handling ---

async def test_dotpe_inbound_stored_and_deduped(client, sent) -> None:
    body = dotpe_inbound("mera order kahan hai", ts=1700000123)
    r1 = await client.post("/webhook/dotpe", content=body, headers=AUTH)
    r2 = await client.post("/webhook/dotpe", content=body, headers=AUTH)  # retry
    assert r1.status_code == 200 and r2.status_code == 200

    async with async_session_factory() as s:
        cust = (
            await s.execute(select(Customer).where(Customer.phone == PHONE))
        ).scalar_one()
        assert cust.last_message_at is not None
        rows = (
            await s.execute(
                select(Conversation).where(Conversation.customer_id == cust.id)
            )
        ).scalars().all()
        inbound = [x for x in rows if x.wa_message_id and x.wa_message_id.startswith("dotpe:")]
        assert len(inbound) == 1, "same DotPe event delivered twice must store once"
    # customer got exactly one reply (retry must not double-reply)
    assert len(sent) == 1


async def test_dotpe_button_reply_preserves_payload(client, sent) -> None:
    body = json.dumps(
        {
            "timestamp": 1700000456,
            "metadata": {"wabaId": "x", "wabaDisplayPhone": settings.DOTPE_WABA_NUMBER},
            "message": {
                "from": PHONE_RAW,
                "profileName": "Test",
                "body": "",
                "button": {"text": "Done", "payload": "order:abc:done"},
                "type": "button",
            },
        }
    ).encode()
    r = await client.post("/webhook/dotpe", content=body, headers=AUTH)
    assert r.status_code == 200
    async with async_session_factory() as s:
        cust = (
            await s.execute(select(Customer).where(Customer.phone == PHONE))
        ).scalar_one()
        conv = (
            await s.execute(
                select(Conversation).where(Conversation.customer_id == cust.id)
            )
        ).scalars().all()
        assert any(c.message_text.startswith("[button:order:abc:done]") for c in conv)


# --- provider switch in the send door ---

async def test_send_door_routes_to_dotpe(monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_text(to_phone: str, body: str) -> str:
        calls.append({"to": to_phone, "body": body})
        return "dotpe:kk-test"

    monkeypatch.setattr(settings, "WHATSAPP_PROVIDER", "dotpe")
    monkeypatch.setattr(dotpe, "send_text", fake_text)

    from datetime import datetime, timezone

    async with async_session_factory() as db:
        db.add(Customer(phone=PHONE, last_message_at=datetime.now(timezone.utc)))
        await db.commit()
        wa_id = await send_message(db, to_phone=PHONE, text="namaste")

    assert wa_id == "dotpe:kk-test"
    # customer ko jaane wale har automated message par AI sign lagta hai —
    # provider badalne se wo niyam nahi badalta
    assert len(calls) == 1 and calls[0]["to"] == PHONE
    assert calls[0]["body"].startswith("namaste")
    assert AI_SIGNATURE in calls[0]["body"]
    async with async_session_factory() as s:
        conv = (
            await s.execute(
                select(Conversation).where(Conversation.wa_message_id == "dotpe:kk-test")
            )
        ).scalar_one()
        assert conv.message_text.startswith("namaste")


async def test_dotpe_provider_rejects_buttons(monkeypatch) -> None:
    monkeypatch.setattr(settings, "WHATSAPP_PROVIDER", "dotpe")
    from datetime import datetime, timezone

    async with async_session_factory() as db:
        db.add(Customer(phone=PHONE, last_message_at=datetime.now(timezone.utc)))
        await db.commit()
        with pytest.raises(SendError, match="buttons"):
            await send_message(
                db, to_phone=PHONE, text="choose",
                buttons=[Button("a", "A")],
            )
