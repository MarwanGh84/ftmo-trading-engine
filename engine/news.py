"""News-blackout evaluation. The Claude morning_brief run fetches ForexFactory
HIGH-impact events and writes them into state['news_windows']; the engine enforces
them deterministically here. Missing/stale windows => fail-safe block (handled by the
rail, which checks `fresh`).

Window shape (written by morning_brief):
  {"ccy": "USD", "start_iso": "...Z", "end_iso": "...Z", "event": "CPI", "kind": "high"|"cb"}
start/end already include the +/- NEWS_WINDOW_MIN padding.
"""
from __future__ import annotations
from datetime import datetime
from zoneinfo import ZoneInfo

from . import state as state_mod


def _parse(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt


def symbol_currencies(symbol: str) -> set[str]:
    s = symbol.upper()
    return {s[:3], s[3:6]} if len(s) >= 6 else {s}


def evaluate(state: dict, symbol: str, now_utc: datetime) -> dict:
    """Return news context for a candidate entry on `symbol`."""
    ccys = symbol_currencies(symbol)
    result = {"fresh": state_mod.news_windows_fresh(state),
              "in_window": False, "cb_hold": False, "event": None}
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=ZoneInfo("UTC"))
    for w in state.get("news_windows", []):
        if w.get("ccy", "").upper() not in ccys:
            continue
        try:
            start, end = _parse(w["start_iso"]), _parse(w["end_iso"])
        except Exception:
            continue
        if w.get("kind") == "cb" and now_utc <= end:
            # No NEW position into an UPCOMING/ongoing central-bank rate decision. Once the
            # event has concluded (now past its window end), the pair is tradeable again —
            # so a CB block must not linger for the rest of the day.
            result["cb_hold"] = True
            result["event"] = w.get("event")
        if start <= now_utc <= end:
            result["in_window"] = True
            result["event"] = w.get("event")
    return result
