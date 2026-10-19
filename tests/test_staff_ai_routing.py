"""Regression tests for work-order recipient resolution."""

from types import SimpleNamespace
import uuid

import pytest

from app.services import ops_agent
from app.services.work_orders import resolve_workers


class _FakeDB:
    pass


@pytest.mark.parametrize("role", ["WASHER", "DELIVERY"])
@pytest.mark.parametrize("selected", [None, "staff"])
async def test_resolve_workers_returns_only_the_central_picker_recipient(
    monkeypatch, role, selected
):
    """The resolver must delegate eligibility and fallback decisions centrally."""
    staff = SimpleNamespace(id=uuid.uuid4(), phone="+919999900091")
    order = SimpleNamespace(
        assigned_washer_id=staff.id,
        assigned_delivery_id=staff.id,
    )
    calls = []

    async def fake_pick_staff(db, requested_role, requested_order):
        calls.append((db, requested_role, requested_order))
        return staff if selected == "staff" else None

    monkeypatch.setattr(ops_agent, "pick_staff", fake_pick_staff)
    db = _FakeDB()

    resolved = await resolve_workers(db, order, role)

    assert resolved == ([staff] if selected == "staff" else [])
    assert calls == [(db, role, order)]
