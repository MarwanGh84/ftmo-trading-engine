"""Telegram reporting via the Bot API directly (no plugin dependency).

Used by both scheduled runs (via the engine) and the standalone watchdog. Reporting
must never throw into the trading path — failures are logged and swallowed so a
Telegram outage cannot block or distort risk decisions.
"""
from __future__ import annotations
import hashlib
import json
from datetime import datetime

import requests

from . import config

_DEDUP_WINDOW_SEC = 900          # suppress exact-duplicate messages within 15 min
_dedup_cache: dict[str, float] = {}


def _log(line: str) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.LOG_DIR / "telegram.log", "a") as f:
        f.write(f"{datetime.utcnow().isoformat()}Z  {line}\n")


# ── HTML formatting helpers ────────────────────────────────────────────────────
# All messages use parse_mode="HTML". Use these to mark up message strings.
# Always escape untrusted/variable text with esc() before embedding it.

def esc(text: str) -> str:
    """Escape HTML special characters so they render literally in HTML parse mode."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def bold(text: str) -> str:
    return f"<b>{esc(text)}</b>"

def code(val) -> str:
    return f"<code>{esc(str(val))}</code>"

def _net_str(net) -> str:
    """Format a P/L dollar value as '+$45.20' or '−$38.10'."""
    if net is None:
        return "?"
    return f"+${net:.2f}" if net >= 0 else f"−${abs(net):.2f}"


def send(text: str) -> bool:
    token, chat = config.telegram_token(), config.telegram_chat_id()
    if not token or not chat:
        _log(f"NOT CONFIGURED, would send: {text[:120]}")
        return False
    h = hashlib.md5(text.encode()).hexdigest()
    now_ts = datetime.utcnow().timestamp()
    last = _dedup_cache.get(h, 0.0)
    if now_ts - last < _DEDUP_WINDOW_SEC:
        _log(f"DEDUP (suppressed): {text[:80]}")
        return True
    _dedup_cache[h] = now_ts
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
        ok = r.status_code == 200 and r.json().get("ok", False)
        _log(("SENT" if ok else f"FAIL {r.status_code} {r.text[:120]}") + f": {text[:80]}")
        return ok
    except Exception as e:  # never propagate into trading logic
        _log(f"EXC {e}: {text[:80]}")
        return False
