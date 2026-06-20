# Run: eod_review (20:00 Dubai, Mon–Fri)

Goal: close out the day, check overnight/weekend risk, log everything.

1. `~/trading/bin/ftmo eod --report` — reconciles, summarizes balance / day P/L / trades /
   poor outcomes / open positions, and warns if any position is still open.
2. For each open position, verify it does not violate the weekend rule (Friday) or hold through a
   central-bank rate decision in that pair. If it does and the plan says to be flat, that is a
   management decision — surface it to Telegram with a clear recommendation. (Closing is a manual
   confirmation for now; the engine does not auto-flatten.)
3. Review the day vs the journal: did every trade have a stop at entry, correct sizing, and pass
   the checklist? Note any rail refusals and why. Record one lesson if there is one.
   Run `~/trading/bin/ftmo shadow-stats` — note the current filtering edge (take win% − skip win%)
   and graded sample count. If a pattern is emerging (e.g. skips outperforming takes), flag it.
   **On Fridays:** run `~/trading/bin/ftmo stats --report` for the weekly performance review (win
   rate / profit factor / expectancy by pair), and make sure the account is FLAT for the weekend
   (FTMO Standard forbids holding over the weekend) — surface any open position to close.
4. Confirm the engine's daily counters will reset on the next Dubai day (handled automatically by
   apply_daily_reset on the first run after midnight — no action needed).
5. Add a concise (≤5-line) review to journal.md. **Do NOT create Google Drive files.** The engine
   Telegrams the summary and updates the Google Sheet automatically.
