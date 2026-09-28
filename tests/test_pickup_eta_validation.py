from datetime import date, timedelta

from app.services.bill_agent import _pickup_reference_date


def test_pickup_reference_date_resolves_relative_days():
    today = date(2026, 9, 28)
    assert _pickup_reference_date("kal 12 baje", today) == today + timedelta(days=1)
    assert _pickup_reference_date("parso 1 PM", today) == today + timedelta(days=2)
    assert _pickup_reference_date("aaj sham", today) == today


def test_pickup_reference_date_ignores_time_without_date():
    today = date(2026, 9, 28)
    assert _pickup_reference_date("12 PM ya 1 PM", today) is None
