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
   Also run `~/trading/bin/ftmo shadow-stats` — report the filtering edge metric (take win% − skip
   win%) and total graded sample count. Once n ≥ 30, this becomes the primary signal for whether
   the filtering adds value. If skips are consistently outperforming takes, flag it in the plan.
5. **Macro positioning layer (COT + FXSSI)** — use this to colour directional bias, NOT to override
   technicals. Fetch CFTC Commitment of Traders data for the currencies in scope for the week. Focus
   on the TFF (Traders in Financial Futures) report for FX pairs: large-speculator net positioning.
   Interpret as follows:
   - Extreme net-long (>90th pctl vs 1y): specs are crowded LONG → watch for long-squeeze risk;
     bias toward fading rallies or tightening trailing stops on long trades.
   - Extreme net-short (<10th pctl): mirror image — fade short setups if spec shorts are crowded.
   - Mid-range: no COT signal; technicals dominate as usual.
   COT data is free at `https://www.cftc.gov/dea/newcot/f_natfin.htm` (TFF report, Friday release).
   Supplement with FXSSI order-book snapshot (`https://fxssi.com/current-ratio`) for intraday
   buy/sell ratio context — use only as a tie-breaker when two setups score equally; don't use
   it to override a strong technical setup.
   Write 1-2 sentences per affected pair in the weekly plan Telegram message.
6. Telegram a concise **weekly plan**: key events by day + per-instrument bias & levels +
   any COT/macro colour. Optionally ≤10 lines in journal.md. **Do NOT create Google Drive files.**
   No trades this run.
