from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_task_kinds_and_duplicate_guard_are_present():
    router = (ROOT / "app/routers/agent_admin.py").read_text()
    service = (ROOT / "app/services/tasks.py").read_text()
    migration = next(ROOT.glob("alembic/versions/u6c91e2b4f70_task_control_room_guards.py")).read_text()

    assert "packing|custom" in router
    assert "TASK_KINDS" in service
    assert "task_duplicate_prevented" in service
    assert "uq_tasks_open_order_kind" in migration
    assert "uq_tasks_open_customer_kind" in migration


def test_manual_reassignment_api_exists():
    router = (ROOT / "app/routers/agent_admin.py").read_text()
    assert '@router.post("/tasks/{code}/assign")' in router
    assert "find_staff" in router
    assert "send_task_to" in router


def test_owner_assign_task_links_order_customer_and_kind():
    tools = (ROOT / "app/services/agent_tools.py").read_text()
    assert 'kind = "pickup"' in tools
    assert 'kind = "wash"' in tools
    assert 'kind = "iron"' in tools
    assert 'kind = "delivery"' in tools
    assert 'kind = "packing"' in tools
    assert 'kind = "custom"' in tools
    assert "customer=customer" in tools
