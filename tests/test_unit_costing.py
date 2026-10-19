from app.services.unit_costing import effective_discount_cap


def test_cost_guard_caps_discount_at_smallest_configured_gross_contribution():
    cap, guarded = effective_discount_cap(30, [
        {"rate": "100", "cost_rate": "65"},
        {"rate": "200", "cost_rate": "150"},
    ])
    assert guarded is True
    assert cap == 25.0


def test_cost_guard_never_exceeds_configured_discount_limit():
    cap, guarded = effective_discount_cap(10, [{"rate": "100", "cost_rate": "20"}])
    assert guarded is True
    assert cap == 10.0


def test_missing_cost_is_unknown_and_disables_margin_guard():
    cap, guarded = effective_discount_cap(15, [
        {"rate": "100", "cost_rate": "40"},
        {"rate": "200", "cost_rate": None},
    ])
    assert guarded is False
    assert cap == 15.0


def test_cost_equal_to_or_above_price_allows_no_percent_discount():
    cap, guarded = effective_discount_cap(25, [
        {"rate": "100", "cost_rate": "100"},
        {"rate": "200", "cost_rate": "220"},
    ])
    assert guarded is True
    assert cap == 0.0


def test_no_rate_card_does_not_claim_margin_guard():
    cap, guarded = effective_discount_cap(12, [])
    assert guarded is False
    assert cap == 12.0