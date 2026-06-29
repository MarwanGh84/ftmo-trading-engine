"""Active trade management — the professional layer that runs every 5 min (via the watchdog/
monitor job) and manages the engine's OWN open positions deterministically:

  - move stop to breakeven at +1R
  - take a partial at +2R
  - step-trail the stop from +2R (lock in floor(R)-1 R)

The decision logic (`plan_actions`) is pure and unit-tested. Orchestration reads the live mid
price, then acts through manage.py (which is ARMED-gated and refuses to widen a stop). Only
positions labelled MANAGE_LABEL are touched — never the user's manual trades.
"""
from __future__ import annotations
import math
from datetime import datetime, timezone

from . import config, manage
from . import chart_overlay


def r_multiple(side: str, entry: float, risk_dist: float, price: float) -> float:
    move = (price - entry) if side == "buy" else (entry - price)
    return (move / risk_dist) if risk_dist else 0.0


def plan_actions(side: str, entry: float, risk_dist: float, price: float, plan: dict):
    """Return (actions, updated_plan) for the current price. Pure — no I/O."""
    R = r_multiple(side, entry, risk_dist, price)
    actions = []

    if R >= config.BE_TRIGGER_R and not plan.get("be_done"):
        actions.append({"type": "be", "sl": entry})
        plan["be_done"] = True

    if R >= config.PARTIAL_R and not plan.get("partial_done"):
        actions.append({"type": "partial", "pct": config.PARTIAL_PCT})
        plan["partial_done"] = True

    if R >= config.TRAIL_START_R:
        # Cap the ratchet to ONE step per management cycle. Without the cap, a news-driven
        # gap from +2.1R to +6.5R in a single bar would move the stop 4 steps in one MCP call
        # — a dramatic stop change that can fail cTrader validation or confuse position tracking.
        ideal_R = int(math.floor(R / config.TRAIL_STEP_R)) - 1   # where we want to be
        current_trail = plan.get("trail_R", 0)
        target_R = min(ideal_R, current_trail + 1)                # at most one step per cycle
        if target_R > current_trail:
            new_sl = entry + target_R * risk_dist if side == "buy" else entry - target_R * risk_dist
            actions.append({"type": "trail", "sl": new_sl, "to_R": target_R})
            plan["trail_R"] = target_R

    # A trail stop is always tighter than the breakeven stop, so drop a same-run BE.
    if any(a["type"] == "trail" for a in actions):
        actions = [a for a in actions if a["type"] != "be"]
    return actions, plan


def is_weekend_flat_time(now) -> bool:
    """True on Friday at/after the configured Dubai hour (or anytime Saturday) — when an FTMO
    Standard account must be flat for the weekend. `now` is a tz-aware Dubai datetime."""
    if not config.WEEKEND_FLAT_ENABLED:
        return False
    wd, hr = now.weekday(), now.hour      # Mon=0 .. Fri=4, Sat=5, Sun=6
    if wd == 4 and hr >= config.WEEKEND_FLAT_HOUR_DUBAI:
        return True
    return wd == 5                        # all of Saturday (in case a run was missed Fri night)


def unprotected_position_ids(state: dict) -> list:
    """Pure: engine positions that have NO broker-side stop loss — an unacceptable state."""
    return [p.get("id") for p in state.get("open_positions", [])
            if p.get("label") == config.MANAGE_LABEL and not p.get("sl")]


def expired_order_ids(state: dict, now: datetime) -> list[str]:
    """Pure: which still-live resting orders have passed their expiry."""
    out = []
    live_ids = {str(o.get("id")) for o in state.get("pending_orders", [])}
    for oid, iso in state.get("order_expiry", {}).items():
        if oid not in live_ids:
            continue
        try:
            if datetime.fromisoformat(iso) <= now:
                out.append(oid)
        except Exception:
            continue
    return out


def cancel_expired_orders(state: dict) -> list[str]:
    """Cancel engine resting orders whose expiry has passed (stale thesis). Prunes the registry."""
    now = datetime.now(timezone.utc)
    exp = state.get("order_expiry", {})
    live_ids = {str(o.get("id")) for o in state.get("pending_orders", [])}
    notes = []
    for oid in expired_order_ids(state, now):
        manage.cancel_pending(int(oid))
        notes.append(f"expired-cancel #{oid}")
    # prune registry of anything no longer live (filled/cancelled/expired)
    for oid in list(exp):
        if oid not in live_ids or oid in {o for o in expired_order_ids(state, now)}:
            exp.pop(oid, None)
    return notes


