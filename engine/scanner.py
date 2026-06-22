"""Market scanner — the continuous-awareness layer. Runs every ~30 min during active hours
(launchd, no Claude cost), sweeps the watchlist, and flags instruments sitting at key daily
levels. Updates a "Watchlist" tab in the Sheet and Telegram-alerts NEW setups (deduped).

Request-frugal: D1 levels (20-day high/low, 20-SMA bias, ATR) are computed once per Dubai day
and cached in state; each subsequent sweep only fetches the live quote per symbol.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen

from . import config, telegram, sheets
from . import state as state_mod
from . import shadow


def _now_iso() -> str:
    return state_mod.now_dubai().isoformat()


def _mins_since(iso) -> float:
    """Minutes since an ISO timestamp; a huge number if missing/unparseable (so 'not recent')."""
    if not iso:
        return 1e9
    try:
        then = datetime.fromisoformat(iso)
    except Exception:
        return 1e9
    return (state_mod.now_dubai() - then).total_seconds() / 60.0


def _sma(closes, n):
    return sum(closes[-n:]) / n if len(closes) >= n else None


def _atr(bars, n=14):
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return 0.0
    return sum(trs[-n:]) / min(len(trs), n)


def compute_levels(bars: list) -> dict:
    """Pure: derive bias + 20-day high/low + ATR + trend REGIME from D1 bars."""
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    sma20 = _sma(closes, 20)
    last = closes[-1] if closes else None
    bias = "flat"
    if sma20 and last is not None:
        bias = "bull" if last > sma20 else "bear"
    atr = _atr(bars)
    # Trend regime: the slope of SMA20 over the lookback, measured in ATRs. A genuine trend marches the
    # SMA one way by >= MIN_ATR; otherwise it's a RANGE (where level-touches tend to bounce, not break).
    # Steadier than `bias`, which a single close either side of the SMA can flip. (The legitimate kernel
    # of the "trend radar" idea — used to QUALIFY candidates, never to decide a trade.)
    regime, trend_strength = "range", 0.0
    lb = config.SCAN_TREND_LOOKBACK
    sma_past = _sma(closes[:-lb], 20) if len(closes) >= 20 + lb else None
    if sma20 and sma_past and atr:
        trend_strength = (sma20 - sma_past) / atr
        if trend_strength >= config.SCAN_TREND_MIN_ATR:
            regime = "trend_up"
        elif trend_strength <= -config.SCAN_TREND_MIN_ATR:
            regime = "trend_down"
    return {
        "bias": bias,
        "recent_high": max(highs[-20:]) if highs else None,
        "recent_low": min(lows[-20:]) if lows else None,
        "atr": atr,
        "regime": regime,
        "trend_strength": round(trend_strength, 2),
    }


def regime_aligned(near: str | None, regime: str | None) -> bool:
    """A support-touch is a bear-continuation cue only in a confirmed DOWNTREND; a resistance-touch only
    in a confirmed UPTREND. Stricter than with_trend() — needs a real slope, not just price one side of
    the SMA. Pure, so the regime gate is unit-testable."""
    return (near == "support" and regime == "trend_down") or (near == "resistance" and regime == "trend_up")


def with_trend(near: str | None, bias: str | None) -> bool:
    """A support-touch is a (bear) continuation cue only in a bear bias; a resistance-touch only in
    a bull bias. Pure, so the scanner's signal filter is unit-testable."""
    return (near == "support" and bias == "bear") or (near == "resistance" and bias == "bull")


def proximity(price: float, levels: dict) -> tuple[str | None, str]:
    """Pure: is price within SCAN_NEAR_ATR*ATR of the 20D high/low? Returns (near, note)."""
    atr, hi, lo = levels.get("atr"), levels.get("recent_high"), levels.get("recent_low")
    if not atr or not hi or not lo:
        return None, ""
    band = config.SCAN_NEAR_ATR * atr
    if abs(price - hi) <= band:
        return "resistance", f"near 20D high {hi:.5f}"
    if abs(price - lo) <= band:
        return "support", f"near 20D low {lo:.5f}"
    return None, ""


def ensure_subscribed(client) -> list:
    """Make sure all watchlist symbols sit in the cTrader watchlist so their quotes stay
    subscribed (get_symbol_details returns live bid/ask). Idempotent. Returns symbols added."""
    try:
        wls = client.call("get_watchlists").get("watchlists", [])
        names = {w.get("name") for w in wls}
        if config.WATCHLIST_NAME not in names:
            client.call("create_watchlist", {"name": config.WATCHLIST_NAME})
            existing = set()
        else:
            existing = set(next((w.get("symbols", []) for w in wls
                                 if w.get("name") == config.WATCHLIST_NAME), []))
        added = []
        for sym in config.WATCHLIST:
            if sym not in existing:
                client.call("add_symbol_to_watchlist",
                            {"watchlistName": config.WATCHLIST_NAME, "symbolName": sym})
                added.append(sym)
        client.call("set_market_watch_panel", {"panel": "watchlists"})
        return added
    except Exception:
        return []


def _within_hours() -> bool:
    now = state_mod.now_dubai()
    if now.weekday() >= 5:   # 5=Saturday, 6=Sunday — markets closed, no alerts
        return False
    return config.SCAN_HOURS_DUBAI[0] <= now.hour < config.SCAN_HOURS_DUBAI[1]


