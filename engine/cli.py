"""Engine CLI — the interface the scheduled Claude runs and launchd call.

Subcommands:
  audit       read-only account+buffer snapshot; reconcile; daily reset; (optional report)
  execute     validate+place a trade from a proposal JSON (rails enforced; honors ARMED)
  manage      move-to-BE / trail / partial-TP on an open position (never-widen guarded)
  eod         end-of-day summary; weekend/CB checks; report
  watchdog    lightweight -2% kill-switch check (for the 5-min launchd job)
  unfreeze    lift an operational freeze (--force required for sticky/phase-target)
  cot-update  download CFTC TFF report; update macro bias cache (run Saturday)

Run from ~/trading:  python3 -m engine.cli <subcommand> [...]
"""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime, timezone, timedelta

from . import config, risk, telegram, sheets
from . import state as state_mod
from . import chart_overlay
from .mcp_client import McpClient, requests_used_today
from .reconcile import reconcile
from . import execute as execute_mod
from . import manage as manage_mod
from . import trade_manager
from . import scanner
from . import guards
from . import backtest as backtest_mod
from . import stats as stats_mod
from . import shadow as shadow_mod
from . import cot as cot_mod


# Scheduled Claude runs (Dubai local, Mon–Fri). Mirrors the .claude/scheduled-tasks crons so the
# dashboard can show what's coming next. Keep in sync if the schedule changes.
_SCHEDULE = [(9, 32, "Morning brief"), (11, 0, "London"), (13, 32, "Midday"),
             (16, 33, "NY"), (20, 6, "EOD review")]


def _next_scheduled_run(now) -> str:
    """Next scheduled session after `now` (a tz-aware Dubai datetime), as 'HH:MM Label'."""
    for day_offset in range(0, 4):
        d = now + timedelta(days=day_offset)
        if d.weekday() >= 5:           # Sat/Sun: no runs
            continue
        for hh, mm, label in _SCHEDULE:
            cand = d.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if cand > now:
                when = cand.strftime("%H:%M") if day_offset == 0 else cand.strftime("%a %H:%M")
                return f"{when} {label}"
    return "Mon 09:32 Morning brief"


def _news_summary(state: dict, now_utc: datetime) -> str:
    """Today's HIGH-impact windows still ahead, e.g. 'USD CPI 12:30Z · GBP BOE 11:00Z'."""
    out = []
    for w in state.get("news_windows", []):
        try:
            end = datetime.fromisoformat(w["end_iso"].replace("Z", "+00:00"))
        except Exception:
            continue
        if end >= now_utc:
            t = w.get("start_iso", "")[11:16]
            out.append(f"{w.get('ccy','')} {w.get('event','')} {t}Z")
    return " · ".join(out[:3]) if out else "none remaining"


def _weekend_flat_summary(now) -> str:
    wd, hr = now.weekday(), now.hour
    if wd >= 5:
        return "weekend — flat"
    if wd == 4:
        return "tonight 23:00" if hr < config.WEEKEND_FLAT_HOUR_DUBAI else "flattening now"
    return "Fri 23:00"