def active_news_currencies(state: dict, now_utc) -> set:
    """Pure: currencies with a HIGH/CB news window currently active."""
    out = set()
    for w in state.get("news_windows", []):
        if w.get("kind") not in ("cb", "high"):
            continue
        try:
            start = datetime.fromisoformat(w["start_iso"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(w["end_iso"].replace("Z", "+00:00"))
        except Exception:
            continue
        if start <= now_utc <= end:
            out.add(w.get("ccy", "").upper())
    return out


def news_flatten(client, state: dict, now_utc) -> tuple[list[str], list[str]]:
    """Close engine positions + cancel engine orders on any currency whose HIGH/CB window is now
    active. Returns (notes, failed_ids) — caller must sticky-freeze if failed is non-empty."""
    if not config.NEWS_FLATTEN_ENABLED:
        return [], []
    ccys = active_news_currencies(state, now_utc)
    if not ccys:
        return [], []
    notes, failed = [], []
    for pos in list(state.get("open_positions", [])):
        sym = pos.get("symbol", "")
        if pos.get("label") == config.MANAGE_LABEL and len(sym) >= 6 and (sym[:3] in ccys or sym[3:6] in ccys):
            r = manage.close_position(pos.get("id"))
            if r.get("ok"):
                notes.append(f"{sym} #{pos.get('id')}")
            else:
                failed.append(f"{sym} #{pos.get('id')}: {r.get('reason', 'close failed')}")
    for o in list(state.get("pending_orders", [])):
        sym = o.get("symbol", "")
        if o.get("label") == config.MANAGE_LABEL and len(sym) >= 6 and (sym[:3] in ccys or sym[3:6] in ccys):
            r = manage.cancel_pending(o.get("id"))
            if r.get("ok"):
                notes.append(f"order #{o.get('id')} cancelled")
            else:
                failed.append(f"order #{o.get('id')}: {r.get('reason', 'cancel failed')}")
    return notes, failed


def weekend_flat(client, state: dict) -> tuple[list[str], list[str]]:
    """Close all engine positions and cancel engine pending orders for the weekend.
    Returns (notes, failed_ids) — caller must sticky-freeze if failed is non-empty."""
    notes, failed = [], []
    for pos in list(state.get("open_positions", [])):
        if pos.get("label") == config.MANAGE_LABEL:
            r = manage.close_position(pos.get("id"))
            if r.get("ok"):
                notes.append(f"{pos.get('symbol')} #{pos.get('id')}")
            else:
                failed.append(f"{pos.get('symbol')} #{pos.get('id')}: {r.get('reason', 'close failed')}")
    for o in list(state.get("pending_orders", [])):
        if o.get("label") == config.MANAGE_LABEL:
            r = manage.cancel_pending(o.get("id"))
            if r.get("ok"):
                notes.append(f"order #{o.get('id')} cancelled")
            else:
                failed.append(f"order #{o.get('id')}: {r.get('reason', 'cancel failed')}")
    return notes, failed


def emergency_flat(state: dict) -> tuple[list[str], list[str]]:
    """Immediately close all engine positions and cancel engine orders (kill-switch enforcement).
    Returns (notes, failed_ids). Does NOT freeze — caller decides severity."""
    notes, failed = [], []
    for pos in list(state.get("open_positions", [])):
        if pos.get("label") == config.MANAGE_LABEL:
            sym, pid = pos.get("symbol", "?"), pos.get("id")
            r = manage.close_position(pid)
            if r.get("ok"):
                notes.append(f"{sym} #{pid}")
            else:
                failed.append(f"{sym} #{pid}: {r.get('reason', 'close failed')}")
    for o in list(state.get("pending_orders", [])):
        if o.get("label") == config.MANAGE_LABEL:
            r = manage.cancel_pending(o.get("id"))
            if r.get("ok"):
                notes.append(f"order #{o.get('id')} cancelled")
            else:
                failed.append(f"order #{o.get('id')}: {r.get('reason', 'cancel failed')}")
    return notes, failed


def manage_open_positions(client, state: dict) -> list[str]:
    """Apply management to each open engine position. Returns human-readable action notes."""
    plans = state.setdefault("trade_plans", {})
    open_ids = {str(p.get("id")) for p in state.get("open_positions", [])}
    # prune plans for positions that no longer exist
    for pid in list(plans):
        if pid not in open_ids:
            plans.pop(pid, None)

    notes = []
    sym_cache: dict = {}
    for pos in state.get("open_positions", []):
        if pos.get("label") != config.MANAGE_LABEL:
            continue
        pid = str(pos.get("id"))
        side, entry, sl = pos.get("side"), pos.get("entry"), pos.get("sl")
        units = pos.get("volume_units")
        if not (side and entry and sl and units):
            continue

        plan = plans.get(pid)
        if plan is None:
            plan = {"entry": entry, "risk_dist": abs(entry - sl), "side": side,
                    "be_done": False, "partial_done": False, "trail_R": 0}
            plans[pid] = plan
        if plan["risk_dist"] <= 0:
            continue

        d = sym_cache.get(pos.get("symbol")) or client.get_symbol_details(pos.get("symbol"))
        sym_cache[pos.get("symbol")] = d
        price = (float(d["bid"]) + float(d["ask"])) / 2

        actions, plan = plan_actions(side, plan["entry"], plan["risk_dist"], price, plan)
        plans[pid] = plan
        for a in actions:
            if a["type"] in ("be", "trail"):
                digits = d.get("digits", 5)
                new_sl = round(a["sl"], digits)
                manage.set_stop(pos.get("id"), new_sl)
                notes.append(f"{pos.get('symbol')} #{pid} {a['type']} → SL {a['sl']:.{digits}f}")
                try:
                    chart_overlay.update_position_bracket(client, pos.get("id"), new_sl)
                except Exception:
                    pass
            elif a["type"] == "partial":
                step = d.get("volumeStep", 1000)
                close_units = math.floor((units * a["pct"]) / step) * step
                if close_units >= d.get("minVolume", 1000) and close_units < units:
                    manage.partial_take_profit(pos.get("id"), close_units)
                    notes.append(f"{pos.get('symbol')} #{pid} partial {close_units:.0f}u")
    return notes
