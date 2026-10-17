"""Unit tests for deterministic customer visit rhythm calculations."""
from datetime import datetime, timezone
from uuid import uuid4

from app.services.customer_rhythm import build_customer_rhythms


def test_customer_rhythm_calculates_gap_usual_items_and_winter_items() -> None:
    customer_id = uuid4()
    orders = [
        {
            "customer_id": customer_id,
            "created_at": datetime(2025, 11, 1, 10, tzinfo=timezone.utc),
            "items": [{"type": "Rajai", "qty": 1}, {"type": "Shirt", "qty": 2}],
        },
        {
            "customer_id": customer_id,
            "created_at": datetime(2025, 11, 1, 17, tzinfo=timezone.utc),
            "items": [{"type": "Shirt", "qty": 1}],
        },
        {
            "customer_id": customer_id,
            "created_at": datetime(2025, 11, 11, 10, tzinfo=timezone.utc),
            "items": [{"type": "Shirt", "qty": 2}],
        },
        {
            "customer_id": customer_id,
            "created_at": datetime(2025, 12, 2, 10, tzinfo=timezone.utc),
            "items": [{"type": "Rajai", "qty": 2}],
        },
    ]

    rhythm = build_customer_rhythms(orders)[customer_id]
    assert rhythm["average_visit_gap_days"] == 15.5
    assert rhythm["last_visit_at"] == datetime(2025, 12, 2, 10, tzinfo=timezone.utc)
    assert rhythm["usual_items"][0] == "Shirt"
    assert "Rajai" in rhythm["seasonal_items"]["winter"]


def test_customer_rhythm_handles_single_visit_and_ignores_billing_adjustments() -> None:
    customer_id = uuid4()
    rhythm = build_customer_rhythms(
        [
            {
                "customer_id": customer_id,
                "created_at": datetime(2026, 1, 4, tzinfo=timezone.utc),
                "items": [
                    {"type": "Blanket", "qty": 1},
                    {"kind": "extra_charge", "label": "Pickup fee", "amount": 50},
                ],
            }
        ]
    )[customer_id]
    assert rhythm["average_visit_gap_days"] is None
    assert rhythm["usual_items"] == ["Blanket"]


def test_customer_rhythm_ignores_rows_without_valid_visit_time() -> None:
    assert build_customer_rhythms([{"customer_id": uuid4(), "created_at": None, "items": []}]) == {}


def test_normal_late_segment_uses_customer_gap_and_excludes_debtors() -> None:
    from datetime import timedelta
    from decimal import Decimal

    from app.services.marketing import _classify

    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    base = {
        "last_order_at": now - timedelta(days=31),
        "first_order_at": now - timedelta(days=180),
        "order_count": 3,
        "outstanding": Decimal("0"),
        "lifetime_paid": Decimal("1000"),
        "average_visit_gap_days": 20,
    }
    assert "normal_late" in _classify(base, now, Decimal("500"))
    assert "lapsed" not in _classify(base, now, Decimal("500"))

    debtor = {**base, "outstanding": Decimal("50")}
    assert "normal_late" not in _classify(debtor, now, Decimal("500"))
    assert "outstanding_dues" in _classify(debtor, now, Decimal("500"))

    not_late = {**base, "last_order_at": now - timedelta(days=29)}
    assert "normal_late" not in _classify(not_late, now, Decimal("500"))
