"""Unit tests for the hard rails. Each test starts from a fully-passing context and
flips ONE thing, asserting the matching rail (and only it) fails. This is the proof
that violations are deterministically refused."""
import copy
import pytest

from engine import rails, risk, config


def good_ctx():
    b = risk.buffers(balance=10000, equity=10000, day_start_balance=10000,
                     account_baseline=10000, daily_loss_limit_pct=2.0,
                     overall_loss_limit_pct=10.0, candidate_worst_case=50.0)
    return {
        "proposal": {"symbol": "EURUSD", "side": "buy", "order_type": "market",
                     "entry": 1.1600, "stop": 1.1570, "target": 1.1660, "risk_pct": 0.5},
        "sizing": {"units": 16000, "lots": 0.16, "stop_pips": 30.0, "rr": 2.0,
                   "worst_case": 50.0, "pip_value_lot_usd": 10.0, "open_risk_pct": 0.0,
                   "buffers": b},
        "market": {"reachable": True, "authenticated": True, "spread": 1.0,
                   "typical_spread": 1.0, "quote_age_sec": 1, "min_volume": 1000,
                   "pip_size": 0.0001},
        "state": {"daily_limit_hit": False, "trades_taken_today": 0,
                  "poor_outcomes_today": 0, "open_positions": [], "pending_orders": [],
                  "frozen": False, "executed_signals": []},
        "equity": 10000.0,
        "news": {"fresh": True, "in_window": False, "cb_hold": False, "event": None},
        "ftmo": {"overall_floor": 9000.0, "equity_after_worst": 10385.0,
                 "daily_loss_after": 50.0, "daily_limit": 500.0},
    }


def test_good_ctx_all_pass():
    results = rails.gate(good_ctx())
    assert rails.passed(results), [r.__dict__ for r in rails.failures(results)]


def _assert_only_fails(ctx, rail_name):
    results = rails.gate(ctx)
    failed = {r.name for r in rails.failures(results)}
    assert rail_name in failed, f"expected {rail_name} to fail; failures={failed}"


def test_frozen_blocks():
    c = good_ctx(); c["state"]["frozen"] = True; c["state"]["frozen_reason"] = "cTrader down"
    _assert_only_fails(c, "not_frozen")


def test_duplicate_pending_blocks():
    c = good_ctx()  # proposal entry 1.1600; an existing buy pending at 1.1602 is within 10 pips
    c["state"]["pending_orders"] = [{"symbol": "EURUSD", "side": "buy", "target_price": 1.1602}]
    _assert_only_fails(c, "no_duplicate_order")


def test_duplicate_signal_id_blocks():
    c = good_ctx(); c["proposal"]["signal_id"] = "abc"; c["state"]["executed_signals"] = ["abc"]
    _assert_only_fails(c, "no_duplicate_order")


def test_target_reached_blocks():
    c = good_ctx(); c["equity"] = 11000.0   # +$1,000 = phase-1 target on a $10k account
    _assert_only_fails(c, "target_reached")


def test_unreachable_blocks():
    c = good_ctx(); c["market"]["authenticated"] = False
    _assert_only_fails(c, "ctrader_reachable")


def test_kill_switch_flag_blocks():
    c = good_ctx(); c["state"]["daily_limit_hit"] = True
    _assert_only_fails(c, "kill_switch")


def test_kill_switch_room_exhausted_blocks():
    c = good_ctx()
    c["sizing"]["buffers"] = risk.buffers(10000, 9800, 10000, 10000, 2.0, 10.0, 50.0)
    _assert_only_fails(c, "kill_switch")


def test_risk_too_high_blocks():
    c = good_ctx(); c["proposal"]["risk_pct"] = 1.0
    _assert_only_fails(c, "per_trade_risk")


def test_risk_too_low_blocks():
    c = good_ctx(); c["proposal"]["risk_pct"] = 0.1
    _assert_only_fails(c, "per_trade_risk")


def test_no_stop_blocks():
    c = good_ctx(); c["proposal"]["stop"] = None; c["sizing"]["stop_pips"] = 0
    _assert_only_fails(c, "stop_loss_required")


def test_below_min_size_blocks():
    c = good_ctx(); c["sizing"]["units"] = 0
    _assert_only_fails(c, "min_broker_size")


def test_rr_too_low_blocks():
    c = good_ctx(); c["sizing"]["rr"] = 1.0
    _assert_only_fails(c, "reward_risk")


def test_max_trades_blocks():
    c = good_ctx(); c["state"]["trades_taken_today"] = 5   # cap is 5 (fills)
    _assert_only_fails(c, "max_trades_per_day")


def test_max_trades_ok_below_cap():
    c = good_ctx(); c["state"]["trades_taken_today"] = 4
    assert all(r.ok for r in rails.gate(c) if r.name == "max_trades_per_day")


def test_aggregate_includes_pending():
    # 0.4% open + 0.4% pending + 0.5% new = 1.3% > 1% -> blocked by pending-aware aggregate
    c = good_ctx()
    c["sizing"]["open_risk_pct"] = 0.4
    c["sizing"]["pending_risk_pct"] = 0.4
    _assert_only_fails(c, "aggregate_risk")