def _account_snapshot(client: McpClient, state: dict) -> dict:
    bal = client.get_balance()
    balance, equity = float(bal["balance"]), float(bal["equity"])
    state_mod.apply_daily_reset(state, balance, equity)
    disc = reconcile(state, client)
    dsb = state.get("day_start_balance") or max(balance, equity)
    base = state.get("account_baseline") or max(balance, equity)
    daily_floor = dsb * (1 - config.DAILY_LOSS_LIMIT_PCT / 100)
    overall_floor = base * (1 - config.FTMO_MAX_LOSS_LIMIT_PCT / 100)
    # Full buffer sizes (the denominators for the dashboard's drawdown gauges).
    daily_room_full = dsb * config.DAILY_LOSS_LIMIT_PCT / 100
    overall_room_full = base * config.FTMO_MAX_LOSS_LIMIT_PCT / 100
    # Performance over the engine's closed trades (best-effort; never breaks the snapshot).
    try:
        _trades = stats_mod.engine_trades(client, config.MANAGE_LABEL)
        perf = stats_mod.compute_stats([t["net"] for t in _trades])
    except Exception:
        perf = None
    now_dubai = state_mod.now_dubai()
    return {
        "balance": balance, "equity": equity,
        "margin": bal.get("margin"), "free_margin": bal.get("freeMargin"),
        "authenticated": bal.get("connectionState") == "Authenticated",
        "day_start_balance": dsb, "account_baseline": base,
        "daily_room": equity - daily_floor, "overall_room": equity - overall_floor,
        "daily_room_full": daily_room_full, "overall_room_full": overall_room_full,
        "stats": perf,
        "next_run": _next_scheduled_run(now_dubai),
        "news_today": _news_summary(state, datetime.now(timezone.utc)),
        "weekend_flat": _weekend_flat_summary(now_dubai),
        "daily_pnl": equity - dsb,
        "open_positions": len(state.get("open_positions", [])),
        "pending_orders": len(state.get("pending_orders", [])),
        "trades_today": state.get("trades_taken_today", 0),
        "poor_outcomes": state.get("poor_outcomes_today", 0),
        "daily_limit_hit": state.get("daily_limit_hit", False),
        "frozen": state.get("frozen", False),
        "frozen_reason": state.get("frozen_reason", ""),
        "profit": equity - config.FTMO_INITIAL_BALANCE,
        "to_target": config.FTMO_PROFIT_TARGET_USD - (equity - config.FTMO_INITIAL_BALANCE),
        "phase": config.FTMO_PHASE,
        "trading_days": len(state.get("trading_days", [])),
        "min_trading_days": config.FTMO_MIN_TRADING_DAYS,
        "requests_used": requests_used_today(),
        "discrepancy": disc,
    }


def cmd_audit(args) -> int:
    client = McpClient()
    state = state_mod.load()
    snap = _account_snapshot(client, state)
    state_mod.save(state)
    now_s = state_mod.now_dubai().strftime("%H:%M")
    pnl_s = telegram._net_str(snap["daily_pnl"])
    kill_s = "🔴 HIT" if snap["daily_limit_hit"] else "✅"
    bal_s   = telegram.code(f"${snap['balance']:.2f}")
    eq_s    = telegram.code(f"${snap['equity']:.2f}")
    droom_s = telegram.code(f"${snap['daily_room']:.2f}")
    oroom_s = telegram.code(f"${snap['overall_room']:.2f}")
    trd_s   = telegram.code(f"{snap['trades_today']}/{config.MAX_TRADES_PER_DAY}")
    poor_s  = telegram.code(f"{snap['poor_outcomes']}/{config.MAX_POOR_OUTCOMES}")
    mcp_s   = telegram.code(f"{snap['requests_used']}/{config.REQUEST_CAP_PER_DAY}")
    armed_s = "✅ live" if config.is_armed() else "🟡 disarmed"
    text = (f"📊 <b>Audit — {now_s} Dubai</b>\n"
            f"Balance {bal_s}  ·  Equity {eq_s}  ·  P/L {telegram.code(pnl_s)}\n"
            f"Daily room {droom_s}  ·  Overall room {oroom_s}\n"
            f"Open {telegram.code(snap['open_positions'])}  ·  "
            f"Pending {telegram.code(snap['pending_orders'])}  ·  "
            f"Trades {trd_s}  ·  Poor {poor_s}\n"
            f"Kill-switch {kill_s}  ·  MCP {mcp_s}  ·  ARMED {armed_s}")
    execute_mod.report_closures(snap["discrepancy"].get("closures", []))
    try:
        chart_overlay.clear_closed_brackets(client, snap["discrepancy"].get("closures", []))
        if args.report:
            chart_overlay.setup_session_charts(client, state)
    except Exception:
        pass
    if snap["discrepancy"]["changed"]:
        text += f"\n🔄 reconciled: {snap['discrepancy']['summary']}"
    sheets.update_dashboard(snap)
    sheets.append_run([state_mod.now_dubai().strftime("%Y-%m-%d %H:%M"), "audit", "report",
                       f"bal ${snap['balance']:.2f} dayP/L ${snap['daily_pnl']:.2f} "
                       f"trades {snap['trades_today']} kill-switch "
                       f"{'HIT' if snap['daily_limit_hit'] else 'ok'}"])
    print(json.dumps(snap, default=str, indent=2))
    if args.report:
        telegram.send(text)
    else:
        print("\n" + text)
    return 0


def cmd_execute(args) -> int:
    if args.file:
        proposal = json.loads(open(args.file).read())
    elif args.json:
        proposal = json.loads(args.json)
    else:
        proposal = json.loads(sys.stdin.read())
    result = execute_mod.execute(proposal)
    print(json.dumps(result, default=str, indent=2))
    return 0 if not result.get("error") else 1


