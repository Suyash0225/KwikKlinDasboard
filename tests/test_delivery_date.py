from datetime import date

from app.services.delivery_date import calculate


def test_normal_delivery_counts_working_days():
    # Thursday 24 Sep + 4 working days = Tuesday 29 Sep.
    assert calculate(date(2026, 9, 24)) == date(2026, 9, 29)


def test_sunday_is_skipped():
    # Friday + 4 working days: Mon, Tue, Wed, Thu.
    assert calculate(date(2026, 9, 25)) == date(2026, 10, 1)


def test_heavy_item_uses_five_working_days():
    assert calculate(
        date(2026, 9, 24),
        [{"type": "Curtain (Large)", "qty": 1}],
    ) == date(2026, 9, 30)


def test_configured_holiday_is_skipped():
    assert calculate(
        date(2026, 9, 24),
        holidays=["2026-09-28"],
    ) == date(2026, 9, 30)
