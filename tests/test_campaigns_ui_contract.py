from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_campaign_customer_picker_has_visible_entry_point():
    html = (ROOT / "app/static/dashboard.html").read_text(encoding="utf-8")
    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")

    assert 'id="camp-custom-audience-launch"' in html
    assert 'onclick="openCampaignCustomerSelection()"' in html
    assert "function openCampaignCustomerSelection()" in js
    assert 'openCampaignCustomerPicker();' in js


def test_campaign_customer_picker_backend_route_is_referenced():
    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    admin = (ROOT / "app/routers/admin.py").read_text(encoding="utf-8")

    assert "/admin/api/customers/search?limit=50" in js
    assert '@router.get("/api/customers/search"' in admin


def test_campaign_create_requires_selected_recipients():
    admin = (ROOT / "app/routers/agent_admin.py").read_text(encoding="utf-8")

    assert 'if body.segment == "selected":' in admin
    assert 'raise HTTPException(status_code=400, detail="select at least one customer")' in admin
    assert 'selected_customer_ids' in admin
