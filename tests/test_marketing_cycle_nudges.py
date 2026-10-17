"""Pure rules for cycle-timed customer pickup reminders."""
from datetime import datetime, timezone

from app.services.marketing import _repeat_cycle_window


def test_repeat_cycle_window_uses_last_three_deliveries_and_two_day_grace() -> None:
    deliveries = [
        datetime(2026, 9, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 11, tzinfo=timezone.utc),
        datetime(2026, 9, 21, tzinfo=timezone.utc),
    ]
    assert _repeat_cycle_window(deliveries, datetime(2026, 10, 1, tzinfo=timezone.utc)) == (10, datetime(2026, 9, 21).date())
    assert _repeat_cycle_window(deliveries, datetime(2026, 10, 4, tzinfo=timezone.utc)) is None


def test_repeat_cycle_window_ignores_short_or_insufficient_history() -> None:
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert _repeat_cycle_window([datetime(2026, 9, 21, tzinfo=timezone.utc), datetime(2026, 9, 30, tzinfo=timezone.utc)], now) is None
    assert _repeat_cycle_window([
        datetime(2026, 9, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 4, tzinfo=timezone.utc),
        datetime(2026, 9, 7, tzinfo=timezone.utc),
    ], now) is None
