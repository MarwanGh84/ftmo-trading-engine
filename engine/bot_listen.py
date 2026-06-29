"""Telegram emergency command bot — runs as a persistent daemon via launchd.

Polls the Telegram bot API every 5 seconds and handles authorized commands from the
configured TELEGRAM_CHAT_ID. This is the operator's phone interface for intervening
without touching the laptop.

Authorized commands:
  /status   — account snapshot (balance, equity, positions, kill-switch state)
  /freeze   — impose a non-sticky operational freeze (blocks new entries)
  /flatten  — emergency close all engine positions and pending orders
  /rearm    — clear a non-sticky freeze (sticky requires manual unfreeze via CLI)
  /disable  — switch engine to dry-run mode for the rest of the session (in-memory)

Security: ONLY messages from TELEGRAM_CHAT_ID are processed. All others are ignored and
logged. This file must never accept commands from unknown senders.

Usage: python3 -m engine.bot_listen   (run via launchd com.ftmo.operator.bot-listen)
"""
from __future__ import annotations
import json
import sys
import time
from datetime import datetime

import requests

from . import config
from . import state as state_mod
from . import telegram
from . import trade_manager
from .mcp_client import McpClient

_POLL_INTERVAL = 5        # seconds between long-poll cycles
_LONG_POLL_TIMEOUT = 4    # seconds for getUpdates timeout (< _POLL_INTERVAL)
_AUTHORIZED_CMDS = {"/status", "/freeze", "/flatten", "/rearm", "/disable"}
_LOG_PREFIX = "bot_listen"


def _log(msg: str) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.LOG_DIR / "bot_listen.log", "a") as f:
        f.write(f"{datetime.utcnow().isoformat()}Z  {msg}\n")


def _api(method: str, params: dict | None = None) -> dict:
    token = config.telegram_token()
    r = requests.get(
        f"https://api.telegram.org/bot{token}/{method}",
        params=params or {},
        timeout=_LONG_POLL_TIMEOUT + 2,
    )
    return r.json()


def _reply(text: str) -> None:
    chat = config.telegram_chat_id()
    token = config.telegram_token()
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        _log(f"reply failed: {e}")


def _cmd_status() -> str:
    try:
        client = McpClient()
        bal = client.get_balance()
        balance = float(bal["balance"])
        equity = float(bal["equity"])
        state = state_mod.load()
        open_pos = state.get("open_positions", [])
        pending = state.get("pending_orders", [])
        frozen = state.get("frozen", False)
        frozen_sticky = state.get("frozen_sticky", False)
        freeze_reason = state.get("frozen_reason", "")
        kill = state.get("daily_limit_hit", False)
        trades = state.get("trades_taken_today", 0)
        poor = state.get("poor_outcomes_today", 0)
        news_fresh = state_mod.news_windows_fresh(state)

        now = state_mod.now_dubai()
        dsb = state.get("day_start_balance") or equity
        daily_pnl = equity - dsb
        pnl_s = telegram.code(telegram._net_str(daily_pnl))

        # System health — one line that tells you immediately if something is wrong
        if kill:
            health = "Kill-switch 🚨 <b>HIT</b> — no new trades today"
        elif frozen and frozen_sticky:
            health = f"🔒 <b>Sticky freeze</b> — {telegram.esc(freeze_reason[:80])}"
        elif frozen:
            health = f"🧊 <b>Frozen</b> — {telegram.esc(freeze_reason[:80])}"
        else:
            health = "✅ All systems ok"

        pos_line = (f"{len(open_pos)} open" + (f"  ·  {len(pending)} pending" if pending else "")
                    if (open_pos or pending) else "flat")

        lines = [
            f"📊 <b>FTMO Engine — {now.strftime('%H:%M')} Dubai</b>",
            "",
            f"<b>Account</b>",
            f"Balance  {telegram.code(f'${balance:.2f}')}  ·  Equity  {telegram.code(f'${equity:.2f}')}",
            f"Day P/L  {pnl_s}",
            "",
            f"<b>Positions</b>  {pos_line}",
            "",
            f"<b>Today</b>",
            f"Fills  {telegram.code(f'{trades}/{config.MAX_TRADES_PER_DAY}')}  ·  "
            f"Poor  {telegram.code(f'{poor}/{config.MAX_POOR_OUTCOMES}')}",
            f"News  {'✅ fresh' if news_fresh else '⚠️ stale — run morning brief'}",
            "",
            f"<b>System</b>",
            health,
            f"ARMED  {'✅ live' if config.is_armed() else '🟡 disarmed (dry-run)'}",
        ]
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ /status failed: {telegram.esc(str(e))}"


