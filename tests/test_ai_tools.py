"""Tests for the customer-safe AI read tool boundary."""

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models import Customer, OrderStatus
from app.services.ai_tools import (
    CUSTOMER_READ_TOOLS,
    get_customer_orders,
    get_customer_profile,
    run_customer_tool,
)


def _customer() -> Customer:
    return Customer(
        phone="+919999900011",
        name="Test Customer",
        address="Test Address",
        is_active=True,
        opted_out=False,
    )


def _order(
    *,
    status: OrderStatus,
    expected_delivery: date | None,
    actual_delivery=None,
    total="500",
    paid="200",
):
    class Row:
        pass

    row = Row()
    row.order_number = "KK-20260921-01"
    row.status = status
    row.expected_delivery = expected_delivery
    row.actual_delivery = actual_delivery
    row.tenant_id = __import__("uuid").uuid4()
    row.id = __import__("uuid").uuid4()
    row.total_amount = Decimal(total) if total is not None else None
    row.amount_paid = Decimal(paid)
    row.created_at = None
    return row


@pytest.mark.asyncio
async def test_customer_profile_returns_only_customer_safe_fields():
    customer = _customer()
    db = AsyncMock()

    result = await get_customer_profile(db, customer)

    assert result == {
        "name": "Test Customer",
        "phone": "+919999900011",
        "address": "Test Address",
        "active": True,
        "opted_out": False,
    }


@pytest.mark.asyncio
async def test_customer_orders_marks_overdue_and_hides_future_date_for_delivered():
    today = date.today()
    delivered = _order(
        status=OrderStatus.DELIVERED,
        expected_delivery=today,
        actual_delivery=None,
    )
    overdue = _order(
        status=OrderStatus.IN_WASH,
        expected_delivery=date.fromordinal(today.toordinal() - 1),
    )

    class ScalarResult:
        def all(self):
            return [delivered, overdue]

    db = AsyncMock()
    db.execute.return_value = MagicMock()
    db.execute.return_value.scalars.return_value = ScalarResult()

    result = await get_customer_orders(db, _customer(), limit=5)

    delivered_result = result[0]
    overdue_result = result[1]

    assert delivered_result["expected_delivery"] is None
    assert delivered_result["actual_delivery"] is None
    assert delivered_result["overdue"] is False
    assert overdue_result["overdue"] is True


@pytest.mark.asyncio
async def test_customer_orders_keeps_delivered_canonical_status():
    delivered = _order(
        status=OrderStatus.DELIVERED,
        expected_delivery=date.today(),
        actual_delivery=None,
    )

    class ScalarResult:
        def all(self):
            return [delivered]

    db = AsyncMock()
    db.execute.return_value = MagicMock()
    db.execute.return_value.scalars.return_value = ScalarResult()

    result = await get_customer_orders(db, _customer())

    assert result[0]["status"] == "DELIVERED"
    assert result[0]["status_label"]


@pytest.mark.asyncio
async def test_unknown_customer_tool_fails_closed():
    with pytest.raises(ValueError, match="unknown customer AI tool"):
        await run_customer_tool(
            AsyncMock(), _customer(), "run_arbitrary_sql"
        )


def test_phase_one_read_registry_is_explicit_and_read_only():
    assert set(CUSTOMER_READ_TOOLS) == {
        "get_customer_profile",
        "get_customer_orders",
        "get_customer_bills",
        "get_shop_rate_card",
    }
    assert all(tool.handler.__name__.startswith(("get_",)) for tool in CUSTOMER_READ_TOOLS.values())


def test_order_status_does_not_trigger_new_customer_onboarding():
    from app.services.ai_agent import _needs_new_customer_onboarding

    customer = _customer()
    customer.name = ""
    customer.address = ""

    assert not _needs_new_customer_onboarding(
        "ORDER_STATUS", [], None, customer
    )


def test_new_order_still_triggers_new_customer_onboarding():
    from app.services.ai_agent import _needs_new_customer_onboarding

    customer = _customer()
    customer.name = ""
    customer.address = ""

    assert _needs_new_customer_onboarding(
        "NEW_ORDER", [], None, customer
    )


def test_phase_two_router_is_deterministic_and_minimal():
    from app.services import ai_agent

    assert ai_agent._select_customer_tools_deterministic(
        "mera order kaha hai?"
    ) == {"get_customer_orders": 5}

    assert ai_agent._select_customer_tools_deterministic(
        "mera bill aur payment kitna due hai?"
    ) == {"get_customer_bills": 5}

    assert ai_agent._select_customer_tools_deterministic(
        "shirt aur blanket ka rate kitna hai?"
    ) == {"get_shop_rate_card": 20}

    assert ai_agent._select_customer_tools_deterministic(
        "mera order status aur bill due kitna hai?"
    ) == {
        "get_customer_orders": 5,
        "get_customer_bills": 5,
    }


@pytest.mark.asyncio
async def test_phase_two_router_uses_no_llm_call(monkeypatch):
    from app.services import ai_agent, llm_client

    async def should_not_call(**kwargs):
        raise AssertionError("tool router must not call Gemini")

    monkeypatch.setattr(llm_client, "ask_json", should_not_call)

    selected = await ai_agent._select_customer_tools(
        AsyncMock(), _customer(), "mera order kaha hai?"
    )
    assert selected == {"get_customer_orders": 5}


def test_smart_model_reserved_for_complex_customer_turns():
    from app.services import ai_agent

    assert not ai_agent._needs_smart_customer_reply("mera order kab milega?")
    assert ai_agent._needs_smart_customer_reply("mera kapda damage hua hai, manager se baat karni hai")
    assert ai_agent._needs_smart_customer_reply("x" * 501)


@pytest.mark.asyncio
async def test_customer_orders_include_bill_url_and_use_recent_window(monkeypatch):
    from app.services import bill_link

    monkeypatch.setattr(bill_link, "url", lambda order: "https://kwikklin.online/b/test.sig")

    recent = _order(
        status=OrderStatus.IN_WASH,
        expected_delivery=date.today(),
    )

    class ScalarResult:
        def all(self):
            return [recent]

    db = AsyncMock()
    db.execute.return_value = MagicMock()
    db.execute.return_value.scalars.return_value = ScalarResult()

    result = await get_customer_orders(db, _customer(), limit=20)

    assert result[0]["bill_url"] == "https://kwikklin.online/b/test.sig"
    stmt = db.execute.call_args.args[0]
    assert "created_at" in str(stmt)
