# Professional upgrade plan (2026-06-16)

Goal: turn the twice-a-day single-shot scanner into a professional operator with continuous
monitoring, active trade management, resting orders at planned levels, a real watchlist, and
performance analytics — within Claude Pro's ~5 scheduled-run/day cap by pushing continuous work
onto the deterministic engine (launchd, no LLM cost) and reserving Claude runs for judgment.

## Architecture
ENGINE (launchd, continuous, ~free):
- Trade Manager (every 5 min): per open engine position, auto-BE at +1R, step-trail, partial at
  +2R, Friday weekend-flat, plus the existing −2% kill-switch. [W1]
- Market Scanner (every ~20 min, active hours): sweep the watchlist, compute level proximity /
  ATR / MA-RSI, update a Sheet "Watchlist" tab, Telegram-alert developing setups. [W3]

CLAUDE (≤5/day, judgment only):
- Mon 08:00 Weekly Plan (calendar, bias, levels). [W4]
- 09:30 Morning Brief (news + daily HTF bias + manage overnight + levels).
- 11:00 London analysis → place resting limit/stop orders at levels. [W2]
- 16:30 NY analysis → re-evaluate, new setups, manage.
- 20:00 EOD review (+ Fri weekly performance stats). [W4]

## Workstreams
- **W1 Continuous trade management** — engine/trade_manager.py + monitor cadence; config R-triggers;
  stores a per-position plan; acts via manage.py (ARMED-gated, respects cTrader confirmation).
- **W2 Resting orders + watchlist** — session prompts place pending orders at levels; define a
  ~10-instrument watchlist; top-down (D1→H4→H1) bias method in CLAUDE.md/prompts.
- **W3 Market scanner** — engine/scanner.py + launchd 20-min; Sheet "Watchlist" tab; Telegram alerts.
- **W4 Weekly plan + analytics** — new Claude weekly tasks; engine `stats` command computing win
  rate / avg R / expectancy by pair & setup from the Sheet/journal.

## Risk additions
- Currency-concentration cap (max net same-currency-direction exposure).
- Resting orders honor the same 15 rails at placement.

## Note on confirmation vs continuous management
cTrader "Require confirmation" ON means each management action (BE/trail/partial) also pops a
confirm dialog. Management is therefore kept to a few discrete, high-value events per trade (BE,
one partial, step-trail) — not dozens of micro-adjustments — so semi-auto stays practical. Going
fully unattended (confirmation OFF) makes management seamless; that's the user's call later.

Build order: W1 → W2 → W3 → W4. Engine stays DISARMED until all are built, tested, and reviewed.
