"""Hard risk rails as deterministic gates. Pure logic over a pre-computed context dict
so every rail is unit-testable without touching the network.

gate(ctx) returns a list of RailResult. If ANY result.ok is False, the trade is refused.
The model cannot bypass these: execute.py calls gate() and will not place an order unless
every rail passes.
"""
from __future__ import annotations
from dataclasses import dataclass

from . import config


@dataclass
class RailResult:
    name: str
    ok: bool
    reason: str


def _ccy_exposure(symbol: str, side: str) -> dict:
    """Signed currency exposure for a 6-char FX symbol. buy=+base/-quote."""
    base, quote = symbol[:3].upper(), symbol[3:6].upper()
    sign = 1 if side.lower() == "buy" else -1
    return {base: sign, quote: -sign}


# ---- individual rails ----------------------------------------------------

def rail_not_frozen(ctx) -> RailResult:
    """Operational fail-closed gate: when the system is frozen (e.g. repeated cTrader
    unreachability, broker-state discrepancy), take NO new entries until cleared."""
    if ctx["state"].get("frozen"):
        return RailResult("not_frozen", False,
                          f"system FROZEN: {ctx['state'].get('frozen_reason', '')}")
    return RailResult("not_frozen", True, "ok")


def rail_no_duplicate_order(ctx) -> RailResult:
    """Reject duplicates: a same-symbol/same-side resting order within DUP_ENTRY_PIPS of this
    entry, or a signal id already executed today (overlapping/retried runs)."""
    p, pip = ctx["proposal"], ctx["market"]["pip_size"]
    entry = p.get("entry")
    for o in ctx["state"].get("pending_orders", []):
        if o.get("symbol") == p["symbol"] and o.get("side") == p["side"]:
            op = o.get("target_price")
            if entry and op and abs(entry - op) / pip <= config.DUP_ENTRY_PIPS:
                return RailResult("no_duplicate_order", False,
                                  f"similar pending {p['symbol']} {p['side']} already at {op}")
    sid = p.get("signal_id")
    if sid and sid in ctx["state"].get("executed_signals", []):
        return RailResult("no_duplicate_order", False, f"signal {sid} already executed today")
    return RailResult("no_duplicate_order", True, "ok")


def rail_target_reached(ctx) -> RailResult:
    """Once the phase profit target is reached, take no new risk — protect the pass."""
    if not config.FTMO_STOP_AT_TARGET:
        return RailResult("target_reached", True, "ok")
    profit = ctx.get("equity", 0.0) - config.FTMO_INITIAL_BALANCE
    if profit >= config.FTMO_PROFIT_TARGET_USD:
        return RailResult("target_reached", False,
                          f"profit ${profit:.0f} ≥ target ${config.FTMO_PROFIT_TARGET_USD:.0f} "
                          f"— protect the pass, no new trades")
    return RailResult("target_reached", True, "ok")


def rail_reachable(ctx) -> RailResult:
    ok = bool(ctx["market"].get("reachable") and ctx["market"].get("authenticated"))
    return RailResult("ctrader_reachable", ok,
                      "ok" if ok else "cTrader unreachable or not authenticated")


def rail_kill_switch(ctx) -> RailResult:
    if ctx["state"].get("daily_limit_hit"):
        return RailResult("kill_switch", False, "daily -2% kill-switch already tripped today")
    # Tripped if current daily loss already at/under the daily floor.
    room = ctx["sizing"]["buffers"].daily_room_now
    ok = room > 0
    return RailResult("kill_switch", ok,
                      "ok" if ok else f"already at/over daily loss limit (room=${room:.2f})")


def rail_per_trade_risk(ctx) -> RailResult:
    r = ctx["proposal"]["risk_pct"]
    ok = config.RISK_PCT_MIN <= r <= config.RISK_PCT_MAX
    return RailResult("per_trade_risk", ok,
                      "ok" if ok else f"risk {r}% outside [{config.RISK_PCT_MIN}, {config.RISK_PCT_MAX}]%")


