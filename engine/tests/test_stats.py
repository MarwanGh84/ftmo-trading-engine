"""Performance analytics math (pure)."""
import pytest

from engine import stats


def test_basic_stats():
    s = stats.compute_stats([100, -50, 80, -30, 60])  # 3 wins, 2 losses
    assert s["trades"] == 5
    assert s["wins"] == 3 and s["losses"] == 2
    assert s["win_rate"] == pytest.approx(0.6)
    assert s["net"] == pytest.approx(160)
    assert s["profit_factor"] == pytest.approx(240 / 80)
    assert s["expectancy"] == pytest.approx(32)


def test_all_wins_infinite_pf():
    s = stats.compute_stats([10, 20, 30])
    assert s["profit_factor"] == float("inf")
    assert s["losses"] == 0


def test_empty():
    s = stats.compute_stats([])
    assert s["trades"] == 0 and s["win_rate"] == 0 and s["expectancy"] == 0


def test_by_symbol():
    per = stats.by_symbol([{"symbol": "EURUSD", "net": 50}, {"symbol": "EURUSD", "net": -20},
                           {"symbol": "GBPUSD", "net": 30}])
    assert per["EURUSD"]["trades"] == 2 and per["EURUSD"]["net"] == pytest.approx(30)
    assert per["GBPUSD"]["trades"] == 1


def test_by_setup():
    per = stats.by_setup([{"setup": "london_continuation", "net": 100},
                          {"setup": "london_continuation", "net": -40},
                          {"setup": "range_mean_reversion", "net": -25},
                          {"setup": "", "net": 10}])
    assert per["london_continuation"]["trades"] == 2
    assert per["range_mean_reversion"]["net"] == pytest.approx(-25)
    assert per["untagged"]["trades"] == 1   # blank setup grouped as 'untagged'


def test_by_regime():
    per = stats.by_regime([{"regime": "trend", "net": 100}, {"regime": "range", "net": -30}])
    assert per["trend"]["net"] == pytest.approx(100)
    assert per["range"]["net"] == pytest.approx(-30)


def test_conf_bucket():
    assert stats.conf_bucket("85") == "80-89"
    assert stats.conf_bucket(70) == "70-79"
    assert stats.conf_bucket("100") == "90-99"   # clamped into the top bucket
    assert stats.conf_bucket("") == ""
    assert stats.conf_bucket("abc") == ""
