#!/usr/bin/env python3
"""One-shot: delete ALL cTrader price alerts whose message starts with 'Engine:'.
Also wipes chart_objects.json scanner_levels so the next scan starts clean.

Run from ~/trading:
    python3 purge_engine_alerts.py
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from engine.mcp_client import McpClient
from engine import config, chart_overlay

GRN = "\033[92m"
RED = "\033[91m"
YLW = "\033[93m"
RST = "\033[0m"

def main():
    client = McpClient()
    client.connect()
    print("Connected.\n")

    # ── 1. Fetch all price alerts ──────────────────────────────────────────
    try:
        alerts = client.call("get_price_alerts").get("alerts", [])
    except Exception as e:
        print(f"{RED}get_price_alerts failed: {e}{RST}")
        return 1

    print(f"Found {len(alerts)} price alert(s) in cTrader total.")

    engine_alerts = [a for a in alerts if str(a.get("message", "")).startswith("Engine:")]
    other_alerts  = [a for a in alerts if not str(a.get("message", "")).startswith("Engine:")]

    print(f"  {YLW}{len(engine_alerts)} Engine: alert(s) to delete{RST}")
    print(f"  {len(other_alerts)} non-engine alert(s) — leaving untouched\n")

    deleted, failed = 0, 0
    for a in engine_alerts:
        aid = a.get("id") or a.get("alertId")
        msg = a.get("message", "")
        try:
            client.call("delete_price_alert", {"alertId": aid})
            print(f"  {GRN}✓{RST} deleted alert {aid}: {msg}")
            deleted += 1
        except Exception as e:
            print(f"  {RED}✗{RST} failed to delete alert {aid}: {e}")
            failed += 1

    # ── 2. Wipe scanner_levels from chart_objects.json ────────────────────
    print()
    data = chart_overlay._load()
    n_levels = len(data.get("scanner_levels", {}))
    data["scanner_levels"] = {}
    chart_overlay._save(data)
    print(f"  Cleared {n_levels} scanner level(s) from chart_objects.json")
    print("  (next scan will redraw cleanly with fresh tracking)")

    # ── 3. Verify ─────────────────────────────────────────────────────────
    print()
    try:
        remaining = client.call("get_price_alerts").get("alerts", [])
        engine_remaining = [a for a in remaining if str(a.get("message", "")).startswith("Engine:")]
        if engine_remaining:
            print(f"{RED}WARNING: {len(engine_remaining)} Engine: alert(s) still present — delete manually.{RST}")
        else:
            print(f"{GRN}✓ Zero Engine: alerts remaining in cTrader.{RST}")
    except Exception as e:
        print(f"Could not verify: {e}")

    print(f"\nDone: {deleted} deleted, {failed} failed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
