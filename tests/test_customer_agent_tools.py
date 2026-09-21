"""Tests for the autonomous customer-agent tool boundary."""

from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.models import Customer
from app.services.customer_agent_tools import AGENT_TOOLS, run_agent_tool


def customer() -> Customer:
    return Customer(
        phone="+919999900011",
        name="Test Customer",
        address="Test Address, Varanasi",
        is_active=True,
        opted_out=False,
    )


def test_agent_tools_are_explicit_and_customer_scoped():
    assert {
        "get_customer_profile",
        "get_customer_orders",
        "get_order_details",
        "get_customer_bills",
        "get_shop_rate_card",
        "request_pickup",
        "send_bill",
        "record_customer_issue",
    } == set(AGENT_TOOLS)


@pytest.mark.asyncio
async def test_unknown_agent_tool_fails_closed():
    with pytest.raises(ValueError, match="unknown customer agent tool"):
        await run_agent_tool(AsyncMock(), customer(), "execute_sql", {})


@pytest.mark.asyncio
async def test_pickup_rejects_past_date_without_touching_db():
    db = AsyncMock()
    result = await __import__(
        "app.services.customer_agent_tools",
        fromlist=["request_pickup"],
    ).request_pickup(
        db, customer(), pickup_date="2000-01-01"
    )
    assert result["ok"] is False
    assert result["error"] == "pickup_date_is_in_the_past"
    db.execute.assert_not_awaited()


def test_agent_tool_schema_is_available():
    from app.services import ai_agent

    assert "request_pickup" in ai_agent._AGENT_TOOL_SCHEMA["properties"]["tool_calls"]["items"]["properties"]["name"]["enum"]
    assert "send_bill" in ai_agent._AGENT_TOOL_SCHEMA["properties"]["tool_calls"]["items"]["properties"]["name"]["enum"]
