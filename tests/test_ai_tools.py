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


@pytest.mark.asyncio
async def test_phase_two_router_only_accepts_registered_read_tools(monkeypatch):
    from app.services import ai_agent, llm_client

    async def fake_ask_json(**kwargs):
        assert kwargs["schema"] is ai_agent._TOOL_CALL_SCHEMA
        return {
            "tool_calls": [
                {"name": "get_customer_orders", "limit": 5},
                {"name": "run_arbitrary_sql", "limit": 5},
            ]
        }

    monkeypatch.setattr(llm_client, "ask_json", fake_ask_json)
    selected = await ai_agent._select_customer_tools(
        AsyncMock(), _customer(), "mera order kaha hai?"
    )

    assert selected == {"get_customer_orders": 5}


@pytest.mark.asyncio
async def test_phase_two_router_falls_back_to_all_read_tools_on_llm_failure(monkeypatch):
    from app.services import ai_agent, llm_client
    from app.services.llm_client import LLMError

    async def failing_ask_json(**kwargs):
        raise LLMError("router unavailable")

    monkeypatch.setattr(llm_client, "ask_json", failing_ask_json)
    selected = await ai_agent._select_customer_tools(
        AsyncMock(), _customer(), "mera bill?"
    )

    assert selected == {name: 5 for name in ai_agent.CUSTOMER_READ_TOOLS}


@pytest.mark.asyncio
async def test_phase_two_router_clamps_tool_limits(monkeypatch):
    from app.services import ai_agent, llm_client

    async def fake_ask_json(**kwargs):
        return {
            "tool_calls": [
                {"name": "get_customer_orders", "limit": 999},
                {"name": "get_customer_bills", "limit": 0},
            ]
        }

    monkeypatch.setattr(llm_client, "ask_json", fake_ask_json)
    selected = await ai_agent._select_customer_tools(
        AsyncMock(), _customer(), "orders and bill"
    )

    assert selected == {
        "get_customer_orders": 10,
        "get_customer_bills": 1,
    }
