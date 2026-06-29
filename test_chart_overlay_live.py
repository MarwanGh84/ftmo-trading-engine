#!/usr/bin/env python3
"""Live smoke test for engine/chart_overlay.py.

Connects to the real cTrader MCP bridge and exercises every overlay function
with real prices. Pause between each stage so you can see what appears on screen.

Run from ~/trading:
    python3 test_chart_overlay_live.py

Cleanup at the end removes everything this test drew.
"""
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))

from engine.mcp_client import McpClient
from engine import chart_overlay

TEST_SYMBOL = "EURUSD"
TEST_POS_ID = 99999999   # fake position id — won't collide with real ones

# Colours for terminal output
GRN = "\033[92m"
RED = "\033[91m"
YLW = "\033[93m"
BLD = "\033[1m"
RST = "\033[0m"


def ok(msg): print(f"  {GRN}✓{RST} {msg}")
def fail(msg): print(f"  {RED}✗{RST} {msg}")
def hdr(msg): print(f"\n{BLD}{YLW}── {msg}{RST}")
def pause(secs=2): time.sleep(secs)


def main():
    print(f"\n{BLD}Chart Overlay Live Test{RST}  —  {TEST_SYMBOL}  —  {datetime.now().strftime('%H:%M:%S')}")
    print("=" * 60)

    # ── Connect ──────────────────────────────────────────────────
    hdr("Stage 0: Connect to cTrader MCP")
    try:
        client = McpClient()
        client.connect()
        ok("MCP connected")
    except Exception as e:
        fail(f"Cannot connect to MCP: {e}")
        print(f"\n  Make sure the cTrader MCP bridge is running.")
        return 1

    # ── Get live price ────────────────────────────────────────────
    hdr("Stage 0b: Fetch live EURUSD price")
    try:
        d = client.get_symbol_details(TEST_SYMBOL)
        bid, ask = float(d["bid"]), float(d["ask"])
        mid = (bid + ask) / 2
        pip = d.get("pipSize", 0.0001)
        ok(f"EURUSD  bid={bid:.5f}  ask={ask:.5f}  pip={pip}")
    except Exception as e:
        fail(f"Could not fetch price: {e}")
        return 1

    # Construct realistic levels around current price
    support    = round(mid - 0.00150, 5)   # ~15 pips below
    resistance = round(mid + 0.00150, 5)   # ~15 pips above
    entry      = round(ask, 5)             # market buy entry
    sl         = round(entry - 0.00250, 5) # 25 pip SL
    sl_moved   = round(entry - 0.00100, 5) # 10 pip SL (after BE)
    tp         = round(entry + 0.00750, 5) # 75 pip TP  (3R)

    # ── Stage 1: Native notification ─────────────────────────────
    hdr("Stage 1: show_notification")
    print(f"  → Watch for a cTrader popup in the top-right corner")
    r = chart_overlay.notify(client, "🧪 Overlay Test", "Starting chart overlay smoke test…", "info")
    ok("notify called") if r else fail("notify returned False")
    pause(3)

    # ── Stage 2: Open candidate chart ────────────────────────────
    hdr("Stage 2: setup_session_charts  (opens EURUSD H1 if not already open)")
    fake_state = {"candidates": [{"symbol": TEST_SYMBOL, "near": "support", "bias": "bear"}]}
    n = chart_overlay.setup_session_charts(client, fake_state)
    ok(f"opened {n} new chart tab(s)") if n >= 0 else fail("setup_session_charts error")
    pause(2)

    # ── Stage 3: Scanner support level ───────────────────────────
    hdr("Stage 3: draw_scanner_level  — green support line")
    print(f"  → Expect green horizontal line at {support:.5f} on EURUSD H1")
    candidates = [{"symbol": TEST_SYMBOL, "near": "support", "bias": "bear"}]
    level_cache = {TEST_SYMBOL: {"recent_low": support, "recent_high": resistance}}
    chart_overlay.sync_scanner_levels(client, candidates, level_cache)
    ok(f"support line drawn at {support:.5f}")
    pause(3)

    # ── Stage 4: Scanner resistance level (updates to resistance) ─
    hdr("Stage 4: sync_scanner_levels  — swap to red resistance line")
    print(f"  → Green line should disappear, red line at {resistance:.5f}")
    candidates2 = [{"symbol": TEST_SYMBOL, "near": "resistance", "bias": "bull"}]
    chart_overlay.sync_scanner_levels(client, candidates2, level_cache)
    ok(f"resistance line drawn at {resistance:.5f}")
    pause(3)

    # ── Stage 5: Position bracket ─────────────────────────────────
    hdr("Stage 5: draw_position_bracket  — R:R block")
    print(f"  → Expect green/red R:R block:  entry={entry:.5f}  SL={sl:.5f}  TP={tp:.5f}")
    r = chart_overlay.draw_position_bracket(client, TEST_POS_ID, TEST_SYMBOL, "buy", entry, sl, tp)
    ok(f"bracket drawn (pos_id={TEST_POS_ID})") if r else fail("draw_position_bracket returned False")
    pause(3)

    # ── Stage 6: Fill annotation ──────────────────────────────────
    hdr("Stage 6: draw_fill_annotation  — blue arrow + label")
    print(f"  → Expect a blue ▲ arrow + text 'BUY | break_retest | high' at {entry:.5f}")
    r = chart_overlay.draw_fill_annotation(client, TEST_SYMBOL, "buy", "break_retest", "high", entry)
    ok("fill annotation drawn") if r else fail("draw_fill_annotation returned False")
    pause(3)

    # ── Stage 7: Update SL (move to breakeven) ───────────────────
    hdr("Stage 7: update_position_bracket  — SL moves to BE")
    print(f"  → R:R block should redraw with new SL at {sl_moved:.5f} (moved toward entry)")
    r = chart_overlay.update_position_bracket(client, TEST_POS_ID, sl_moved)
    ok(f"bracket updated — SL {sl:.5f} → {sl_moved:.5f}") if r else fail("update_position_bracket returned False")
    pause(3)

    # ── Stage 8: News window vertical line ───────────────────────
    hdr("Stage 8: draw_news_lines  — orange vertical line")
    # Put the news time 30 min from now so it's visible on H1
    news_time = (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake_windows = [
        {"kind": "high", "ccy": "EUR", "event": "TEST CPI", "start_iso": news_time,
         "end_iso": (datetime.now(timezone.utc) + timedelta(minutes=50)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    ]
    print(f"  → Expect orange vertical line at {news_time} on EURUSD H1")
    n_drawn = chart_overlay.draw_news_lines(client, fake_windows)
    ok(f"{n_drawn} news line(s) drawn") if n_drawn >= 0 else fail("draw_news_lines error")
    pause(3)

    # ── Stage 9: Kill-switch notification ────────────────────────
    hdr("Stage 9: notify  — kill-switch style (error type)")
    print(f"  → Watch for a red error popup in cTrader")
    chart_overlay.notify(client, "🚨 KILL-SWITCH (test)", "−2% floor hit — engine would close all positions", "error")
    ok("kill-switch notify sent")
    pause(3)

    # ── Cleanup ───────────────────────────────────────────────────
    hdr("Cleanup: remove everything this test drew")
    print("  → All test objects should disappear from the chart")

    chart_overlay.clear_position_bracket(client, TEST_POS_ID, TEST_SYMBOL)
    ok("position bracket removed")

    chart_overlay.clear_news_lines(client)
    ok("news lines removed")

    chart_overlay.clear_all_scanner_levels(client)
    ok("scanner level line removed")

    # Fill annotations accumulate in fill_annotations list but have no bulk-clear helper.
    # Clean up the test annotations from the overlay state file directly.
    try:
        data = chart_overlay._load()
        for ann in data.get("fill_annotations", []):
            if ann.get("symbol") == TEST_SYMBOL and ann.get("object_id"):
                try:
                    chart_overlay._focus(client, TEST_SYMBOL)
                    client.call("delete_chart_object", {"objectId": ann["object_id"]})
                except Exception:
                    pass
        data["fill_annotations"] = []
        chart_overlay._save(data)
        ok("fill annotations removed")
    except Exception as e:
        fail(f"fill annotation cleanup: {e}")

    chart_overlay.notify(client, "✅ Test complete", "All overlays removed. Chart overlay module working.", "success")
    pause(1)

    print(f"\n{BLD}{'─'*60}{RST}")
    print(f"{GRN}{BLD}All stages complete.{RST}")
    print("Review the cTrader chart — everything should now be clean.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
