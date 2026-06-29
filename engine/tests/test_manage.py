"""Tests for engine/manage.py — guards, armed/disarmed paths, failure handling."""
from unittest.mock import MagicMock, patch

import pytest

from engine import manage, config


# ── _tighter_only (pure) ──────────────────────────────────────────────────────

def test_buy_stop_may_rise():
    assert manage._tighter_only("buy", 1.1000, 1.1010) is True


def test_buy_stop_may_stay():
    assert manage._tighter_only("buy", 1.1000, 1.1000) is True


def test_buy_stop_may_not_fall():
    assert manage._tighter_only("buy", 1.1000, 1.0990) is False


def test_sell_stop_may_fall():
    assert manage._tighter_only("sell", 1.1000, 1.0990) is True


def test_sell_stop_may_stay():
    assert manage._tighter_only("sell", 1.1000, 1.1000) is True


def test_sell_stop_may_not_rise():
    assert manage._tighter_only("sell", 1.1000, 1.1010) is False


def test_no_existing_sl_always_allowed():
    assert manage._tighter_only("buy", None, 1.0950) is True
    assert manage._tighter_only("sell", None, 1.1050) is True


# ── close_position — disarmed path ────────────────────────────────────────────

def test_close_position_disarmed_returns_dry_run():
    calls = []
    with patch.object(manage.telegram, "send", side_effect=lambda m: calls.append(m)):
        with patch.object(manage.config, "is_armed", return_value=False):
            result = manage.close_position(123)
    assert result == {"ok": True, "dry_run": True}
    assert any("WOULD close" in m for m in calls)


# ── close_position — exception path ───────────────────────────────────────────

def test_close_position_exception_returns_ok_false():
    fake_client = MagicMock()
    fake_client.call.side_effect = Exception("cTrader timeout")
    with patch.object(manage.config, "is_armed", return_value=True):
        with patch("engine.manage.McpClient", return_value=fake_client):
            with patch.object(manage.telegram, "send"):
                result = manage.close_position(456)
    assert result["ok"] is False
    assert "timeout" in result["reason"]


# ── set_stop — widening refusal ───────────────────────────────────────────────

def test_set_stop_refuses_widening_buy():
    fake_client = MagicMock()
    fake_client.get_positions.return_value = {"positions": [{
        "id": 99, "symbolName": "EURUSD", "tradeSide": "Buy",
        "stopLossPrice": 1.1000, "stopLoss": 1.1000,
    }]}
    tg_calls = []
    with patch("engine.manage.McpClient", return_value=fake_client):
        with patch.object(manage.telegram, "send", side_effect=lambda m: tg_calls.append(m)):
            result = manage.set_stop(99, 1.0980)   # lower SL on a buy → widen

    assert result["ok"] is False
    assert result["reason"] == "would widen stop"
    assert any("Refused" in m or "WIDEN" in m for m in tg_calls)


def test_set_stop_allows_tighter_buy():
    fake_client = MagicMock()
    fake_client.get_positions.return_value = {"positions": [{
        "id": 99, "symbolName": "EURUSD", "tradeSide": "Buy",
        "stopLossPrice": 1.1000, "stopLoss": 1.1000,
    }]}
    with patch("engine.manage.McpClient", return_value=fake_client):
        with patch.object(manage.config, "is_armed", return_value=False):
            with patch.object(manage.telegram, "send"):
                result = manage.set_stop(99, 1.1020)   # higher SL on buy → tighter

    assert result.get("ok") is True


def test_set_stop_position_not_found():
    fake_client = MagicMock()
    fake_client.get_positions.return_value = {"positions": []}
    with patch("engine.manage.McpClient", return_value=fake_client):
        result = manage.set_stop(9999, 1.0900)

    assert result["ok"] is False
    assert "not found" in result["reason"]
