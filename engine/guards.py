"""Pure operational guard logic (state-consistency / fail-closed). No I/O — unit-tested.
The watchdog/scanner call these and act (freeze, alert, force-exit) on the results.
"""
from __future__ import annotations
from datetime import datetime

from . import config


def phase_target_reached(state: dict, equity: float) -> bool:
    """True once the phase profit target is reached AND min trading days are met — time to
    stop and await the next account (sticky freeze)."""
    profit = equity - config.FTMO_INITIAL_BALANCE
    return (config.FTMO_STOP_AT_TARGET
            and profit >= config.FTMO_PROFIT_TARGET_USD
            and len(state.get("trading_days", [])) >= config.FTMO_MIN_TRADING_DAYS)


def unknown_positions(state: dict) -> list:
    """Open positions the engine did not place (manual or unexpected) — a consistency risk."""
    return [p.get("id") for p in state.get("open_positions", [])
            if p.get("label") != config.MANAGE_LABEL]


def auto_freeze_reason(state: dict, feed_quoting: int | None = None) -> str | None:
    """Auto-clearing freeze reasons, recomputed each cycle. feed_quoting = count of watchlist
    symbols currently quoting, or None to skip the feed check this cycle."""
    if config.FREEZE_ON_UNKNOWN_POSITION:
        unk = unknown_positions(state)
        if unk:
            return f"non-engine position(s) open: {unk}"
    if feed_quoting is not None and feed_quoting < config.DATA_FEED_MIN_QUOTING:
        return f"market data feed degraded ({feed_quoting} symbols quoting)"
    return None


def trade_age_violations(state: dict, now: datetime) -> list:
    """Engine position ids open longer than MAX_TRADE_AGE_HOURS (forgotten-position guard)."""
    out = []
    for p in state.get("open_positions", []):
        if p.get("label") != config.MANAGE_LABEL:
            continue
        ot = p.get("open_time")
        if not ot:
            continue
        try:
            t = datetime.fromisoformat(str(ot).replace("Z", "+00:00"))
        except Exception:
            continue
        if (now - t).total_seconds() / 3600 > config.MAX_TRADE_AGE_HOURS:
            out.append(p.get("id"))
    return out