def cmd_manage(args) -> int:
    if args.action in ("be", "trail", "partial", "close") and args.position is None:
        print(f"{args.action} requires --position"); return 2
    if args.action == "be":
        r = manage_mod.move_to_breakeven(args.position, args.offset or 0.0)
    elif args.action == "trail":
        if args.sl is None:
            print("trail requires --sl"); return 2
        r = manage_mod.set_stop(args.position, args.sl, args.tp)
    elif args.action == "partial":
        if args.units is None:
            print("partial requires --units"); return 2
        r = manage_mod.partial_take_profit(args.position, args.units)
    elif args.action == "cancel":
        oid = args.order if args.order is not None else args.position
        if oid is None:
            print("cancel requires --order"); return 2
        r = manage_mod.cancel_pending(oid)
    elif args.action == "close":
        r = manage_mod.close_position(args.position)
    else:
        print("unknown action"); return 2
    print(json.dumps(r, default=str, indent=2))
    return 0 if r.get("ok") else 1


def cmd_watchdog(args) -> int:
    """Continuous monitor (every 5 min): -2% kill-switch + closure detection + active trade
    management of the engine's own positions. Skips the cTrader call entirely when flat.
    """
    state = state_mod.load()
    flat = (not state.get("open_positions") and not state.get("pending_orders")
            and not state.get("daily_limit_hit") and not state.get("trade_plans"))
    if (flat and not state.get("frozen")
            and not trade_manager.is_weekend_flat_time(state_mod.now_dubai())):
        print("watchdog: flat, no open risk -> skip")
        return 0
    try:
        client = McpClient()
        bal = client.call("get_balance", critical=True)   # kill-switch must never be cap-blocked
    except Exception as e:
        # Fail closed: after N consecutive unreachable cycles, FREEZE new entries.
        state["unreachable_streak"] = state.get("unreachable_streak", 0) + 1
        streak = state["unreachable_streak"]
        if streak >= config.UNREACHABLE_FREEZE_CYCLES:
            state_mod.freeze(state, f"cTrader unreachable {streak} cycles")
            if streak == config.UNREACHABLE_FREEZE_CYCLES:
                # Alert exactly once when first hitting the threshold (not on every subsequent cycle).
                telegram.send(f"🧊 <b>FROZEN — cTrader unreachable {streak}× ({streak * 5} min)</b>\n"
                              f"No new entries until it recovers. Broker-side stops still protect open trades.")
        state_mod.save(state)
        print(f"watchdog: unreachable streak {streak}: {e}")
        return 0
    balance, equity = float(bal["balance"]), float(bal["equity"])
    # Reachable again — clear the streak and lift a connectivity freeze.
    if state.get("unreachable_streak", 0) > 0:
        state["unreachable_streak"] = 0
        if state.get("frozen") and state.get("frozen_reason", "").startswith("cTrader unreachable"):
            if state_mod.unfreeze(state):
                telegram.send("✅ <b>Recovered</b> — cTrader reachable again, freeze lifted.")
    state_mod.apply_daily_reset(state, balance, equity)

    # 1. Kill-switch FIRST (the one thing that must always run). Guard against a transient bad/zero
    #    equity reading (e.g. during a cTrader reconnect) FALSELY tripping it — equity must be a
    #    plausible positive value. A real −2% hit (equity ≈ 98% of balance) easily passes this.
    dsb = state.get("day_start_balance") or max(balance, equity)
    floor = dsb * (1 - config.DAILY_LOSS_LIMIT_PCT / 100)
    equity_plausible = equity > 0 and balance > 0 and equity >= balance * 0.5
    if equity_plausible and equity <= floor and not state.get("daily_limit_hit"):
        state["daily_limit_hit"] = True
        telegram.send(f"🚨 <b>KILL-SWITCH</b>\n"
                      f"Equity {telegram.code(f'${equity:.2f}')} hit −{config.DAILY_LOSS_LIMIT_PCT}% "
                      f"floor {telegram.code(f'${floor:.2f}')} — no new trades today.")
        try:
            chart_overlay.notify(
                client,
                "🚨 KILL-SWITCH",
                f"−{config.DAILY_LOSS_LIMIT_PCT}% floor hit — closing all engine positions",
                "error",
            )
        except Exception:
            pass
        flat_notes, flat_failed = trade_manager.emergency_flat(state)
        if flat_notes or flat_failed:
            lines = [f"  {telegram.esc(n)} ✓" for n in flat_notes]
            lines += [f"  {telegram.esc(f)} ✗" for f in flat_failed]
            status = "partial" if flat_failed else f"{len(flat_notes)} closed"
            telegram.send(f"⏹ <b>Kill-switch flatten — {status}</b>\n" + "\n".join(lines))
        if flat_failed:
            state_mod.freeze(state, "kill-switch flatten failed — manual close required", sticky=True)
            telegram.send(f"🛑 <b>FROZEN</b> — kill-switch flatten failed.\n"
                          f"Close manually in cTrader: {telegram.esc(', '.join(flat_failed))}")
    # Phase target reached + min trading days -> sticky freeze (protect the pass, await next account).
    if guards.phase_target_reached(state, equity, balance=balance) and not state.get("frozen_sticky"):
        state_mod.freeze(state, "phase target reached + min trading days — protect the pass; "
                         "await the next account and re-arm manually", sticky=True)
        telegram.send("🏁 <b>Phase target reached</b> — new trades frozen to protect the pass.\n"
                      "Await the next FTMO account, then follow SWITCH_ACCOUNT.md to re-arm.")
    state_mod.save(state)   # persist the flag even if the steps below fail on cap/connectivity

    # 2. Best-effort reconcile + closures + active management (may skip if cap/conn fails).
    notes = []
    try:
        # A3. Snapshot pre-reconcile engine state so we can detect crash-recovery vs normal fills.
        _old_eng_pos = [p for p in state.get("open_positions", []) if p.get("label") == config.MANAGE_LABEL]
        _old_eng_ord = [o for o in state.get("pending_orders", []) if o.get("label") == config.MANAGE_LABEL]
        disc = reconcile(state, client)
        execute_mod.report_closures(disc.get("closures", []))
        try:
            chart_overlay.clear_closed_brackets(client, disc.get("closures", []))
        except Exception:
            pass
        # A3 alert: engine was fully flat in state but broker has engine positions — crash recovery.
        # Not triggered by normal fills (pending order existed = we knew about it) or normal operation.
        if not _old_eng_pos and not _old_eng_ord:
            appeared_engine = [p for p in state.get("open_positions", [])
                               if p.get("label") == config.MANAGE_LABEL]
            if appeared_engine:
                syms = ", ".join(f"{p.get('symbol')} #{p.get('id')}" for p in appeared_engine)
                telegram.send(f"⚠️ <b>Startup reconcile</b> — state was flat but broker has "
                              f"{len(appeared_engine)} engine position(s).\n"
                              f"{telegram.esc(syms)}\nRecovered. Verify no duplicate risk.")
        # Fail-closed: an engine position with no broker stop is emergency-closed + sticky freeze.
        for pid in trade_manager.unprotected_position_ids(state):
            manage_mod.close_position(pid)
            state_mod.freeze(state, f"engine position #{pid} had no broker stop", sticky=True)
            telegram.send(f"🛑 <b>EMERGENCY</b> — position {telegram.code(f'#{pid}')} had no stop.\n"
                          f"Emergency closed + sticky freeze. Verify cTrader settings.")
        # Trade-age guard: force-exit forgotten positions.
        for pid in guards.trade_age_violations(state, datetime.now(timezone.utc)):
            manage_mod.close_position(pid)
            telegram.send(f"⏱ Force-exit {telegram.code(f'#{pid}')} — "
                          f"open > {config.MAX_TRADE_AGE_HOURS}h (trade-age cap).")
        # Auto-clearing freeze: unknown/manual positions (recomputed each cycle).
        if not state.get("frozen_sticky"):
            reason = guards.auto_freeze_reason(state)
            if reason:
                if state_mod.freeze(state, reason):
                    telegram.send(f"🧊 <b>Frozen</b> — {telegram.esc(reason)}\nNo new entries until cleared.")
            elif state.get("frozen"):
                if state_mod.unfreeze(state):
                    telegram.send("✅ <b>Unfrozen</b> — operational condition cleared.")
        if trade_manager.is_weekend_flat_time(state_mod.now_dubai()):
            flat_notes, flat_failed = trade_manager.weekend_flat(client, state)
            if flat_notes or flat_failed:
                lines = [f"  {telegram.esc(n)} ✓" for n in flat_notes]
                lines += [f"  {telegram.esc(f)} ✗" for f in flat_failed]
                status = "partial" if flat_failed else f"{len(flat_notes)} closed"
                telegram.send(f"⏹ <b>Weekend flatten — {status}</b>\n" + "\n".join(lines))
            if flat_failed:
                state_mod.freeze(state, "weekend flatten failed — manual close required", sticky=True)
                telegram.send(f"🛑 <b>FROZEN</b> — weekend flatten failed.\n"
                              f"Close manually in cTrader: {telegram.esc(', '.join(flat_failed))}")
        else:
            n_notes, n_failed = trade_manager.news_flatten(client, state, datetime.now(timezone.utc))
            if n_notes or n_failed:
                lines = [f"  {telegram.esc(n)} ✓" for n in n_notes]
                lines += [f"  {telegram.esc(f)} ✗" for f in n_failed]
                status = "partial" if n_failed else f"{len(n_notes)} closed"
                telegram.send(f"⏹ <b>News flatten — {status}</b>\n" + "\n".join(lines))
            if n_failed:
                state_mod.freeze(state, "news flatten failed — manual close required", sticky=True)
                telegram.send(f"🛑 <b>FROZEN</b> — news flatten failed.\n"
                              f"Close manually in cTrader: {telegram.esc(', '.join(n_failed))}")
            notes += trade_manager.cancel_expired_orders(state)
            if state.get("open_positions"):
                notes += trade_manager.manage_open_positions(client, state)
        if notes:
            items = "\n".join(f"  {telegram.esc(n)}" for n in notes)
            telegram.send(f"🛠 <b>Trade management</b> ({len(notes)})\n{items}")
        state_mod.save(state)
    except Exception as e:
        print(f"watchdog: manage/reconcile skipped: {e}")
    print(f"watchdog: eq ${equity:.2f} floor ${floor:.2f} hit={state.get('daily_limit_hit')} "
          f"managed={len(notes)}")
    return 0


