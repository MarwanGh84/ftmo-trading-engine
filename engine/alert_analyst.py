"""Auto-analysis when a scanner price alert fires.

Calls the Claude API (claude-sonnet-4-6) with full context — D1/H1 bars, COT bias,
news windows, open positions, account state — and executes the trade if the verdict
is TAKE.

All existing engine rails still run inside execute.execute() so Claude's TAKE can
still be refused by daily limits, news windows, spread spikes, or risk caps.
Fail-safe: any exception falls back to a bare "alert fired" Telegram message.
"""
from __future__ import annotations
import json
import re
from datetime import datetime, timezone, timedelta

try:
    import anthropic as _anthropic
    _HAS_ANTHROPIC = True
except ImportError:
    _HAS_ANTHROPIC = False

from . import config, telegram, cot as cot_mod, news as news_mod
from . import execute as execute_mod
from . import state as state_mod

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 1024

_SYSTEM = """\
You are the trading brain for an FTMO Challenge account ($10,000 USD, Phase 1).
Strategy: trade key support/resistance levels identified by the scanner.

Setup types:
  break_retest   — price broke through level, now retesting from the other side
                   (trade in the breakout direction)
  resistance_fade — price hit resistance, D1 bear trend → SELL from resistance
  support_bounce  — price hit support, D1 bull trend → BUY from support

All of these must be TRUE for TAKE:
  1. D1 bias aligns with trade direction (bull → buy only, bear → sell only)
  2. H1 confirmed: break-retest requires last H1 bar to CLOSE beyond the level;
     bounce requires a clear rejection candle (wick > 55% of range, body away from level)
  3. R:R >= 1.5 (target / stop distance)
  4. Stop >= 10 pips from entry
  5. Not inside a HIGH-impact or CB news window right now; no CB event upcoming
  6. COT: skip if a pair currency is at a crowded extreme AGAINST the trade direction
  7. Portfolio: skip if open_positions already has 2 positions on the same base or quote currency

WAIT = setup forming but H1 not confirmed yet (check again next cycle).
SKIP = any gate above fails, or counter-trend, or correlated overload.

Respond with ONLY valid JSON, no markdown:
{
  "verdict": "TAKE" | "WAIT" | "SKIP",
  "side": "buy" | "sell" | null,
  "entry": <float> | null,
  "stop": <float> | null,
  "target": <float> | null,
  "setup_type": "break_retest" | "support_bounce" | "resistance_fade" | null,
  "confidence": "high" | "medium" | "low",
  "reasoning": "<1-3 sentences on the key deciding factor>"
}"""


def _sma(vals: list, n: int) -> float | None:
    return sum(vals[-n:]) / n if len(vals) >= n else None


def _atr14(bars: list) -> float:
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-14:]) / min(len(trs), 14) if trs else 0.0


def _cot_for(symbol: str) -> str:
    bias = cot_mod.load_bias()
    if not bias:
        return "COT data unavailable"
    sym = symbol.upper()
    base, quote = sym[:3], sym[3:6]
    parts = []
    for ccy in (base, quote):
        d = bias.get(ccy)
        if d:
            sig = d["signal"].replace("_", " ")
            net_k = d["net"] / 1000
            parts.append(f"{ccy}: net {net_k:+.1f}k ({d['percentile']:.0f}th pctl, {sig})")
    return "; ".join(parts) if parts else "COT unavailable"


def _news_for(state: dict, symbol: str) -> str:
    now = datetime.now(timezone.utc)
    nb = news_mod.evaluate(state, symbol, now)
    if not nb["fresh"]:
        return "NEWS: windows stale — run morning brief first"
    parts = []
    if nb["in_window"]:
        evt = nb.get("event") or ""
        parts.append(f"IN BLACKOUT NOW ({evt})")
    if nb["cb_hold"]:
        evt = nb.get("event") or ""
        parts.append(f"CB hold ({evt})")
    ccys = news_mod.symbol_currencies(symbol)
    for w in state.get("news_windows", []):
        if w.get("ccy", "").upper() not in ccys:
            continue
        try:
            raw = w["start_iso"].replace("Z", "+00:00")
            start = datetime.fromisoformat(raw)
            if now < start <= now + timedelta(hours=2):
                mins = int((start - now).total_seconds() / 60)
                parts.append(f"{w.get('event')} in {mins}min ({w.get('ccy')})")
        except Exception:
            pass
    return "NEWS: " + ("; ".join(parts) if parts else "clear")


def _positions_for(state: dict) -> str:
    positions = state.get("open_positions", [])
    if not positions:
        return "none"
    return ", ".join(
        f"{p.get('symbol')} {p.get('side')} #{p.get('id')}"
        for p in positions
    )


