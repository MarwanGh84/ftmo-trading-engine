"""Unit tests for position-sizing and buffer math (pure functions)."""
import math
import pytest

from engine import risk


def test_stop_pips_majors():
    assert risk.stop_pips(1.1600, 1.1570, 0.0001) == pytest.approx(30.0)


def test_stop_pips_jpy():
    assert risk.stop_pips(160.20, 159.90, 0.01) == pytest.approx(30.0)


def test_risk_dollars():
    assert risk.risk_dollars(10000, 0.5) == pytest.approx(50.0)


def test_pip_value_usd_quote():
    # EURUSD: $10 per pip per lot
    assert risk.pip_value_per_lot_usd(0.0001, 100000, 1.0) == pytest.approx(10.0)


def test_pip_value_jpy_quote():
    # USDJPY at 160.19 -> quote_to_usd = 1/160.19 -> ~$6.24/pip/lot
    pv = risk.pip_value_per_lot_usd(0.01, 100000, 1 / 160.19)
    assert pv == pytest.approx(6.24, abs=0.02)


def test_lots_for_risk():
    # $50 risk, 30 pip stop, $10/pip/lot -> 0.1667 lots
    lots = risk.lots_for_risk(50.0, 30.0, 10.0)
    assert lots == pytest.approx(50 / 300)


def test_units_snap_down_to_grid():
    # 0.1667 lots * 100000 = 16666.7 units -> snap down to 16000 on step 1000
    units = risk.units_from_lots(50 / 300, 100000, 1000, 1000)
    assert units == 16000.0


def test_units_below_minimum_returns_zero():
    # tiny size below broker min -> 0 (caller must reject, never oversize)
    assert risk.units_from_lots(0.000001, 100000, 1000, 1000) == 0.0


def test_worst_case_includes_frictions():
    # 16000 units = 0.16 lots, 30 pips, $10/pip/lot = $48 + $2 frictions = $50
    wc = risk.worst_case_usd(16000, 30.0, 10.0, 100000, spread_cost=1, commission=0.5, swap=0.5)
    assert wc == pytest.approx(48.0 + 2.0)


def test_worst_case_includes_slippage():
    # 0.16 lots * 1.5 pips slippage * $10/pip/lot = $2.40 on top of the $48 stop loss.
    base = risk.worst_case_usd(16000, 30.0, 10.0, 100000)
    wc = risk.worst_case_usd(16000, 30.0, 10.0, 100000, slippage=0.16 * 1.5 * 10.0)
    assert base == pytest.approx(48.0)
    assert wc == pytest.approx(48.0 + 2.4)


def test_reward_risk():
    assert risk.reward_risk(1.1600, 1.1570, 1.1660) == pytest.approx(2.0)


def test_reward_risk_zero_risk_raises():
    with pytest.raises(ValueError):
        risk.reward_risk(1.16, 1.16, 1.17)


def test_buffers_room_calculation():
    # day_start 10000, equity 10000, -2% floor = 9800 -> room now 200.
    # candidate worst case 50 -> room after 150.
    b = risk.buffers(balance=10000, equity=10000, day_start_balance=10000,
                     account_baseline=10000, daily_loss_limit_pct=2.0,
                     overall_loss_limit_pct=10.0, candidate_worst_case=50.0)
    assert b.daily_room_now == pytest.approx(200.0)
    assert b.daily_room_after == pytest.approx(150.0)
    assert b.overall_room_now == pytest.approx(1000.0)
    assert b.overall_room_after == pytest.approx(950.0)


def test_buffers_breach_detectable():
    # equity already down to 9810, worst case 50 -> after = -40 (breach)
    b = risk.buffers(balance=10000, equity=9810, day_start_balance=10000,
                     account_baseline=10000, daily_loss_limit_pct=2.0,
                     overall_loss_limit_pct=10.0, candidate_worst_case=50.0)
    assert b.daily_room_after < 0
