"""Pure tests for deterministic cost metrics and missing-data disclosure."""
from decimal import Decimal
from types import SimpleNamespace

from app.models import OrderStatus
from app.services.cost_calculator import summarize_costs


def test_cost_metrics_use_delivered_orders_and_decimal_math():
    order = SimpleNamespace(
        status=OrderStatus.DELIVERED,
        items=[
            {"type": "shirt", "garment": "Shirt", "service": "wash_iron", "qty": 4},
            {"kind": "extra_charge", "type": "Pickup", "qty": 100, "amount": 50},
        ],
    )
    rate = SimpleNamespace(service="wash_iron", garment="Shirt", unit="pc")
    result = summarize_costs([Decimal("120"), Decimal("30")], [order], [rate], "test")
    assert result["expense_total"] == Decimal("150")
    assert result["pieces"] == Decimal("4")
    assert result["cost_per_piece_proxy"] == Decimal("37.50")
    assert result["kilograms"] == Decimal("0")
    assert result["completed_orders"] == 1
    assert "Shared-expense proxy" in result["cost_proxy_note"]


def test_cost_metrics_do_not_count_cancelled_or_unknown_units():
    cancelled = SimpleNamespace(
        status=OrderStatus.CANCELLED,
        items=[{"type": "shirt", "qty": 8}],
    )
    delivered = SimpleNamespace(
        status=OrderStatus.DELIVERED,
        items=[{"type": "mystery", "qty": 2}],
    )
    result = summarize_costs([], [cancelled, delivered], [], "test")
    assert result["completed_orders"] == 1
    assert result["pieces"] == Decimal("0")
    assert result["cost_per_piece_proxy"] is None
    assert result["unclassified_lines"] == 1
    assert result["has_expenses"] is False
    assert "not zero" in result["cost_proxy_note"]


def test_missing_expenses_do_not_become_zero_cost():
    order = SimpleNamespace(
        status=OrderStatus.DELIVERED,
        items=[{"type": "shirt", "garment": "Shirt", "service": "wash_iron", "qty": 2}],
    )
    rate = SimpleNamespace(service="wash_iron", garment="Shirt", unit="pc")
    result = summarize_costs([], [order], [rate], "test")
    assert result["pieces"] == Decimal("2")
    assert result["cost_per_piece_proxy"] is None
    assert result["has_expenses"] is False


def test_mixed_units_suppress_misleading_unit_cost_proxies():
    order = SimpleNamespace(
        status=OrderStatus.DELIVERED,
        items=[
            {"type": "shirt", "service": "wash_iron", "garment": "Shirt", "qty": 2},
            {"type": "wash fold", "service": "wash_fold", "garment": "", "qty": 3},
        ],
    )
    rates = [
        SimpleNamespace(service="wash_iron", garment="Shirt", unit="pc"),
        SimpleNamespace(service="wash_fold", garment="", unit="kg"),
    ]
    result = summarize_costs([Decimal("100")], [order], rates, "test")
    assert result["pieces"] == Decimal("2")
    assert result["kilograms"] == Decimal("3")
    assert result["mixed_units"] is True
    assert result["cost_per_piece_proxy"] is None
    assert result["cost_per_kg_proxy"] is None
    assert "suppressed" in result["cost_proxy_note"]


def test_single_unit_proxy_is_available_with_expenses_and_classified_volume():
    order = SimpleNamespace(
        status=OrderStatus.DELIVERED,
        items=[{"type": "shirt", "service": "wash_iron", "garment": "Shirt", "qty": 2}],
    )
    rate = SimpleNamespace(service="wash_iron", garment="Shirt", unit="pc")
    result = summarize_costs([Decimal("100")], [order], [rate], "test")
    assert result["cost_per_piece_proxy"] == Decimal("50.00")
    assert result["cost_per_kg_proxy"] is None
    assert result["mixed_units"] is False