def _build_prompt(client, symbol: str, level: float, near: str, state: dict) -> str:
    now = datetime.now(timezone.utc)

    sym_d = client.get_symbol_details(symbol)
    pip = float(sym_d.get("pipSize") or 0.0001)
    bid = float(sym_d.get("bid") or level)
    ask = float(sym_d.get("ask") or level)

    from . import bars_cache
    d1_raw = bars_cache.get_bars(client, symbol, "d1", 30, 25).get("bars", [])

    h1_raw = client.call("get_trendbars", {
        "symbolName": symbol, "timeframe": "h1",
        "from": (now - timedelta(days=3)).isoformat(),
        "to": now.isoformat(), "limit": 72,
    }).get("bars", [])

    # D1 summary
    closes = [b["close"] for b in d1_raw]
    sma20 = _sma(closes, 20)
    atr = _atr14(d1_raw)
    atr_p = round(atr / pip)
    bias = ("bull" if closes[-1] > sma20 else "bear") if (sma20 and closes) else "flat"
    sma20_s = f"{sma20:.5f}" if sma20 else "n/a"
    last_close_s = f"{closes[-1]:.5f}" if closes else "n/a"

    # H1 last 12 bars
    h1_recent = h1_raw[-12:]
    h1_lines = []
    for b in h1_recent:
        t = (b.get("time") or "")[:16] or "?"
        o, h, lo, c = b["open"], b["high"], b["low"], b["close"]
        h1_lines.append(f"  {t}  O={o:.5f} H={h:.5f} L={lo:.5f} C={c:.5f}")

    if near == "resistance":
        broke = any(b["high"] > level for b in h1_recent)
        extreme = max((b["high"] for b in h1_recent), default=level)
    else:
        broke = any(b["low"] < level for b in h1_recent)
        extreme = min((b["low"] for b in h1_recent), default=level)
    extreme_s = f"{extreme:.5f}"

    # Account state
    bal = client.get_balance()
    balance = float(bal.get("balance", 0))
    equity = float(bal.get("equity", 0))
    dsb = state.get("day_start_balance") or balance
    daily_loss = dsb - equity
    fills = state.get("trades_taken_today", 0)
    poor = state.get("poor_outcomes_today", 0)
    frozen = "YES" if state.get("frozen") else "no"
    spread_p = round((ask - bid) / pip, 1)

    lines = [
        f"ALERT: {symbol} {near} at {level:.5f}",
        f"Current: bid={bid:.5f} ask={ask:.5f} spread={spread_p}p",
        f"",
        f"D1: bias={bias}, SMA20={sma20_s}, ATR14={atr_p}p, last_close={last_close_s}",
        f"",
        f"H1 last 12 bars (oldest first, most recent last):",
        *h1_lines,
        f"H1 broke level: {broke} | H1 extreme beyond level: {extreme_s}",
        f"",
        f"COT: {_cot_for(symbol)}",
        f"",
        _news_for(state, symbol),
        f"",
        f"Open positions: {_positions_for(state)}",
        f"Account: balance={balance:.2f} equity={equity:.2f} "
        f"daily_loss=${daily_loss:.2f} fills_today={fills}/5 "
        f"poor_outcomes={poor}/2 frozen={frozen}",
    ]
    return "\n".join(lines)


