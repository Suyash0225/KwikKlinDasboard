"""Bill-by-text agent tests — LLM mocked; DB, pricing and confirm loop real."""

from datetime import date, timedelta

import pytest
from sqlalchemy import select, text as sqltext

import app.services.bill_agent as bill_module
from app.database import async_session_factory
from app.models import Customer, Order, OrderStatus, Rate
from app.services.bill_agent import _PENDING, handle_staff_message
from app.services.llm_client import LLMUnavailable
from app.services.order_service import create_order

SENDER = "+911111100001"          # pretend staff/manager phone
CUST_PHONE = "+919999900124"      # bill target customer
SERVICE = "TestServiceX"          # our own rate rows -> deterministic pricing


def _extract_result(**overrides) -> dict:
    base = {
        "action": "other",
        "customer_name": "",
        "customer_phone": "",
        "items": [],
        "advance": 0,
        "expected_delivery": "",
        "order_number": "",
        "new_date": "",
        "reason": "",
        "new_status": "",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
async def _setup_and_cleanup():
    _PENDING.clear()
    async with async_session_factory() as s:
        s.add(Rate(service=SERVICE, garment="Kurta", unit="pc", rate=40))
        await s.commit()
    yield
    _PENDING.clear()
    async with async_session_factory() as s:
        sub = f"(SELECT id FROM customers WHERE phone = '{CUST_PHONE}')"
        await s.execute(
            sqltext(
                "DELETE FROM order_status_history WHERE order_id IN "
                f"(SELECT id FROM orders WHERE customer_id IN {sub})"
            )
        )
        for table in ("conversations", "orders"):
            await s.execute(sqltext(f"DELETE FROM {table} WHERE customer_id IN {sub}"))
        await s.execute(sqltext(f"DELETE FROM customers WHERE phone = '{CUST_PHONE}'"))
        await s.execute(sqltext(f"DELETE FROM rate_card WHERE service = '{SERVICE}'"))
        await s.commit()


def _patch_extract(monkeypatch, result: dict):
    async def fake_ask_json(**kw):
        return result

    monkeypatch.setattr(bill_module.llm_client, "ask_json", fake_ask_json)


async def _order_for_customer() -> Order | None:
    async with async_session_factory() as s:
        return (
            await s.execute(
                select(Order)
                .join(Customer, Customer.id == Order.customer_id)
                .where(Customer.phone == CUST_PHONE)
            )
        ).scalar_one_or_none()


async def test_bill_draft_then_confirm_creates_order(monkeypatch, sent) -> None:
    _patch_extract(
        monkeypatch,
        _extract_result(
            action="new_bill",
            customer_name="Sharma ji",
            customer_phone="9999900124",
            items=[{"service": SERVICE, "garment": "Kurta", "qty": 2}],
            advance=20,
        ),
    )
    async with async_session_factory() as db:
        draft_reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager", text="Sharma ji 2 kurta"
        )
    assert draft_reply is not None
    assert "₹80" in draft_reply and "Advance: ₹20" in draft_reply
    assert "Sharma ji" in draft_reply and SENDER in _PENDING
    assert await _order_for_customer() is None  # nothing written yet!

    async with async_session_factory() as db:
        done = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager", text="haan"
        )
    assert done is not None and "ban gaya" in done
    order = await _order_for_customer()
    assert order is not None
    assert float(order.total_amount) == 80.0
    assert float(order.amount_paid) == 20.0
    assert SENDER not in _PENDING
    # customer got the order_confirmed notification via the patched sender
    assert any("mil gaya" in (c["text"] or "") for c in sent)


async def test_cancel_discards_draft(monkeypatch) -> None:
    _patch_extract(
        monkeypatch,
        _extract_result(
            action="new_bill",
            customer_phone="9999900124",
            items=[{"service": SERVICE, "garment": "Kurta", "qty": 1}],
        ),
    )
    async with async_session_factory() as db:
        await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager", text="1 kurta"
        )
        reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager", text="nahi"
        )
    assert "cancel" in reply.lower()
    assert SENDER not in _PENDING
    assert await _order_for_customer() is None


