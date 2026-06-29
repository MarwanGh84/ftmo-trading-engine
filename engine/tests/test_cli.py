"""Tests for pure helper functions in engine/cli.py."""
from datetime import datetime, timezone, timedelta

import pytest

from engine.cli import _news_summary, _weekend_flat_summary
from engine import config


def _utc(offset_min: int = 0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=offset_min)


# ── _news_summary ──────────────────────────────────────────────────────────────

def test_news_summary_no_windows():
    state = {"news_windows": []}
    assert _news_summary(state, _utc()) == "none remaining"


def test_news_summary_missing_key():
    assert _news_summary({}, _utc()) == "none remaining"


def test_news_summary_future_window_included():
    end = _utc(60)
    state = {"news_windows": [
        {"ccy": "USD", "event": "CPI", "start_iso": _utc(30).isoformat(),
         "end_iso": end.isoformat(), "kind": "high"},
    ]}
    result = _news_summary(state, _utc())
    assert "USD" in result
    assert "CPI" in result


def test_news_summary_past_window_excluded():
    past_end = _utc(-10)
    state = {"news_windows": [
        {"ccy": "USD", "event": "NFP", "start_iso": _utc(-60).isoformat(),
         "end_iso": past_end.isoformat(), "kind": "high"},
    ]}
    assert _news_summary(state, _utc()) == "none remaining"


def test_news_summary_caps_at_three():
    end = _utc(120)
    windows = [
        {"ccy": c, "event": f"E{i}", "start_iso": _utc(i * 10).isoformat(),
         "end_iso": end.isoformat(), "kind": "high"}
        for i, c in enumerate(["USD", "GBP", "EUR", "AUD"])
    ]
    result = _news_summary({"news_windows": windows}, _utc())
    assert result.count("·") <= 2   # at most 3 items → at most 2 separators


# ── _weekend_flat_summary ──────────────────────────────────────────────────────

def _dubai_datetime(weekday: int, hour: int) -> datetime:
    # Build a concrete date for the given weekday/hour (timezone-naive; the function only uses .weekday() and .hour)
    # Monday=0 … Saturday=5 Sunday=6
    # 2026-06-22 is a Monday (weekday=0)
    monday = datetime(2026, 6, 22, hour, 0, 0)
    return monday + timedelta(days=weekday)


def test_weekend_flat_summary_weekday():
    now = _dubai_datetime(0, 10)   # Monday 10:00
    assert _weekend_flat_summary(now) == "Fri 23:00"


def test_weekend_flat_summary_friday_before_flat_hour():
    now = _dubai_datetime(4, config.WEEKEND_FLAT_HOUR_DUBAI - 1)
    assert _weekend_flat_summary(now) == "tonight 23:00"


def test_weekend_flat_summary_friday_at_flat_hour():
    now = _dubai_datetime(4, config.WEEKEND_FLAT_HOUR_DUBAI)
    assert _weekend_flat_summary(now) == "flattening now"


def test_weekend_flat_summary_saturday():
    now = _dubai_datetime(5, 12)   # Saturday noon
    assert _weekend_flat_summary(now) == "weekend — flat"


def test_weekend_flat_summary_sunday():
    now = _dubai_datetime(6, 8)    # Sunday morning
    assert _weekend_flat_summary(now) == "weekend — flat"
