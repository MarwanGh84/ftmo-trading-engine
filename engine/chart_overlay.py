"""Visual overlays on cTrader charts — scanner levels, position brackets,
fill annotations, and news window markers.

All public functions are fail-safe: every exception is caught internally so a
chart API failure never propagates into the trading path. Object IDs are
persisted in logs/chart_objects.json so overlays survive engine restarts and
can be cleaned up correctly after a crash.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

from . import config


def _path() -> Path:
    return config.LOG_DIR / "chart_objects.json"


def _load() -> dict:
    p = _path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"scanner_levels": {}, "position_brackets": {}, "fill_annotations": [], "news_lines": []}


def _save(data: dict) -> None:
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        _path().write_text(json.dumps(data, indent=2))
    except Exception:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _obj_id(res: dict) -> str | None:
    return res.get("objectId") if isinstance(res, dict) else None


def _focus(client, symbol: str, timeframe: str = "h1") -> bool:
    """Focus the chart for symbol+timeframe, opening it if not already open."""
    try:
        charts = client.call("list_charts").get("charts", [])
        sym, tf = symbol.upper(), timeframe.lower()
        for c in charts:
            c_sym = (c.get("symbol") or c.get("symbolName") or "").upper()
            c_tf = (c.get("timeframe") or "").lower()
            if c_sym == sym and c_tf == tf:
                # BUG 1 FIX: live API returns `isActive`, not `active`
                if not (c.get("active") or c.get("isActive")):
                    cid = str(c.get("id") or c.get("chartId") or "")
                    if cid:
                        client.call("focus_chart", {"chartId": cid})
                return True
        client.call("open_chart", {"symbolName": sym, "timeframe": tf})
        return True
    except Exception:
        return False


# ── Notifications ─────────────────────────────────────────────────────────────

def notify(client, caption: str, description: str, type_: str = "info") -> bool:
    """Show an in-app cTrader popup. type_: 'info', 'success', 'error', 'progress'."""
    try:
        client.call("show_notification",
                    {"caption": caption, "description": description, "type": type_})
        return True
    except Exception:
        return False


# ── Scanner levels ─────────────────────────────────────────────────────────────

def sync_scanner_levels(client, candidates: list, level_cache: dict) -> None:
    """After each scan: draw lines for current candidates, remove lines for stale ones.
    level_cache is state['watchlist_levels'] — keyed by symbol, contains recent_high/low."""
    try:
        data = _load()
        current_syms: set[str] = set()
        for cand in candidates:
            sym = (cand.get("symbol") or "").upper()
            near = cand.get("near")
            if not sym or not near:
                continue
            current_syms.add(sym)
            lv = level_cache.get(sym, {})
            level = lv.get("recent_low") if near == "support" else lv.get("recent_high")
            if not level:
                continue
            existing = data["scanner_levels"].get(sym, {})
            if existing.get("level") == level and existing.get("near") == near:
                continue  # unchanged — skip redraw
            _draw_level(client, sym, level, near, cand.get("bias", ""), data)
        for sym in list(data["scanner_levels"]):
            if sym not in current_syms:
                _clear_level(client, sym, data)
        _save(data)
    except Exception:
        pass


def _draw_level(client, symbol: str, level: float, near: str,
                bias: str, data: dict) -> None:
    """Draw a horizontal line + create a price alert. Mutates data in-place.

    BUG 4 FIX: pop the old entry FIRST so that if the second _focus() fails,
    state is clean (no stale object_id) rather than pointing at a deleted object.
    This also fixes the self-healing problem: the next sweep will not hit the
    `continue` skip-condition and will retry the draw.
    """
    old = data["scanner_levels"].pop(symbol, None)
    if old:
        if old.get("object_id") and _focus(client, symbol):
            try:
                client.call("delete_chart_object", {"objectId": old["object_id"]})
            except Exception:
                pass
        # BUG 4 FIX (also covers old-alert cleanup on replacement):
        if old.get("alert_id"):
            try:
                client.call("delete_price_alert", {"alertId": old["alert_id"]})
            except Exception:
                pass
    if not _focus(client, symbol):
        return  # state already clean (popped above) — no stale entry left
    color = "#22aa55" if near == "support" else "#dd3333"
    label = f"Engine: 20D {'low' if near == 'support' else 'high'} {level:.5f}"
    res = client.call("add_chart_object", {
        "object_type": "horizontal_line",
        "price1": level,
        "color": color,
        "text": label,
    })
    alert_id = None
    try:
        cond = "below" if near == "support" else "above"
        ar = client.call("create_price_alert", {
            "symbolName": symbol, "price": level, "condition": cond,
            "message": f"Engine: {symbol} {near} {level:.5f} touched",
        })
        alert_id = ar.get("id") or ar.get("alertId")
    except Exception:
        pass
    data["scanner_levels"][symbol] = {
        "object_id": _obj_id(res), "level": level, "near": near, "alert_id": alert_id
    }


def _clear_level(client, symbol: str, data: dict) -> None:
    """Remove one scanner level (drawing object + price alert). Mutates data in-place."""
    entry = data["scanner_levels"].pop(symbol, None)
    if not entry:
        return
    if entry.get("object_id") and _focus(client, symbol):
        try:
            client.call("delete_chart_object", {"objectId": entry["object_id"]})
        except Exception:
            pass
    if entry.get("alert_id"):
        try:
            client.call("delete_price_alert", {"alertId": entry["alert_id"]})
        except Exception:
            pass


def clear_all_scanner_levels(client) -> None:
    """Remove every scanner line and its price alert (called at EOD)."""
    try:
        data = _load()
        for sym, entry in list(data["scanner_levels"].items()):
            if entry.get("object_id") and _focus(client, sym):
                try:
                    client.call("delete_chart_object", {"objectId": entry["object_id"]})
                except Exception:
                    pass
            if entry.get("alert_id"):
                try:
                    client.call("delete_price_alert", {"alertId": entry["alert_id"]})
                except Exception:
                    pass
        data["scanner_levels"] = {}
        _save(data)
    except Exception:
        pass


# ── Position brackets ──────────────────────────────────────────────────────────

def draw_position_bracket(client, position_id, symbol: str, side: str,
                           entry: float, sl: float, tp: float) -> bool:
    """Draw a risk/reward block for a new position."""
    try:
        if not _focus(client, symbol):
            return False
        res = client.call("add_chart_object", {
            "object_type": "risk_reward",
            "side": side.lower(),
            "price1": entry,
            "price2": sl,
            "price3": tp,
            "time1": _now_iso(),
        })
        oid = _obj_id(res)
        data = _load()
        data["position_brackets"][str(position_id)] = {
            "object_id": oid, "symbol": symbol,
            "side": side, "entry": entry, "sl": sl, "tp": tp,
        }
        _save(data)
        return True
    except Exception:
        return False


def update_position_bracket(client, position_id, new_sl: float) -> bool:
    """Redraw the bracket with an updated SL (delete + redraw keeps it consistent).

    BUG 6 FIX: persist the updated `sl` and nulled `object_id` even when the
    redraw call fails, so the next management cycle can self-heal by attempting
    the redraw again instead of seeing a stale SL and stale object_id.
    """
    try:
        data = _load()
        bk = data["position_brackets"].get(str(position_id))
        if not bk:
            return False
        sym = bk.get("symbol", "")
        if not sym or not _focus(client, sym):
            return False
        if bk.get("object_id"):
            try:
                client.call("delete_chart_object", {"objectId": bk["object_id"]})
            except Exception:
                pass
        bk["sl"] = new_sl
        bk["object_id"] = None  # mark absent so a partial failure doesn't leave a stale id
        try:
            res = client.call("add_chart_object", {
                "object_type": "risk_reward",
                "side": bk["side"].lower(),
                "price1": bk["entry"],
                "price2": new_sl,
                "price3": bk["tp"],
                "time1": _now_iso(),
            })
            bk["object_id"] = _obj_id(res)
        except Exception:
            pass
        data["position_brackets"][str(position_id)] = bk
        _save(data)
        return bk["object_id"] is not None
    except Exception:
        return False


def clear_position_bracket(client, position_id, symbol: str = "") -> bool:
    """Remove the bracket for a closed/cancelled position."""
    try:
        data = _load()
        bk = data["position_brackets"].pop(str(position_id), None)
        if not bk or not bk.get("object_id"):
            _save(data)
            return True
        sym = symbol or bk.get("symbol", "")
        if sym and _focus(client, sym):
            try:
                client.call("delete_chart_object", {"objectId": bk["object_id"]})
            except Exception:
                pass
        _save(data)
        return True
    except Exception:
        return False


def clear_closed_brackets(client, closures: list) -> None:
    """Remove brackets for all positions that just closed."""
    for c in closures:
        pid = c.get("id")
        if pid is not None:
            clear_position_bracket(client, pid, c.get("symbol", ""))


# ── Fill annotations ───────────────────────────────────────────────────────────

def draw_fill_annotation(client, symbol: str, side: str,
                          setup_type: str, confidence: str, entry: float) -> bool:
    """Mark a fill on the chart: directional arrow + text label at entry price.

    BUG 3 FIX: each object is saved individually so a failure on the second
    call (text label) does not orphan the first (arrow) — its ID was already
    persisted before the second call runs.
    """
    try:
        if not _focus(client, symbol):
            return False
        now = _now_iso()
        color = "#2266cc" if side.lower() == "buy" else "#cc6622"
        arrow = "up_arrow" if side.lower() == "buy" else "down_arrow"
        label = f"{side.upper()} | {setup_type} | {confidence}"
        data = _load()
        drew_any = False
        for obj_type, extra in ((arrow, {}), ("text", {"text": label})):
            try:
                res = client.call("add_chart_object", {
                    "object_type": obj_type, "price1": entry,
                    "time1": now, "color": color, **extra,
                })
                oid = _obj_id(res)
                if oid:
                    data["fill_annotations"].append({"object_id": oid, "symbol": symbol})
                    _save(data)
                    drew_any = True
            except Exception:
                pass
        return drew_any
    except Exception:
        return False


def clear_fill_annotations(client) -> bool:
    """Remove all fill arrow/label annotations (called at EOD).

    BUG 2 FIX: fill_annotations were previously never cleaned up — they
    accumulated in chart_objects.json and on the chart indefinitely.
    """
    try:
        data = _load()
        by_sym: dict[str, list] = {}
        for entry in data["fill_annotations"]:
            by_sym.setdefault((entry.get("symbol") or "").upper(), []).append(entry)
        if not by_sym:
            return True
        charts = client.call("list_charts").get("charts", [])
        chart_by_sym = {
            (c.get("symbol") or c.get("symbolName") or "").upper(): c
            for c in charts
        }
        for sym, entries in by_sym.items():
            c = chart_by_sym.get(sym)
            if not c:
                continue
            cid = str(c.get("id") or c.get("chartId") or "")
            if not cid:
                continue
            try:
                client.call("focus_chart", {"chartId": cid})
                for entry in entries:
                    try:
                        client.call("delete_chart_object", {"objectId": entry["object_id"]})
                    except Exception:
                        pass
            except Exception:
                pass
        data["fill_annotations"] = []
        _save(data)
        return True
    except Exception:
        return False


# ── News lines ─────────────────────────────────────────────────────────────────

def draw_news_lines(client, windows: list) -> int:
    """Draw orange vertical lines at HIGH/CB news times on currently open charts.
    Only operates on already-open charts — never opens new ones.
    Returns number of lines drawn."""
    drawn = 0
    try:
        charts = client.call("list_charts").get("charts", [])
        data = _load()
        for w in windows:
            if w.get("kind") not in ("cb", "high"):
                continue
            ccy = (w.get("ccy") or "").upper()
            start_iso = (w.get("start_iso") or "").replace("+00:00", "Z")
            if not ccy or not start_iso:
                continue
            if not start_iso.endswith("Z"):
                start_iso += "Z"
            for c in charts:
                sym = (c.get("symbol") or c.get("symbolName") or "").upper()
                if len(sym) < 6 or ccy not in (sym[:3], sym[3:6]):
                    continue
                cid = str(c.get("id") or c.get("chartId") or "")
                if not cid:
                    continue
                try:
                    client.call("focus_chart", {"chartId": cid})
                    res = client.call("add_chart_object", {
                        "object_type": "vertical_line",
                        "time1": start_iso,
                        "color": "#ff9900",
                    })
                    oid = _obj_id(res)
                    if oid:
                        data["news_lines"].append({"object_id": oid, "symbol": sym})
                        drawn += 1
                except Exception:
                    pass
        _save(data)
    except Exception:
        pass
    return drawn


def clear_news_lines(client) -> bool:
    """Remove all news vertical lines (called at EOD)."""
    try:
        data = _load()
        by_sym: dict[str, list] = {}
        for entry in data["news_lines"]:
            by_sym.setdefault((entry.get("symbol") or "").upper(), []).append(entry)
        if not by_sym:
            return True
        charts = client.call("list_charts").get("charts", [])
        chart_by_sym = {
            (c.get("symbol") or c.get("symbolName") or "").upper(): c
            for c in charts
        }
        for sym, entries in by_sym.items():
            c = chart_by_sym.get(sym)
            if not c:
                continue
            cid = str(c.get("id") or c.get("chartId") or "")
            if not cid:
                continue
            try:
                client.call("focus_chart", {"chartId": cid})
                for entry in entries:
                    try:
                        client.call("delete_chart_object", {"objectId": entry["object_id"]})
                    except Exception:
                        pass
            except Exception:
                pass
        data["news_lines"] = []
        _save(data)
        return True
    except Exception:
        return False


# ── Alert trigger detection ────────────────────────────────────────────────────

def check_triggered_alerts(client) -> list[dict]:
    """Return scanner levels whose price alerts have fired (disappeared from cTrader).

    cTrader removes an alert from the list when it fires. By diffing our tracked IDs
    against the live list we detect which levels were touched since the last watchdog run.
    Clears the fired alert_id from state so we don't re-detect on the next cycle.
    """
    try:
        data = _load()
        levels = data.get("scanner_levels", {})
        if not any(e.get("alert_id") for e in levels.values()):
            return []
        live_ids = {
            str(a.get("id") or a.get("alertId"))
            for a in client.call("get_price_alerts").get("alerts", [])
        }
        triggered = []
        changed = False
        for sym, entry in levels.items():
            aid = entry.get("alert_id")
            if aid and str(aid) not in live_ids:
                triggered.append({
                    "symbol": sym,
                    "level": entry.get("level"),
                    "near": entry.get("near"),
                })
                entry["alert_id"] = None   # consumed — don't re-detect next cycle
                changed = True
        if changed:
            _save(data)
        return triggered
    except Exception:
        return []


# ── Session setup ──────────────────────────────────────────────────────────────

def setup_session_charts(client, state: dict) -> int:
    """Open H1 charts for scanner candidates that aren't already open.
    Returns number of new chart tabs opened."""
    opened = 0
    try:
        candidates = state.get("candidates", [])
        if not candidates:
            return 0
        charts = client.call("list_charts").get("charts", [])
        open_h1 = {
            (c.get("symbol") or c.get("symbolName") or "").upper()
            for c in charts if (c.get("timeframe") or "").lower() == "h1"
        }
        for cand in candidates:
            sym = (cand.get("symbol") or "").upper()
            if sym and sym not in open_h1:
                try:
                    client.call("open_chart", {"symbolName": sym, "timeframe": "h1"})
                    opened += 1
                except Exception:
                    pass
    except Exception:
        pass
    return opened
