"""Auto-analysis when a scanner price alert fires.

Rule-based (no Claude API call) — fetches D1 + H1 bars and applies a deterministic
decision matrix: break-retest vs fade, take/wait/skip, with entry/SL/TP parameters.
Fail-safe: any exception is swallowed; at worst a raw "alert fired" message is sent.
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta

from . import config, telegram


def _sma(vals: list, n: int) -> float | None:
    return sum(vals[-n:]) / n if len(vals) >= n else None


def _atr14(bars: list) -> float:
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-14:]) / min(len(trs), 14) if trs else 0.0


def _h1_context(bars: list, level: float, near: str) -> dict:
    """Classify what H1 bars show around the alert level."""
    if not bars:
        return {"broke": False, "last_close": level, "last_low": level,
                "last_high": level, "extreme": level, "rejection": False}
    recent = bars[-12:]
    last = recent[-1]

    if near == "resistance":
        broke = any(b["high"] > level for b in recent)
        extreme = max(b["high"] for b in recent)
        # Rejection: high touches/exceeds level but close is significantly below
        rng = last["high"] - last["low"]
        rejection = (rng > 0
                     and last["high"] >= level * 0.9999
                     and (last["high"] - last["close"]) / rng > 0.55)
    else:  # support
        broke = any(b["low"] < level for b in recent)
        extreme = min(b["low"] for b in recent)
        rng = last["high"] - last["low"]
        rejection = (rng > 0
                     and last["low"] <= level * 1.0001
                     and (last["close"] - last["low"]) / rng > 0.55)

    return {
        "broke": broke,
        "extreme": extreme,
        "last_close": last["close"],
        "last_high": last["high"],
        "last_low": last["low"],
        "rejection": rejection,
    }


def analyze_and_notify(client, symbol: str, level: float, near: str) -> None:
    """Public entry point — always fail-safe."""
    try:
        _run(client, symbol, level, near)
    except Exception as e:
        try:
            telegram.send(
                f"🔔 <b>Alert: {telegram.esc(symbol)} {near} {level:.5f} touched</b>\n"
                f"⚠ Auto-analysis failed: {telegram.esc(str(e))}"
            )
        except Exception:
            pass


def _run(client, symbol: str, level: float, near: str) -> None:
    now = datetime.now(timezone.utc)

    # ── Fetch data ───────────────────────────────────────────────────────────
    sym_d = client.get_symbol_details(symbol)
    pip = float(sym_d.get("pipSize") or 0.0001)
    bid = float(sym_d.get("bid") or level)
    ask = float(sym_d.get("ask") or level)
    mid = (bid + ask) / 2

    d1 = client.call("get_trendbars", {
        "symbolName": symbol, "timeframe": "d1",
        "from": (now - timedelta(days=90)).isoformat(),
        "to": now.isoformat(), "limit": 90,
    }).get("bars", [])

    h1 = client.call("get_trendbars", {
        "symbolName": symbol, "timeframe": "h1",
        "from": (now - timedelta(days=5)).isoformat(),
        "to": now.isoformat(), "limit": 120,
    }).get("bars", [])

    # ── D1 context ───────────────────────────────────────────────────────────
    closes = [b["close"] for b in d1]
    sma20 = _sma(closes, 20)
    atr = _atr14(d1)
    atr_p = round(atr / pip)
    bias = ("bull" if closes[-1] > sma20 else "bear") if (sma20 and closes) else "flat"

    # ── H1 structure ─────────────────────────────────────────────────────────
    st = _h1_context(h1, level, near)

    # ── Decision matrix ───────────────────────────────────────────────────────
    #
    # resistance + bull + broke above → break-retest BUY
    # resistance + bear + no break    → resistance fade SELL
    # support    + bear + broke below → break-retest SELL
    # support    + bull + no break    → support bounce BUY
    # everything else                 → SKIP (counter-trend)
    #
    if near == "resistance":
        if st["broke"] and bias == "bull":
            setup, side = "break_retest", "buy"
            entry = level
            retest_low = st["last_low"]
            sl = round(retest_low - atr * 0.20, 5)
            run = max(st["extreme"] - level, atr * 0.3)
            tp = round(entry + run * 2.5, 5)
            # Confirmation: last H1 closed ABOVE the level
            confirmed = st["last_close"] > level
            wait_reason = "retest dipping below level — wait for H1 close above" if not confirmed else ""

        elif not st["broke"] and bias == "bear":
            setup, side = "resistance_fade", "sell"
            entry = ask
            sl = round(level + atr * 0.25, 5)
            tp = round(entry - atr * 1.5, 5)
            confirmed = st["rejection"]
            wait_reason = "no clear rejection candle yet — wait for H1 close" if not confirmed else ""

        else:
            _send_skip(symbol, level, near, bias, st, atr_p)
            return

    else:  # support
        if st["broke"] and bias == "bear":
            setup, side = "break_retest", "sell"
            entry = level
            retest_high = st["last_high"]
            sl = round(retest_high + atr * 0.20, 5)
            run = max(level - st["extreme"], atr * 0.3)
            tp = round(entry - run * 2.5, 5)
            confirmed = st["last_close"] < level
            wait_reason = "retest pushing above level — wait for H1 close below" if not confirmed else ""

        elif not st["broke"] and bias == "bull":
            setup, side = "support_bounce", "buy"
            entry = bid
            sl = round(level - atr * 0.25, 5)
            tp = round(entry + atr * 1.5, 5)
            confirmed = st["rejection"]
            wait_reason = "no clear bounce candle yet — wait for H1 close" if not confirmed else ""

        else:
            _send_skip(symbol, level, near, bias, st, atr_p)
            return

    # ── Quality gates ─────────────────────────────────────────────────────────
    stop_pips = abs(entry - sl) / pip
    tgt_pips = abs(tp - entry) / pip
    rr = tgt_pips / stop_pips if stop_pips else 0.0

    skip_reasons = []
    if stop_pips < 10:
        skip_reasons.append(f"stop too tight ({stop_pips:.0f}p < 10p min)")
    if rr < 1.5:
        skip_reasons.append(f"R:R {rr:.1f} < 1.5 min")

    if skip_reasons:
        verdict, emoji = "SKIP", "❌"
    elif not confirmed:
        verdict, emoji = "WAIT", "⏳"
    else:
        verdict, emoji = "TAKE", "✅"

    # ── Format message ────────────────────────────────────────────────────────
    setup_label = {
        "break_retest": "Break-retest",
        "resistance_fade": "Resistance fade",
        "support_bounce": "Support bounce",
    }.get(setup, setup)

    h1_note = ""
    if st["broke"]:
        h1_note = f"broke → {st['extreme']:.5f}"
        if st["last_close"] < level and side == "buy":
            h1_note += f" → retesting below ({st['last_close']:.5f})"
        elif st["last_close"] > level and side == "sell":
            h1_note += f" → retesting above ({st['last_close']:.5f})"
        else:
            h1_note += f" → held ({st['last_close']:.5f})"
    else:
        h1_note = f"touched, no break · last {st['last_close']:.5f}"

    lines = [
        f"🔔 <b>{telegram.esc(symbol)} {near} {telegram.code(f'{level:.5f}')} touched</b>",
        f"",
        f"📊 <b>{setup_label} {side.upper()}</b>",
        f"D1: {bias} · ATR {atr_p}p · SMA20 {sma20:.5f}",
        f"H1: {telegram.esc(h1_note)}",
        f"",
        f"{emoji} <b>{verdict}</b>",
    ]

    if skip_reasons:
        lines.append("  " + " · ".join(skip_reasons))
    elif wait_reason:
        lines.append(f"  {telegram.esc(wait_reason)}")

    if verdict != "SKIP":
        entry_s = f"{entry:.5f}"
        sl_s = f"{sl:.5f} ({stop_pips:.0f}p)"
        tp_s = f"{tp:.5f} ({tgt_pips:.0f}p · R:R {rr:.1f})"
        lines += [
            f"",
            f"  Entry  {telegram.code(entry_s)}",
            f"  SL     {telegram.code(sl_s)}",
            f"  TP     {telegram.code(tp_s)}",
        ]

    telegram.send("\n".join(lines))


def _send_skip(symbol: str, level: float, near: str, bias: str, st: dict, atr_p: int) -> None:
    reason = f"{near} touch in {bias} trend — counter-trend, no edge"
    telegram.send(
        f"🔔 <b>{telegram.esc(symbol)} {near} {telegram.code(f'{level:.5f}')} touched</b>\n\n"
        f"❌ <b>SKIP</b> — {telegram.esc(reason)}\n"
        f"D1 bias: {bias} · ATR {atr_p}p"
    )
