"""Reconcile state.json against live cTrader before every decision.

If they disagree, cTrader is the source of truth: we overwrite state and flag the
discrepancy so the caller can Telegram-alert it.
"""
from __future__ import annotations

from . import config
from . import state as state_mod
from .mcp_client import McpClient


def _norm_position(p: dict) -> dict:
    # cTrader returns: id, symbolName, tradeSide, volumeInUnits/Lots, entryPrice,
    # currentPrice, stopLossPrice/Pips, takeProfitPrice/Pips, netProfit. Fallbacks kept
    # for safety in case the schema shifts.
    return {
        "id": p.get("id") or p.get("positionId"),
        "symbol": p.get("symbolName") or p.get("symbol") or "",
        "side": (p.get("tradeSide") or p.get("side") or "").lower(),
        "volume_units": p.get("volumeInUnits"),
        "lots": p.get("volumeInLots"),
        "entry": p.get("entryPrice") or p.get("openPrice"),
        "current": p.get("currentPrice"),
        "sl": p.get("stopLossPrice") or p.get("stopLoss"),
        "sl_pips": p.get("stopLossPips"),
        "tp": p.get("takeProfitPrice") or p.get("takeProfit"),
        "tp_pips": p.get("takeProfitPips"),
        "net_profit": p.get("netProfit"),
        "label": p.get("label") or "",
        "open_time": p.get("openTime"),
    }


def _norm_order(o: dict) -> dict:
    return {
        "id": o.get("id") or o.get("orderId"),
        "symbol": o.get("symbolName") or o.get("symbol") or "",
        "side": (o.get("tradeSide") or o.get("side") or "").lower(),
        "type": o.get("orderType") or o.get("type"),
        "volume_units": o.get("volumeInUnits"),
        "target_price": o.get("targetPrice") or o.get("limitPrice"),
        "sl_pips": o.get("stopLossPips"),
        "tp_pips": o.get("takeProfitPips"),
        "label": o.get("label") or "",
    }


def live_snapshot(client: McpClient) -> tuple[list[dict], list[dict]]:
    positions = [_norm_position(p) for p in client.get_positions().get("positions", [])]
    orders = [_norm_order(o) for o in client.get_pending_orders().get("orders", [])]
    return positions, orders


def _scratch_threshold(state: dict) -> float:
    """A near-breakeven band (~10% of one max-risk trade) used to tag scratches."""
    base = state.get("day_start_balance") or config.FTMO_ACCOUNT_SIZE
    return 0.10 * (config.RISK_PCT_MAX / 100.0) * base


def _classify(net: float | None, scratch_thr: float) -> str:
    if net is None:
        return "UNKNOWN"
    if net < -scratch_thr:
        return "LOSS"
    if net <= scratch_thr:
        return "SCRATCH"
    return "WIN"


def _realized_pnl(items: list, position_id) -> float | None:
    """Find a closed position's realized P/L in order history (defensive on key names)."""
    for it in items:
        pid = it.get("positionId") or it.get("id")
        if pid == position_id:
            for k in ("netProfit", "profit", "grossProfit", "pnl"):
                if it.get(k) is not None:
                    return float(it[k])
    return None


def detect_closures(state: dict, old_by_id: dict, new_ids: set, client: McpClient) -> list[dict]:
    """For positions that vanished since last reconcile, determine win/loss/scratch and
    bump poor_outcomes_today. Loss, scratch, and UNKNOWN (unclassifiable) all count as
    poor outcomes — conservative, so the 'stop after 2 poor outcomes' rail can't be evaded.
    """
    vanished = set(old_by_id) - new_ids
    if not vanished:
        return []
    try:
        hist = client.call("get_order_history")
        items = hist if isinstance(hist, list) else (
            hist.get("history") or hist.get("trades") or hist.get("orders") or [])
    except Exception:
        items = []
    scratch_thr = _scratch_threshold(state)
    closures = []
    for pid in vanished:
        net = _realized_pnl(items, pid)
        # Do NOT fall back to last-seen floating P/L — that number is unrealized and could
        # misclassify a closed loss as a win. If history lookup failed, leave net=None so
        # _classify() returns UNKNOWN, which counts as a poor outcome (fail-closed).
        result = _classify(net, scratch_thr)
        # Only the engine's OWN trades count toward the "stop after 2 poor outcomes" tilt
        # limit — a manually-opened position closing at a loss shouldn't halt the engine.
        engine_placed = old_by_id.get(pid, {}).get("label") == "ftmo-engine"
        poor = engine_placed and result in ("LOSS", "SCRATCH", "UNKNOWN")
        if poor:
            state["poor_outcomes_today"] = state.get("poor_outcomes_today", 0) + 1
        closures.append({"id": pid, "symbol": old_by_id.get(pid, {}).get("symbol", ""),
                         "net": net, "result": result, "poor": poor, "engine": engine_placed})
    return closures


def reconcile(state: dict, client: McpClient) -> dict:
    """Overwrite state's open_positions/pending_orders from live cTrader, and detect any
    positions that closed since the last run (updating poor_outcomes_today).

    Returns {changed, summary, positions, orders, closures} for the caller to report.
    """
    old_by_id = {p.get("id"): p for p in state.get("open_positions", [])}
    live_pos, live_ord = live_snapshot(client)
    old_ids = set(old_by_id)
    new_ids = {p.get("id") for p in live_pos}
    old_ord = {o.get("id") for o in state.get("pending_orders", [])}
    new_ord = {o.get("id") for o in live_ord}

    closures = detect_closures(state, old_by_id, new_ids, client)

    # Count FILLS toward the daily trade cap: an engine position that just APPEARED (a market fill
    # or a resting limit that filled) and hasn't been counted yet. Pending limits don't count until
    # they fill. counted_positions resets each FTMO day, so positions held from a prior day aren't
    # re-counted (they're in both old and new, never "appeared").
    counted = state.setdefault("counted_positions", [])
    for pid in (new_ids - old_ids):
        pos = next((p for p in live_pos if p.get("id") == pid), None)
        if pos and pos.get("label") == config.MANAGE_LABEL and pid not in counted:
            state["trades_taken_today"] = state.get("trades_taken_today", 0) + 1
            counted.append(pid)
            state_mod.record_trading_day(state)

    changed = old_ids != new_ids or old_ord != new_ord
    summary = ""
    if changed:
        appeared = new_ids - old_ids
        vanished = old_ids - new_ids
        parts = []
        if appeared:
            parts.append(f"positions appeared: {sorted(str(i) for i in appeared)}")
        if vanished:
            parts.append(f"positions closed: {sorted(str(i) for i in vanished)}")
        if old_ord != new_ord:
            parts.append("pending-order set changed")
        summary = "; ".join(parts)

    state["open_positions"] = live_pos
    state["pending_orders"] = live_ord
    return {"changed": changed, "summary": summary, "positions": live_pos,
            "orders": live_ord, "closures": closures}