def cmd_stats(args) -> int:
    """Performance analytics over the engine's closed trades (from cTrader order history)."""
    client = McpClient()
    engine_only = not args.all
    trades = stats_mod.engine_trades(client, config.MANAGE_LABEL, include_all=args.all)
    overall = stats_mod.compute_stats([t["net"] for t in trades])
    out = {"overall": overall, "by_symbol": stats_mod.by_symbol(trades),
           "by_setup": stats_mod.by_setup(trades), "by_regime": stats_mod.by_regime(trades),
           "by_confidence": stats_mod._group(trades, "confidence", "—")}
    text = "📈 Performance (" + ("engine trades" if engine_only else "all trades") + ")\n" + \
           stats_mod.format_summary(overall)
    for sym, s in sorted(out["by_symbol"].items(), key=lambda kv: kv[1]["net"], reverse=True):
        text += f"\n  {sym}: {s['trades']}t {s['win_rate']*100:.0f}% net ${s['net']:.2f}"
    for label, grp in (("By setup", out["by_setup"]), ("By regime", out["by_regime"]),
                       ("By confidence", out["by_confidence"])):
        tagged = {k: v for k, v in grp.items() if k not in ("", "untagged", "—")}
        if tagged:
            text += f"\n{label}:"
            for k, s in sorted(tagged.items(), key=lambda kv: kv[1]["net"], reverse=True):
                text += f"\n  {k}: {s['trades']}t {s['win_rate']*100:.0f}% exp ${s['expectancy']:.2f}"
    print(json.dumps(out, default=str, indent=2))
    if args.report:
        telegram.send(text)
    else:
        print("\n" + text)
    return 0


