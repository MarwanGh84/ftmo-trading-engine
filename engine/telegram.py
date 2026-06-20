"""Telegram reporting via the Bot API directly (no plugin dependency).

Used by both scheduled runs (via the engine) and the standalone watchdog. Reporting
must never throw into the trading path — failures are logged and swallowed so a
Telegram outage cannot block or distort risk decisions.
"""
from __future__ import annotations
import json
from datetime import datetime

import requests

from . import config


def _log(line: str) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.LOG_DIR / "telegram.log", "a") as f:
        f.write(f"{datetime.utcnow().isoformat()}Z  {line}\n")


def send(text: str) -> bool:
    token, chat = config.telegram_token(), config.telegram_chat_id()
    if not token or not chat:
        _log(f"NOT CONFIGURED, would send: {text[:120]}")
        return False
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
