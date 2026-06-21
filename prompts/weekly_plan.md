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
5. **Macro positioning layer (COT)** — use to colour directional bias, NOT to override technicals.
   Run: `~/trading/bin/ftmo cot-update`
   This downloads the CFTC Leveraged Money (hedge fund) net positioning for all major FX pairs
   from cftc.gov, computes the 52-week percentile rank, and prints a ready-to-read summary.
   Interpret the output:
   - `crowded_long` (≥80th pctl): specs are piled long → squeeze risk; favour fading rallies or
     tightening trail stops on long trades this week. Do NOT blindly short — it's a caution flag.
   - `crowded_short` (≤20th pctl): mirror image — be cautious adding to short setups.
   - `neutral`: no COT signal; technicals dominate as usual.
   Note: COT is a weekly file released Friday ~15:30 ET. The launchd job runs this every Saturday
   morning so the data is already fresh by Monday — no manual fetch needed.
   If the command fails (network down), skip COT this week and note it in the Telegram message.
   Supplement with FXSSI order-book snapshot (`https://fxssi.com/current-ratio`) for intraday
   buy/sell ratio — use only as a tie-breaker when two setups are equally rated, never as a
   primary signal.
6. Telegram a concise **weekly plan**: key events by day + per-instrument bias & levels +
   COT colour for any pair with a non-neutral signal. Optionally ≤10 lines in journal.md.
   **Do NOT create Google Drive files.** No trades this run.