def cmd_backtest(args) -> int:
    """Backtest candidate deterministic strategies on the watchlist's real history (no risk)."""
    client = McpClient()
    names = [args.strategy] if args.strategy else list(backtest_mod.STRATEGIES)
    rows = []
    full = {}
    for name in names:
        r = backtest_mod.run(client, config.WATCHLIST, name, timeframe=args.timeframe, days=args.days)
        s = stats_mod.compute_stats(r["all_R"])
        full[name] = {"overall": s, "per_symbol": {k: stats_mod.compute_stats(v)
                                                   for k, v in r["per_symbol"].items()}}
        pf = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
        rows.append((name, s["trades"], s["win_rate"] * 100, s["expectancy"], pf, s["net"]))
    print(json.dumps(full, default=str, indent=2))
    print(f"\n=== Backtest ({args.timeframe}, ~{args.days}d, {len(config.WATCHLIST)} pairs) ===")
    print(f"{'strategy':<16}{'trades':>7}{'win%':>7}{'exp(R)':>9}{'PF':>7}{'totalR':>9}")
    for name, n, wr, exp, pf, net in rows:
        print(f"{name:<16}{n:>7}{wr:>6.0f}%{exp:>9.3f}{pf:>7}{net:>9.1f}")
    print("\nexp(R) = average R per trade (the edge metric). >0 over many trades = a real edge.")
    return 0