def rail_sl_present(ctx) -> RailResult:
    p = ctx["proposal"]
    ok = p.get("stop") is not None and ctx["sizing"]["stop_pips"] > 0
    return RailResult("stop_loss_required", ok,
                      "ok" if ok else "no stop loss / zero stop distance")


def rail_min_size(ctx) -> RailResult:
    ok = ctx["sizing"]["units"] >= ctx["market"]["min_volume"]
    return RailResult("min_broker_size", ok, "ok" if ok else
                      "risk-correct size is below broker minimum (would force oversizing)")


def rail_rr(ctx) -> RailResult:
    rr = ctx["sizing"]["rr"]
    ok = rr >= config.MIN_RR
    return RailResult("reward_risk", ok, "ok" if ok else f"R:R {rr:.2f} < {config.MIN_RR}")


def rail_trade_count(ctx) -> RailResult:
    n = ctx["state"].get("trades_taken_today", 0)
    ok = n < config.MAX_TRADES_PER_DAY
    return RailResult("max_trades_per_day", ok, "ok" if ok else
                      f"already {n} trades today (max {config.MAX_TRADES_PER_DAY})")


def rail_poor_outcomes(ctx) -> RailResult:
    n = ctx["state"].get("poor_outcomes_today", 0)
    ok = n < config.MAX_POOR_OUTCOMES
    return RailResult("poor_outcomes", ok, "ok" if ok else
                      f"{n} poor outcomes today (stop after {config.MAX_POOR_OUTCOMES})")


def rail_reduce_after_loss(ctx) -> RailResult:
    """After any poor outcome today, risk must be cut to the floor (0.25%)."""
    poor = ctx["state"].get("poor_outcomes_today", 0)
    if poor >= 1 and ctx["proposal"]["risk_pct"] > config.RISK_PCT_MIN + 1e-9:
        return RailResult("reduce_after_loss", False,
                          f"after {poor} poor outcome(s), risk must be ≤ {config.RISK_PCT_MIN}%")
    return RailResult("reduce_after_loss", True, "ok")


def rail_daily_buffer(ctx) -> RailResult:
    after = ctx["sizing"]["buffers"].daily_room_after
    ok = after >= 0
    return RailResult("daily_buffer_after", ok, "ok" if ok else
                      f"worst case breaches -2% daily floor (room after=${after:.2f})")


def rail_overall_buffer(ctx) -> RailResult:
    after = ctx["sizing"]["buffers"].overall_room_after
    ok = after >= 0
    return RailResult("overall_buffer_after", ok, "ok" if ok else
                      f"worst case breaches overall max-loss floor (room after=${after:.2f})")


def rail_aggregate_risk(ctx) -> RailResult:
    total = (ctx["sizing"]["open_risk_pct"] + ctx["sizing"].get("pending_risk_pct", 0.0)
             + ctx["proposal"]["risk_pct"])
    ok = total <= config.AGG_RISK_PCT_MAX + 1e-9
    return RailResult("aggregate_risk", ok, "ok" if ok else
                      f"aggregate (open+pending+new) risk {total:.2f}% > {config.AGG_RISK_PCT_MAX}%")


def rail_no_correlated_opposing(ctx) -> RailResult:
    """Block direct hedges and correlated opposing exposure (FTMO compliance + risk)."""
    cand = _ccy_exposure(ctx["proposal"]["symbol"], ctx["proposal"]["side"])
    net: dict[str, int] = {}
    for pos in ctx["state"].get("open_positions", []):
        sym, side = pos.get("symbol", ""), pos.get("side", "")
        if len(sym) < 6 or side not in ("buy", "sell"):
            continue
        for ccy, sgn in _ccy_exposure(sym, side).items():
            net[ccy] = net.get(ccy, 0) + sgn
    for ccy, sgn in cand.items():
        existing = net.get(ccy, 0)
        if existing != 0 and (existing > 0) != (sgn > 0):
            return RailResult("no_correlated_opposing", False,
                              f"opposing exposure on {ccy} vs open positions")
    return RailResult("no_correlated_opposing", True, "ok")


