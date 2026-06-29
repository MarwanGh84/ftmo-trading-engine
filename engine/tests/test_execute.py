"""Tests for engine/execute.py — build_context() shape and pure helpers."""
from unittest.mock import MagicMock, patch

import pytest

from engine import execute, news as news_mod, config


# ── Fake client ───────────────────────────────────────────────────────────────

def _eurusd_client():
    c = MagicMock()
    c.get_balance.return_value = {
        "balance": "10000.00", "equity": "10100.00",
        "connectionState": "Authenticated",
    }
    c.get_symbol_details.return_value = {
        "bid": "1.08500", "ask": "1.08510",
        "pipSize": 0.0001, "lotSize": 100000.0,
        "minVolume": 1000, "volumeStep": 1000,
    }
    return c


# ── _quote_to_usd ─────────────────────────────────────────────────────────────

def test_quote_to_usd_returns_one_for_usd():
    c = MagicMock()
    assert execute._quote_to_usd(c, "USD", {}) == 1.0
    c.get_symbol_details.assert_not_called()


def test_quote_to_usd_direct_pair():
    c = MagicMock()
    c.get_symbol_details.return_value = {"bid": "1.27000", "ask": "1.27010"}
    result = execute._quote_to_usd(c, "GBP", {})
    assert result == pytest.approx((1.27000 + 1.27010) / 2, rel=1e-6)


def test_quote_to_usd_inverted_pair():
    c = MagicMock()
    # First attempt (JPYUSD) fails; second attempt (USDJPY) succeeds → inverted
    c.get_symbol_details.side_effect = [
        Exception("no JPYUSD"),
        {"bid": "150.00", "ask": "150.02"},
    ]
    result = execute._quote_to_usd(c, "JPY", {})
    assert result == pytest.approx(1.0 / 150.01, rel=1e-4)


def test_quote_to_usd_uses_cache():
    c = MagicMock()
    c.get_symbol_details.return_value = {"bid": "0.65000", "ask": "0.65010"}
    cache: dict = {}
    r1 = execute._quote_to_usd(c, "AUD", cache)
    r2 = execute._quote_to_usd(c, "AUD", cache)
    assert r1 == r2
    assert c.get_symbol_details.call_count == 1   # cache hit on second call


# ── _open_risk_pct ────────────────────────────────────────────────────────────

def test_open_risk_pct_empty_positions():
    c = MagicMock()
    pct = execute._open_risk_pct(c, [], 10000.0, {}, {})
    assert pct == 0.0


def test_open_risk_pct_skips_positions_without_sl():
    c = MagicMock()
    pos = [{"symbol": "EURUSD", "volume_units": 10000, "sl_pips": None}]
    pct = execute._open_risk_pct(c, pos, 10000.0, {}, {})
    assert pct == 0.0


# ── build_context() shape ─────────────────────────────────────────────────────

def test_build_context_required_keys():
    c = _eurusd_client()
    state = {"open_positions": [], "pending_orders": [], "news_windows": []}
    proposal = {
        "symbol": "EURUSD", "side": "buy",
        "stop": 1.0820, "target": 1.0900, "risk_pct": 0.5,
    }
    with patch.object(news_mod, "evaluate", return_value={"blocked": False, "windows": []}):
        ctx = execute.build_context(c, proposal, state)

    top_keys = ("proposal", "sizing", "market", "state", "news", "ftmo", "balance", "equity")
    for k in top_keys:
        assert k in ctx, f"build_context() missing top-level key: {k!r}"

    sizing_keys = ("units", "lots", "stop_pips", "rr", "worst_case",
                   "pip_value_lot_usd", "open_risk_pct", "pending_risk_pct", "buffers")
    for k in sizing_keys:
        assert k in ctx["sizing"], f"build_context()['sizing'] missing key: {k!r}"

    market_keys = ("reachable", "authenticated", "spread", "bid", "ask", "lot_size", "pip_size")
    for k in market_keys:
        assert k in ctx["market"], f"build_context()['market'] missing key: {k!r}"

    ftmo_keys = ("overall_floor", "equity_after_worst", "daily_loss_after", "daily_limit")
    for k in ftmo_keys:
        assert k in ctx["ftmo"], f"build_context()['ftmo'] missing key: {k!r}"


def test_build_context_stale_quote_raises():
    c = MagicMock()
    c.get_balance.return_value = {"balance": "10000.00", "equity": "10000.00",
                                  "connectionState": "Authenticated"}
    c.get_symbol_details.return_value = {
        "bid": None, "ask": None,
        "pipSize": 0.0001, "lotSize": 100000.0,
        "minVolume": 1000, "volumeStep": 1000,
    }
    state = {"open_positions": [], "pending_orders": [], "news_windows": []}
    proposal = {"symbol": "EURUSD", "side": "buy", "stop": 1.08, "target": 1.09, "risk_pct": 0.5}

    with pytest.raises(RuntimeError, match="no live bid/ask"):
        execute.build_context(c, proposal, state)
