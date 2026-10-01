from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_task_control_room_is_database_linked():
    router = (ROOT / "app/routers/agent_admin.py").read_text()
    model = (ROOT / "app/models/task.py").read_text()
    service = (ROOT / "app/services/tasks.py").read_text()
    js = (ROOT / "app/static/app.js").read_text()

    assert '"/tasks/customers"' in router
    assert 'class TaskCustomerIn' in router
    assert "customer_id" in router
    assert 'kind: str = Field' in router
    assert "due_at: datetime | None" in router
    assert "customer_id:" in model
    assert "due_at:" in model
    assert "customer=customer" in router
    assert "due_at=body.due_at" in router
    assert "customer_id=(customer.id" in service
    assert "task.due_at" in service
    assert "searchTaskCustomers" in js
    assert "newTaskCustomerModal" in js
    assert "type=\"datetime-local\"" in js
    assert "taskDueISO" in js


def test_task_due_migration_links_customer_and_due_at():
    migration = next(
        (ROOT / "alembic/versions").glob("*task_due_at.py")
    ).read_text()
    assert 'add_column("tasks", sa.Column("customer_id"' in migration
    assert 'create_foreign_key("fk_tasks_customer_id"' in migration
    assert 'add_column("tasks", sa.Column("due_at"' in migration


def test_selected_customer_loads_running_orders():
    router = (ROOT / "app/routers/agent_admin.py").read_text()
    js = (ROOT / "app/static/app.js").read_text()
    assert '"/tasks/customers/{customer_id}/orders"' in router
    assert "_O.customer_id == customer.id" in router
    assert '"/admin/api/tasks/customers/" + encodeURIComponent(customerId) + "/orders"' in js
    assert "nt-order-select" in js
    assert "loadTaskCustomerOrders" in js
