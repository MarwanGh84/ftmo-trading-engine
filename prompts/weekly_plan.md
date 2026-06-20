# Run: weekly_plan (Monday 08:00 Dubai) — NO EXECUTION

Goal: set the week's map so the daily session runs trade with context, not blind.
All engine commands use `~/trading/bin/ftmo`. NEVER call cTrader MCP tools directly.

1. `~/trading/bin/ftmo audit --report` — confirm account health for the week.
2. Pull the WEEK's HIGH-impact ForexFactory events (Mon–Fri) for the major currencies via the
   forexfactory tool. Flag the big ones — FOMC, NFP, CPI, central-bank rate decisions — with their
   day/time. These are the days to be cautious or flat.
3. Establish weekly/daily bias + key levels for each watchlist instrument (EURUSD, GBPUSD, USDJPY,
   AUDUSD, USDCAD, NZDUSD, USDCHF, EURJPY, GBPJPY, EURGBP, XAUUSD, AUDJPY, CADJPY, NZDJPY, EURAUD,
   GBPAUD, EURCHF):
   `~/trading/bin/ftmo bars --symbol X --timeframe d1 --days 180` (use w1 if helpful). For each, note
   bias (up / down / range) and the major support/resistance levels to watch this week.
4. Review last week: `~/trading/bin/ftmo stats --report` (engine performance) — note what worked.
5. Telegram a concise **weekly plan**: key events by day + per-instrument bias & levels. Optionally
   ≤10 lines in journal.md. **Do NOT create Google Drive files.** No trades this run.
