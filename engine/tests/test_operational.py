"""Operational controls: FTMO (Prague) daily reset, freeze/unfreeze, order expiry, trading days."""
from datetime import datetime, timezone, timedelta

from engine import state as s
from engine import trade_manager as tm


def test_transaction_lock_is_exclusive():
    # An outer transaction holds the lock; an inner acquisition (separate open file description,
    # contends even in-process) must time out and signal best-effort rather than deadlock.
    with s.transaction(timeout=2.0) as outer:
        assert outer is True
        with s.transaction(timeout=0.3) as inner:
            assert inner is False   # busy -> gave up -> caller proceeds best-effort
    # Once released, the lock is acquirable again.
    with s.transaction(timeout=2.0) as again:
        assert again is True


def test_daily_reset_on_prior_ftmo_day():
    st = dict(s._SCHEMA)
    st["last_run_iso"] = "2020-01-01T00:00:00+04:00"   # clearly a prior FTMO day
    st["trades_taken_today"] = 3
    st["poor_outcomes_today"] = 2
    reset = s.apply_daily_reset(st, balance=10100, equity=10080)
    assert reset is True
    assert st["trades_taken_today"] == 0 and st["poor_outcomes_today"] == 0
    assert st["day_start_balance"] == 10100        # balance, not max(balance, equity)
    assert st["account_baseline"] is not None      # set on first ever run


def test_daily_reset_uses_balance_not_equity():
    """day_start_balance must key to CLOSED balance, not floating equity.
    Using max(balance, equity) sets the daily floor artificially high when a trade is open."""
    st = dict(s._SCHEMA)
    st["last_run_iso"] = "2020-01-01T00:00:00+04:00"
    s.apply_daily_reset(st, balance=10000, equity=10300)  # open winner floating +300
    assert st["day_start_balance"] == 10000   # balance, NOT 10300 (equity)


def test_no_reset_same_ftmo_day():
    st = dict(s._SCHEMA)
    st["last_run_iso"] = s.now_dubai().isoformat()
    st["account_baseline"] = 10000
    st["day_start_balance"] = 10000
    st["trades_taken_today"] = 2
    assert s.apply_daily_reset(st, 10000, 10000) is False
    assert st["trades_taken_today"] == 2           # untouched


def test_freeze_and_unfreeze():
    st = {}
    assert s.freeze(st, "cTrader unreachable 2 cycles") is True   # newly frozen
    assert st["frozen"] is True and "unreachable" in st["frozen_reason"]
    assert s.freeze(st, "again") is False          # already frozen
    result = s.unfreeze(st)
    assert result is True
    assert st["frozen"] is False and st["frozen_reason"] == ""


def test_sticky_freeze_cannot_be_auto_unfrozen():
    """A sticky freeze (phase target, emergency) must survive unfreeze() calls —
    only CLI --force should clear it."""
    st = {}
    s.freeze(st, "phase target reached", sticky=True)
    assert st["frozen_sticky"] is True
    result = s.unfreeze(st)          # routine auto-unfreeze (e.g. from feed-recovered path)
    assert result is False           # blocked
    assert st["frozen"] is True      # freeze remains in place


def test_record_trading_day_dedup():
    st = {}
    s.record_trading_day(st)
    s.record_trading_day(st)
    assert len(st["trading_days"]) == 1            # same day counted once


def test_expired_order_ids():
    now = datetime.now(timezone.utc)
    past = (now - timedelta(hours=1)).isoformat()
    future = (now + timedelta(hours=1)).isoformat()
    st = {
        "pending_orders": [{"id": 111}, {"id": 222}],
        "order_expiry": {"111": past, "222": future, "333": past},  # 333 not live anymore
    }
    expired = tm.expired_order_ids(st, now)
    assert expired == ["111"]                       # 222 future, 333 not live


def test_active_news_currencies():
    now = datetime.now(timezone.utc)
    st = {"news_windows": [
        {"ccy": "USD", "kind": "cb", "start_iso": (now - timedelta(minutes=10)).isoformat(),
         "end_iso": (now + timedelta(minutes=50)).isoformat()},      # active
        {"ccy": "EUR", "kind": "high", "start_iso": (now + timedelta(hours=2)).isoformat(),
         "end_iso": (now + timedelta(hours=3)).isoformat()},         # future, not active
    ]}
    assert tm.active_news_currencies(st, now) == {"USD"}


def test_unprotected_position_detection():
    st = {"open_positions": [
        {"id": 1, "label": "ftmo-engine", "sl": 1.0950},   # protected
        {"id": 2, "label": "ftmo-engine", "sl": None},     # NO stop -> flagged
        {"id": 3, "label": "", "sl": None},                # manual, not engine -> ignored
    ]}
    assert tm.unprotected_position_ids(st) == [2]