def rail_currency_concentration(ctx) -> RailResult:
    """Don't over-concentrate: cap how many open positions push the same currency the same way
    (e.g. three simultaneous long-USD trades is one bet, not three)."""
    cand = _ccy_exposure(ctx["proposal"]["symbol"], ctx["proposal"]["side"])
    counts: dict[str, int] = {}
    for pos in ctx["state"].get("open_positions", []):
        sym, side = pos.get("symbol", ""), pos.get("side", "")
        if len(sym) < 6 or side not in ("buy", "sell"):
            continue
        for ccy, sgn in _ccy_exposure(sym, side).items():
            if ccy in cand and (cand[ccy] > 0) == (sgn > 0):  # same currency, same direction
                counts[ccy] = counts.get(ccy, 0) + 1
    for ccy, n in counts.items():
        if n + 1 > config.MAX_SAME_CCY_POSITIONS:
            return RailResult("currency_concentration", False,
                              f"would be {n + 1} positions same-way on {ccy} "
                              f"(max {config.MAX_SAME_CCY_POSITIONS})")
    return RailResult("currency_concentration", True, "ok")


def rail_news_blackout(ctx) -> RailResult:
    nb = ctx["news"]
    if not nb.get("fresh"):
        return RailResult("news_blackout", False,
                          "news windows missing/stale for today -> fail-safe block")
    if nb.get("in_window"):
        return RailResult("news_blackout", False,
                          f"within +/-{config.NEWS_WINDOW_MIN}min of HIGH event: {nb.get('event')}")
    if nb.get("cb_hold"):
        return RailResult("news_blackout", False,
                          f"central-bank rate decision affects {nb.get('event')}")
    return RailResult("news_blackout", True, "ok")


def rail_ftmo_hard_floor(ctx) -> RailResult:
    """Absolute backstop: worst case must never touch FTMO's real $ limits, independent
    of the percentage buffer math. Floor and daily limit come from the confirmed dashboard."""
    f = ctx.get("ftmo")
    if not f:
        return RailResult("ftmo_hard_floor", True, "ok (no ftmo limits in context)")
    if f["equity_after_worst"] < f["overall_floor"]:
        return RailResult("ftmo_hard_floor", False,
                          f"worst case breaches FTMO overall floor ${f['overall_floor']:.0f}")
    if f["daily_loss_after"] > f["daily_limit"]:
        return RailResult("ftmo_hard_floor", False,
                          f"worst case breaches FTMO daily loss limit ${f['daily_limit']:.0f}")
    return RailResult("ftmo_hard_floor", True, "ok")


def rail_spread_quote(ctx) -> RailResult:
    m = ctx["market"]
    typical = m.get("typical_spread") or 0
    spread = m.get("spread") or 0
    if typical > 0 and spread > typical * config.SPREAD_SPIKE_MULT:
        return RailResult("spread_quote", False,
                          f"spread spiked ({spread} > {typical}x{config.SPREAD_SPIKE_MULT})")
    if m.get("quote_age_sec", 0) > config.QUOTE_MAX_AGE_SEC:
        return RailResult("spread_quote", False,
                          f"stale quote ({m.get('quote_age_sec')}s)")
    return RailResult("spread_quote", True, "ok")


ALL_RAILS = [
    rail_not_frozen, rail_no_duplicate_order, rail_target_reached,
    rail_reachable, rail_kill_switch, rail_per_trade_risk, rail_sl_present,
    rail_min_size, rail_rr, rail_trade_count, rail_poor_outcomes, rail_reduce_after_loss,
    rail_daily_buffer, rail_overall_buffer, rail_aggregate_risk,
    rail_no_correlated_opposing, rail_currency_concentration, rail_news_blackout,
    rail_spread_quote, rail_ftmo_hard_floor,
]


def gate(ctx) -> list[RailResult]:
    return [rail(ctx) for rail in ALL_RAILS]


def failures(results: list[RailResult]) -> list[RailResult]:
    return [r for r in results if not r.ok]


def passed(results: list[RailResult]) -> bool:
    return all(r.ok for r in results)
