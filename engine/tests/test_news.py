"""News-blackout evaluation, especially the central-bank window timing: a CB block must
apply only until the event concludes, never linger for the rest of the day."""
from datetime import datetime, timedelta, timezone

from engine import news
from engine import state as state_mod


def _state(windows):
    return {"news_windows": windows,
            "news_windows_date": state_mod.now_dubai().date().isoformat()}


def test_cb_block_active_before_event_ends():
    now = datetime.now(timezone.utc)
    w = [{"ccy": "JPY", "start_iso": (now - timedelta(minutes=10)).isoformat(),
          "end_iso": (now + timedelta(minutes=20)).isoformat(), "event": "BOJ", "kind": "cb"}]
    r = news.evaluate(_state(w), "USDJPY", now)
    assert r["cb_hold"] is True
    assert r["in_window"] is True  # currently inside the window too


def test_cb_block_released_after_event_passed():
    now = datetime.now(timezone.utc)
    # CB event ended 2 hours ago -> must NOT block the pair for the rest of the day.
    w = [{"ccy": "AUD", "start_iso": (now - timedelta(hours=2, minutes=30)).isoformat(),
          "end_iso": (now - timedelta(hours=2)).isoformat(), "event": "RBA", "kind": "cb"}]
    r = news.evaluate(_state(w), "AUDUSD", now)
    assert r["cb_hold"] is False
    assert r["in_window"] is False


def test_unrelated_currency_not_blocked():
    now = datetime.now(timezone.utc)
    w = [{"ccy": "JPY", "start_iso": (now - timedelta(minutes=5)).isoformat(),
          "end_iso": (now + timedelta(minutes=20)).isoformat(), "event": "BOJ", "kind": "cb"}]
    r = news.evaluate(_state(w), "EURUSD", now)  # no JPY
    assert r["cb_hold"] is False and r["in_window"] is False


def test_stale_windows_not_fresh():
    w = [{"ccy": "USD", "start_iso": "2020-01-01T00:00:00Z",
          "end_iso": "2020-01-01T00:30:00Z", "event": "CPI", "kind": "high"}]
    st = {"news_windows": w, "news_windows_date": "2020-01-01"}  # old date
    r = news.evaluate(st, "EURUSD", datetime.now(timezone.utc))
    assert r["fresh"] is False
