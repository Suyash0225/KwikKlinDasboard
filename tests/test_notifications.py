"""Notification policy + degradation tests, and webhook rule-based replies.

Real WhatsApp is never called — the shared `sent` fixture records sends.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text as sqltext

import app.services.order_service as order_service_module
from app.database import async_session_factory
from app.models import OrderStatus
from app.services.messages import get_message, status_label
from app.services.order_service import (
    create_order,
    set_expected_delivery,
    update_status,
)
from app.services.whatsapp import SendError, WindowClosedError
from tests.conftest import meta_payload, sign_body

PHONE = "+919999900033"
PHONE_RAW = "919999900033"
ITEMS = [{"type": "kurta", "qty": 2, "service": "wash_iron"}]

S = OrderStatus


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    from tests.conftest import purge_phones

    await purge_phones(PHONE)


# --- notification policy ---

async def test_create_order_notifies_confirmation(sent) -> None:
    async with async_session_factory() as db:
        order = await create_order(db, customer_phone=PHONE, items=ITEMS, created_by="test")
    assert len(sent) == 1
    assert sent[0]["to"] == PHONE
    assert order.order_number in sent[0]["text"]


async def test_milestones_notify_but_wash_stages_silent(sent) -> None:
    async with async_session_factory() as db:
        order = await create_order(db, customer_phone=PHONE, items=ITEMS, created_by="test")
        sent.clear()  # drop the confirmation

        await update_status(db, order, S.IN_WASH, changed_by="test")
        await update_status(db, order, S.IN_DRY, changed_by="test")
        await update_status(db, order, S.IN_IRON, changed_by="test")
        assert sent == [], "wash/dry/iron must be silent"

        await update_status(db, order, S.READY, changed_by="test")
        assert len(sent) == 1 and "taiyar" in sent[0]["text"]

        await update_status(db, order, S.OUT_FOR_DELIVERY, changed_by="test")
        await update_status(db, order, S.DELIVERED, changed_by="test")
        assert len(sent) == 3
        assert "Dhanyawad" in sent[-1]["text"]


async def test_date_revision_notifies_without_internal_reason(sent) -> None:
    async with async_session_factory() as db:
        order = await create_order(db, customer_phone=PHONE, items=ITEMS, created_by="test")
        sent.clear()
        new_date = date.today() + timedelta(days=2)
        await set_expected_delivery(
            db, order, new_date, changed_by="staff:ravi", internal_reason="machine kharab"
        )
    assert len(sent) == 1
    assert "machine kharab" not in sent[0]["text"], "INTERNAL reason leaked to customer!"
    assert new_date.strftime("%d %b %Y") in sent[0]["text"]


async def test_notification_failure_never_breaks_order_update(monkeypatch) -> None:
    async def exploding_send(db, **kwargs):
        raise SendError("meta is down")

    monkeypatch.setattr(order_service_module, "send_message", exploding_send)
    async with async_session_factory() as db:
        order = await create_order(db, customer_phone=PHONE, items=ITEMS, created_by="test")
        await update_status(db, order, S.READY, changed_by="test")
    assert order.status is S.READY  # business change survived the send failure


async def test_window_closed_falls_back_to_template(monkeypatch) -> None:
    calls: list[dict] = []

    async def window_closed_then_record(db, *, to_phone, text=None, template_name=None, **kw):
        if text is not None:
            raise WindowClosedError("closed")
        calls.append({"template": template_name, **kw})
        return "wamid.FAKE"

    monkeypatch.setattr(order_service_module, "send_message", window_closed_then_record)
    async with async_session_factory() as db:
        order = await create_order(db, customer_phone=PHONE, items=ITEMS, created_by="test")
    assert len(calls) == 1
    assert calls[0]["template"] == "kk_order_confirmed"
    assert calls[0]["template_params"][0] == order.order_number


# --- webhook rule-based replies ---

async def _post_text(client, body_text: str, wamid: str):
    body = meta_payload(
        messages=[{"from": PHONE_RAW, "id": wamid, "type": "text", "text": {"body": body_text}}]
    )
    return await client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign_body(body)}
    )


async def test_single_active_order_gets_status_reply(client, sent) -> None:
    async with async_session_factory() as db:
        order = await create_order(db, customer_phone=PHONE, items=ITEMS, created_by="test")
        await update_status(db, order, S.IN_WASH, changed_by="test")
    sent.clear()

    r = await _post_text(client, "mera order kahan hai", "wamid.TESTN-1")
    assert r.status_code == 200
    assert len(sent) == 1
    assert order.order_number in sent[0]["text"]
    assert status_label(S.IN_WASH) in sent[0]["text"]


async def test_order_number_in_message_wins(client, sent) -> None:
    async with async_session_factory() as db:
        o1 = await create_order(db, customer_phone=PHONE, items=ITEMS, created_by="test")
        o2 = await create_order(db, customer_phone=PHONE, items=ITEMS, created_by="test")
        await update_status(db, o2, S.READY, changed_by="test")
    sent.clear()

    r = await _post_text(client, f"bhai {o2.order_number.lower()} ka kya hua", "wamid.TESTN-2")
    assert r.status_code == 200
    assert len(sent) == 1
    assert o2.order_number in sent[0]["text"]
    assert o1.order_number not in sent[0]["text"]


async def test_multiple_orders_get_list(client, sent) -> None:
    async with async_session_factory() as db:
        o1 = await create_order(db, customer_phone=PHONE, items=ITEMS, created_by="test")
        o2 = await create_order(db, customer_phone=PHONE, items=ITEMS, created_by="test")
    sent.clear()

    r = await _post_text(client, "status batao", "wamid.TESTN-3")
    assert r.status_code == 200
    assert len(sent) == 1
    assert o1.order_number in sent[0]["text"] and o2.order_number in sent[0]["text"]


async def test_foreign_order_number_not_leaked(client, sent) -> None:
    """Another customer's order number must never reveal their status."""
    async with async_session_factory() as db:
        other = await create_order(
            db, customer_phone="+919999900044", items=ITEMS, created_by="test"
        )
    sent.clear()
    try:
        r = await _post_text(client, f"{other.order_number} kahan hai", "wamid.TESTN-4")
        assert r.status_code == 200
        assert len(sent) == 1
        assert sent[0]["text"] == get_message("order_not_found")
    finally:
        from tests.conftest import purge_phones

        await purge_phones("+919999900044")


async def test_no_orders_falls_back_to_ack(client, sent) -> None:
    r = await _post_text(client, "hello ji", "wamid.TESTN-5")
    assert r.status_code == 200
    assert len(sent) == 1
    assert sent[0]["text"] == get_message("ack_received")
