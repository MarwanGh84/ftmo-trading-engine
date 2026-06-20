"""Shadow journal: first-touch grading + take-vs-skip edge summary."""
from engine import shadow


def _bar(h, l):
    return {"high": h, "low": l}


def test_grade_sell_target_first_is_win():
    # sell entry 1.3200, stop 1.3230, target 1.3100. Price drifts down to target.
    bars = [_bar(1.3205, 1.3180), _bar(1.3185, 1.3095)]
    assert shadow.grade("sell", 1.3200, 1.3230, 1.3100, bars) == "win"


def test_grade_sell_stop_first_is_loss():
    bars = [_bar(1.3235, 1.3190)]   # spikes up through 1.3230 stop
    assert shadow.grade("sell", 1.3200, 1.3230, 1.3100, bars) == "loss"


def test_grade_buy_target_first_is_win():
    bars = [_bar(1.1610, 1.1595), _bar(1.1665, 1.1640)]
    assert shadow.grade("buy", 1.1600, 1.1570, 1.1660, bars) == "win"


def test_grade_straddle_bar_scored_loss():
    # one bar hits BOTH stop and target -> conservatively a loss (never overstate edge)
    bars = [_bar(1.3235, 1.3095)]
    assert shadow.grade("sell", 1.3200, 1.3230, 1.3100, bars) == "loss"


def test_grade_no_touch_is_open():
    bars = [_bar(1.3205, 1.3190), _bar(1.3210, 1.3195)]
    assert shadow.grade("sell", 1.3200, 1.3230, 1.3100, bars) == "open"


def test_summary_take_vs_skip_edge(monkeypatch):
    items = [
        {"verdict": "take", "status": "win", "result": "win"},
        {"verdict": "take", "status": "win", "result": "win"},
        {"verdict": "take", "status": "loss", "result": "loss"},
        {"verdict": "skip", "status": "loss", "result": "loss"},
        {"verdict": "skip", "status": "loss", "result": "loss"},
        {"verdict": "skip", "status": "open", "result": ""},
    ]
    monkeypatch.setattr(shadow, "_load", lambda: items)
    s = shadow.summary()
    assert s["take"]["graded"] == 3 and s["take"]["win_rate"] == 2 / 3
    assert s["skip"]["graded"] == 2 and s["skip"]["win_rate"] == 0.0
    assert s["skip"]["open"] == 1
    # takes win, skips don't -> the filtering shows positive edge
    assert s["take"]["win_rate"] > s["skip"]["win_rate"]
