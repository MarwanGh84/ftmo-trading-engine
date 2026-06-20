"""Open-position management: move-to-breakeven, trail, partial take-profit.

Hard guard: a stop may only move in the favorable direction. Widening or removing a
stop is refused — this mirrors the "never widen/remove a stop" rail for live positions.
"""
from __future__ import annotations

from . import config, telegram
from .mcp_client import McpClient


def _find(client: McpClient, position_id) -> dict | None:
    for p in client.get_positions().get("positions", []):
        if (p.get("positionId") or p.get("id")) == position_id:
            return p
    return None


def _tighter_only(side: str, current_sl, new_sl) -> bool:
    """True if new_sl is equal/tighter (never looser) than current_sl."""
    if current_sl is None:
        return True  # adding a stop where none existed is always allowed
    if side.lower() == "buy":
        return new_sl >= current_sl   # buy stop may only rise
    return new_sl <= current_sl       # sell stop may only fall


def set_stop(position_id, new_sl: float, new_tp: float | None = None) -> dict:
    client = McpClient()
    pos = _find(client, position_id)
    if not pos:
        return {"ok": False, "reason": f"position {position_id} not found"}
    side = (pos.get("tradeSide") or pos.get("side") or "").lower()
    cur_sl = pos.get("stopLossPrice") or pos.get("stopLoss")
    if not _tighter_only(side, cur_sl, new_sl):
        msg = (f"⛔ Refused stop change on {pos.get('symbolName') or pos.get('symbol')} "
               f"#{position_id}: would WIDEN stop ({cur_sl} -> {new_sl})")
        telegram.send(msg)
        return {"ok": False, "reason": "would widen stop"}
    sym = pos.get("symbolName") or pos.get("symbol")
    if not config.is_armed():
        telegram.send(f"🟡 WOULD move stop on {sym} #{position_id} -> {new_sl} (disarmed)")
        return {"ok": True, "dry_run": True, "new_sl": new_sl}
    args = {"positionId": position_id, "stopLoss": new_sl}
    if new_tp is not None:
        args["takeProfit"] = new_tp
    res = client.call("amend_position", args, retries=1)   # idempotent; never spam confirm dialogs
    telegram.send(f"🛠 Stop moved on {sym} #{position_id} -> {new_sl}")
    return {"ok": True, "result": res}


def move_to_breakeven(position_id, offset_price: float = 0.0) -> dict:
    client = McpClient()
    pos = _find(client, position_id)
    if not pos:
        return {"ok": False, "reason": f"position {position_id} not found"}
    entry = pos.get("entryPrice") or pos.get("openPrice")
    side = (pos.get("side") or pos.get("tradeSide") or "").lower()
    be = entry + offset_price if side == "buy" else entry - offset_price
    return set_stop(position_id, be)


def close_position(position_id) -> dict:
    if not config.is_armed():
        telegram.send(f"🟡 WOULD close #{position_id} (disarmed)")
        return {"ok": True, "dry_run": True}
    client = McpClient()
    res = client.call("close_position", {"positionId": position_id}, retries=1)
    telegram.send(f"⏹ Closed #{position_id}")
    return {"ok": True, "result": res}


def cancel_pending(order_id) -> dict:
    if not config.is_armed():
        telegram.send(f"🟡 WOULD cancel order #{order_id} (disarmed)")
        return {"ok": True, "dry_run": True}
    client = McpClient()
    res = client.call("cancel_order", {"orderId": order_id}, retries=1)
    telegram.send(f"🗑 Cancelled pending #{order_id}")
    return {"ok": True, "result": res}


def partial_take_profit(position_id, units: float) -> dict:
    if not config.is_armed():
        telegram.send(f"🟡 WOULD partial-close {units:.0f}u on #{position_id} (disarmed)")
        return {"ok": True, "dry_run": True}
    client = McpClient()
    res = client.call("close_position_partial",
                      {"positionId": position_id, "volume": units, "volumeType": "units"}, retries=1)
    telegram.send(f"🟢 Partial TP {units:.0f}u on #{position_id}")
    return {"ok": True, "result": res}