def test_poor_outcomes_blocks():
    c = good_ctx(); c["state"]["poor_outcomes_today"] = 2
    _assert_only_fails(c, "poor_outcomes")


def test_reduce_after_loss_blocks_full_risk():
    c = good_ctx(); c["state"]["poor_outcomes_today"] = 1  # one loss -> must cut risk
    _assert_only_fails(c, "reduce_after_loss")  # risk still 0.5%


def test_reduce_after_loss_allows_floor_risk():
    c = good_ctx(); c["state"]["poor_outcomes_today"] = 1; c["proposal"]["risk_pct"] = 0.25
    results = rails.gate(c)
    assert all(r.ok for r in results if r.name == "reduce_after_loss")


def test_daily_buffer_breach_blocks():
    c = good_ctx()
    c["sizing"]["buffers"] = risk.buffers(10000, 9810, 10000, 10000, 2.0, 10.0, 50.0)
    # equity 9810: room now 10 (kill-switch ok), after = -40 -> daily buffer fails
    _assert_only_fails(c, "daily_buffer_after")


def test_overall_buffer_breach_blocks():
    c = good_ctx()
    # daily floor (9000*0.98=8820) leaves room, but overall floor (10000*0.9=9000)
    # sits right at equity, so the worst case breaches overall but not daily.
    c["sizing"]["buffers"] = risk.buffers(10000, 9000, 9000, 10000, 2.0, 10.0, 50.0)
    _assert_only_fails(c, "overall_buffer_after")


def test_aggregate_risk_blocks():
    c = good_ctx(); c["sizing"]["open_risk_pct"] = 0.8  # +0.5 candidate = 1.3% > 1%
    _assert_only_fails(c, "aggregate_risk")


def test_direct_hedge_blocks():
    c = good_ctx()
    c["state"]["open_positions"] = [{"symbol": "EURUSD", "side": "sell"}]
    _assert_only_fails(c, "no_correlated_opposing")


def test_correlated_opposing_blocks():
    # open long EURUSD (+EUR); new short EURGBP (-EUR) -> opposing on EUR
    c = good_ctx()
    c["proposal"]["symbol"] = "EURGBP"; c["proposal"]["side"] = "sell"
    c["state"]["open_positions"] = [{"symbol": "EURUSD", "side": "buy"}]
    _assert_only_fails(c, "no_correlated_opposing")


def test_correlated_same_direction_ok():
    # long EURUSD (+EUR) + long EURGBP (+EUR) -> same direction, allowed
    c = good_ctx()
    c["proposal"]["symbol"] = "EURGBP"; c["proposal"]["side"] = "buy"
    c["state"]["open_positions"] = [{"symbol": "EURUSD", "side": "buy"}]
    results = rails.gate(c)
    assert all(r.ok for r in results if r.name == "no_correlated_opposing")


def test_currency_concentration_blocks():
    # already long EURUSD (+USD? no: buy EURUSD = -USD). Use two existing long-USD positions.
    c = good_ctx()
    c["proposal"]["symbol"] = "USDJPY"; c["proposal"]["side"] = "buy"   # +USD
    c["state"]["open_positions"] = [{"symbol": "USDCAD", "side": "buy"},   # +USD
                                    {"symbol": "USDCHF", "side": "buy"}]   # +USD
    _assert_only_fails(c, "currency_concentration")


def test_currency_concentration_ok_at_limit():
    c = good_ctx()
    c["proposal"]["symbol"] = "USDJPY"; c["proposal"]["side"] = "buy"
    c["state"]["open_positions"] = [{"symbol": "USDCAD", "side": "buy"}]   # only 1 existing +USD
    results = rails.gate(c)
    assert all(r.ok for r in results if r.name == "currency_concentration")


def test_news_window_blocks():
    c = good_ctx(); c["news"]["in_window"] = True; c["news"]["event"] = "US CPI"
    _assert_only_fails(c, "news_blackout")


def test_stale_news_fail_safe_blocks():
    c = good_ctx(); c["news"]["fresh"] = False
    _assert_only_fails(c, "news_blackout")


def test_cb_hold_blocks():
    c = good_ctx(); c["news"]["cb_hold"] = True; c["news"]["event"] = "FOMC"
    _assert_only_fails(c, "news_blackout")


def test_spread_spike_blocks():
    c = good_ctx(); c["market"]["spread"] = 5.0; c["market"]["typical_spread"] = 1.0
    _assert_only_fails(c, "spread_quote")


def test_stale_quote_blocks():
    c = good_ctx(); c["market"]["quote_age_sec"] = 120
    _assert_only_fails(c, "spread_quote")


def test_ftmo_overall_floor_backstop_blocks():
    c = good_ctx(); c["ftmo"]["equity_after_worst"] = 8999.0  # below $9,000 floor
    _assert_only_fails(c, "ftmo_hard_floor")


def test_ftmo_daily_loss_backstop_blocks():
    c = good_ctx(); c["ftmo"]["daily_loss_after"] = 501.0  # over $500 daily limit
    _assert_only_fails(c, "ftmo_hard_floor")