def scan(client, state: dict) -> list[str]:
    """Sweep the watchlist; refresh cached levels once per day; alert new proximities."""
    today = state_mod.now_dubai().date().isoformat()
    cache = state.setdefault("watchlist_levels", {})
    fresh = cache.get("_date") == today
    if not fresh:
        cache = {"_date": today}

    ensure_subscribed(client)   # keep watchlist quotes flowing
    alerts_state = state.setdefault("scanner_alerts", {})
    rows, alerts, candidates = [], [], []
    now = state_mod.now_dubai().strftime("%H:%M")
    _RLABEL = {"trend_up": "D1 uptrend", "trend_down": "D1 downtrend", "range": "D1 range"}

    for sym in config.WATCHLIST:
        try:
            d = client.get_symbol_details(sym)
            price = (float(d["bid"]) + float(d["ask"])) / 2
        except Exception:
            continue
        # Recompute when uncached OR when the cached entry predates a compute_levels schema change
        # (e.g. the new `regime` field) — so a same-day cache self-heals after an upgrade.
        if sym not in cache or "regime" not in cache.get(sym, {}):
            to = datetime.now(timezone.utc)
            frm = to - timedelta(days=120)
            try:
                bars = client.call("get_trendbars", {"symbolName": sym, "timeframe": "d1",
                                   "from": frm.isoformat(), "to": to.isoformat(), "limit": 120}).get("bars", [])
            except Exception:
                bars = []
            cache[sym] = compute_levels(bars)
        lv = cache[sym]
        near, note = proximity(price, lv)
        bias = lv.get("bias")
        regime = lv.get("regime", "range")
        rows.append([sym, f"{price:.5f}", bias or "", _RLABEL.get(regime, regime),
                     f"{lv.get('recent_low') or 0:.5f}", f"{lv.get('recent_high') or 0:.5f}",
                     near or "—", note, now])
        if near:
            candidates.append({"symbol": sym, "price": f"{price:.5f}", "bias": bias,
                               "regime": regime, "near": near, "note": note, "time": now})
        # Signal-quality gate: prefer the trend-REGIME filter (a slope-confirmed trend aligned with the
        # level — support needs a downtrend, resistance an uptrend), which drops level-touches in chop.
        # Falls back to the lighter bias-based with_trend filter if regime gating is off.
        if config.SCAN_TREND_REGIME_ONLY:
            aligned = regime_aligned(near, regime)
        elif config.SCAN_WITH_TREND_ONLY:
            aligned = with_trend(near, bias)
        else:
            aligned = bool(near)
        worth_alert = bool(near) and aligned
        # Time-cooldown de-dupe: don't re-ping the same symbol+level within the cooldown, even if
        # price oscillates in and out of the band. Only touch the record when we actually alert,
        # so the cooldown clock persists across band re-entries.
        key = near or "none"
        last = alerts_state.get(sym)
        last = last if isinstance(last, dict) else {}
        recent = last.get("key") == key and _mins_since(last.get("ts")) < config.SCAN_ALERT_COOLDOWN_MIN
        if worth_alert and not recent:
            pip = d.get("pipSize") or 0.0001
            level = lv.get("recent_low") if near == "support" else lv.get("recent_high")
            dist = abs(price - level) / pip if level else 0
            loc = "AT level" if dist <= 3 else f"{dist:.0f}p {'above' if price > level else 'below'}"
            alerts.append(f"{sym} [{bias} · {_RLABEL.get(regime, regime)}] — {note} (px {price:.5f}, {loc})")
            alerts_state[sym] = {"key": key, "ts": _now_iso()}

    state["watchlist_levels"] = cache
    # Publish the flagged pairs as CANDIDATES for the scheduled Claude runs to prioritize (this is the
    # bot "handing off" to Claude — the run reads these and analyzes them first). Now carries the regime.
    state["candidates"] = candidates
    state["candidates_time"] = now
    sheets.update_watchlist(rows)
    for a in alerts:
        telegram.send(f"👀 Watch: {a}")

    # Grade any open shadow candidates (would-have outcomes) — free here, no Claude cost.
    try:
        shadow.grade_open(client)
    except Exception:
        pass

    # Data-feed health: a broad quote outage is a visible FREEZE, not silent per-trade skips.
    if not state.get("frozen_sticky"):
        n = len(rows)
        if n < config.DATA_FEED_MIN_QUOTING:
            if state_mod.freeze(state, f"market data feed degraded ({n} symbols quoting)"):
                telegram.send(f"🧊 FROZEN — market data degraded ({n} symbols quoting). "
                              f"Check cTrader Market Watch.")
        elif state.get("frozen") and "feed degraded" in state.get("frozen_reason", ""):
            if state_mod.unfreeze(state):
                telegram.send("✅ Unfroze — market data restored.")

    # Uptime Kuma heartbeat: ping the push URL after every successful sweep so the monitor can
    # alert if the scanner silently stops running (launchd dies, Mac sleeps, cTrader unreachable).
    _ping_uptime_kuma()

    return alerts


def _ping_uptime_kuma() -> None:
    url = config.uptime_kuma_url()
    if not url:
        return
    try:
        urlopen(url, timeout=5)
    except Exception:
        pass
