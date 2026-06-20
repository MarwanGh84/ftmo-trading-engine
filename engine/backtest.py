"""Event-driven backtester for candidate DETERMINISTIC strategies — so we measure edge on real
history BEFORE risking money. No look-ahead: a signal at bar i uses only bars[:i+1], enters at
bar i+1 open, and exits when a later bar's high/low hits the stop or target (stop assumed first if
both hit in the same bar — conservative). Outcomes are in R multiples (+rr win, -1 loss).
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta


def _sma(closes, n):
    return sum(closes[-n:]) / n if len(closes) >= n else None


def _atr(bars, n=14):
    trs = []
    for k in range(1, len(bars)):
        h, l, pc = bars[k]["high"], bars[k]["low"], bars[k - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / min(len(trs), n) if trs else 0.0


# ---- candidate strategies: (bars, i) -> {side, stop_dist, rr} | None -------

def trend_pullback(bars, i):
    """With-trend pullback to the 20-SMA (buy dips in an uptrend / sell rips in a downtrend)."""
    if i < 50:
        return None
    closes = [b["close"] for b in bars[:i + 1]]
    sma20, sma50 = _sma(closes, 20), _sma(closes, 50)
    atr = _atr(bars[:i + 1])
    if not sma20 or not sma50 or atr <= 0:
        return None
    c, bar = closes[-1], bars[i]
    if c > sma50 and c > sma20 and bar["low"] <= sma20 + 0.30 * atr:
        return {"side": "buy", "stop_dist": 1.5 * atr, "rr": 2.0}
    if c < sma50 and c < sma20 and bar["high"] >= sma20 - 0.30 * atr:
        return {"side": "sell", "stop_dist": 1.5 * atr, "rr": 2.0}
    return None


def breakout(bars, i):
    """Breakout of the prior 20-bar range (momentum)."""
    if i < 25:
        return None
    highs = [b["high"] for b in bars[:i + 1]]
    lows = [b["low"] for b in bars[:i + 1]]
    atr = _atr(bars[:i + 1])
    if atr <= 0:
        return None
    c = bars[i]["close"]
    if c > max(highs[-21:-1]):
        return {"side": "buy", "stop_dist": 1.5 * atr, "rr": 2.0}
    if c < min(lows[-21:-1]):
        return {"side": "sell", "stop_dist": 1.5 * atr, "rr": 2.0}
    return None


def mean_reversion(bars, i):
    """Fade a >2-ATR stretch from the 20-SMA back toward the mean."""
    if i < 20:
        return None
    closes = [b["close"] for b in bars[:i + 1]]
    sma20 = _sma(closes, 20)
    atr = _atr(bars[:i + 1])
    if not sma20 or atr <= 0:
        return None
    c = closes[-1]
    if c < sma20 - 2.0 * atr:
        return {"side": "buy", "stop_dist": 1.5 * atr, "rr": 1.5}
    if c > sma20 + 2.0 * atr:
        return {"side": "sell", "stop_dist": 1.5 * atr, "rr": 1.5}
    return None


STRATEGIES = {"trend_pullback": trend_pullback, "breakout": breakout, "mean_reversion": mean_reversion}


def simulate(bars: list, strat) -> list:
    """Return the list of R-multiple outcomes for one symbol's bars."""
    out, i, n = [], 50, len(bars)
    while i < n - 1:
        sig = strat(bars, i)
        if not sig:
            i += 1
            continue
        entry = bars[i + 1]["open"]
        sd, rr, buy = sig["stop_dist"], sig["rr"], sig["side"] == "buy"
        if sd <= 0:
            i += 1
            continue
        stop = entry - sd if buy else entry + sd
        target = entry + sd * rr if buy else entry - sd * rr
        outcome, exit_i = None, i + 1
        for j in range(i + 1, n):
            hi, lo = bars[j]["high"], bars[j]["low"]
            if buy:
                if lo <= stop:
                    outcome, exit_i = -1.0, j; break
                if hi >= target:
                    outcome, exit_i = rr, j; break
            else:
                if hi >= stop:
                    outcome, exit_i = -1.0, j; break
                if lo <= target:
                    outcome, exit_i = rr, j; break
        if outcome is None:                      # still open at series end
            last = bars[-1]["close"]
            outcome = ((last - entry) if buy else (entry - last)) / sd
            exit_i = n - 1
        out.append(outcome)
        i = exit_i + 1                           # no overlapping trades on one symbol
    return out


def run(client, symbols: list, strat_name: str, timeframe: str = "d1", days: int = 1400) -> dict:
    """Fetch history per symbol, simulate, return per-symbol + aggregate R stats."""
    strat = STRATEGIES[strat_name]
    to = datetime.now(timezone.utc)
    frm = to - timedelta(days=days)
    per, all_R = {}, []
    for sym in symbols:
        try:
            bars = client.call("get_trendbars", {"symbolName": sym, "timeframe": timeframe,
                               "from": frm.isoformat(), "to": to.isoformat(), "limit": 1000}).get("bars", [])
        except Exception:
            continue
        if len(bars) < 60:
            continue
        Rs = simulate(bars, strat)
        per[sym] = Rs
        all_R.extend(Rs)
    return {"strategy": strat_name, "timeframe": timeframe, "per_symbol": per, "all_R": all_R}
