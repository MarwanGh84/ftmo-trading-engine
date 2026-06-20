"""Lock the cTrader position/order field mapping. Built from a real live get_positions
payload (id, symbolName, tradeSide, stopLossPrice, takeProfitPrice, ...). If the engine
ever silently fails to extract symbol/side/sl, the anti-hedge and aggregate-risk rails
would under-count a position — so this is a safety regression guard."""
from engine import reconcile


# A real position dict as returned by the cTrader MCP.
RAW_POSITION = {
    "id": 7622885, "symbolName": "EURUSD", "tradeSide": "Buy",
    "volumeInUnits": 10000, "volumeInLots": 0.1, "lotSize": 100000,
    "entryPrice": 1.16001, "currentPrice": 1.16001, "pips": 0.0,
    "grossProfit": 0.0, "netProfit": -0.5, "swap": 0, "commission": -0.25,
    "stopLossPrice": 1.15601, "stopLossPips": 40.0,
    "takeProfitPrice": 1.16891, "takeProfitPips": 89.0,
    "openTime": "2026-06-15T18:23:31.977Z", "label": "", "comment": "",
}


def test_position_fields_extracted():
    n = reconcile._norm_position(RAW_POSITION)
    assert n["id"] == 7622885
    assert n["symbol"] == "EURUSD"      # was the bug: came back "" via wrong key
    assert n["side"] == "buy"
    assert n["volume_units"] == 10000
    assert n["entry"] == 1.16001
    assert n["sl"] == 1.15601           # stopLossPrice, not stopLoss
    assert n["sl_pips"] == 40.0
    assert n["tp"] == 1.16891           # takeProfitPrice, not takeProfit
    assert n["tp_pips"] == 89.0


def test_symbol_never_blank_when_present():
    # The anti-hedge rail keys off symbol length >= 6; a blank symbol would skip the check.
    assert len(reconcile._norm_position(RAW_POSITION)["symbol"]) >= 6


# ---- closure detection ---------------------------------------------------

def test_classify_win_loss_scratch():
    thr = 5.0
    assert reconcile._classify(50.0, thr) == "WIN"
    assert reconcile._classify(-50.0, thr) == "LOSS"
    assert reconcile._classify(2.0, thr) == "SCRATCH"
    assert reconcile._classify(-2.0, thr) == "SCRATCH"
    assert reconcile._classify(None, thr) == "UNKNOWN"


def test_realized_pnl_lookup():
    items = [{"positionId": 111, "netProfit": 73.5}, {"id": 222, "profit": -40.0}]
    assert reconcile._realized_pnl(items, 111) == 73.5
    assert reconcile._realized_pnl(items, 222) == -40.0
    assert reconcile._realized_pnl(items, 999) is None


class _FakeClient:
    def __init__(self, history):
        self._h = history

    def call(self, tool, args=None):
        return {"history": self._h}


def test_detect_closure_loss_bumps_poor_outcome():
    state = {"day_start_balance": 10000, "poor_outcomes_today": 0}
    old_by_id = {7622885: {"id": 7622885, "symbol": "EURUSD", "net_profit": -30.0,
                           "label": "ftmo-engine"}}
    closures = reconcile.detect_closures(
        state, old_by_id, new_ids=set(),
        client=_FakeClient([{"positionId": 7622885, "netProfit": -42.0}]))
    assert closures[0]["result"] == "LOSS"
    assert closures[0]["poor"] is True
    assert state["poor_outcomes_today"] == 1


def test_manual_trade_loss_does_not_count_as_poor():
    # A position the engine did NOT place (label "") closing at a loss must NOT halt the engine.
    state = {"day_start_balance": 10000, "poor_outcomes_today": 0}
    old_by_id = {99: {"id": 99, "symbol": "EURUSD", "net_profit": -25.0, "label": ""}}
    closures = reconcile.detect_closures(
        state, old_by_id, new_ids=set(),
        client=_FakeClient([{"positionId": 99, "netProfit": -25.0}]))
    assert closures[0]["result"] == "LOSS"
    assert closures[0]["poor"] is False
    assert state["poor_outcomes_today"] == 0


def test_detect_closure_win_no_bump():
    state = {"day_start_balance": 10000, "poor_outcomes_today": 0}
    old_by_id = {1: {"id": 1, "symbol": "GBPUSD", "net_profit": 80.0, "label": "ftmo-engine"}}
    closures = reconcile.detect_closures(
        state, old_by_id, new_ids=set(),
        client=_FakeClient([{"positionId": 1, "netProfit": 80.0}]))
    assert closures[0]["result"] == "WIN"
    assert state["poor_outcomes_today"] == 0


def test_detect_closure_fails_closed_when_history_missing():
    """When order history lookup fails, net=None → UNKNOWN → counts as poor outcome (fail-closed).
    We must not fall back to last-seen floating P/L which could misclassify a real loss as a win."""
    state = {"day_start_balance": 10000, "poor_outcomes_today": 0}
    old_by_id = {5: {"id": 5, "symbol": "EURUSD", "net_profit": -25.0, "label": "ftmo-engine"}}
    closures = reconcile.detect_closures(state, old_by_id, new_ids=set(),
                                         client=_FakeClient([]))  # empty history
    assert closures[0]["net"] is None
    assert closures[0]["result"] == "UNKNOWN"
    assert state["poor_outcomes_today"] == 1