async def test_confirm_without_phone_keeps_draft(monkeypatch) -> None:
    _patch_extract(
        monkeypatch,
        _extract_result(
            action="new_bill",
            customer_name="Verma",
            items=[{"service": SERVICE, "garment": "Kurta", "qty": 1}],
        ),
    )
    async with async_session_factory() as db:
        draft_reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager", text="Verma 1 kurta"
        )
        assert "number nahi mila" in draft_reply
        blocked = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager", text="haan"
        )
    assert "number nahi mila" in blocked
    assert SENDER in _PENDING  # draft survives until a phone arrives


async def test_unknown_item_not_priced(monkeypatch) -> None:
    _patch_extract(
        monkeypatch,
        _extract_result(
            action="new_bill",
            customer_phone="9999900124",
            items=[{"service": "Alien Service", "garment": "Spacesuit", "qty": 1}],
        ),
    )
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager", text="1 spacesuit"
        )
    assert "rate card mein nahi" in reply
    assert "Total: ₹0" in reply


async def test_delay_update_writes_db_first_and_hides_reason(monkeypatch, sent) -> None:
    async with async_session_factory() as db:
        order = await create_order(
            db,
            customer_phone=CUST_PHONE,
            items=[{"type": "Kurta", "qty": 1}],
            created_by="test",
        )
        number = order.order_number
    new_date = (date.today() + timedelta(days=3)).isoformat()
    _patch_extract(
        monkeypatch,
        _extract_result(
            action="delay_update",
            order_number=number,
            new_date=new_date,
            reason="paani nahi aaya",
        ),
    )
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="Ravi", text=f"{number} kal nahi hoga"
        )
    assert reply.startswith("✅")
    async with async_session_factory() as s:
        fresh = (
            await s.execute(select(Order).where(Order.order_number == number))
        ).scalar_one()
        assert fresh.expected_delivery == date.fromisoformat(new_date)
        assert "paani nahi aaya" in (fresh.notes or "")
    # customer messages must NEVER contain the internal reason
    for call in sent:
        assert "paani" not in (call["text"] or "")


async def test_status_update_respects_state_machine(monkeypatch, sent) -> None:
    async with async_session_factory() as db:
        order = await create_order(
            db,
            customer_phone=CUST_PHONE,
            items=[{"type": "Kurta", "qty": 1}],
            created_by="test",
        )
        number = order.order_number
    _patch_extract(
        monkeypatch,
        _extract_result(action="status_update", order_number=number, new_status="READY"),
    )
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="Ravi", text=f"{number} ready hai"
        )
    assert "READY" in reply
    async with async_session_factory() as s:
        fresh = (
            await s.execute(select(Order).where(Order.order_number == number))
        ).scalar_one()
        assert fresh.status is OrderStatus.READY

    # backward move -> polite refusal, no change
    _patch_extract(
        monkeypatch,
        _extract_result(action="status_update", order_number=number, new_status="IN_WASH"),
    )
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="Ravi", text=f"{number} dhulai me"
        )
    assert "allowed nahi" in reply


async def test_cancel_via_whatsapp_refused(monkeypatch) -> None:
    _patch_extract(
        monkeypatch,
        _extract_result(
            action="status_update", order_number="KK-20260101-01", new_status="CANCELLED"
        ),
    )
    async with async_session_factory() as db:
        reply = await handle_staff_message(
            db, sender_phone=SENDER, sender_label="manager", text="cancel kar do"
        )
    assert "dashboard" in reply


async def test_other_quiet_for_staff_loud_for_manager(monkeypatch) -> None:
    _patch_extract(monkeypatch, _extract_result(action="other"))
    async with async_session_factory() as db:
        assert (
            await handle_staff_message(
                db, sender_phone=SENDER, sender_label="Ravi", text="thik hai bhaiya"
            )
            is None
        )
        assert (
            await handle_staff_message(
                db, sender_phone=SENDER, sender_label="manager", text="thik hai"
            )
            is not None
        )


async def test_llm_down_stays_silent(monkeypatch) -> None:
    async def fake_ask_json(**kw):
        raise LLMUnavailable("down")

    monkeypatch.setattr(bill_module.llm_client, "ask_json", fake_ask_json)
    async with async_session_factory() as db:
        assert (
            await handle_staff_message(
                db, sender_phone=SENDER, sender_label="manager", text="Sharma 2 kurta"
            )
            is None
        )
