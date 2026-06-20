"""Backtest simulator sanity (no look-ahead, correct R outcomes)."""
from engine import backtest as bt


def _bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c, "volume": 1}


def test_simulate_take_profit_and_stop():
    # A forced strategy that always goes long with stop_dist 1.0, rr 2.0.
    def always_long(bars, i):
        return {"side": "buy", "stop_dist": 1.0, "rr": 2.0} if i == 50 else None

    bars = [_bar(100, 100.5, 99.5, 100) for _ in range(51)]
    # entry at bar 51 open = 100; target = 102, stop = 99
    bars.append(_bar(100, 100, 100, 100))       # 51: entry bar
    bars.append(_bar(100, 102.5, 100, 102))     # 52: hits target 102 -> +2R
    Rs = bt.simulate(bars, always_long)
    assert Rs == [2.0]


def test_simulate_stop_first_when_both_in_bar():
    def always_long(bars, i):
        return {"side": "buy", "stop_dist": 1.0, "rr": 2.0} if i == 50 else None

    bars = [_bar(100, 100.5, 99.5, 100) for _ in range(51)]
    bars.append(_bar(100, 100, 100, 100))       # entry 100
    bars.append(_bar(100, 102.5, 98.5, 100))    # both target(102) and stop(99) in range -> stop first
    Rs = bt.simulate(bars, always_long)
    assert Rs == [-1.0]


def test_atr_positive():
    bars = [_bar(100 + k, 101 + k, 99 + k, 100 + k) for k in range(20)]
    assert bt._atr(bars) > 0
