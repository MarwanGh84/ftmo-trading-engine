"""Standalone client for the local cTrader MCP (streamable HTTP).

The engine — not Claude — is the sole writer to cTrader. This client speaks the MCP
JSON-RPC handshake (initialize -> notifications/initialized -> tools/call) directly,
enforces the daily request cap, and never retries more than RETRY_MAX times.
"""
from __future__ import annotations
import atexit
import fcntl
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from . import config

TZ = ZoneInfo(config.TZ)


class McpError(RuntimeError):
    pass


class RequestCapExceeded(McpError):
    pass


def _today() -> str:
    return datetime.now(TZ).date().isoformat()


def _load_counter() -> dict:
    if config.REQUEST_LOG.exists():
        try:
            return json.loads(config.REQUEST_LOG.read_text())
        except Exception:
            pass
    return {"date": _today(), "count": 0}


def _bump_counter(n: int = 1) -> int:
    """Atomically increment the daily request counter using an exclusive file lock.
    Without the lock, concurrent scanner + watchdog calls could both read count=1799,
    both write 1800, and a real 1801st request would slip through the cap."""
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = config.LOG_DIR / "requests.lock"
    with open(lock_path, "w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX)
            c = _load_counter()
            if c.get("date") != _today():
                c = {"date": _today(), "count": 0}
            c["count"] += n
            config.REQUEST_LOG.write_text(json.dumps(c))
            return c["count"]
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def requests_used_today() -> int:
    c = _load_counter()
    return c["count"] if c.get("date") == _today() else 0


class McpClient:
    def __init__(self, url: str = config.MCP_URL, timeout: float = 10.0):
        self.url = url
        self.timeout = timeout
        self.session_id: str | None = None
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        # Close our session on process exit so we don't leak sessions on the bridge
        # (leaked sessions appear to be what makes it start returning 404s).
        atexit.register(self.close)

    # -- low level ---------------------------------------------------------
    def _post(self, body: dict, notif: bool = False, timeout: float | None = None) -> requests.Response:
        h = dict(self._headers)
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return requests.post(self.url, headers=h, json=body, timeout=timeout or self.timeout)

    @staticmethod
    def _parse(resp: requests.Response) -> dict:
        txt = resp.text
        ctype = resp.headers.get("Content-Type", "")
        if "text/event-stream" in ctype or txt.lstrip().startswith("event:"):
            data = None
            for line in txt.splitlines():
                if line.startswith("data:"):
                    data = line[5:].strip()
            txt = data or "{}"
        return json.loads(txt)

    def connect(self) -> None:
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": config.MCP_PROTOCOL, "capabilities": {},
                       "clientInfo": {"name": "ftmo-engine", "version": "1.0"}},
        }
        # The bridge sometimes 404s transiently; patiently retry the (idempotent) handshake so a
        # brief drop doesn't abort a whole run (longer outages still fail safe -> no trade).
        last = None
        for attempt in range(1, config.CONNECT_RETRIES + 1):
            try:
                r = self._post(body)
                if r.status_code == 200 and r.headers.get("Mcp-Session-Id"):
                    self.session_id = r.headers["Mcp-Session-Id"]
                    self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, notif=True)
                    return
                last = f"HTTP {r.status_code} {r.text[:120]}"
            except requests.RequestException as e:
                last = str(e)
            if attempt < config.CONNECT_RETRIES:
                time.sleep(min(config.RETRY_BACKOFF_SEC * attempt, config.CONNECT_BACKOFF_MAX_SEC))
        raise McpError(f"initialize failed after {config.CONNECT_RETRIES} attempts: {last}")

    def close(self) -> None:
        """Politely end the MCP session (HTTP DELETE) so it doesn't leak on the bridge."""
        if not self.session_id:
            return
        try:
            h = dict(self._headers)
            h["Mcp-Session-Id"] = self.session_id
            requests.delete(self.url, headers=h, timeout=5)
        except Exception:
            pass
        finally:
            self.session_id = None

    # -- tool calls --------------------------------------------------------
    def call(self, tool: str, arguments: dict | None = None, retries: int | None = None,
             timeout: float | None = None, critical: bool = False) -> dict:
        """Call a cTrader tool. Returns the parsed result payload (dict).

        Enforces the daily request cap UNLESS critical=True (the kill-switch must never be
        blocked by the cap). Retries up to RETRY_MAX by default, but ORDER PLACEMENT must
        pass retries=1 (a retried place could create DUPLICATE orders) and a longer timeout
        (to wait out the cTrader confirmation dialog). Raises on failure so callers can abort.
        """
        if not critical and requests_used_today() >= config.REQUEST_CAP_PER_DAY:
            raise RequestCapExceeded(
                f"daily request cap {config.REQUEST_CAP_PER_DAY} reached")

        if self.session_id is None:
            self.connect()
            _bump_counter()

        max_attempts = retries if retries is not None else config.RETRY_MAX
        last_err = None
        for attempt in range(1, max_attempts + 1):
            try:
                _bump_counter()
                r = self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": tool, "arguments": arguments or {}}},
                               timeout=timeout)
                if r.status_code == 404 and max_attempts > 1:  # session expired -> reconnect
                    self.session_id = None
                    self.connect()
                    _bump_counter()
                    continue
                if r.status_code != 200:
                    raise McpError(f"{tool}: HTTP {r.status_code} {r.text[:200]}")
                payload = self._parse(r)
                if "error" in payload:
                    raise McpError(f"{tool}: {payload['error']}")
                return self._unwrap(payload)
            except (requests.RequestException, McpError) as e:
                last_err = e
                if attempt < max_attempts:
                    time.sleep(config.RETRY_BACKOFF_SEC * attempt)
        raise McpError(f"{tool} failed after {max_attempts} attempt(s): {last_err}")

    @staticmethod
    def _unwrap(payload: dict) -> dict:
        """MCP wraps results as result.content[0].text (a JSON string). Decode it."""
        result = payload.get("result", {})
        content = result.get("content")
        if isinstance(content, list) and content and content[0].get("type") == "text":
            txt = content[0]["text"]
            try:
                return json.loads(txt)
            except json.JSONDecodeError:
                return {"text": txt}
        return result

    # -- convenience read wrappers ----------------------------------------
    def get_balance(self) -> dict:
        return self.call("get_balance")

    def get_positions(self) -> dict:
        return self.call("get_positions")

    def get_pending_orders(self) -> dict:
        return self.call("get_pending_orders")

    def get_symbol_details(self, symbol: str) -> dict:
        return self.call("get_symbol_details", {"symbolName": symbol})

    def get_spot_prices(self, symbol: str) -> dict:
        return self.call("get_spot_prices", {"symbolName": symbol})

    def get_server_time(self) -> dict:
        return self.call("get_server_time")
