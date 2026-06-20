"""Operational guards (pure): phase target, unknown positions, trade age."""
from datetime import datetime, timezone, timedelta

from engine import guards, config


def test_phase_target_reached_needs_target_and_min_days():
    st = {"trading_days": ["a", "b", "c", "d"]}   # >= 4 min days
    assert guards.phase_target_reached(st, config.FTMO_INITIAL_BALANCE + config.FTMO_PROFIT_TARGET_USD) is True
    # target hit but not enough trading days
    assert guards.phase_target_reached({"trading_days": ["a"]},
                                       config.FTMO_INITIAL_BALANCE + config.FTMO_PROFIT_TARGET_USD) is False
    # enough days but target not hit
    assert guards.phase_target_reached(st, config.FTMO_INITIAL_BALANCE + 100) is False


def test_phase_target_uses_balance_not_equity():
    """Equity above target but balance below — must NOT trigger a sticky freeze."""
    st = {"trading_days": ["a", "b", "c", "d"]}
    equity_above_target = config.FTMO_INITIAL_BALANCE + config.FTMO_PROFIT_TARGET_USD + 50
    balance_below_target = config.FTMO_INITIAL_BALANCE + config.FTMO_PROFIT_TARGET_USD - 100
    # Old bug: would return True using equity; correct: False because balance is below target
    assert guards.phase_target_reached(st, equity_above_target, balance=balance_below_target) is False
    # With balance above target it should fire
    balance_above_target = config.FTMO_INITIAL_BALANCE + config.FTMO_PROFIT_TARGET_USD + 50
    assert guards.phase_target_reached(st, equity_above_target, balance=balance_above_target) is True


def test_unknown_positions():
    st = {"open_positions": [{"id": 1, "label": "ftmo-engine"}, {"id": 2, "label": ""},
                             {"id": 3, "label": "other"}]}
    assert guards.unknown_positions(st) == [2, 3]


def test_unknown_position_freeze_respects_flag(monkeypatch):
    st = {"open_positions": [{"id": 9, "label": ""}]}
    monkeypatch.setattr(config, "FREEZE_ON_UNKNOWN_POSITION", True)
    assert "non-engine" in guards.auto_freeze_reason(st)
    monkeypatch.setattr(config, "FREEZE_ON_UNKNOWN_POSITION", False)   # user's manual trades allowed
    assert guards.auto_freeze_reason(st) is None


def test_auto_freeze_reason_feed():
    # feed degraded (independent of the unknown-position flag)
    assert "feed degraded" in guards.auto_freeze_reason({"open_positions": []}, feed_quoting=1)
    assert guards.auto_freeze_reason({"open_positions": []}, feed_quoting=11) is None


def test_trade_age_violations():
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=config.MAX_TRADE_AGE_HOURS + 5)).isoformat()
    fresh = (now - timedelta(hours=2)).isoformat()
    st = {"open_positions": [
        {"id": 1, "label": "ftmo-engine", "open_time": old},     # too old -> flagged
        {"id": 2, "label": "ftmo-engine", "open_time": fresh},   # fresh
        {"id": 3, "label": "", "open_time": old},                # not engine -> ignored
    ]}
    assert guards.trade_age_violations(st, now) == [1]