def cmd_subscribe(args) -> int:
    """Ensure all watchlist symbols are subscribed in cTrader (so live quotes flow)."""
    client = McpClient()
    added = scanner.ensure_subscribed(client)
    print(f"subscribe: ensured {len(config.WATCHLIST)} symbols; added {added}")
    return 0


def cmd_candidates(args) -> int:
    """Show the scanner's latest flagged candidates (pairs sitting at key levels) for a run to
    prioritize. This is the bot's hand-off to the Claude analysis runs."""
    state = state_mod.load()
    cands = state.get("candidates", [])
    print(json.dumps({"as_of": state.get("candidates_time"), "candidates": cands}, default=str, indent=2))
    if cands:
        print("\nScanner candidates (analyze these FIRST):")
        for c in cands:
            print(f"  {c.get('symbol')}: {c.get('bias')} — {c.get('note') or c.get('near')}")
    else:
        print("\n(no candidates flagged at the last scan)")
    return 0


def cmd_scan(args) -> int:
    """Market scanner — sweep the watchlist, update the Sheet, alert new level proximities.
    Self-gates to active hours unless --force."""
    if not args.force and not scanner._within_hours():
        print("scan: outside active hours -> skip")
        return 0
    state = state_mod.load()
    client = McpClient()
    try:
        alerts = scanner.scan(client, state)
    except Exception as e:
        print(f"scan error: {e}")
        return 0
    state_mod.save(state)
    print(f"scan: {len(alerts)} new alert(s)")
    return 0


def cmd_bars(args) -> int:
    """Serve OHLC bars to the analysis run so it never calls cTrader tools directly."""
    from datetime import datetime, timezone, timedelta
    client = McpClient()
    to = datetime.now(timezone.utc)
    frm = to - timedelta(days=args.days)
    r = client.call("get_trendbars", {"symbolName": args.symbol.upper(), "timeframe": args.timeframe,
                                       "from": frm.isoformat(), "to": to.isoformat(), "limit": args.limit})
    print(json.dumps(r, default=str))
    return 0


def cmd_quote(args) -> int:
    """Serve current quote + contract specs (bid/ask/spread/pip/lot) for a symbol."""
    client = McpClient()
    d = client.get_symbol_details(args.symbol.upper())
    spread = None
    if d.get("ask") and d.get("bid") and d.get("pipSize"):
        spread = round((float(d["ask"]) - float(d["bid"])) / d["pipSize"], 1)
    out = {k: d.get(k) for k in ("name", "bid", "ask", "pipSize", "lotSize",
                                 "minVolume", "volumeStep", "digits")}
    out["spread_pips"] = spread
    print(json.dumps(out, default=str))
    return 0


def cmd_set_news(args) -> int:
    """Persist today's news blackout windows (called by morning_brief).

    Expects a JSON list of {ccy, start_iso, end_iso, event, kind(high|cb)} with the
    +/- NEWS_WINDOW_MIN padding already applied. Marks them fresh for today's Dubai date.
    """
    windows = json.loads(args.json) if args.json else json.loads(sys.stdin.read())
    if not isinstance(windows, list):
        print("set-news expects a JSON list"); return 2
    state = state_mod.load()
    state["news_windows"] = windows
    state["news_windows_date"] = state_mod.now_ftmo().date().isoformat()  # FTMO day, not Dubai
    state_mod.save(state)
    print(f"stored {len(windows)} news window(s) for {state['news_windows_date']}")
    # BUG 5 FIX: clear before draw so re-running set-news doesn't duplicate lines
    try:
        _overlay_client = McpClient()
        chart_overlay.clear_news_lines(_overlay_client)
        n_drawn = chart_overlay.draw_news_lines(_overlay_client, windows)
        if n_drawn:
            print(f"drew {n_drawn} news line(s) on open charts")
    except Exception:
        pass
    return 0


