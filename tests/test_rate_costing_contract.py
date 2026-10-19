from pathlib import Path


def test_rate_card_exposes_optional_internal_unit_cost():
    root = Path(__file__).resolve().parents[1]
    admin = (root / "app/routers/admin.py").read_text()
    js = (root / "app/static/app.js").read_text()
    agent = (root / "app/routers/agent_admin.py").read_text()
    assert '"cost_rate": str(r.cost_rate) if r.cost_rate is not None else None' in admin
    assert "Internal cost per unit" in js
    assert "rmCellCost" in js
    assert "effective_discount_cap" in agent