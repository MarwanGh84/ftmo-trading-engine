"""Active trade-management decision logic (pure). entry 1.1000, risk 0.0030 (30 pips), long."""
import pytest

from engine import trade_manager as tm
from engine import config


def _plan():
    return {"be_done": False, "partial_done": False, "trail_R": 0}


def test_no_action_below_1r():
    acts, _ = tm.plan_actions("buy", 1.1000, 0.0030, 1.1010, _plan())  # +0.33R
    assert acts == []


def test_breakeven_at_1r():
    acts, plan = tm.plan_actions("buy", 1.1000, 0.0030, 1.1031, _plan())  # +1.03R
    assert any(a["type"] == "be" and a["sl"] == 1.1000 for a in acts)
    assert plan["be_done"] is True


def test_partial_and_trail_at_2r():
    acts, plan = tm.plan_actions("buy", 1.1000, 0.0030, 1.1061, _plan())  # +2.03R
    types = {a["type"] for a in acts}
    assert "partial" in types
    # trail locks in floor(2.03)-1 = 1R -> SL at entry + 1*risk = 1.1030
    trail = next(a for a in acts if a["type"] == "trail")
    assert trail["sl"] == 1.1030 and trail["to_R"] == 1
    # BE is dropped in favour of the tighter trail
    assert "be" not in types


def test_trail_ratchets_up_at_3r():
    plan = {"be_done": True, "partial_done": True, "trail_R": 1}
    acts, plan = tm.plan_actions("buy", 1.1000, 0.0030, 1.1091, plan)  # +3.03R -> lock 2R
    trail = next(a for a in acts if a["type"] == "trail")
    assert trail["to_R"] == 2 and trail["sl"] == 1.1060
    assert plan["trail_R"] == 2


def test_trail_does_not_move_backwards():
    plan = {"be_done": True, "partial_done": True, "trail_R": 2}
    acts, _ = tm.plan_actions("buy", 1.1000, 0.0030, 1.1091, plan)  # still +3R, already locked 2R
    assert all(a["type"] != "trail" for a in acts)


def test_short_side_breakeven():
    acts, _ = tm.plan_actions("sell", 1.1000, 0.0030, 1.0969, _plan())  # +1.03R for a short
    assert any(a["type"] == "be" and a["sl"] == 1.1000 for a in acts)


def test_short_trail_direction():
    plan = {"be_done": True, "partial_done": True, "trail_R": 0}
    acts, _ = tm.plan_actions("sell", 1.1000, 0.0030, 1.0939, plan)  # +2.03R short -> lock 1R
    trail = next(a for a in acts if a["type"] == "trail")
    assert trail["sl"] == pytest.approx(1.0970)  # entry - 1*risk


def test_r_multiple():
    assert tm.r_multiple("buy", 1.10, 0.0030, 1.1030) == pytest.approx(1.0)
    assert tm.r_multiple("sell", 1.10, 0.0030, 1.0970) == pytest.approx(1.0)


def test_trail_ratchet_capped_to_one_step_per_cycle():
    """A news gap from +2.1R to +6.5R in one bar must only advance trail by 1 step, not 4.
    Jumping multiple steps in one MCP call can exceed cTrader's stop-change validation."""
    plan = {"be_done": True, "partial_done": True, "trail_R": 1}  # currently locked at 1R
    # Price jumped to +6.5R (ideal would be +5R lock, but cap to +2R this cycle)
    acts, updated_plan = tm.plan_actions("buy", 1.1000, 0.0030, 1.1195, plan)  # +6.5R
    trail_acts = [a for a in acts if a["type"] == "trail"]
    assert len(trail_acts) == 1
    assert updated_plan["trail_R"] == 2   # only one step forward from trail_R=1
    assert trail_acts[0]["to_R"] == 2
