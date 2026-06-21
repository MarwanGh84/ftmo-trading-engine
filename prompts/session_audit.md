# Run: session_audit (11:00 London + 16:30 NY overlap, Dubai, Mon–Fri)

Goal: find at most one clean setup and let the engine execute it; manage open positions.

**All engine commands use the wrapper `~/trading/bin/ftmo`. NEVER call cTrader MCP tools directly.**

0. **Pre-session context checks (read-only, 60 seconds):**
   - `~/trading/bin/ftmo shadow-stats` — how many graded samples and current filtering edge (take − skip win%). If n < 30, note it; proceed with caution — edge is statistically unproven.
   - `cat ~/trading/cot_bias.json` — check for any non-neutral COT signal on currencies in today's candidates. A `crowded_long` or `crowded_short` signal (≥80th / ≤20th percentile of leveraged money positioning) is a CAUTION FLAG, not a veto: avoid adding to an already-crowded direction, and tighten trail stops on positions running in the crowded direction. If the file doesn't exist yet, skip and proceed.
1. `~/trading/bin/ftmo audit --report`. If cTrader unreachable, STOP. If kill-switch HIT,
   or trades_today (fills) ≥ 5, or poor_outcomes ≥ 2 → manage existing positions only, take no new entry,
   Telegram "limits reached — no new trades," and finish.
2. Open positions are **auto-managed by the engine every 5 min** (breakeven at +1R, partial at +2R,
   step-trail) and **auto-flattened before HIGH/CB news** — you do NOT need to micro-manage them. To
   cancel a stale pending order whose thesis is dead: `~/trading/bin/ftmo manage --action cancel
   --order <id>`. Only act on engine-labelled positions; leave the user's manual trades alone.
3. **Start with the scanner's flagged candidates:** `~/trading/bin/ftmo candidates` lists the pairs
   the bot found sitting at key levels since the last run — analyze THESE first (this is the bot
   handing setups to you). Each candidate now includes a **D1 regime** (`trend_up` / `trend_down` /
   `range`): a support touch in `trend_down` or resistance touch in `trend_up` is a higher-quality
   cue than the same touch in `range` — weight it accordingly. A `range` regime candidate needs
   extra confluence to qualify. Then continue with a **top-down scan of the FULL watchlist** (EURUSD,
   GBPUSD, USDJPY, AUDUSD, USDCAD, NZDUSD, USDCHF, EURJPY, GBPJPY, EURGBP, XAUUSD, AUDJPY, CADJPY,
   NZDJPY, EURAUD, GBPAUD, EURCHF). On USD-news days,
   lean on the non-USD crosses (AUD/CAD/NZD/EUR/GBP/JPY/CHF combos) that the event doesn't touch. For
   EACH quoting pair pull D1 (bias + key levels) → H4 (structure) → H1 (timing), via the engine only:
   - `~/trading/bin/ftmo bars --symbol X --timeframe d1 --days 120` (then h4 --days 30, h1 --days 7)
   - `~/trading/bin/ftmo quote --symbol X`
   (TradingView is crypto/stock only — never for FX.)
   **Output a one-line read for every pair** (e.g. `EURUSD: D1 down, near 1.1620 resistance — watch
   short`) so the scan is transparent, THEN shortlist the best 1–3 setups.
   - News handling: a distant event is NOT a reason to skip. Set `expiry_hours` so a resting order
     dies before the event, AND know the engine auto-flattens any position/order the moment that
     currency's HIGH/CB window opens — so a pre-news setup is safe either way. Only refuse to *enter*
     when the window is already active/imminent (the news rail enforces that). Don't blanket-skip a
     currency all day for a distant event.
   - **Direction:** prefer **with-trend continuation**, but a high-quality **counter-trend reversal at a
     major D1 level** (clear rejection + ≥2 confluences) is allowed — that's what the reversal/sweep/
     mean-reversion setup types are for.
   Apply the PRE-TRADE CHECKLIST + 9-POINT SCORE from CLAUDE.md: ≥2 independent confluences, R:R ≥1.5
   (prefer ≥2), clear invalidation.
4. Prefer **resting limit orders at planned levels** (`order_type:"limit"`, entry at the level —
   buy-limit below price, sell-limit above) so they fill autonomously; use `market` only when price is
   already at the level. **Submit your best setups, one proposal at a time, up to the remaining daily
   budget** (up to 5 FILLS/day — pending limit orders do NOT count, only fills; the audit shows the
   live count as X/5). **Quality over frequency: SKIP is the default outcome of a run. Aim for the
   1–2 highest-conviction setups, not a quota — the 5/day is a hard ceiling, NOT a target. The
   system has no proven edge yet, so a forced/mediocre trade just bleeds spread+slippage. "Nothing
   qualifies" is a perfectly good, common result.** Always tag `setup_type`,
   `regime`, `confidence`:
   `~/trading/bin/ftmo execute --json '{"symbol":"EURUSD","side":"buy","order_type":"limit","entry":<level>,"stop":<price>,"target":<price>,"risk_pct":0.5,"setup_type":"daily_level_rejection","regime":"trend","confidence":80,"rationale":"...","confluences":["...","..."]}'`
   The engine sizes from the stop, runs every rail, and places (ARMED) or dry-runs (disarmed). Report
   exactly what it returns. Do NOT resubmit a refused trade.
5. **Log the decision (shadow journal) for every setup you SERIOUSLY evaluated — both the ones you
   place AND the ones you reject.** This is how we measure whether the filtering has edge without
   risking capital: the engine grades the would-have outcome. For each shortlisted candidate, log the
   bracket you'd use and your verdict:
   `~/trading/bin/ftmo shadow --json '{"symbol":"X","side":"buy|sell","entry":<lvl>,"stop":<p>,"target":<p>,"setup_type":"...","confidence":<0-100>,"verdict":"take|skip","rationale":"one line why"}'`
   (Use `take` for anything you submitted to execute, `skip` for anything you analyzed but passed on.
   Don't log the trivial mid-range pairs — only the real shortlist you weighed.)
6. If genuinely nothing qualifies after scanning all pairs: Telegram "no setup — waiting" with a
   one-line why (e.g. "all majors mid-range / news-locked").
7. Optionally add a ≤3-line note to journal.md. **Do NOT create Google Drive files** (the engine
   already logs to the Google Sheet, journal.md, and Telegram).
