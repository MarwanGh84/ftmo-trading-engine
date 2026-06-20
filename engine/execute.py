"""Trade orchestration: the ONLY path that can place an order.

Flow: reconcile -> build market+sizing context -> rails.gate -> (if armed & passed)
place atomic bracket order (entry + SL + TP) -> update state + journal -> report.
If any rail fails, the trade is refused with the specific reason and alerted.

The scheduled Claude run cannot place orders directly (permission-denied on cTrader
write tools); it can only hand a proposal dict to this module.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta

from . import config, risk, rails, news, telegram, sheets
from . import state as state_mod
from .mcp_client import McpClient
from .reconcile import reconcile


def _journal(line: str) -> None:
    with open(config.JOURNAL_FILE, "a") as f:
        f.write(f"\n### {datetime.now(state_mod.TZ).isoformat()}\n{line}\n")


def report_closures(closures: list[dict]) -> None:
    """Telegram + journal + Sheets a row for each position that closed since last run."""
    for c in closures:
        net = c.get("net")
        net_s = f"${net:.2f}" if net is not None else "?"
        tag = " (counts as poor outcome)" if c.get("poor") else ""
        telegram.send(f"📕 Closed {c.get('symbol')} #{c.get('id')} — {c.get('result')} {net_s}{tag}")
        _journal(f"CLOSED {c.get('symbol')} #{c.get('id')} {c.get('result')} net {net_s} poor={c.get('poor')}")
        sheets.append_trade([datetime.now(state_mod.TZ).strftime("%Y-%m-%d %H:%M"),
                             c.get("symbol"), "", "close", "", "", "", "", "", "", "",
                             f"{net:.2f}" if net is not None else "",
                             f"CLOSED-{c.get('result')}", "poor outcome" if c.get("poor") else "win"])


def _sheet_trade(proposal: dict, status: str, detail: str, ctx: dict | None = None) -> None:
    """Append one row to the Trades tab for any terminal outcome (fail-safe)."""
    ts = datetime.now(state_mod.TZ).strftime("%Y-%m-%d %H:%M")
    p = (ctx or {}).get("proposal", proposal)
    s = (ctx or {}).get("sizing")
    bal = (ctx or {}).get("balance")
    if s:
        risk_d = risk.risk_dollars(bal, p["risk_pct"]) if bal else ""
        row = [ts, p.get("symbol"), p.get("side"), p.get("order_type", "market"),
               f"{s['units']:.0f}", f"{s['lots']:.2f}", f"{s['stop_pips']:.1f}",
               f"{risk.stop_pips(p['entry'], p['target'], ctx['market']['pip_size']):.1f}",
               f"{risk_d:.2f}" if risk_d != "" else "", p.get("risk_pct"),
               f"{s['rr']:.2f}", f"{s['worst_case']:.2f}", status, detail]
    else:
        row = [ts, proposal.get("symbol"), proposal.get("side"),
               proposal.get("order_type", "market"), "", "", "", "", "",
               proposal.get("risk_pct"), "", "", status, detail]
    sheets.append_trade(row)


def _quote_to_usd(client: McpClient, quote_ccy: str, cache: dict) -> float | None:
    """Convert one unit of `quote_ccy` into USD using live cTrader quotes."""
    quote_ccy = quote_ccy.upper()
    if quote_ccy == "USD":
        return 1.0
    if quote_ccy in cache:
        return cache[quote_ccy]
    # Try QUOTEUSD (e.g. GBPUSD) then USDQUOTE (e.g. USDJPY -> 1/price).
    # Use get_symbol_details (returns bid/ask even when the symbol isn't subscribed in
    # the market watch) rather than get_spot_prices, which 404s on unsubscribed symbols.
    for sym, invert in ((f"{quote_ccy}USD", False), (f"USD{quote_ccy}", True)):
        try:
            d = client.get_symbol_details(sym)
            mid = (float(d["bid"]) + float(d["ask"])) / 2
            val = (1.0 / mid) if invert else mid
            cache[quote_ccy] = val
            return val
        except Exception:
            continue
    return None


def _open_risk_pct(client: McpClient, positions: list[dict], balance: float,
                   sym_cache: dict, fx_cache: dict) -> float:
    """Aggregate worst-case risk of currently open positions, as % of balance."""
    total = 0.0
    for p in positions:
        sym, units, sl_pips = p.get("symbol", ""), p.get("volume_units"), p.get("sl_pips")
        if not sym or not units or not sl_pips:
            continue  # a position without a stop is handled by management, not sizing
        d = sym_cache.get(sym) or client.get_symbol_details(sym)
        sym_cache[sym] = d
        q2u = _quote_to_usd(client, sym[3:6], fx_cache) or 1.0
        pv = risk.pip_value_per_lot_usd(d["pipSize"], d["lotSize"], q2u)
        lots = units / d["lotSize"]
        total += lots * float(sl_pips) * pv
    return (total / balance * 100.0) if balance else 0.0


def build_context(client: McpClient, proposal: dict, state: dict) -> dict:
    """Assemble the full decision context (market + sizing + state + news)."""
    bal = client.get_balance()
    balance, equity = float(bal["balance"]), float(bal["equity"])
    authed = bal.get("connectionState") == "Authenticated"

    sym = proposal["symbol"].upper()
    d = client.get_symbol_details(sym)
    pip_size, lot_size = d["pipSize"], d["lotSize"]
    min_vol, vol_step = d["minVolume"], d["volumeStep"]
    # A symbol not subscribed in the market watch (or a stale feed) returns bid/ask=None.
    # Fall back to the last bar close for sizing, but mark the quote stale so the sanity rail
    # REFUSES the trade — never trade without a live quote.
    quote_stale = d.get("bid") is None or d.get("ask") is None
    if quote_stale:
        try:
            from datetime import timedelta as _td
            _to = datetime.now(timezone.utc)
            bars = client.call("get_trendbars", {"symbolName": sym, "timeframe": "h1",
                               "from": (_to - _td(days=3)).isoformat(), "to": _to.isoformat(),
                               "limit": 1}).get("bars", [])
            last = float(bars[-1]["close"]) if bars else None
        except Exception:
            last = None
        if last is None:
            raise RuntimeError(f"no live quote and no fallback price for {sym}")
        bid = ask = last
    else:
        bid, ask = float(d["bid"]), float(d["ask"])

    side = proposal["side"].lower()
    if proposal.get("order_type", "market") == "market":
        entry = ask if side == "buy" else bid
    else:
        entry = float(proposal["entry"])
    stop, target = float(proposal["stop"]), float(proposal["target"])

    fx_cache, sym_cache = {}, {sym: d}
    q2u = _quote_to_usd(client, sym[3:6], fx_cache)
    if q2u is None:
        raise RuntimeError(f"cannot determine {sym[3:6]}->USD conversion")
    pv_lot = risk.pip_value_per_lot_usd(pip_size, lot_size, q2u)

    sp = risk.stop_pips(entry, stop, pip_size)
    risk_d = risk.risk_dollars(balance, proposal["risk_pct"])
    lots = risk.lots_for_risk(risk_d, sp, pv_lot) if sp > 0 else 0.0
    units = risk.units_from_lots(lots, lot_size, min_vol, vol_step)
    rr = risk.reward_risk(entry, stop, target) if entry != stop else 0.0

    spread_pips = (ask - bid) / pip_size
    spread_cost = (units / lot_size) * spread_pips * pv_lot
    # Assume the stop fills a fixed number of pips worse than its price (slippage is real,
    # esp. post-news — today's GBPUSD lost −$56 vs a −$50 plan). Folded into worst-case so the
    # floor/buffer rails stay conservative; it does NOT change the (risk-correct) size above.
    slippage_cost = (units / lot_size) * config.STOP_SLIPPAGE_PIPS * pv_lot
    worst = risk.worst_case_usd(units, sp, pv_lot, lot_size,
                                spread_cost=spread_cost, slippage=slippage_cost)

    open_risk = _open_risk_pct(client, state.get("open_positions", []), balance,
                               sym_cache, fx_cache)
    # Pending resting orders also consume the risk budget — if several fill together they could
    # exceed the 1% aggregate cap, so count them now (open + pending + candidate must stay ≤ 1%).
    pending_risk = _open_risk_pct(client, state.get("pending_orders", []), balance,
                                  sym_cache, fx_cache)

    buf = risk.buffers(
        balance=balance, equity=equity,
        day_start_balance=state.get("day_start_balance") or max(balance, equity),
        account_baseline=state.get("account_baseline") or max(balance, equity),
        daily_loss_limit_pct=config.DAILY_LOSS_LIMIT_PCT,
        overall_loss_limit_pct=config.FTMO_MAX_LOSS_LIMIT_PCT,
        candidate_worst_case=worst,
    )

    now_utc = datetime.now(timezone.utc)
    nb = news.evaluate(state, sym, now_utc)

    ftmo_day = state_mod.now_ftmo().date().isoformat()
    signal_id = proposal.get("signal_id") or f"{ftmo_day}:{sym}:{side}:{round(entry, 4)}"

    dsb = state.get("day_start_balance") or max(balance, equity)
    ftmo = {
        "overall_floor": config.FTMO_OVERALL_FLOOR_USD,
        "equity_after_worst": equity - worst,
        "daily_loss_after": (dsb - equity) + worst,   # projected daily loss from day start
        "daily_limit": config.FTMO_DAILY_LOSS_USD,
    }

    return {
        "proposal": {**proposal, "symbol": sym, "side": side, "entry": entry,
                     "stop": stop, "target": target, "signal_id": signal_id},
        "sizing": {"units": units, "lots": units / lot_size, "stop_pips": sp, "rr": rr,
                   "worst_case": worst, "pip_value_lot_usd": pv_lot,
                   "open_risk_pct": open_risk, "pending_risk_pct": pending_risk, "buffers": buf},
        "market": {"reachable": True, "authenticated": authed,
                   "spread": spread_pips, "typical_spread": config.TYPICAL_SPREADS.get(sym),
                   "quote_age_sec": 999 if quote_stale else 0, "min_volume": min_vol,
                   "bid": bid, "ask": ask, "lot_size": lot_size, "pip_size": pip_size},
        "state": state,
        "news": nb,
        "ftmo": ftmo,
        "balance": balance, "equity": equity,
    }


def execute(proposal: dict) -> dict:
    """Validate and (if armed) place a trade. Returns a result report dict."""
    client = McpClient()
    state = state_mod.load()
    report = {"proposal": proposal, "placed": False, "armed": config.is_armed()}

    # 1. Reachability + reconcile (cTrader is source of truth).
    try:
        bal = client.get_balance()
    except Exception as e:
        msg = f"⚠️ cTrader unreachable — no trade. {e}"
        telegram.send(msg)
        _sheet_trade(proposal, "ABORTED", msg)
        return {**report, "refused": True, "reason": msg}

    state_mod.apply_daily_reset(state, float(bal["balance"]), float(bal["equity"]))
    disc = reconcile(state, client)
    report_closures(disc.get("closures", []))
    if disc["changed"]:
        telegram.send(f"🔄 State/cTrader discrepancy reconciled: {disc['summary']}")

    # 2. Build context + run the gate.
    try:
        ctx = build_context(client, proposal, state)
    except Exception as e:
        state_mod.save(state)
        msg = f"⚠️ Could not build trade context — no trade. {e}"
        telegram.send(msg)
        _sheet_trade(proposal, "ABORTED", msg)
        return {**report, "refused": True, "reason": msg}

    results = rails.gate(ctx)
    report["rails"] = [{"name": r.name, "ok": r.ok, "reason": r.reason} for r in results]
    s = ctx["sizing"]

    if not rails.passed(results):
        reasons = "; ".join(f"{r.name}: {r.reason}" for r in rails.failures(results))
        state_mod.save(state)
        telegram.send(f"⛔ Trade refused {ctx['proposal']['symbol']} "
                      f"{ctx['proposal']['side']} — {reasons}")
        _journal(f"REFUSED {ctx['proposal']['symbol']} {ctx['proposal']['side']}: {reasons}")
        _sheet_trade(proposal, "REFUSED", reasons, ctx)
        return {**report, "refused": True, "reason": reasons}

    # 3. Passed all rails. Place (armed) or dry-run (disarmed).
    p = ctx["proposal"]
    sl_pips = round(s["stop_pips"], 1)
    tp_pips = round(risk.stop_pips(p["entry"], p["target"], ctx["market"]["pip_size"]), 1)
    order_args = {"symbolName": p["symbol"], "side": p["side"],
                  "volume": s["units"], "volumeType": "units",
                  "stopLossPips": sl_pips, "takeProfitPips": tp_pips,
                  "label": "ftmo-engine",
                  # setup|regime|confidence — persists into trade history for edge analysis (D/E)
                  "comment": "|".join([str(p.get("setup_type", "")), str(p.get("regime", "")),
                                       str(p.get("confidence", ""))])[:48]}

    summary = (f"{p['symbol']} {p['side'].upper()} {s['units']:.0f}u "
               f"({s['lots']:.2f} lots) | SL {sl_pips}p TP {tp_pips}p | "
               f"risk ${risk.risk_dollars(ctx['balance'], p['risk_pct']):.2f} "
               f"({p['risk_pct']}%) | R:R {s['rr']:.2f} | "
               f"worst -${s['worst_case']:.2f}")

    if not config.is_armed():
        state_mod.save(state)
        telegram.send(f"🟡 WOULD PLACE (disarmed): {summary}")
        _journal(f"DRY-RUN WOULD PLACE: {summary}")
        _sheet_trade(proposal, "DRY-RUN", summary, ctx)
        return {**report, "refused": False, "dry_run": True, "summary": summary,
                "order_args": order_args}

    # IDs live BEFORE the place, so we can detect whether an order actually landed even if
    # the call times out (e.g. waiting on the cTrader confirmation dialog).
    def _live_ids() -> set:
        return ({pp.get("id") for pp in state.get("open_positions", [])} |
                {oo.get("id") for oo in state.get("pending_orders", [])})
    pre_ids = _live_ids()

    # Heads-up that the engine is placing an order (auto-fills when cTrader confirmation is OFF).
    telegram.send(f"📍 Placing {p['symbol']} {p['side'].upper()}\n{summary}")

    # retries=1 so a slow confirmation can NEVER cause a duplicate order; long timeout to
    # wait out the manual confirmation dialog.
    try:
        tool = "place_market_order" if p.get("order_type", "market") == "market" else "place_limit_order"
        args = order_args if tool == "place_market_order" else {**order_args, "limitPrice": p["entry"]}
        res = client.call(tool, args, retries=1, timeout=config.CONFIRM_TIMEOUT_SEC)
    except Exception as e:
        # The order may have landed despite a timeout. Reconcile and check before failing —
        # never blindly resend (that's how you get duplicates).
        reconcile(state, client)
        if _live_ids() - pre_ids:
            res = {"recovered_after_timeout": True, "error": str(e)}
            telegram.send(f"⚠️ Place call errored but an order DID land — treating as placed.\n{summary}")
        else:
            state_mod.save(state)
            telegram.send(f"⚠️ Order send FAILED (nothing landed) {p['symbol']} {p['side']} — {e}")
            _journal(f"ORDER FAILED: {summary} :: {e}")
            _sheet_trade(proposal, "ORDER FAILED", str(e), ctx)
            return {**report, "refused": False, "placed": False, "error": str(e)}

    # 4. Re-reconcile to confirm the fill/placement and capture broker IDs. A market order fills
    #    immediately, so reconcile counts it toward trades_taken_today here; a limit order is only
    #    counted when it later fills. Pending limits never inflate the daily trade count.
    reconcile(state, client)
    sid = p.get("signal_id")
    if sid and sid not in state.setdefault("executed_signals", []):
        state["executed_signals"].append(sid)
    # A resting (limit) order gets an expiry so a stale thesis can't fill hours later.
    if p.get("order_type", "market") != "market":
        order_id = (res or {}).get("orderId")
        if order_id is not None:
            hours = p.get("expiry_hours") or config.PENDING_MAX_HOURS   # model may set a custom expiry
            exp = (datetime.now(timezone.utc) + timedelta(hours=float(hours))).isoformat()
            state.setdefault("order_expiry", {})[str(order_id)] = exp
    state_mod.save(state)
    # A market order is filled immediately; a limit/stop order is only PLACED (pending) until hit.
    status = "FILLED" if p.get("order_type", "market") == "market" else "PLACED"
    telegram.send(f"✅ {status}: {summary}\n{json.dumps(res)[:300]}")
    _journal(f"{status}: {summary} :: {json.dumps(res)[:500]}")
    _sheet_trade(proposal, status, summary, ctx)
    return {**report, "refused": False, "placed": True, "status": status,
            "summary": summary, "result": res}