def cmd_eod(args) -> int:
    client = McpClient()
    state = state_mod.load()
    snap = _account_snapshot(client, state)
    open_syms = [p.get("symbol") for p in state.get("open_positions", [])]
    open_str = ", ".join(open_syms) if open_syms else "flat"
    date_s  = state_mod.now_dubai().strftime("%a %d %b")
    bal_s   = telegram.code(f"${snap['balance']:.2f}")
    pnl_s   = telegram.code(telegram._net_str(snap["daily_pnl"]))
    trd_s   = telegram.code(f"{snap['trades_today']}/{config.MAX_TRADES_PER_DAY}")
    poor_s  = telegram.code(f"{snap['poor_outcomes']}/{config.MAX_POOR_OUTCOMES}")
    text = (f"🌙 <b>EOD — {date_s}</b>\n"
            f"Balance {bal_s}  ·  P/L {pnl_s}\n"
            f"Trades {trd_s}  ·  Poor {poor_s}\n"
            f"Positions  {telegram.code(open_str)}")
    if open_syms:
        text += "\n⚠️ Open positions held — verify no weekend/CB-event exposure"
    execute_mod.report_closures(snap["discrepancy"].get("closures", []))
    try:
        chart_overlay.clear_closed_brackets(client, snap["discrepancy"].get("closures", []))
        chart_overlay.clear_fill_annotations(client)   # BUG 2 FIX: fill annotations EOD cleanup
        chart_overlay.clear_news_lines(client)
        chart_overlay.clear_all_scanner_levels(client)
    except Exception:
        pass
    state_mod.save(state)
    sheets.update_dashboard(snap)
    sheets.append_run([state_mod.now_dubai().strftime("%Y-%m-%d %H:%M"), "eod", "review",
                       f"day P/L ${snap['daily_pnl']:.2f} trades {snap['trades_today']} "
                       f"open {open_syms or 'flat'}"])
    with open(config.JOURNAL_FILE, "a") as f:
        f.write(f"\n## EOD {state_mod.now_dubai().date().isoformat()}\n{text}\n")
    if args.report:
        telegram.send(text)
    print(text)
    return 0


def cmd_shadow(args) -> int:
    """Log one analyzed candidate (take OR skip) with its hypothetical bracket, so the engine can
    grade the would-have outcome later and measure whether the filtering has edge."""
    payload = json.loads(args.json) if args.json else json.loads(sys.stdin.read())
    rec = shadow_mod.log(payload)
    print(json.dumps(rec, indent=2))
    print(f"\n🧪 shadow logged: {rec['symbol']} {rec['side']} [{rec['verdict']}] "
          f"entry {rec['entry']} stop {rec['stop']} target {rec['target']}")
    return 0


def cmd_shadow_grade(args) -> int:
    """Grade open shadows against price (also runs automatically inside the scanner sweep)."""
    graded = shadow_mod.grade_open(McpClient())
    for s in graded:
        print(f"{s['symbol']} {s['side']} [{s['verdict']}] -> {s['result']}")
    print(f"graded {len(graded)} shadow(s)")
    return 0


def cmd_shadow_stats(args) -> int:
    text = shadow_mod.format_summary(shadow_mod.summary())
    print(text)
    if args.report:
        telegram.send(text)
    return 0


def cmd_unfreeze(args) -> int:
    """Lift an operational freeze. Sticky freezes (phase target, emergency) require --force.

    Without --force: clears a routine freeze (e.g. cTrader unreachable, feed degraded).
    With --force:    clears a sticky freeze — use after manually confirming the phase
                     target / incident is resolved. Requires explicit human intent.
    """
    state = state_mod.load()
    is_sticky = state.get("frozen_sticky", False)
    reason = state.get("frozen_reason", "")

    if not state.get("frozen"):
        print("Not currently frozen — nothing to do.")
        return 0

    if is_sticky and not args.force:
        print(f"Sticky freeze active: '{reason}'")
        print("Use --force to override (confirm the phase target / incident is resolved first).")
        return 1

    state["frozen"] = False
    state["frozen_reason"] = ""
    state["frozen_sticky"] = False
    state_mod.save(state)
    msg = f"Freeze cleared{'  (--force)' if args.force else ''}. Reason was: '{reason}'"
    print(msg)
    telegram.send(f"🔓 {msg}")
    return 0


