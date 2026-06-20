# FTMO Autonomous Trading Operator — Overview

A fully autonomous, rules-enforced trading system that analyzes the FX market, places and manages
trades, and reports everything — running unattended on your Mac.

**Account:** FTMO 2-Step Challenge · Standard · cTrader · login 7568956 · $10,000 · leverage 1:100
**Status:** ARMED & live, fully unattended (cTrader confirmation OFF)

---

## How it's built — two layers

**1. The deterministic engine (`~/trading/engine/`, Python)** — the part that enforces everything.
It is the *only* component that talks to cTrader, sizes trades, checks the rails, and places/manages
orders. It cannot be talked out of a rule. Key commands (run via `~/trading/bin/ftmo <cmd>`):
`audit · execute · manage · watchdog · scan · stats · bars · quote · eod · set-news`.

**2. The Claude "brain" (scheduled runs)** — does the *judgment*: reads the market top-down, picks
setups, and proposes them to the engine as JSON. It is permission-blocked from placing orders
directly — every trade goes through the engine's rails.

This split means continuous work (monitoring, management) is free and unlimited (the engine via
launchd), while the limited Claude runs are spent only on decisions.

---

## What runs, and when (Dubai time)

| Cadence | Job | What it does |
|---|---|---|
| every 5 min | Trade manager + kill-switch (launchd) | auto breakeven/partial/trail; halt at −2%; weekend-flat |
| every 30 min | Market scanner (launchd) | sweeps 11-instrument watchlist, updates Sheet, alerts setups |
| Mon 08:03 | Weekly plan | week's calendar, bias, levels, last-week stats |
| 09:32 | Morning brief | news blackout windows + daily bias |
| 11:00 / 16:33 / 18:32 | Session analysis | top-down scan → place resting limit orders at levels |
| 20:06 (Fri +stats) | EOD review | summary, weekend check, performance |

Trades fill **autonomously** when price reaches a resting order; the engine then manages them to
target or a trailed stop — no human input needed.

---

## The safety rails (17, enforced in code — non-bypassable)

Kill-switch −2% daily · per-trade risk 0.25–0.5% (cut to 0.25% after a loss) · max 5 trades/day (fills) ·
stop after 2 poor outcomes · stop-loss mandatory, never widened · R:R ≥1.5 · news blackout ±15 min ·
no entry into a central-bank decision · aggregate risk ≤1% · no correlated/opposing positions · no
>2 same-currency-direction trades · absolute FTMO floors ($9,000 / $500 daily) · weekend-flat (no
Standard weekend holds) · pre-trade sanity gate (reachable, spread normal, fresh quote, buffers safe) ·
request cap 1,800/day.

The engine sizes every trade from its stop (never from desired profit) and refuses anything that
breaks a rail, regardless of how good the setup looks.

---

## Reporting

- **Telegram** (@FTMO_MGH_Ctrader_bot) — every run, trade, management action, alert, kill-switch.
- **Google Sheet** ("FTMO Operator Dashboard") — live Dashboard + Trades + Runs + Watchlist tabs.
- **`~/trading/journal.md`** — local append-only log.

---

## Operating it

- **Stop trading:** set `ARMED=false` in `~/trading/.env` (blocks new trades instantly; broker stops remain).
- **Check performance:** `~/trading/bin/ftmo stats --report`
- **Switch accounts later:** follow `~/trading/SWITCH_ACCOUNT.md`.
- **Tests:** `python3 -m pytest ~/trading/engine/tests/` (78 tests).

## Honest caveat
The rails bound losses; they don't create an edge. The strategy is discretionary and unproven —
watch the stats build over the first weeks and disarm if the picks aren't sound.
