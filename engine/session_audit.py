"""Auto session audit — calls Claude API with full market context for all 17 pairs.

Flow:
  1. D1 summary for all watchlist pairs (bias/ATR/proximity to levels)
  2. H1 bars for scanner candidates only
  3. Claude returns a ranked list of setups (TAKE / WAIT / SKIP)
  4. TAKE setups → execute.execute(); all shortlisted → shadow log
  5. Telegram summary of what was found / placed / skipped

One audit call costs ~$0.02-0.04 in API tokens. Runs 3×/day via launchd.
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
from . import shadow as shadow_mod
from . import state as state_mod

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 2048

_SYSTEM = """\
You are the trading brain for an FTMO Challenge account ($10,000 USD, Phase 1).
Strategy: find at most 1-2 clean setups per session from key support/resistance levels.
Quality over quantity — SKIP is the default outcome. A forced trade is worse than no trade.

Setup types allowed:
  break_retest      — price broke through a level, retesting from the other side
  resistance_fade   — price at D1 resistance, bias bear → SELL
  support_bounce    — price at D1 support, bias bull → BUY
  daily_level_rejection — strong D1 level rejection with H1 confirmation
  trend_continuation    — pullback into moving average in trending market

For each pair you analyze, provide a one-line read. For your shortlist (≤3 best candidates),
provide full setup parameters.

Rules:
  1. D1 bias alignment required (bull → buy, bear → sell; counter-trend needs ≥2 extra confluences)
  2. H1 confirmation: rejection candle or closed-beyond-level for break-retest
  3. R:R ≥ 1.5 (prefer ≥ 2.0)
  4. Stop ≥ 10 pips
  5. No entry inside a HIGH/CB news window; avoid pairs with CB event upcoming
  6. COT: skip if pair currency is at crowded extreme AGAINST the trade direction
  7. Portfolio: skip if already 2 open positions share the base or quote currency
  8. Prefer limit orders at planned levels (order_type: "limit") over market orders
  9. Only include in "setups" pairs you genuinely evaluated at the level — not all 17