def cmd_cot_update(args) -> int:
    """Download the CFTC TFF report and update the local COT macro bias cache.

    Fetches the current-year Leveraged Money (hedge fund / CTA) net positioning for
    major FX futures from cftc.gov, merges new records into logs/cot_history.json, and
    writes a clean summary to cot_bias.json. Safe to run repeatedly — only new weeks
    are added. Prints a formatted report; --report also sends it via Telegram.
    """
    try:
        bias, added = cot_mod.update()
    except Exception as e:
        last = cot_mod.load_bias()
        last_date = next((v.get("date", "?") for v in last.values() if isinstance(v, dict)), "none")
        msg = f"⚠️ COT fetch failed — using last known bias ({last_date}). Error: {e}"
        telegram.send(msg)   # always alert (COT runs weekly via launchd, not interactively)
        print(msg)
        return 1
    report = cot_mod.format_report(bias)
    print(report)
    print(f"\n(+{added} new week(s) added to history)")
    if args.report:
        telegram.send(report)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="engine")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("audit"); a.add_argument("--report", action="store_true")
    a.set_defaults(func=cmd_audit)

    e = sub.add_parser("execute")
    e.add_argument("--json"); e.add_argument("--file")
    e.set_defaults(func=cmd_execute)

    m = sub.add_parser("manage")
    m.add_argument("--action", required=True, choices=["be", "trail", "partial", "cancel", "close"])
    m.add_argument("--position", type=int)
    m.add_argument("--order", type=int, help="pending order id (for cancel)")
    m.add_argument("--sl", type=float); m.add_argument("--tp", type=float)
    m.add_argument("--offset", type=float); m.add_argument("--units", type=float)
    m.set_defaults(func=cmd_manage)

    w = sub.add_parser("watchdog"); w.set_defaults(func=cmd_watchdog)

    n = sub.add_parser("set-news"); n.add_argument("--json")
    n.set_defaults(func=cmd_set_news)

    b = sub.add_parser("bars")
    b.add_argument("--symbol", required=True)
    b.add_argument("--timeframe", default="h4")
    b.add_argument("--days", type=int, default=30)
    b.add_argument("--limit", type=int, default=200)
    b.set_defaults(func=cmd_bars)

    q = sub.add_parser("quote"); q.add_argument("--symbol", required=True)
    q.set_defaults(func=cmd_quote)

    sc = sub.add_parser("scan"); sc.add_argument("--force", action="store_true")
    sc.set_defaults(func=cmd_scan)

    cd = sub.add_parser("candidates"); cd.set_defaults(func=cmd_candidates)

    sb = sub.add_parser("subscribe"); sb.set_defaults(func=cmd_subscribe)

    bt = sub.add_parser("backtest")
    bt.add_argument("--strategy", choices=list(backtest_mod.STRATEGIES))
    bt.add_argument("--timeframe", default="d1")
    bt.add_argument("--days", type=int, default=1400)
    bt.set_defaults(func=cmd_backtest)

    stp = sub.add_parser("stats")
    stp.add_argument("--report", action="store_true")
    stp.add_argument("--all", action="store_true", help="include manual trades, not just engine")
    stp.set_defaults(func=cmd_stats)

    d = sub.add_parser("eod"); d.add_argument("--report", action="store_true")
    d.set_defaults(func=cmd_eod)

    sh = sub.add_parser("shadow"); sh.add_argument("--json")
    sh.set_defaults(func=cmd_shadow)
    sg = sub.add_parser("shadow-grade"); sg.set_defaults(func=cmd_shadow_grade)
    sst = sub.add_parser("shadow-stats"); sst.add_argument("--report", action="store_true")
    sst.set_defaults(func=cmd_shadow_stats)

    uf = sub.add_parser("unfreeze")
    uf.add_argument("--force", action="store_true",
                    help="clear a sticky freeze (phase target / emergency) — use with care")
    uf.set_defaults(func=cmd_unfreeze)

    cot = sub.add_parser("cot-update")
    cot.add_argument("--report", action="store_true", help="also send result via Telegram")
    cot.set_defaults(func=cmd_cot_update)

    args = ap.parse_args(argv)
    # Serialize state-mutating commands across processes (5-min watchdog / scheduled runs /
    # interactive calls) so a concurrent save() can't drop an accumulated counter. Read-only
    # commands (bars/quote/scan/candidates/stats/backtest/subscribe) don't take the lock, so
    # data fetches are never blocked by a long-running placement.
    _MUTATING = {cmd_audit, cmd_execute, cmd_manage, cmd_watchdog, cmd_set_news, cmd_eod}
    if args.func in _MUTATING:
        with state_mod.transaction() as got_lock:
            if not got_lock:
                print("⚠️ state lock busy after timeout — proceeding best-effort")
            return args.func(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