def _call_claude(prompt: str) -> dict:
    if not _HAS_ANTHROPIC:
        raise RuntimeError("anthropic package not installed")
    key = config.anthropic_api_key()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env")
    ac = _anthropic.Anthropic(api_key=key)

    import time as _time
    last_exc: Exception = RuntimeError("no attempts made")
    last_raw: str = ""
    for attempt in range(3):
        try:
            msg = ac.messages.create(
                model=_MODEL,
                max_tokens=_MAX_TOKENS,
                temperature=0,
                system=_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            if not msg.content:
                raise ValueError(f"no content blocks (stop_reason={msg.stop_reason})")
            last_raw = msg.content[0].text.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", last_raw)
            raw = re.sub(r"\s*```$", "", raw).strip()
            if not raw:
                raise ValueError(f"empty body after strip (stop_reason={msg.stop_reason})")
            return json.loads(raw)
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                _time.sleep(3 * (attempt + 1))

    # All 3 attempts failed — log for debugging and return conservative WAIT
    _log_brain_error(last_exc, last_raw, prompt)
    return {
        "verdict": "WAIT",
        "side": None, "entry": None, "stop": None, "target": None,
        "setup_type": None, "confidence": "low",
        "reasoning": f"Brain parse failed after 3 attempts: {str(last_exc)[:120]}",
    }


def _log_brain_error(exc: Exception, raw: str, prompt: str) -> None:
    import os
    log_path = os.path.expanduser("~/trading/brain_errors.log")
    try:
        from datetime import datetime as _dt
        ts = _dt.now().isoformat(timespec="seconds")
        with open(log_path, "a") as f:
            f.write(f"\n=== {ts} ===\n")
            f.write(f"ERROR: {exc}\n")
            f.write(f"RAW ({len(raw)} chars): {raw[:300]}\n")
            f.write(f"PROMPT (first 400 chars):\n{prompt[:400]}\n")
    except Exception:
        pass


def _do_execute(symbol: str, near: str, analysis: dict) -> dict | None:
    e = analysis.get("entry")
    s = analysis.get("stop")
    t = analysis.get("target")
    side = analysis.get("side")
    if not (e and s and t and side):
        return None
    # Use reduced risk after a poor outcome (rails enforce this anyway, but submitting
    # at the correct level avoids a noisy REFUSED log entry from rail_reduce_after_loss).
    state = state_mod.load()
    poor = state.get("poor_outcomes_today", 0)
    risk_pct = config.RISK_PCT_MIN if poor > 0 else config.RISK_PCT_MAX
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    proposal = {
        "symbol": symbol,
        "side": side,
        "entry": float(e),
        "stop": float(s),
        "target": float(t),
        "order_type": "market",
        "risk_pct": risk_pct,
        "setup_type": analysis.get("setup_type") or "",
        "confidence": analysis.get("confidence") or "medium",
        "signal_id": f"auto:{symbol}:{near}:{today}",
    }
    return execute_mod.execute(proposal)


def _pip_size(symbol: str) -> float:
    sym = symbol.upper()
    if "JPY" in sym:
        return 0.01
    if "XAU" in sym:
        return 0.01
    return 0.0001


def _send_telegram(symbol: str, level: float, near: str,
                   analysis: dict, execute_result: dict | None) -> None:
    verdict = analysis.get("verdict") or "SKIP"
    side = analysis.get("side") or ""
    reasoning = analysis.get("reasoning") or ""
    setup = analysis.get("setup_type") or ""
    confidence = analysis.get("confidence") or ""

    emoji = {"TAKE": "✅", "WAIT": "⏳", "SKIP": "❌"}.get(verdict, "🔔")
    side_s = f" {side.upper()}" if side else ""
    setup_label = {
        "break_retest": "Break-retest",
        "resistance_fade": "Resistance fade",
        "support_bounce": "Support bounce",
    }.get(setup, setup)

    lines = [
        f"🔔 <b>{telegram.esc(symbol)} {near} {telegram.code(f'{level:.5f}')} fired</b>",
        f"",
        f"{emoji} <b>{verdict}{side_s}</b>  {telegram.esc(setup_label)}  [{confidence}]",
        f"{telegram.esc(reasoning)}",
    ]

    if verdict == "TAKE" and execute_result:
        if execute_result.get("placed"):
            lines.append(
                f"\n📍 <b>Order sent</b> — {telegram.esc(execute_result.get('summary', ''))}"
            )
        elif execute_result.get("dry_run"):
            lines.append(
                f"\n🟡 <b>Dry-run</b> (disarmed) — {telegram.esc(execute_result.get('summary', ''))}"
            )
        elif execute_result.get("refused"):
            lines.append(
                f"\n⛔ <b>Rails refused</b>: {telegram.esc(execute_result.get('reason', ''))}"
            )

    e = analysis.get("entry")
    s = analysis.get("stop")
    t = analysis.get("target")
    if e and s and t and verdict in ("TAKE", "WAIT"):
        pip = _pip_size(symbol)
        sl_p = round(abs(e - s) / pip)
        tp_p = round(abs(t - e) / pip)
        rr = tp_p / sl_p if sl_p else 0.0
        e_s = f"{e:.5f}"
        s_s = f"{s:.5f} ({sl_p}p)"
        t_s = f"{t:.5f} ({tp_p}p · R:R {rr:.1f})"
        lines += [
            f"",
            f"  Entry  {telegram.code(e_s)}",
            f"  SL     {telegram.code(s_s)}",
            f"  TP     {telegram.code(t_s)}",
        ]

    telegram.send("\n".join(lines))


def analyze_and_notify(client, symbol: str, level: float, near: str) -> None:
    """Public entry point — always fail-safe."""
    try:
        _run(client, symbol, level, near)
    except Exception as e:
        try:
            telegram.send(
                f"🔔 <b>Alert: {telegram.esc(symbol)} {near} {level:.5f} fired</b>\n"
                f"⚠ Brain unavailable: {telegram.esc(str(e))}"
            )
        except Exception:
            pass


def _run(client, symbol: str, level: float, near: str) -> None:
    state = state_mod.load()
    prompt = _build_prompt(client, symbol, level, near, state)
    analysis = _call_claude(prompt)

    execute_result = None
    if analysis.get("verdict") == "TAKE":
        execute_result = _do_execute(symbol, near, analysis)

    _send_telegram(symbol, level, near, analysis, execute_result)
