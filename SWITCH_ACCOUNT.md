# Go-live on the new FTMO account (2-Step · Standard · cTrader · $10,000)

## Already prepared (done 2026-06-16, while you buy the challenge)
- Engine config updated for the new account: $10k, daily $500 (5%), overall $1,000 (10%, floor
  $9,000), Standard rules noted (no weekend holding; news ±15-min already stricter than FTMO's 2-min).
- `state.json` reset (trial data archived to `logs/trial-archive/`) — baseline re-seeds on first audit.
- `journal.md` reset (trial journal archived); MCP request counter reset.
- Google Sheet reset: Trades + Runs history cleared (styled headers kept), Dashboard shows
  "Awaiting new account".
- The 4 scheduled tasks are **DISABLED** so nothing runs against the dead trial.
- Engine 49 tests green. Wrapper `~/trading/bin/ftmo`, Telegram, permissions all unchanged and ready.

## When the challenge is purchased and you have the new login — do these in order
1. **Log cTrader into the new account.** In Settings → MCP Server confirm "Enable MCP Server" +
   "Allow trading via MCP" are on (keep "Require confirmation" ON for now). Verify:
   `~/trading/bin/ftmo audit` → confirm the NEW balance (~$10,000) and that it's the new login.
   (This first audit seeds `account_baseline` from the new account.)
2. **Confirm the new account's objectives** on the FTMO dashboard match config
   (daily $500 / max $1,000 / size $10k). If FTMO shows different numbers, update `engine/config.py`
   `FTMO_*` and re-run tests.
3. **Re-enable the 4 scheduled tasks** (I'll do this — just say "re-enable the tasks"): morning-brief
   09:32, session-london 11:00, session-ny 16:33, eod-review 20:06.
4. **Dry-run proving period** (ARMED=false): let the scheduled runs produce WOULD-PLACE / no-setup
   reports for a stretch so you can judge the engine's picks on the real account, risk-free.
5. **Arm** only on your explicit go-ahead: set `ARMED=true` in `~/trading/.env`. First armed action
   must Telegram which account/login is live. Keep cTrader "Require confirmation" ON for the first
   trades (semi-auto) before going fully unattended.

## Standard-account reminder (vs the Swing trial)
FTMO Standard forbids holding positions over the **weekend** and restricts news trading. The engine's
news blackout already covers news; for weekends, eod_review flags open positions on Friday — when
armed, ensure positions are closed before the weekend (a future enhancement can auto-enforce this).
