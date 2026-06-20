"""Weekend-flat timing for the FTMO Standard account (no weekend holds)."""
from datetime import datetime
from zoneinfo import ZoneInfo

from engine import trade_manager as tm

TZ = ZoneInfo("Asia/Dubai")
# June 2026: 18th = Thursday, 19th = Friday, 20th = Saturday, 21st = Sunday.


def _d(day, hour):
    return datetime(2026, 6, day, hour, 0, tzinfo=TZ)


def test_friday_after_cutoff_is_flat_time():
    assert tm.is_weekend_flat_time(_d(19, 23)) is True


def test_friday_before_cutoff_is_not():
    assert tm.is_weekend_flat_time(_d(19, 22)) is False


def test_all_saturday_is_flat_time():
    assert tm.is_weekend_flat_time(_d(20, 9)) is True


def test_sunday_is_not_flat_time():
    assert tm.is_weekend_flat_time(_d(21, 12)) is False


def test_thursday_night_is_not():
    assert tm.is_weekend_flat_time(_d(18, 23)) is False
