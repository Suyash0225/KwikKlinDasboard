"""Template Studio: create/submit, validation, list+dynamic registry, delete."""

import pytest

import app.routers.agent_admin as aa
from app.config import settings

AUTH = {"X-API-Key": settings.ADMIN_API_KEY}


@pytest.fixture
def graph(monkeypatch):
    calls: list[dict] = []

    async def fake_graph(method, path, **kw):
        calls.append({"method": method, "path": path, **kw})
        if method == "GET":
            return 200, {"data": [
                {"name": "kk_dyn_ready", "status": "APPROVED", "category": "UTILITY",
                 "language": "en_US",
                 "components": [{"type": "BODY", "text": "Order {{1}} ready hai"}]},
                {"name": "kk_rejected_one", "status": "REJECTED", "category": "MARKETING",
                 "language": "en_US", "rejected_reason": "INVALID_FORMAT",
                 "components": [{"type": "BODY", "text": "Offer!"}]},
            ]}
        if method == "POST":
            return 200, {"status": "PENDING", "id": "123"}
        return 200, {"success": True}

    monkeypatch.setattr(aa, "_graph", fake_graph)
    monkeypatch.setattr(settings, "WHATSAPP_WABA_ID", "999000")
    return calls


async def test_list_registers_approved_for_sending(client, graph) -> None:
    r = await client.get("/admin/api/templates", headers=AUTH)
    assert r.status_code == 200
    rows = r.json()
    assert rows[0]["name"] == "kk_dyn_ready" and rows[0]["status"] == "APPROVED"
    assert rows[1]["rejected_reason"] == "INVALID_FORMAT"
    # approved one is now sendable through the registry
    from app.services.templates import build_template

    payload = build_template("kk_dyn_ready", ["KK-20260803-01"])
    assert payload["name"] == "kk_dyn_ready"


async def test_create_validates_and_submits(client, graph) -> None:
    # variable without sample -> 400
    r = await client.post("/admin/api/templates", headers=AUTH, json={
        "name": "My Offer!", "category": "MARKETING",
        "body": "Namaste {{1}}, 10% off!", "samples": [],
    })
    assert r.status_code == 400 and "sample" in r.json()["detail"].lower()

    # good one -> slugified name + submitted with buttons
    r = await client.post("/admin/api/templates", headers=AUTH, json={
        "name": "My Offer!", "category": "MARKETING",
        "body": "Namaste {{1}}, 10% off!", "samples": ["Sharma ji"],
        "footer": "Kwik Klin",
        "buttons": [{"type": "URL", "text": "Order karein", "url": "https://wa.me/919696856069"}],
    })
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "my_offer_"
    submitted = [c for c in graph if c["method"] == "POST"][0]
    comps = submitted["json"]["components"]
    assert any(c["type"] == "BUTTONS" for c in comps)
    assert any(c["type"] == "FOOTER" for c in comps)

    # gap in variables -> 400
    r = await client.post("/admin/api/templates", headers=AUTH, json={
        "name": "bad_vars", "category": "UTILITY",
        "body": "Hello {{2}}", "samples": ["x"],
    })
    assert r.status_code == 400


async def test_delete_template(client, graph) -> None:
    r = await client.delete("/admin/api/templates/kk_old_one", headers=AUTH)
    assert r.status_code == 200 and r.json()["deleted"] == "kk_old_one"
