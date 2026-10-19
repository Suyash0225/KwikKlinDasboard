from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_pickup_billing_reuses_existing_staff_bill_ui():
    js = (ROOT / "app/static/staff.js").read_text(encoding="utf-8")
    html = (ROOT / "app/static/staff.html").read_text(encoding="utf-8")
    assert "openPickupBill" in js
    assert '/bill-context' in js
    assert "pickup_task_code" in js
    assert 'function showNewBill()' in js
    # The existing bill UI is rendered from staff.js, not static staff.html.
    assert 'id="b-save"' in js


def test_pickup_billing_backend_is_linked_to_existing_bill_endpoint():
    source = (ROOT / "app/routers/staff_panel.py").read_text(encoding="utf-8")
    assert '@router.get("/tasks/{code}/bill-context"' in source
    assert 'pickup_task_code: str' in source
    assert 'if body.pickup_task_code.strip():' in source
    assert 'OrderStatus.PICKED_UP' in source
    assert '"pickup_completed": pickup_task is not None' in source



def test_campaign_editor_wires_ai_draft_without_auto_sending():
    html = (ROOT / "app/static/dashboard.html").read_text(encoding="utf-8")
    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    assert 'id="camp-ai-goal"' in html
    assert 'onclick="generateCampaignDraft(this)"' in html
    assert 'id="camp-ai-gmb-preview"' in html
    assert 'api("/admin/api/campaigns/ai-draft"' in js
    assert "Nothing has been sent" in js
    assert 'api("/admin/api/campaigns/" + draft.id + "/approve"' not in js