Respond ONLY with valid JSON, no markdown:
{
  "summary": "<one sentence on today's market character>",
  "pair_reads": {
    "EURUSD": "<one line>",
    "GBPUSD": "<one line>",
    ... (all 17 pairs)
  },
  "setups": [
    {
      "symbol": "GBPUSD",
      "verdict": "TAKE" | "WAIT" | "SKIP",
      "side": "buy" | "sell",
      "order_type": "market" | "limit",
      "entry": <float>,
      "stop": <float>,
      "target": <float>,
      "setup_type": "<type>",
      "regime": "trend" | "range",
      "confidence": <0-100>,
      "rationale": "<1-2 sentences>",
      "confluences": ["<item>", "<item>"]
    }
  ]
}
The "setups" array should only contain your ≤3 SERIOUSLY EVALUATED shortlist candidates.
Pairs that are clearly mid-range with no story do NOT go into setups."""


def _sma(vals: list, n: int) -> float | None:
    return sum(vals[-n:]) / n if len(vals) >= n else None


def _atr14(bars: list) -> float:
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-14:]) / min(len(trs), 14) if trs else 0.0


def _d1_summaries(client, symbols: list[str]) -> dict[str, str]:
    """Fetch D1 data for all pairs and return compact summaries."""
    now = datetime.now(timezone.utc)
    out = {}
    for sym in symbols:
        try:
            d = client.get_symbol_details(sym)
            pip = float(d.get("pipSize") or 0.0001)
            bid = float(d.get("bid") or 0)
            from . import bars_cache
            bars = bars_cache.get_bars(client, sym, "d1", 35, 25).get("bars", [])
            closes = [b["close"] for b in bars]
            sma20 = _sma(closes, 20)
            atr = _atr14(bars)
            atr_p = round(atr / pip)
            bias = ("bull" if closes[-1] > sma20 else "bear") if (sma20 and closes) else "flat"
            last = closes[-1] if closes else 0
            sma_s = f"{sma20:.5f}" if sma20 else "n/a"
            dist_p = round(abs(last - sma20) / pip) if sma20 else 0
            out[sym] = (f"bias={bias}, price={last:.5f}, SMA20={sma_s} ({dist_p}p away), "
                        f"ATR14={atr_p}p, bid={bid:.5f}")
        except Exception as e:
            out[sym] = f"data unavailable: {e}"
    return out


def _h1_bars_for(client, symbols: list[str]) -> dict[str, str]:
    """Fetch H1 last 12 bars for a subset of symbols (candidates only)."""
    now = datetime.now(timezone.utc)
    out = {}
    for sym in symbols:
        try:
            d = client.get_symbol_details(sym)
            pip = float(d.get("pipSize") or 0.0001)
            bars = client.call("get_trendbars", {
                "symbolName": sym, "timeframe": "h1",
                "from": (now - timedelta(days=3)).isoformat(),
                "to": now.isoformat(), "limit": 48,
            }).get("bars", [])
            recent = bars[-12:]
            lines = []
            for b in recent:
                t = (b.get("time") or "")[:16] or "?"
                lines.append(f"  {t} O={b['open']:.5f} H={b['high']:.5f} L={b['low']:.5f} C={b['close']:.5f}")
            out[sym] = "\n".join(lines) if lines else "  no bars"
        except Exception as e:
            out[sym] = f"  unavailable: {e}"
    return out


def _cot_block() -> str:
    bias = cot_mod.load_bias()
    if not bias:
        return "COT data unavailable"
    order = ["DXY", "EUR", "GBP", "JPY", "AUD", "CAD", "NZD", "CHF"]
    lines = []
    for ccy in order:
        d = bias.get(ccy)
        if d:
            sig = d["signal"].replace("_", " ")
            lines.append(f"  {ccy}: net {d['net']/1000:+.1f}k ({d['percentile']:.0f}th pctl, {sig})")
    return "\n".join(lines) if lines else "no COT data"


def _news_block(state: dict) -> str:
    now = datetime.now(timezone.utc)
    if not state_mod.news_windows_fresh(state):
        return "⚠ NEWS WINDOWS STALE — morning brief may not have run"
    windows = state.get("news_windows", [])
    if not windows:
        return "No HIGH-impact events today"
    lines = []
    for w in windows:
        try:
            start = datetime.fromisoformat(w["start_iso"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(w["end_iso"].replace("Z", "+00:00"))
            active = "ACTIVE NOW" if start <= now <= end else ""
            lines.append(f"  {w['ccy']} {w['event'][:35]} {w['start_iso'][11:16]}Z-{w['end_iso'][11:16]}Z {active}".strip())
        except Exception:
            pass
    return "\n".join(lines) if lines else "none"


def _positions_block(state: dict) -> str:
    positions = state.get("open_positions", [])
    if not positions:
        return "none"
    return ", ".join(f"{p.get('symbol')} {p.get('side')} #{p.get('id')}" for p in positions)


def _build_prompt(client, state: dict, snap: dict) -> str:
    now_s = state_mod.now_dubai().strftime("%H:%M Dubai, %a %d %b %Y")
    candidates = state.get("candidates", [])
    cand_syms = [c.get("symbol") for c in candidates if c.get("symbol")]

    d1 = _d1_summaries(client, config.WATCHLIST)
    h1 = _h1_bars_for(client, cand_syms) if cand_syms else {}

    # Shadow stats brief
    try:
        from . import shadow as _sh
        ss = _sh.summary()
        n = ss.get("graded", 0)
        edge = ss.get("edge_pct")
        edge_s = f"{edge:.1f}%" if edge is not None else "n/a"
        shadow_s = f"n={n} graded, edge={edge_s} {'(insufficient sample)' if n < 30 else ''}"
    except Exception:
        shadow_s = "unavailable"

    # D1 block
    d1_lines = [f"  {sym}: {summary}" for sym, summary in d1.items()]

    # Candidates block
    if candidates:
        cand_lines = []
        for c in candidates:
            cand_lines.append(
                f"  {c.get('symbol')}: near {c.get('near')} {c.get('level', '')} "
                f"({c.get('regime', '')} regime, {c.get('bias', '')} bias) — {c.get('note', '')}"
            )
        cand_block = "\n".join(cand_lines)
    else:
        cand_block = "  (no scanner candidates flagged at last scan)"

    # H1 block for candidates
    h1_block_parts = []
    for sym, bars_text in h1.items():
        level_info = next((c for c in candidates if c.get("symbol") == sym), {})
        level_s = f"near {level_info.get('near', '')} {level_info.get('level', '')}"
        h1_block_parts.append(f"{sym} ({level_s}):\n{bars_text}")
    h1_block = "\n\n".join(h1_block_parts) if h1_block_parts else "  (no candidates — no H1 data fetched)"

    bal_s = f"{snap['balance']:.2f}"
    eq_s = f"{snap['equity']:.2f}"
    pnl_s = f"{snap['daily_pnl']:+.2f}"
    fills = snap["trades_today"]
    poor = snap["poor_outcomes"]

    lines = [
        f"SESSION AUDIT — {now_s}",
        f"",
        f"ACCOUNT: balance={bal_s} equity={eq_s} daily_pnl=${pnl_s} fills={fills}/5 poor={poor}/2 frozen={'YES' if snap['frozen'] else 'no'}",
        f"SHADOW STATS: {shadow_s}",
        f"",
        f"COT MACRO BIAS (CFTC Leveraged Money, 52-week percentile):",
        _cot_block(),
        f"",
        f"TODAY'S NEWS WINDOWS:",
        _news_block(state),
        f"",
        f"OPEN POSITIONS: {_positions_block(state)}",
        f"",
        f"SCANNER CANDIDATES (analyze these FIRST — bot's hand-off to you):",
        cand_block,
        f"",
        f"D1 DATA — all 17 pairs:",
        *d1_lines,
        f"",
        f"H1 DATA — candidates only (last 12 bars):",
        h1_block,
    ]
    return "\n".join(lines)


def _call_claude(prompt: str) -> dict:
    if not _HAS_ANTHROPIC:
        raise RuntimeError("anthropic package not installed")
    key = config.anthropic_api_key()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env")
    ac = _anthropic.Anthropic(api_key=key)
    msg = ac.messages.create(
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        temperature=0,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def _do_execute(setup: dict) -> dict | None:
    e = setup.get("entry")
    s = setup.get("stop")
    t = setup.get("target")
    side = setup.get("side")
    sym = setup.get("symbol")
    if not (e and s and t and side and sym):
        return None
    state = state_mod.load()
    poor = state.get("poor_outcomes_today", 0)
    risk_pct = config.RISK_PCT_MIN if poor > 0 else config.RISK_PCT_MAX
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    session_label = state_mod.now_dubai().strftime("%H%M")
    proposal = {
        "symbol": sym,
        "side": side,
        "entry": float(e),
        "stop": float(s),
        "target": float(t),
        "order_type": setup.get("order_type", "market"),
        "risk_pct": risk_pct,
        "setup_type": setup.get("setup_type") or "",
        "regime": setup.get("regime") or "",
        "confidence": setup.get("confidence") or 50,
        "signal_id": f"session:{sym}:{today}:{session_label}",
    }
    return execute_mod.execute(proposal)


def _log_shadow(setup: dict) -> None:
    try:
        payload = {
            "symbol": setup.get("symbol"),
            "side": setup.get("side"),
            "entry": setup.get("entry"),
            "stop": setup.get("stop"),
            "target": setup.get("target"),
            "setup_type": setup.get("setup_type") or "",
            "confidence": setup.get("confidence") or 50,
            "verdict": "take" if setup.get("verdict") == "TAKE" else "skip",
            "rationale": setup.get("rationale") or "",
        }
        shadow_mod.log(payload)
    except Exception:
        pass


def run(client, state: dict, snap: dict) -> None:
    """Run a full session audit. snap is from _account_snapshot()."""
    # Skip entirely if hard limits are hit
    if snap.get("daily_limit_hit"):
        telegram.send("📊 <b>Session audit</b> — kill-switch HIT, no new trades today.")
        return
    if snap.get("trades_today", 0) >= config.MAX_TRADES_PER_DAY:
        telegram.send(f"📊 <b>Session audit</b> — {config.MAX_TRADES_PER_DAY} fills reached, no new trades.")
        return
    if snap.get("poor_outcomes", 0) >= config.MAX_POOR_OUTCOMES:
        telegram.send(f"📊 <b>Session audit</b> — {config.MAX_POOR_OUTCOMES} poor outcomes, no new trades.")
        return

    prompt = _build_prompt(client, state, snap)
    analysis = _call_claude(prompt)

    summary = analysis.get("summary", "")
    setups = analysis.get("setups", [])
    pair_reads = analysis.get("pair_reads", {})

    # Execute TAKEs + log all shadows
    results = []
    for setup in setups:
        _log_shadow(setup)
        if setup.get("verdict") == "TAKE":
            ex = _do_execute(setup)
            results.append((setup, ex))
        elif setup.get("verdict") == "WAIT":
            results.append((setup, None))

    # Telegram summary
    now_s = state_mod.now_dubai().strftime("%H:%M")
    lines = [f"📊 <b>Session audit — {now_s} Dubai</b>"]
    if summary:
        lines.append(f"{telegram.esc(summary)}")
    lines.append("")

    if not setups:
        lines.append("⏸ No setup — waiting")
        if pair_reads:
            # Show a brief summary of flagged pairs (ones that aren't just "mid-range")
            notable = [(sym, read) for sym, read in pair_reads.items()
                       if any(w in read.lower() for w in ("near", "level", "watch", "setup", "break", "touch"))]
            if notable:
                lines.append("")
                lines.append("Watching:")
                for sym, read in notable[:5]:
                    lines.append(f"  {telegram.code(sym)}: {telegram.esc(read[:60])}")
    else:
        for setup, ex in results:
            verdict = setup.get("verdict", "SKIP")
            sym = setup.get("symbol", "")
            side = setup.get("side", "")
            conf = setup.get("confidence", 0)
            rationale = setup.get("rationale", "")
            emoji = {"TAKE": "✅", "WAIT": "⏳", "SKIP": "❌"}.get(verdict, "•")
            side_s = f" {side.upper()}" if side else ""
            conf_s = f" [{conf}%]" if conf else ""
            lines.append(f"{emoji} <b>{telegram.esc(sym)}{side_s}</b>{conf_s}")
            if rationale:
                lines.append(f"   {telegram.esc(rationale[:80])}")
            if ex:
                if ex.get("placed"):
                    lines.append(f"   📍 {telegram.esc(ex.get('summary', ''))}")
                elif ex.get("dry_run"):
                    lines.append(f"   🟡 dry-run: {telegram.esc(ex.get('summary', ''))}")
                elif ex.get("refused"):
                    lines.append(f"   ⛔ refused: {telegram.esc(ex.get('reason', '')[:60])}")

    takes = sum(1 for s, _ in results if s.get("verdict") == "TAKE")
    skips = sum(1 for s in setups if s.get("verdict") == "SKIP")
    if setups:
        lines.append(f"\n<i>{takes} executed · {skips} skipped · {len(setups)} shadows logged</i>")

    telegram.send("\n".join(lines))
