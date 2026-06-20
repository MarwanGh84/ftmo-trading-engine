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


def test_cost_model_deflates_wins_and_inflates_losses():
    """With costs applied, win R must be smaller and loss R must be larger (more negative)."""
    def always_long(bars, i):
        return {"side": "buy", "stop_dist": 1.0, "rr": 2.0} if i == 50 else None

    def always_long_stop(bars, i):
        return {"side": "buy", "stop_dist": 1.0, "rr": 2.0} if i == 50 else None

    # Win case
    win_bars = [_bar(100, 100.5, 99.5, 100) for _ in range(51)]
    win_bars.append(_bar(100, 100, 100, 100))
    win_bars.append(_bar(100, 102.5, 100, 102))   # hits target -> +2R gross

    cost_r = 0.05
    [R_no_cost] = bt.simulate(win_bars, always_long)
    [R_with_cost] = bt.simulate(win_bars, always_long, spread_r=cost_r)
    assert R_no_cost == 2.0
    assert R_with_cost == 2.0 - cost_r  # deflated

    # Loss case
    loss_bars = [_bar(100, 100.5, 99.5, 100) for _ in range(51)]
    loss_bars.append(_bar(100, 100, 100, 100))
    loss_bars.append(_bar(100, 100, 98.5, 100))   # hits stop 99 -> -1R gross

    [R_loss_no_cost] = bt.simulate(loss_bars, always_long_stop)
    [R_loss_with_cost] = bt.simulate(loss_bars, always_long_stop, spread_r=cost_r)
    assert R_loss_no_cost == -1.0
    assert R_loss_with_cost == -1.0 - cost_r  # more negative
