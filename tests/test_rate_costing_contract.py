from pathlib import Path


def test_rate_card_keeps_cost_server_side_but_owner_does_not_type_it():
    root = Path(__file__).resolve().parents[1]
    admin = (root / "app/routers/admin.py").read_text()
    js = (root / "app/static/app.js").read_text()
    agent = (root / "app/routers/agent_admin.py").read_text()
    assert '"cost_rate": str(r.cost_rate) if r.cost_rate is not None else None' in admin
    # Owner ne kaha: cost haath se bharne ka box nahi chahiye
    assert "Internal cost per unit" not in js
    assert "rmCellCost" not in js
    assert "effective_discount_cap" in agent
