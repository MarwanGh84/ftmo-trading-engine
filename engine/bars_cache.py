"""Disk cache for get_trendbars — cuts repeat MCP requests for slow-moving timeframes.

D1 bars change once a day and H4 every 4 hours, yet the three daily session audits
re-fetch 120 days of D1 for all 17 watchlist pairs each run. Caching those fetches
locally keeps the daily MCP request budget (REQUEST_CAP_PER_DAY) far from the cap.

Fast timeframes (h1 and below) are never cached — entry timing needs live bars.
"""
from __future__ import annotations
import json
import re
import time
from datetime import datetime, timezone, timedelta

from . import config

_CACHE_DIR = config.LOG_DIR / "bars_cache"

# TTL per timeframe (seconds). Timeframes not listed are never cached.
_TTL = {
    "d1": 6 * 3600,   # one fetch covers all three session runs
    "w1": 24 * 3600,
    "h4": 2 * 3600,
}


def _path(symbol: str, timeframe: str, days: int, limit: int):
    key = re.sub(r"[^A-Za-z0-9_]", "", f"{symbol}_{timeframe}_{days}_{limit}")
    return _CACHE_DIR / f"{key}.json"


def get_bars(client, symbol: str, timeframe: str, days: int, limit: int = 500) -> dict:
    """Fetch trendbars through the cache. Returns the raw get_trendbars response dict."""
    tf = timeframe.lower()
    ttl = _TTL.get(tf)
    p = _path(symbol, tf, days, limit)

    if ttl:
        try:
            cached = json.loads(p.read_text())
            if time.time() - cached["ts"] < ttl:
                return cached["data"]
        except Exception:
            pass

    to = datetime.now(timezone.utc)
    frm = to - timedelta(days=days)
    data = client.call("get_trendbars", {
        "symbolName": symbol.upper(), "timeframe": tf,
        "from": frm.isoformat(), "to": to.isoformat(), "limit": limit,
    })

    if ttl:
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"ts": time.time(), "data": data}))
        except Exception:
            pass
    return data
