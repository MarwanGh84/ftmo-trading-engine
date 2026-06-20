"""Scanner level + proximity logic (pure)."""
import pytest

from engine import scanner


def _bars(seq):
    # build D1 bars from a list of closes; high/low padded around close
    return [{"open": c, "high": c + 0.002, "low": c - 0.002, "close": c, "volume": 1} for c in seq]


def test_bias_bull_when_above_sma():
    closes = [1.10] * 19 + [1.20]  # last well above the 20-SMA
    lv = scanner.compute_levels(_bars(closes))
    assert lv["bias"] == "bull"
    assert lv["recent_high"] == pytest.approx(1.202)


def test_bias_bear_when_below_sma():
    closes = [1.20] * 19 + [1.10]
    lv = scanner.compute_levels(_bars(closes))
    assert lv["bias"] == "bear"


def test_proximity_resistance():
    lv = {"atr": 0.0030, "recent_high": 1.1050, "recent_low": 1.0900}
    near, note = scanner.proximity(1.1049, lv)   # within 0.5*ATR (0.0015) of the high
    assert near == "resistance"


def test_proximity_support():
    lv = {"atr": 0.0030, "recent_high": 1.1050, "recent_low": 1.0900}
    near, _ = scanner.proximity(1.0905, lv)
    assert near == "support"


def test_proximity_none_in_middle():
    lv = {"atr": 0.0030, "recent_high": 1.1050, "recent_low": 1.0900}
    near, _ = scanner.proximity(1.0975, lv)
    assert near is None


def test_proximity_handles_missing_levels():
    assert scanner.proximity(1.10, {"atr": 0, "recent_high": None, "recent_low": None}) == (None, "")


def test_with_trend_filter():
    # support touch is a cue only in a bear bias; resistance only in a bull bias
    assert scanner.with_trend("support", "bear") is True
    assert scanner.with_trend("support", "bull") is False     # bouncing support in an uptrend -> noise
    assert scanner.with_trend("resistance", "bull") is True
    assert scanner.with_trend("resistance", "bear") is False
    assert scanner.with_trend(None, "bear") is False


def test_mins_since_missing_is_huge():
    assert scanner._mins_since(None) > 1e8
    assert scanner._mins_since("not-a-date") > 1e8


def test_mins_since_recent_is_small():
    from engine import state as state_mod
    iso = state_mod.now_dubai().isoformat()
    assert scanner._mins_since(iso) < 1.0


def test_regime_trend_down_on_steady_decline():
    # 30 steadily falling closes -> SMA20 marches down by well over 1 ATR -> trend_down
    closes = [1.30 - 0.004 * i for i in range(30)]
    lv = scanner.compute_levels(_bars(closes))
    assert lv["regime"] == "trend_down"
    assert lv["trend_strength"] < 0


def test_regime_trend_up_on_steady_rise():
    closes = [1.10 + 0.004 * i for i in range(30)]
    lv = scanner.compute_levels(_bars(closes))
    assert lv["regime"] == "trend_up"


def test_regime_range_when_flat():
    # tiny oscillation around a flat mean -> SMA barely moves -> range
    closes = [1.20 + (0.0005 if i % 2 else -0.0005) for i in range(30)]
    lv = scanner.compute_levels(_bars(closes))
    assert lv["regime"] == "range"


def test_regime_range_when_too_few_bars():
    lv = scanner.compute_levels(_bars([1.20] * 12))   # < 20 + lookback
    assert lv["regime"] == "range"


def test_regime_aligned_truth_table():
    assert scanner.regime_aligned("support", "trend_down") is True
    assert scanner.regime_aligned("support", "trend_up") is False
    assert scanner.regime_aligned("support", "range") is False    # touch in chop -> no cue
    assert scanner.regime_aligned("resistance", "trend_up") is True
    assert scanner.regime_aligned("resistance", "range") is False
    assert scanner.regime_aligned(None, "trend_down") is False
