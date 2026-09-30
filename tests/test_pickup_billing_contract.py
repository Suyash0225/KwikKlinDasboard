from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_pickup_billing_reuses_existing_staff_bill_ui():
    js = (ROOT / "app/static/staff.js").read_text(encoding="utf-8")
    html = (ROOT / "app/static/staff.html").read_text(encoding="utf-8")
    assert "openPickupBill" in js
    assert '/bill-context' in js
    assert "pickup_task_code" in js
    assert 'function showNewBill()' in js
    assert 'id="b-save"' in html


def test_pickup_billing_backend_is_linked_to_existing_bill_endpoint():
    source = (ROOT / "app/routers/staff_panel.py").read_text(encoding="utf-8")
    assert '@router.get("/tasks/{code}/bill-context"' in source
    assert 'pickup_task_code: str' in source
    assert 'if body.pickup_task_code.strip():' in source
    assert 'OrderStatus.PICKED_UP' in source
    assert '"pickup_completed": pickup_task is not None' in source
