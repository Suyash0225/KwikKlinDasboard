"""Regression tests for per-staff AI work-order routing."""

from types import SimpleNamespace
import uuid

import pytest

from app.models import Staff
from app.services.work_orders import resolve_workers


class _FakeDB:
    def __init__(self, staff):
        self.staff = staff

    async def get(self, model, key):
        assert model is Staff
        return self.staff if key == self.staff.id else None


@pytest.mark.parametrize(
    ("role", "assignment_field", "enabled"),
    [
        ("WASHER", "assigned_washer_id", False),
        ("WASHER", "assigned_washer_id", True),
        ("DELIVERY", "assigned_delivery_id", False),
        ("DELIVERY", "assigned_delivery_id", True),
    ],
)
async def test_assigned_staff_ai_toggle_controls_work_order_routing(
    role, assignment_field, enabled
):
    """Assigned active staff receive automated work only when their AI toggle is ON."""
    staff = SimpleNamespace(
        id=uuid.uuid4(),
        is_active=True,
        ai_agent_enabled=enabled,
        phone="+919999900091",
    )
    order = SimpleNamespace(assigned_washer_id=None, assigned_delivery_id=None)
    setattr(order, assignment_field, staff.id)

    resolved = await resolve_workers(_FakeDB(staff), order, role)

    assert resolved == ([staff] if enabled else [])
