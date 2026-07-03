# Run: morning_brief (09:00 Dubai, Mon–Fri) — NO EXECUTION

Goal: set up the day. Audit the account and publish today's news blackout windows so the
engine can enforce them. You place no trades in this run.

⚠️ **NEVER run `ftmo morning-brief`** — that CLI fetches ForexFactory directly via urlopen,
gets rate-limited (HTTP 429), and sends duplicate/error Telegrams that pollute the brief.
Always use the `forexfactory` MCP tool (step 2) + `ftmo set-news` (step 3) instead.

1. `~/trading/bin/ftmo audit --report` — confirms cTrader reachable, reconciles state,
   applies the daily reset, records day_start_balance, reports buffers to Telegram.
   If it reports cTrader unreachable, STOP (the alert already fired).
2. Pull today's ForexFactory calendar (HIGH-impact, plus notable medium) via the forexfactory tool
   for the relevant FX currencies (USD, EUR, GBP, JPY, CHF, AUD, CAD, NZD). Build a blackout window
   per event using TIERED padding (UTC ISO timestamps, padding already applied):
   - **Tier 1** (FOMC, NFP, CPI, central-bank rate decisions): −60 min to **+45 min**. Rate decisions
     also set `kind:"cb"`; everything else `kind:"high"`.
   - **Tier 2** (GDP, PCE, PMI, unemployment, retail sales): −30 min to **+30 min**, `kind:"high"`.
   - **Tier 3** (other medium HIGH-impact): −15 min to **+15 min**, `kind:"high"`.
   (Post-event padding is deliberately wide — the whipsaw after a major release routinely runs
   20–45 min past the print. A tight-stop entry into that retracement is how today's GBPUSD lost.)
3. Persist them:
   `~/trading/bin/ftmo set-news --json '[{"ccy":"USD","start_iso":"2026-..Z","end_iso":"2026-..Z","event":"CPI","kind":"high"}, ...]'`
   If there are NO high-impact events today, still publish an empty list `[]` so the engine knows
   news data is fresh (otherwise it fail-safe-blocks all trades).
4. Telegram a short brief: account/buffers (from step 1), today's high-impact events + affected
   currencies + the no-trade windows, and any open positions to watch.
5. Optionally add a ≤3-line note to journal.md. **Do NOT create Google Drive files** — reporting is
   Telegram + the live Google Sheet + journal.md only.