def _cmd_freeze() -> str:
    try:
        with state_mod.transaction():
            state = state_mod.load()
            state_mod.freeze(state, "manual Telegram /freeze command", sticky=False)
            state_mod.save(state)
        _log("FREEZE command executed")
        return ("🧊 <b>Frozen</b> — no new entries until /rearm.\n"
                "Management of open positions (trail, BE, partial) continues.")
    except Exception as e:
        return f"⚠️ /freeze failed: {telegram.esc(str(e))}"


def _cmd_flatten() -> str:
    try:
        with state_mod.transaction():
            state = state_mod.load()
            notes, failed = trade_manager.emergency_flat(state)
            if failed:
                state_mod.freeze(state, "Telegram /flatten had failures — manual close required", sticky=True)
            state_mod.save(state)
        _log(f"FLATTEN command: closed={notes}, failed={failed}")
        if not notes and not failed:
            return "✅ <b>/flatten</b> — already flat, nothing to close."
        lines = [f"  {telegram.esc(n)} ✓" for n in notes]
        lines += [f"  {telegram.esc(f)} ✗" for f in failed]
        body = "\n".join(lines)
        if failed:
            return (f"⚠️ <b>/flatten partial</b>\n{body}\n\n"
                    f"🔒 <b>Sticky freeze</b> — close remaining positions manually in cTrader.")
        return f"✅ <b>/flatten complete</b> — {len(notes)} closed\n{body}"
    except Exception as e:
        return f"⚠️ /flatten failed: {telegram.esc(str(e))}"


def _cmd_rearm() -> str:
    try:
        with state_mod.transaction():
            state = state_mod.load()
            if not state.get("frozen"):
                return "✅ <b>/rearm</b> — not frozen, nothing to do."
            if state.get("frozen_sticky"):
                return ("🔒 <b>Sticky freeze active</b> — cannot clear via Telegram.\n"
                        "Run <code>ftmo unfreeze --force</code> on the machine after confirming "
                        "the underlying issue is resolved.")
            state_mod.unfreeze(state)
            state_mod.save(state)
        _log("REARM command executed")
        return "✅ <b>Rearmed</b> — freeze cleared. New entries now allowed."
    except Exception as e:
        return f"⚠️ /rearm failed: {telegram.esc(str(e))}"


def _cmd_disable() -> str:
    # /disable is intentionally NOT implemented: it would require patching config.is_armed()
    # at runtime which is architecturally unsafe (ARMED must come from .env, not Telegram).
    return ("⛔ <b>/disable not supported</b> — it would bypass the <code>.env</code> ARMED gate.\n"
            "To stop trading: /freeze to block new entries, then /flatten to close positions.")


_HANDLERS = {
    "/status": _cmd_status,
    "/freeze": _cmd_freeze,
    "/flatten": _cmd_flatten,
    "/rearm": _cmd_rearm,
    "/disable": _cmd_disable,
}


def run() -> None:
    token = config.telegram_token()
    chat_id = str(config.telegram_chat_id())
    if not token or not chat_id:
        print(f"{_LOG_PREFIX}: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not configured — exiting")
        sys.exit(1)

    _log("bot_listen started")
    _reply("🤖 <b>Bot listener started</b>\nCommands: /status  /freeze  /flatten  /rearm")

    offset = 0
    while True:
        try:
            data = _api("getUpdates", {"offset": offset, "timeout": _LONG_POLL_TIMEOUT,
                                       "allowed_updates": ["message"]})
            updates = data.get("result", [])
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                from_chat = str(msg.get("chat", {}).get("id", ""))
                text = (msg.get("text") or "").strip()
                cmd = text.split()[0].lower().split("@")[0] if text else ""

                if from_chat != chat_id:
                    _log(f"IGNORED message from unauthorized chat {from_chat}: {text[:60]}")
                    continue

                if cmd not in _AUTHORIZED_CMDS:
                    if cmd.startswith("/"):
                        _reply(f"Unknown command: {telegram.code(cmd)}\n"
                               f"Available: {' '.join(sorted(_AUTHORIZED_CMDS))}")
                    continue

                _log(f"CMD {cmd} from authorized chat")
                handler = _HANDLERS[cmd]
                response = handler()
                _reply(response)

        except KeyboardInterrupt:
            _log("bot_listen stopped by KeyboardInterrupt")
            break
        except Exception as e:
            _log(f"poll error: {e}")

        time.sleep(_POLL_INTERVAL)


if __name__ == "__main__":
    run()
