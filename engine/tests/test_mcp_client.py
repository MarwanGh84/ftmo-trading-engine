"""Tests for engine/mcp_client.py — request cap, _bump_counter concurrency, 404 reconnect."""
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from engine import config
from engine.mcp_client import McpClient, RequestCapExceeded, _bump_counter, _load_counter


# ── _bump_counter ─────────────────────────────────────────────────────────────

def test_bump_counter_increments(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    monkeypatch.setattr(config, "REQUEST_LOG", tmp_path / "requests.json")
    assert _bump_counter(1) == 1
    assert _bump_counter(1) == 2
    assert _bump_counter(3) == 5


def test_bump_counter_resets_on_date_change(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    req_log = tmp_path / "requests.json"
    monkeypatch.setattr(config, "REQUEST_LOG", req_log)
    req_log.write_text(json.dumps({"date": "2000-01-01", "count": 1799}))
    new_total = _bump_counter(1)
    assert new_total == 1, "counter should have reset for a new date"


def test_bump_counter_concurrent_no_lost_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    monkeypatch.setattr(config, "REQUEST_LOG", tmp_path / "requests.json")
    errors = []
    results = []

    def worker():
        try:
            results.append(_bump_counter(1))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert max(results) == 20, "20 concurrent bumps must total exactly 20"
    assert len(set(results)) == 20, "each bump must land on a unique count"


# ── request cap ───────────────────────────────────────────────────────────────

def test_request_cap_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    monkeypatch.setattr(config, "REQUEST_LOG", tmp_path / "requests.json")
    monkeypatch.setattr(config, "REQUEST_CAP_PER_DAY", 5)
    (tmp_path / "requests.json").write_text(
        json.dumps({"date": __import__("engine.mcp_client", fromlist=["_today"])._today(), "count": 5})
    )
    c = McpClient()
    c.session_id = "fake-session"   # skip connect
    with pytest.raises(RequestCapExceeded):
        c.call("get_balance")


def test_critical_call_bypasses_cap(tmp_path, monkeypatch):
    """critical=True must bypass the request cap so the kill-switch can always fire."""
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    monkeypatch.setattr(config, "REQUEST_LOG", tmp_path / "requests.json")
    monkeypatch.setattr(config, "REQUEST_CAP_PER_DAY", 0)
    monkeypatch.setattr(config, "RETRY_MAX", 1)
    monkeypatch.setattr(config, "RETRY_BACKOFF_SEC", 0)

    result_text = json.dumps({"result": {"content": [{"type": "text", "text": '{"balance":"9000"}'}]}})

    class _Resp:
        status_code = 200
        text = result_text
        headers = {"Content-Type": "application/json"}

    c = McpClient()
    c.session_id = "fake"
    with patch("engine.mcp_client.requests.post", return_value=_Resp()):
        result = c.call("get_balance", critical=True)
    assert result == {"balance": "9000"}


# ── 404 reconnect ─────────────────────────────────────────────────────────────

class _MockResponse:
    def __init__(self, status: int, body: str = "{}", headers: dict | None = None):
        self.status_code = status
        self.text = body
        self.headers = headers or {"Content-Type": "application/json"}


def _make_post_sequence(*responses):
    it = iter(responses)
    def _side_effect(*args, **kwargs):
        return next(it)
    return _side_effect


def test_404_triggers_reconnect(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    monkeypatch.setattr(config, "REQUEST_LOG", tmp_path / "requests.json")
    monkeypatch.setattr(config, "REQUEST_CAP_PER_DAY", 9999)
    monkeypatch.setattr(config, "CONNECT_RETRIES", 1)
    monkeypatch.setattr(config, "RETRY_MAX", 2)
    monkeypatch.setattr(config, "RETRY_BACKOFF_SEC", 0)

    tool_result = json.dumps({"result": {"content": [{"type": "text", "text": '{"ok": true}'}]}})

    responses = _make_post_sequence(
        _MockResponse(200, "{}", {"Mcp-Session-Id": "session-A", "Content-Type": "application/json"}),  # connect
        _MockResponse(200),                                                                               # notifications/initialized
        _MockResponse(404),                                                                               # tool call → 404
        _MockResponse(200, "{}", {"Mcp-Session-Id": "session-B", "Content-Type": "application/json"}),  # reconnect
        _MockResponse(200),                                                                               # notifications/initialized
        _MockResponse(200, tool_result, {"Content-Type": "application/json"}),                           # tool call retry → 200
    )

    with patch("engine.mcp_client.requests.post", side_effect=responses):
        c = McpClient()
        result = c.call("get_balance", retries=2)

    assert result == {"ok": True}
    assert c.session_id == "session-B"    # reconnected to new session
