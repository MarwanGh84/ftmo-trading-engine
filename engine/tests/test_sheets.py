"""Sheets reporting must fail-safe: when unconfigured (no creds), every function
no-ops and returns False without raising — it can never break the trading path."""
from engine import sheets


def test_disabled_when_unconfigured(monkeypatch):
    monkeypatch.setattr(sheets.config, "gsheet_id", lambda: "")
    monkeypatch.setattr(sheets.config, "google_sa_json", lambda: "")
    assert sheets.enabled() is False


def test_all_calls_noop_safely(monkeypatch):
    monkeypatch.setattr(sheets.config, "gsheet_id", lambda: "")
    monkeypatch.setattr(sheets.config, "google_sa_json", lambda: "")
    assert sheets.update_dashboard({"Balance $": "100"}) is False
    assert sheets.append_trade(["t", "EURUSD", "buy"]) is False
    assert sheets.append_run(["t", "audit", "report", "ok"]) is False
