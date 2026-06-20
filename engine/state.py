"""Atomic state.json read/write. The DAILY boundary is keyed to FTMO's reset (00:00
Europe/Prague), NOT Dubai — FTMO's Maximum Daily Loss resets at CE(S)T midnight, so the
day-start balance and daily counters must align to that. Dubai time is still used for
scheduling/display.

state.json is written ONLY by the engine (atomic tmp+rename).
"""
from __future__ import annotations
import contextlib
import fcntl
import json
import os
import tempfile
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config

TZ = ZoneInfo(config.TZ)                 # Dubai — scheduling/display
FTMO_TZ = ZoneInfo(config.FTMO_RESET_TZ)  # Europe/Prague — the daily-loss reset boundary

# Cross-process advisory lock so concurrent engine invocations (the 5-min watchdog, the
# scheduled session runs, and any interactive call) can't lost-update state.json. The atomic
# tmp+rename in save() prevents file CORRUPTION; this lock prevents LOST UPDATES of the
# accumulated counters (poor_outcomes_today, daily_limit_hit, executed_signals, order_expiry)
# that aren't re-derived from cTrader on reconcile.
LOCK_FILE = config.ROOT / ".state.lock"


@contextlib.contextmanager
def transaction(timeout: float = 45.0):
    """Hold an exclusive lock for the whole load→mutate→save critical section.

    Blocks up to `timeout` seconds. If it can't acquire in time (e.g. a placement is stuck on
    a confirmation dialog) it proceeds BEST-EFFORT rather than deadlocking — safety paths like
    the watchdog kill-switch must never be blocked indefinitely. Normal hold is ~seconds, so
    contention almost never reaches the timeout.
    """
    fh = open(LOCK_FILE, "w")
    acquired = False
    deadline = time.time() + timeout
    try:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    break   # give up — proceed best-effort
                time.sleep(0.2)
        yield acquired
    finally:
        if acquired:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except Exception:
                pass
        fh.close()

# Fields that reset at the start of each FTMO (Prague) trading day.
_DAILY_DEFAULTS = {
    "daily_pnl": 0.0,
    "trades_taken_today": 0,
    "poor_outcomes_today": 0,
    "daily_limit_hit": False,
    "day_start_balance": None,   # max(balance, equity) at the first run of the FTMO day
    "news_windows": [],          # populated by morning_brief
    "news_windows_date": None,   # date the windows were generated for (staleness guard)
    "executed_signals": [],      # signal ids executed today (duplicate-order guard)
    "counted_positions": [],     # engine position ids already counted toward trades_taken_today
}

_SCHEMA = {
    "open_positions": [],
    "pending_orders": [],
    "account_baseline": None,    # equity at first ever run; basis for the overall max-loss floor
    "last_run_iso": None,
    "frozen": False,             # operational freeze — block NEW entries (management continues)
    "frozen_reason": "",
    "frozen_sticky": False,      # sticky freeze (e.g. phase-target/emergency) needs MANUAL clearing
    "unreachable_streak": 0,     # consecutive cTrader-unreachable monitor cycles
    "order_expiry": {},          # {order_id: iso} — resting orders auto-cancel after expiry
    "trading_days": [],          # distinct FTMO dates a trade was taken (min-trading-days tracker)
    "candidates": [],            # scanner-flagged pairs at key levels (handed to the Claude runs)
    "candidates_time": None,
    **_DAILY_DEFAULTS,
}


def now_dubai() -> datetime:
    return datetime.now(TZ)


def now_ftmo() -> datetime:
    return datetime.now(FTMO_TZ)


def _ftmo_date(iso: str | None) -> str | None:
    if not iso:
        return None
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(FTMO_TZ).date().isoformat()


def load() -> dict:
    if not config.STATE_FILE.exists():
        return dict(_SCHEMA)
    data = json.loads(config.STATE_FILE.read_text())
    for k, v in _SCHEMA.items():
        data.setdefault(k, v.copy() if isinstance(v, (list, dict)) else v)
    return data


def save(state: dict) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    state["last_run_iso"] = now_dubai().isoformat()
    fd, tmp = tempfile.mkstemp(dir=str(config.ROOT), suffix=".state.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, config.STATE_FILE)   # atomic on POSIX
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def apply_daily_reset(state: dict, balance: float, equity: float) -> bool:
    """Reset daily fields when the FTMO (Prague) calendar day has rolled over.

    Records day_start_balance = max(balance, equity) at the first run of the new FTMO day —
    this is the basis FTMO uses for the Maximum Daily Loss. Returns True if a reset ran.
    """
    today = now_ftmo().date().isoformat()
    last_day = _ftmo_date(state.get("last_run_iso"))

    if state.get("account_baseline") is None:
        state["account_baseline"] = max(balance, equity)

    if last_day != today:
        for k, v in _DAILY_DEFAULTS.items():
            state[k] = v.copy() if isinstance(v, (list, dict)) else v
        state["day_start_balance"] = balance  # FTMO daily limit is measured from closed balance, not equity
        return True

    if state.get("day_start_balance") is None:
        state["day_start_balance"] = balance
    return False


def news_windows_fresh(state: dict) -> bool:
    """True only if morning_brief populated news windows for today's FTMO (Prague) date.

    Must use the Prague date (not Dubai) because the FTMO trading day resets at 00:00 Prague.
    A morning brief at 09:32 Dubai stores news_windows_date as the Prague date; a run at
    23:50 Dubai is still the SAME Prague day — checking Dubai date would falsely mark them stale
    and block all trades for the rest of a valid FTMO day."""
    return state.get("news_windows_date") == now_ftmo().date().isoformat()


# ---- operational freeze (fail-closed) ------------------------------------

def freeze(state: dict, reason: str, sticky: bool = False) -> bool:
    """Block new entries (management/protection continue). sticky=True means it won't
    auto-clear (needs manual unfreeze). Returns True if newly frozen."""
    was = state.get("frozen", False)
    state["frozen"] = True
    state["frozen_reason"] = reason
    if sticky:
        state["frozen_sticky"] = True
    return not was


def unfreeze(state: dict) -> bool:
    """Clear a non-sticky operational freeze. Returns False if blocked by a sticky freeze
    (phase-target / emergency) — those require explicit CLI `ftmo unfreeze --force`."""
    if state.get("frozen_sticky"):
        return False
    state["frozen"] = False
    state["frozen_reason"] = ""
    return True


def record_trading_day(state: dict) -> None:
    d = now_ftmo().date().isoformat()
    days = state.setdefault("trading_days", [])
    if d not in days:
        days.append(d)
